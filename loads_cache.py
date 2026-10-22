"""
loads_cache.py — Local persistent cache for duplicate detection.

Primary source: bot_data/fixtures/loads_all.json
Startup (init): load JSON + refresh current month from Sheets (1 API call, background).
is_duplicate(): pure local search, no API calls.
append_row(): add to memory + save to disk immediately, no API calls.
Weekly: if JSON older than 7 days → full snapshot on next startup (background thread).
"""

import json
import os
import threading
from datetime import datetime, timedelta

_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE, "bot_data", "fixtures")
_ALL_FILE = os.path.join(_DATA_DIR, "loads_all.json")
_WEEK = timedelta(days=7)

# In-memory store — populated by init()
# Protected by _LOCK for all reads and writes from any thread.
_ALL_ROWS: list[dict] = []
_initialized: bool = False
_LOCK = threading.Lock()


# ── Disk helpers ──────────────────────────────────────────────────────────────


def _load_disk() -> tuple[list[dict], str | None]:
    """Load rows and created_at from loads_all.json. Returns (rows, created_at_str)."""
    try:
        with open(_ALL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("rows", []), data.get("created_at")
    except (FileNotFoundError, json.JSONDecodeError):
        return [], None


def _save_disk(rows: list[dict]) -> None:
    """
    Persist rows to loads_all.json atomically.
    Writes to a .tmp file first, then renames — safe against crash mid-write.
    Caller must hold _LOCK or pass a snapshot copy.
    """
    os.makedirs(_DATA_DIR, exist_ok=True)
    months = sorted({r.get("month", "") for r in rows if r.get("month")})
    data = {
        "created_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "months": months,
        "rows": rows,
    }
    tmp = _ALL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _ALL_FILE)  # atomic on Windows (NTFS) and POSIX


# ── Sheets fetch helpers ───────────────────────────────────────────────────────


def _sheet_rows_from_ws(ws, month_name: str) -> list[dict]:
    """Parse a gspread Worksheet into row dicts for loads_all.json format."""
    values = ws.get_all_values()
    rows = []
    for i, row in enumerate(values[3:], start=4):  # 3 header rows, 1-based
        while len(row) < 36:
            row.append("")
        if not row[0]:  # empty Nr → skip
            continue
        rows.append(
            {
                "month": month_name,
                "sheet_row": i,
                "A": row[0],  # seq number (Nr)
                "B": row[1],  # client order number
                "C": row[2],  # our order number
                "D": row[3],  # company name
                "G": row[6],  # loading place
                "H": row[7],  # unloading place
                "J": row[9],  # unloading date (DD.MM.YYYY)
                "L": row[11],  # carrier/truck code
                "V": row[21] if len(row) > 21 else "",  # invoice nr to client
            }
        )
    return rows


def _open_sheet():
    """Open the Loads Info Google Spreadsheet via OAuth. Returns gspread.Spreadsheet."""
    from actions import _get_oauth_creds
    import gspread
    from config import LOADS_INFO_SHEET_ID

    gc = gspread.authorize(_get_oauth_creds())
    return gc.open_by_key(LOADS_INFO_SHEET_ID)


def _fetch_month_rows(month_name: str) -> list[dict]:
    """Fetch one named month sheet. Returns [] on error or missing sheet."""
    try:
        sh = _open_sheet()
        for ws in sh.worksheets():
            if ws.title.strip().lower() == month_name.strip().lower():
                rows = _sheet_rows_from_ws(ws, ws.title.strip())
                print(f"[loads_cache] Fetched {len(rows)} rows for '{month_name}'")
                return rows
        print(f"[loads_cache] Sheet '{month_name}' not found")
        return []
    except Exception as e:
        print(f"[loads_cache] _fetch_month_rows failed: {e}")
        return []


def _fetch_all_months() -> list[dict]:
    """Fetch all month sheets. Used for first-run and weekly full refresh."""
    from config import MONTH_NAMES

    try:
        sh = _open_sheet()
        valid = set(MONTH_NAMES.values())
        all_rows = []
        for ws in sh.worksheets():
            title = ws.title.strip()
            if title not in valid:
                continue
            all_rows.extend(_sheet_rows_from_ws(ws, title))
        print(f"[loads_cache] Full snapshot: {len(all_rows)} rows across all months")
        return all_rows
    except Exception as e:
        print(f"[loads_cache] _fetch_all_months failed: {e}")
        return []


# ── Public API ────────────────────────────────────────────────────────────────


def init() -> None:
    """
    Initialize on app startup (call once from MailTab.__init__).

    Steps:
    1. Load loads_all.json from disk → _ALL_ROWS
    2. If file missing or > 7 days old → full snapshot in background thread
    3. Otherwise → refresh current month only in background thread
    """
    global _ALL_ROWS, _initialized

    rows, created_at = _load_disk()
    _ALL_ROWS = rows

    # Decide: full refresh or current-month-only?
    needs_full = False
    if created_at is None:
        needs_full = True
        print("[loads_cache] loads_all.json missing — queuing full snapshot")
    else:
        try:
            age = datetime.now() - datetime.strptime(created_at, "%d.%m.%Y %H:%M")
            if age > _WEEK:
                needs_full = True
                print(
                    f"[loads_cache] loads_all.json is {age.days}d old — queuing full snapshot"
                )
        except ValueError:
            needs_full = True

    if needs_full:

        def _bg_full():
            global _ALL_ROWS
            fetched = _fetch_all_months()  # no lock needed — pure network fetch
            if fetched:
                with _LOCK:
                    _ALL_ROWS = fetched
                    snapshot = list(_ALL_ROWS)
                _save_disk(snapshot)  # write outside lock

        threading.Thread(target=_bg_full, daemon=True, name="loads_cache_full").start()
    else:

        def _bg_current():
            global _ALL_ROWS
            from config import MONTH_NAMES

            month_name = MONTH_NAMES.get(datetime.now().month, "")
            if not month_name:
                return
            fresh = _fetch_month_rows(month_name)  # pure network, no lock
            if not fresh:
                return
            with _LOCK:
                other = [r for r in _ALL_ROWS if r.get("month") != month_name]
                _ALL_ROWS = other + fresh
                snapshot = list(_ALL_ROWS)
            _save_disk(snapshot)  # write outside lock
            print(f"[loads_cache] '{month_name}' refreshed ({len(fresh)} rows)")

        threading.Thread(
            target=_bg_current, daemon=True, name="loads_cache_month"
        ).start()

    _initialized = True
    print(f"[loads_cache] init done — {len(_ALL_ROWS)} rows loaded from disk")


def is_duplicate(client_order_number: str) -> dict | None:
    """
    Search _ALL_ROWS for matching client order number (col B).
    Pure local lookup — no API calls. Thread-safe via _LOCK.
    Returns matching row dict or None.
    """
    if not client_order_number:
        return None
    needle = client_order_number.strip().upper()
    with _LOCK:
        rows_snapshot = list(_ALL_ROWS)  # copy under lock, iterate outside
    for row in rows_snapshot:
        b = (row.get("B") or "").strip().upper()
        if b and b == needle:
            return row
    return None


def append_row(parsed_order: dict, our_order_number: str = "") -> None:
    """
    Append a newly processed order to _ALL_ROWS and immediately save to disk.
    Called after executor confirms the row was written to Sheets.
    No API calls.
    """
    from config import MONTH_NAMES

    month_name = MONTH_NAMES.get(datetime.now().month, "")
    lp = parsed_order.get("loading_places") or []
    up = parsed_order.get("unloading_places") or []
    row = {
        "month": month_name,
        "sheet_row": None,  # confirmed by Sheets later
        "B": (parsed_order.get("client_order_number") or "").strip(),
        "C": our_order_number or (parsed_order.get("our_order_number") or ""),
        "D": (parsed_order.get("company_name") or "").strip(),
        "G": "+".join(str(p) for p in lp if p),
        "H": "+".join(str(p) for p in up if p),
    }
    with _LOCK:
        _ALL_ROWS.append(row)
        snapshot = list(_ALL_ROWS)
    try:
        _save_disk(snapshot)  # atomic write, outside lock
        print(f"[loads_cache] Appended B={row['B']!r}  C={row['C']!r}")
    except Exception as e:
        print(f"[loads_cache] append_row save failed: {e}")


# ── Legacy shims — keeps existing callers in app.py working ───────────────────


def get_loads_cache() -> dict:
    """Compatibility: returns old-style cache dict with 'rows' using legacy keys."""
    with _LOCK:
        snapshot = list(_ALL_ROWS)
    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": [
            {
                "sheet_row": r.get("sheet_row"),
                "client_order_nr": r.get("B", ""),
                "our_order_nr": r.get("C", ""),
                "company": r.get("D", ""),
                "loading": r.get("G", ""),
                "unloading": r.get("H", ""),
            }
            for r in snapshot
        ],
    }


def refresh_cache() -> dict:
    """Compatibility: synchronous current-month refresh, returns get_loads_cache()."""
    from config import MONTH_NAMES

    global _ALL_ROWS
    try:
        month_name = MONTH_NAMES.get(datetime.now().month, "")
        if month_name:
            fresh = _fetch_month_rows(month_name)  # network, no lock
            with _LOCK:
                other = [r for r in _ALL_ROWS if r.get("month") != month_name]
                _ALL_ROWS = other + fresh
                snapshot = list(_ALL_ROWS)
            _save_disk(snapshot)
    except Exception as e:
        print(f"[loads_cache] refresh_cache failed: {e}")
    return get_loads_cache()
