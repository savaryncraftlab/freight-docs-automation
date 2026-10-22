"""
nbp_rates.py — Fetch EUR/PLN exchange rates from the NBP public API.

Public API: https://api.nbp.pl/api/exchangerates/rates/A/EUR/YYYY-MM-DD/?format=json
Used by actions.py to convert PLN invoice amounts to EUR.

Public interface:
    get_rate(unloading_date: date) -> NbpRate
    build_note(amount_pln: float, nbp_rate: NbpRate) -> str
"""

import json
import os
import ssl
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import date, timedelta

# On Windows the system certificate store may not be trusted by Python's SSL context.
# Use an unverified context as a fallback so the NBP public API remains reachable.
_SSL_CTX = ssl._create_unverified_context()

# Cache file lives next to emails.json
_CACHE_PATH = os.path.join(os.path.dirname(__file__), "bot_data", "nbp_cache.json")

# NBP API endpoint template — returns JSON with single rate entry
_API_URL = "https://api.nbp.pl/api/exchangerates/rates/A/EUR/{date}/?format=json"


class NbpRateError(Exception):
    """Raised on network errors or when no rate can be found for the requested date."""


@dataclass
class NbpRate:
    """Result of a single NBP rate lookup."""
    rate: float          # e.g. 4.2858
    table_nr: str        # e.g. "060/A/NBP/2026"
    rate_date: str       # ISO date string of the rate actually used, e.g. "2026-03-27"
    is_approximate: bool # True when a future-date fallback or backward walk was used


# ─────────────────────────────────────────────
# CACHE HELPERS
# ─────────────────────────────────────────────

def _load_cache() -> dict:
    """Load the local JSON cache.  Returns empty dict on missing / corrupt file."""
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    """Persist the cache dict to disk (atomic-ish: write then rename would be ideal,
    but for a local single-process bot a direct write is acceptable)."""
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    with open(_CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# NBP API FETCH
# ─────────────────────────────────────────────

def _fetch_rate(rate_date: date) -> tuple[float, str] | None:
    """
    Query the NBP API for the EUR mid-rate on *rate_date*.

    Returns (mid_rate, table_nr) on success.
    Returns None when the API responds with 404 (no rate for that day — weekend/holiday).
    Raises NbpRateError on any other network / HTTP error.
    """
    url = _API_URL.format(date=rate_date.isoformat())
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    # Windows often lacks the NBP root cert; skip verification for this public gov API.
    import ssl as _ssl
    _ctx = _ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = _ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        entry = data["rates"][0]
        return float(entry["mid"]), str(entry["no"])
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # No rate for this calendar day (weekend or public holiday)
            return None
        raise NbpRateError(f"NBP API HTTP {exc.code} for {rate_date}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise NbpRateError(f"NBP API network error for {rate_date}: {exc.reason}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise NbpRateError(f"NBP API unexpected response for {rate_date}: {exc}") from exc


# ─────────────────────────────────────────────
# WEEKDAY ADJUSTMENT
# ─────────────────────────────────────────────

def _last_business_day(d: date) -> date:
    """
    NBP does not publish rates on weekends.
    Move backwards to the closest preceding Friday when necessary:
      Monday (0) → Friday (-3 days)
      Saturday (5) → Friday (-1 day)
      Sunday  (6) → Friday (-2 days)
    Tuesday–Friday are already business days and returned unchanged.
    """
    wd = d.weekday()  # 0=Mon … 6=Sun
    if wd == 0:       # Monday
        return d - timedelta(days=3)
    if wd == 5:       # Saturday
        return d - timedelta(days=1)
    if wd == 6:       # Sunday
        return d - timedelta(days=2)
    return d          # Tue–Fri: no adjustment needed


# ─────────────────────────────────────────────
# PUBLIC: get_rate()
# ─────────────────────────────────────────────

def get_rate(unloading_date: date) -> NbpRate:
    """
    Return the EUR/PLN mid-rate to use for a transport with *unloading_date*.

    Business rule: the rate is taken for (unloading_date - 1 day), adjusted
    to the nearest preceding business day.  If that date is still in the future,
    walk backwards day by day until a rate is published, marking the result as
    is_approximate=True.

    Historical rates are cached permanently in bot_data/nbp_cache.json.
    Raises NbpRateError on network failure.
    """
    # Step 1: compute the target rate date (day before unloading, business-day adjusted)
    target = _last_business_day(unloading_date - timedelta(days=1))

    cache = _load_cache()
    is_approximate = False

    # Step 2: walk backwards from target until a rate is found (handles future dates
    # and public holidays that slipped through the weekday filter)
    current = target
    today = date.today()

    while True:
        cache_key = current.isoformat()

        # Return immediately if already cached
        if cache_key in cache:
            entry = cache[cache_key]
            return NbpRate(
                rate=entry["rate"],
                table_nr=entry["table_nr"],
                rate_date=entry["rate_date"],
                is_approximate=is_approximate,
            )

        # Cannot fetch future dates — walk one day back and flag as approximate
        if current > today:
            is_approximate = True
            current -= timedelta(days=1)
            current = _last_business_day(current)
            continue

        # Fetch from API
        result = _fetch_rate(current)
        if result is not None:
            mid, table_nr = result
            entry = {"rate": mid, "table_nr": table_nr, "rate_date": cache_key}
            cache[cache_key] = entry
            _save_cache(cache)
            return NbpRate(
                rate=mid,
                table_nr=table_nr,
                rate_date=cache_key,
                is_approximate=is_approximate,
            )

        # 404 → public holiday, walk one more day back
        is_approximate = is_approximate or (current != target)
        current -= timedelta(days=1)
        current = _last_business_day(current)

        # Safety net: never walk more than 10 calendar days back from target
        if (target - current).days > 10:
            raise NbpRateError(
                f"No NBP rate found within 10 days before {target} — check API availability"
            )


# ─────────────────────────────────────────────
# PUBLIC: build_note()
# ─────────────────────────────────────────────

def build_note(amount_pln: float, nbp_rate: NbpRate) -> str:
    """
    Build the Payment Plan note text for a PLN invoice converted to EUR.

    Format:
        {amount_pln} zl

        1 EUR = {rate} PLN
        Kurs ustawiany automatycznie: Tabela NBP nr {table_nr} z dnia {rate_date DD/MM/YYYY}
    """
    # Format amount without trailing zeros when whole, otherwise 2 decimal places
    amount_str = f"{amount_pln:,.2f}".replace(",", " ")  # Polish-style thousands space
    rate_date_obj = date.fromisoformat(nbp_rate.rate_date)
    rate_date_fmt = rate_date_obj.strftime("%d/%m/%Y")   # DD/MM/YYYY

    return (
        f"{amount_str} zl\n\n"
        f"1 EUR = {nbp_rate.rate} PLN\n"
        f"Kurs ustawiany automatycznie: Tabela NBP nr {nbp_rate.table_nr} z dnia {rate_date_fmt}"
    )
