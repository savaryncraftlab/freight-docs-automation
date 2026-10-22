"""
actions.py — All Google API actions: Sheets, Drive, Gmail.
"""

import re
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import (
    CREDENTIALS_PATH,
    DRIVE_ROOT_FOLDER_ID,
    LOADS_INFO_SHEET_ID,
    MONTH_NAMES,
    PAYMENT_PLAN_SHEET_ID,
)
from email_templates import build_email, _is_polish_company


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.compose",
]


# ─────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────


def _col_letter(idx: int) -> str:
    """Convert 0-based column index to letter(s): 0→A, 25→Z, 26→AA."""
    result = ""
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def _fix_date(d) -> str:
    """
    Normalize any date string to DD.MM.YYYY.
    Handles: YYYY-MM-DD, DD.MM.YYYY, DD/MM/YYYY, DD.MM.YY
    """
    if not d:
        return ""
    d = str(d).strip()
    # Already correct
    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", d):
        return d
    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            pass
    # DD/MM/YYYY
    if re.match(r"^\d{2}/\d{2}/\d{4}$", d):
        try:
            return datetime.strptime(d, "%d/%m/%Y").strftime("%d.%m.%Y")
        except ValueError:
            pass
    # YYYY.MM.DD
    if re.match(r"^\d{4}\.\d{2}\.\d{2}$", d):
        try:
            return datetime.strptime(d, "%Y.%m.%d").strftime("%d.%m.%Y")
        except ValueError:
            pass
    # DD.MM.YY (2-digit year)
    if re.match(r"^\d{2}\.\d{2}\.\d{2}$", d):
        try:
            return datetime.strptime(d, "%d.%m.%y").strftime("%d.%m.%Y")
        except ValueError:
            pass
    return d


def _format_date(date_str) -> str:
    """Final safety pass: convert YYYY-MM-DD → DD.MM.YYYY; leave correct format unchanged."""
    if not date_str:
        return ""
    date_str = str(date_str).strip()
    if "-" in date_str and len(date_str) == 10:
        parts = date_str.split("-")
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return date_str


def _rgb(hex_color: str) -> dict:
    """Convert #RRGGBB to Sheets API color dict."""
    h = hex_color.lstrip("#")
    return {
        "red": int(h[0:2], 16) / 255,
        "green": int(h[2:4], 16) / 255,
        "blue": int(h[4:6], 16) / 255,
    }


def _get_service_creds():
    return Credentials.from_service_account_file(str(CREDENTIALS_PATH), scopes=SCOPES)


def _get_oauth_creds():
    token_path = Path(CREDENTIALS_PATH).parent / "token.json"
    oauth_creds_path = Path(CREDENTIALS_PATH).parent / "oauth_credentials.json"

    creds = None
    if token_path.exists():
        creds = OAuthCredentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(oauth_creds_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


_CLIENTS_CACHE = None


def _get_clients():
    """Всі сервіси через OAuth token.json — записується від імені Anna Lewandowska.

    Singleton cache: builds service objects once per process. If the OAuth
    token becomes invalid (e.g. ``invalid_grant``), the caller should set
    ``_CLIENTS_CACHE = None`` and retry.
    """
    global _CLIENTS_CACHE
    if _CLIENTS_CACHE is not None:
        return _CLIENTS_CACHE

    oauth = _get_oauth_creds()
    gspread_client = gspread.authorize(oauth)
    sheets_service = build("sheets", "v4", credentials=oauth)
    drive_service = build("drive", "v3", credentials=oauth)
    gmail_service = build("gmail", "v1", credentials=oauth)
    _CLIENTS_CACHE = (gspread_client, sheets_service, drive_service, gmail_service)
    return _CLIENTS_CACHE


def _get_or_create_folder(drive_service, parent_id: str, folder_name: str) -> str:
    """Find or create a Drive folder."""
    query = (
        f"name='{folder_name}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    result = (
        drive_service.files()
        .list(
            q=query,
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    created = (
        drive_service.files()
        .create(
            body={
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return created["id"]


def _normalize_payment_type(value: str) -> str:
    return "Originals" if "original" in str(value or "").lower() else "Scans"


def _clean_money_value(value, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    cleaned = (
        str(value)
        .replace(",", ".")
        .replace(" ", "")
        .replace("€", "")
        .replace("в‚¬", "")
        .replace("zł", "")
        .replace("zЕ‚", "")
        .replace("EUR", "")
        .replace("PLN", "")
        .strip()
    )
    try:
        return round(float(cleaned), 2)
    except Exception:
        return default


def _carrier_payment(carrier_info, client_days: int, client_type: str):
    """Resolve carrier payment terms per config rules."""

    def _default_terms(days: int, doc_type: str):
        doc_type = doc_type.lower()
        if days == 60 and doc_type == "scans":
            return 30, "Originals"
        if days == 45 and doc_type == "scans":
            return 30, "Scans"
        if 45 <= days <= 60 and doc_type == "originals":
            return 30, "Originals"
        return 30, "Scans"

    if not carrier_info:
        return _default_terms(client_days, client_type)

    days_rule = carrier_info.get("payment_days_rule", "fixed_30")
    type_rule = carrier_info.get("payment_type_rule", "scans")

    # OWN1/OWN2 — use conditional rules
    if days_rule == "own_conditional" or type_rule == "own_conditional":
        return _default_terms(client_days, client_type)

    if days_rule == "same_as_client":
        carrier_days = client_days
    elif str(days_rule).startswith("fixed_"):
        carrier_days = int(str(days_rule).replace("fixed_", ""))
    else:
        carrier_days = 30

    if type_rule == "same_as_client":
        carrier_type = client_type
    elif type_rule == "originals":
        carrier_type = "Originals"
    elif type_rule == "scans_or_originals":
        carrier_type = (
            "Originals"
            if (client_days > 45 or client_type.lower() == "originals")
            else "Scans"
        )
    else:
        carrier_type = "Scans"

    return carrier_days, carrier_type


def _actual_row_from_append(append_result: dict, fallback: int) -> int:
    updated_range = append_result.get("updates", {}).get("updatedRange", "")
    m = re.search(r":?[A-Z]+(\d+)$", updated_range)
    return int(m.group(1)) if m else fallback


CARRIER_COLORS = {
    "KWD": "D9D2E9",
    "KWDL": "C27BA0",
    "LKW9": "FFD966",
    "LKW4": "BF9000",
    "OLA": "9FC5E8",
    "PWT": "CFE2F3",
    "OWN1": "E69138",
    "OWN2": "F6B26B",
    "THETA": "B6D7A8",
}


def _apply_row_formatting(
    sheets_service,
    spreadsheet_id: str,
    worksheet_id: int,
    row: int,
    adr_value: str,
    carrier_code: str = "",
    is_brokeralpha: bool = False,
    log_fn=None,
    license_plates: str = "",
) -> None:
    """
    Apply all required formatting to a newly written row:
      - Column E  → left-aligned
      - All other columns → center-aligned
      - Column R (index 17) → light blue background #CCE5FF
      - Column K (index 10) ADR: YES=red, NO=green
      - Column J (index 9) unloading date → yellow #FFFF00
      - Columns L and M colored by carrier code if matched
    """
    r = row - 1  # 0-based start index

    def _repeat(start_col, end_col, cell_fmt, fields):
        return {
            "repeatCell": {
                "range": {
                    "sheetId": worksheet_id,
                    "startRowIndex": r,
                    "endRowIndex": r + 1,
                    "startColumnIndex": start_col,
                    "endColumnIndex": end_col,
                },
                "cell": {"userEnteredFormat": cell_fmt},
                "fields": fields,
            }
        }

    requests = [
        # Font Calibri 11 for entire row
        _repeat(
            0,
            36,
            {"textFormat": {"fontFamily": "Calibri", "fontSize": 11}},
            "userEnteredFormat.textFormat.fontFamily,userEnteredFormat.textFormat.fontSize",
        ),
        # Center-align all 36 columns
        _repeat(
            0,
            36,
            {"horizontalAlignment": "CENTER"},
            "userEnteredFormat.horizontalAlignment",
        ),
        # Left-align column E (index 4) — email
        _repeat(
            4,
            5,
            {"horizontalAlignment": "LEFT"},
            "userEnteredFormat.horizontalAlignment",
        ),
        # Left-align column AJ (index 35) — paid to carrier
        _repeat(
            35,
            36,
            {"horizontalAlignment": "LEFT"},
            "userEnteredFormat.horizontalAlignment",
        ),
        # Light blue background on column R (index 17)
        _repeat(
            17,
            18,
            {"backgroundColor": _rgb("#E3FCF5")},
            "userEnteredFormat.backgroundColor",
        ),
        # Yellow background on columns I(8) and J(9) — endColumnIndex is exclusive, so 8-10 = indices 8,9
        _repeat(
            8,
            10,
            {"backgroundColor": _rgb("#FFFF00")},
            "userEnteredFormat.backgroundColor",
        ),
        # Date number format on columns I and J
        _repeat(
            8,
            10,
            {"numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"}},
            "userEnteredFormat.numberFormat",
        ),
        # Euro number format on column R (index 17)
        _repeat(
            17,
            18,
            {"numberFormat": {"type": "NUMBER", "pattern": '#,##0.00" \u20ac"'}},
            "userEnteredFormat.numberFormat",
        ),
    ]

    # Column K (index 10) ADR colors — YES=green, NO=red, anything else=no fill (skip).
    # Conditional append so unknown/empty ADR never inherits a wrong color.
    _adr_norm = str(adr_value).strip().upper()
    if _adr_norm == "YES":
        requests.append(
            _repeat(
                10,
                11,
                {"backgroundColor": _rgb("#00FF00")},
                "userEnteredFormat.backgroundColor",
            )
        )
    elif _adr_norm == "NO":
        requests.append(
            _repeat(
                10,
                11,
                {"backgroundColor": _rgb("#EA9999")},
                "userEnteredFormat.backgroundColor",
            )
        )

    # Carrier color for columns L, M, N (11-14) if carrier_code matches.
    # KWDL has two sub-variants: LU plates → D9D2E9 (lavender), BK plates → C27BA0 (pink)
    carrier_code_upper = (carrier_code or "").upper()
    if carrier_code_upper == "KWDL":
        plates_upper = (license_plates or "").upper()
        hex_color = "C27BA0" if "BK" in plates_upper else "D9D2E9"
    else:
        hex_color = CARRIER_COLORS.get(carrier_code_upper)
    if hex_color:
        color_dict = _rgb(f"#{hex_color}")
        requests.append(
            _repeat(
                11,
                14,
                {"backgroundColor": color_dict},
                "userEnteredFormat.backgroundColor",
            )
        )

    # Columns Y (24) and AG (32): bot writes "Scans"/"Originals" text only.
    # Manual chip dropdowns in the sheet handle coloring — no backgroundColor needed.

    # EUR format for O, P, Q (cols 14-17) — BrokerAlpha only
    if is_brokeralpha:
        requests.append(
            _repeat(
                14,
                17,
                {"numberFormat": {"type": "NUMBER", "pattern": '#,##0.00" \u20ac"'}},
                "userEnteredFormat.numberFormat",
            )
        )

    _log = log_fn or print
    try:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    except Exception as e:
        import traceback

        _log(f"⚠️ Formatting error: {repr(e)}")
        _log(traceback.format_exc())


# ─────────────────────────────────────────────
# SHEET HELPERS
# ─────────────────────────────────────────────


def _detect_manager(sender_email: str) -> str:
    se = (sender_email or "").lower()
    if "dispo@acme" in se:
        return "M1"
    if "info@acme" in se:
        return "M2"
    return ""


def _get_next_nr_and_row(worksheet) -> tuple[int, int]:
    """
    Returns (next_nr, insert_row_1based).
    - next_nr    : max Nr found in column A + 1
    - insert_row : row after the last row with ANY non-empty content
                   (not just the last Nr row, so partial writes from
                   instant_data don't get silently overwritten)
    """
    try:
        all_values = worksheet.get_all_values()
        last_nr = 0
        last_nr_row_idx = 0
        for i, row in enumerate(all_values):
            try:
                n = int(str(row[0]).strip())
                if n > 0 and n > last_nr:
                    last_nr = n
                    last_nr_row_idx = i
            except (ValueError, TypeError):
                pass
        insert_row = last_nr_row_idx + 2
        next_nr = last_nr + 1
        return next_nr, insert_row
    except Exception:
        return 1, 2


def _get_next_order_nr(worksheet, now) -> str:
    """
    Scan column C bottom-to-top for the last order number entry.
    Supports both formats: YYYY-MM-NNNNN (old) and plain number (new).
    Returns next sequence as plain string e.g. "13".
    """
    import re

    _ORDER_NR_RE = re.compile(r"^(\d{4})-(\d{2})-(\d+)$")
    _PLAIN_NR_RE = re.compile(r"^(\d+)$")
    try:
        all_values = worksheet.get_all_values()
        for row in reversed(all_values):
            if len(row) < 3:
                continue
            val = str(row[2]).strip()
            if not val or val == "-":
                continue
            # Try YYYY-MM-NNNNN format (old style)
            m = _ORDER_NR_RE.match(val)
            if m:
                year, month, seq = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if year == now.year and month == now.month:
                    return str(seq + 1)
                else:
                    return "1"
            # Try plain number format (new style, written as "12")
            m2 = _PLAIN_NR_RE.match(val)
            if m2:
                seq = int(m2.group(1))
                return str(seq + 1)
    except Exception:
        pass
    return "1"


def _get_worksheet(spreadsheet):
    """
    Returns the worksheet for the current month (e.g. '03 Marzec').
    Falls back to the first worksheet if not found.
    """
    target = MONTH_NAMES.get(datetime.now().month, "")
    if target:
        try:
            return spreadsheet.worksheet(target)
        except Exception:
            pass
    return spreadsheet.get_worksheet(0)


# ─────────────────────────────────────────────
# PUBLIC FUNCTIONS
# ─────────────────────────────────────────────


def write_to_loads_info(
    parsed_order: dict,
    carrier_info: dict | None,
    our_order_number: str,
    row_nr: int = 0,
    sender_email: str = "",
    log_fn=None,
    total_minutes: float = 0.0,
    doc_type: str = "order",
) -> tuple[bool, int, str] | None:
    """
    Writes an order to Loads Info.

    Returns (success, seq_nr, our_order_number_used).
    seq_nr is the Nr written in column A (used to name the Drive folder).
    our_order_number_used is the final value written to column C.
    """
    log = log_fn or print
    parsed_order = {k: (v if v is not None else "") for k, v in parsed_order.items()}

    if parsed_order.get("sheet_row"):
        print(f" Already in Sheets (row {parsed_order['sheet_row']}), skipping")
        return None

    try:
        gc, sheets_service, _, _ = _get_clients()
        spreadsheet = gc.open_by_key(LOADS_INFO_SHEET_ID)
        worksheet = _get_worksheet(spreadsheet)  # FIX #9: correct month tab
        sheet_name = worksheet.title
        log(f"   Sheet: {sheet_name}")

        loading_place = "+".join(
            str(p) for p in (parsed_order.get("loading_places") or []) if p
        )
        unloading_place = "+".join(
            str(p) for p in (parsed_order.get("unloading_places") or []) if p
        )
        loading_date = _format_date(_fix_date(parsed_order.get("loading_date", "")))
        unloading_date = _format_date(_fix_date(parsed_order.get("unloading_date", "")))

        _is_brokeralpha = (
            "brokeralpha" in (parsed_order.get("company_name") or "").lower()
        )
        client_days = int(parsed_order.get("payment_days") or 0)
        # Fix 4: BrokerAlpha always pays in 14 days regardless of what AI parsed
        if _is_brokeralpha:
            client_days = 14
        client_type = _normalize_payment_type(parsed_order.get("payment_type"))
        carrier_days_parsed = int(parsed_order.get("carrier_payment_days") or 0)
        carrier_days_from_rule, carrier_type = _carrier_payment(
            carrier_info, client_days, client_type
        )
        # AH: BrokerAlpha = 14; else use carrier rule from _carrier_payment(); fallback to parsed or 30
        if _is_brokeralpha:
            carrier_days = 14
        elif carrier_days_from_rule > 0:
            carrier_days = carrier_days_from_rule
        else:
            carrier_days = carrier_days_parsed if carrier_days_parsed > 0 else 30

        # FIX #4: always take carrier name from CARRIERS config when available
        carrier_name = (
            (carrier_info or {}).get("name")
            or parsed_order.get("carrier_name", "")
            or parsed_order.get("license_plates", "")  # last resort
        )
        carrier_name = carrier_name.strip()
        license_plates = parsed_order.get("license_plates") or ""
        if not license_plates and carrier_info and carrier_info.get("license_plates"):
            plates_list = carrier_info["license_plates"]
            license_plates = (
                plates_list[0] if isinstance(plates_list, list) else str(plates_list)
            )

        # FIX #8: skip O/P/Q if carrier price is 0 or missing
        raw_netto = parsed_order.get("price_netto")
        carrier_netto = _clean_money_value(raw_netto)
        # Override for BrokerAlpha: carrier price = 90% of price_netto
        if _is_brokeralpha:
            carrier_netto = round(carrier_netto * 0.9, 2)
        turnover_netto = _clean_money_value(parsed_order.get("price_netto"))

        # PLN → EUR conversion for col R using NBP rate (day before unloading)
        _nbp_result = None  # NbpRate or None; used after row write for note + bg
        _nbp_note_text = ""
        _currency = parsed_order.get("currency", "")
        if (
            _currency == "PLN"
            and turnover_netto > 0
            and unloading_date  # dd.MM.yyyy string; empty → skip
        ):
            try:
                from nbp_rates import get_rate, build_note as _build_nbp_note

                _unload_dt = datetime.strptime(unloading_date, "%d.%m.%Y").date()
                _nbp_result = get_rate(_unload_dt)
                _eur_value = round(turnover_netto / _nbp_result.rate, 2)
                _nbp_note_text = _build_nbp_note(turnover_netto, _nbp_result)
                log(
                    f"   PLN→EUR: {turnover_netto} PLN / {_nbp_result.rate} = {_eur_value} EUR"
                    f" (table {_nbp_result.table_nr}, approx={_nbp_result.is_approximate})"
                )
                turnover_netto = _eur_value  # write EUR value to col R
            except Exception as _nbp_err:
                log(f"⚠️ NBP rate error — col R will contain PLN value: {_nbp_err}")
                _nbp_result = None  # no note, no color override

        # VAT column S — Polish company → formula =R*0.23, foreign → 0
        # VAT is based on CLIENT's country (NIP), NOT on loading/unloading route
        company_name = parsed_order.get("company_name", "")
        _currency = parsed_order.get("currency", "")
        nip = parsed_order.get("nip", "") or ""
        _nip_upper = nip.upper().replace(" ", "")
        # If NIP starts with a known foreign EU prefix → definitely not Polish
        _EU_PREFIXES = (
            "DE",
            "NL",
            "BE",
            "FR",
            "AT",
            "CZ",
            "SK",
            "HU",
            "IT",
            "ES",
            "LU",
            "DK",
            "SE",
            "FI",
            "IE",
            "PT",
            "RO",
            "BG",
            "HR",
            "SI",
            "LT",
            "LV",
            "EE",
            "EL",
            "CY",
            "MT",
        )
        _is_foreign_nip = any(_nip_upper.startswith(p) for p in _EU_PREFIXES)
        _is_polish = not _is_foreign_nip and (
            _nip_upper.startswith("PL")
            or _is_polish_company(company_name)
            or _currency == "PLN"
        )

        now = datetime.now()
        today = now.strftime("%d.%m.%Y")
        manager = _detect_manager(sender_email)
        next_nr, insert_row = _get_next_nr_and_row(worksheet)
        log(f"   Nr: {next_nr}, row: {insert_row}, Manager: {manager or '?'}")

        # Determine carrier code for column L (needed before is_own check)
        carrier_code = ""
        from config import CARRIERS

        if carrier_info and isinstance(carrier_info, dict):
            for key, info in CARRIERS.items():
                if info is carrier_info or info.get("name") == carrier_info.get("name"):
                    carrier_code = key
                    break

        # Step 2 — body scan: if carrier_code still empty, scan first 5 body lines.
        # Uses startswith + longest-alias-first so "KWDL Kowalski Trans..." → KWDL,
        # not KWD, and "LKW-4 ..." → LKW4 after normalisation.
        if not carrier_code:
            _body_scan = parsed_order.get("_body", "") or ""
            _SCAN_ALIASES = {
                "KWDL": "KWDL",
                "KWD": "KWD",
                "LKW4": "LKW4",
                "LKW9": "LKW9",
                "OWN1": "OWN1",
                "OWN2": "OWN2",
                "OLA": "OLA",
                "PWT": "PWT",
                "PWTRANS": "PWT",
            }
            for _bl in _body_scan.strip().split("\n")[:5]:
                _bn = _bl.strip().upper().replace("-", "").replace(" ", "")
                for _alias in sorted(_SCAN_ALIASES.keys(), key=len, reverse=True):
                    if _bn.startswith(_alias):
                        carrier_code = _SCAN_ALIASES[_alias]
                        break
                if carrier_code:
                    break

        # Final override: truck_type from parsed_order is the most reliable source —
        # it comes directly from the email body and is more specific than
        # carrier_name matching (KWD vs KWDL may share the same display name)
        truck_type_direct = (
            (parsed_order.get("truck_type") or "")
            .upper()
            .strip()
            .replace("-", "")
            .replace(" ", "")
        )
        if truck_type_direct in CARRIERS:
            carrier_code = truck_type_direct
        carrier_code = carrier_code.replace(" ", "")

        # Override carrier_name from CARRIERS config — AI output is unreliable
        if carrier_code and carrier_code in CARRIERS:
            carrier_name = CARRIERS[carrier_code]["name"]
        else:
            carrier_name = parsed_order.get("carrier_name", "")

        # Use CARRIERS config plates when carrier_code is known.
        # For carriers with multiple truck sets (comma-separated, e.g. KWDL),
        # match the AI-extracted plate to the correct set.
        if carrier_code and carrier_code in CARRIERS:
            _cfg_plates = CARRIERS.get(carrier_code, {}).get("license_plates", "")
            _plate_sets = [s.strip() for s in _cfg_plates.split(",") if s.strip()]
            if len(_plate_sets) <= 1:
                # Single truck — always use config plates
                license_plates = _cfg_plates
            else:
                # Multiple trucks — match AI plates to the correct set
                _ai_plates = (parsed_order.get("license_plates") or "").upper()
                _matched_set = ""
                for _pset in _plate_sets:
                    # Check if any plate from this set appears in AI result
                    _first_plate = _pset.split("/")[0].strip().upper()
                    if _first_plate and _first_plate in _ai_plates:
                        _matched_set = _pset
                        break
                license_plates = _matched_set or _plate_sets[0]

        # AG / AH rules:
        # MIRROR_CARRIERS: AG = client payment_type (Y), AH = client payment_days (Z)
        #   — KWD and KWDL mirror because they are sub-contractors paid on same terms as client.
        #   — OWN1/OWN2 mirror because they are our own trucks (conditional terms).
        # Others (LKW4/LKW9/PWT/OLA): AG/AH from CARRIERS config payment rules.
        _DAYS_RULE = {"fixed_14": 14, "fixed_30": 30, "fixed_45": 45}
        MIRROR_CARRIERS = ("OWN1", "OWN2", "KWD", "KWDL")
        if carrier_code in MIRROR_CARRIERS:
            carrier_payment_days = int(parsed_order.get("payment_days") or 30)
            carrier_docs_type = (
                (parsed_order.get("payment_type") or "scans").lower().strip()
            )
        else:
            _cfg = CARRIERS.get(carrier_code, {})
            carrier_payment_days = _DAYS_RULE.get(
                _cfg.get("payment_days_rule", "fixed_30"), 30
            )
            _type_rule = _cfg.get("payment_type_rule", "scans")
            carrier_docs_type = (
                _type_rule if _type_rule in ("scans", "originals") else "scans"
            )
        # Normalise to Title Case regardless of branch ("Scans" / "Originals")
        carrier_docs_type = (carrier_docs_type or "scans").strip().capitalize()
        # BrokerAlpha override: KWDL gets 7 days, others get 14
        if _is_brokeralpha:
            carrier_payment_days = 7 if carrier_code == "KWDL" else 14

        # Column C: detect OWN carriers (own trucks — no order number)
        is_own = (carrier_info or {}).get(
            "payment_days_rule"
        ) == "own_conditional" or carrier_code.upper() in ("OWN1", "OWN2")
        if is_own:
            col_c = "-"
        else:
            seq = _get_next_order_nr(worksheet, now)  # plain sequence number e.g. "59"
            col_c = seq

        # BUG 2 fix: ensure order_number field is captured (could also come as "order_number" key)
        order_nr = (
            parsed_order.get("order_number")
            or parsed_order.get("client_order_number")
            or ""
        )

        adr_value = (parsed_order.get("adr") or "").strip().upper()

        # Guard: ACME is NEVER a client — if AI returned it, clear the field
        if "acme" in company_name.lower() or "acme" in company_name.lower():
            log("⚠️ AI returned ACME as company_name — clearing (ACME is us)")
            company_name = ""

        # B: normalise to "" when AI returns None
        client_order_number = (parsed_order.get("client_order_number") or "").strip()

        # E: BrokerAlpha always uses partner email, not the per-order contact
        if _is_brokeralpha:
            contact_email = "partner@brokeralpha.example"
        else:
            contact_email = (parsed_order.get("contact_email") or "").strip()

        # L/M/N only written for order type, not for carrier_invoice
        _fill_carrier = doc_type != "carrier_invoice"
        _carrier_name_full = carrier_name if _fill_carrier else ""

        placeholder_row = [
            next_nr,  # A[0]  — Nr
            client_order_number,  # B[1]  — Client order nr
            col_c,  # C[2]  — Our order nr
            company_name,  # D[3]  — Company
            contact_email,  # E[4]  — Email
            manager,  # F[5]  — Manager
            loading_place,  # G[6]  — Loading place
            unloading_place,  # H[7]  — Unloading place
            loading_date,  # I[8]  — Loading date
            unloading_date,  # J[9]  — Unloading date
            adr_value,  # K[10] — ADR
            carrier_code if _fill_carrier else "",  # L[11] — Carrier code
            _carrier_name_full,  # M[12] — Carrier name
            license_plates if _fill_carrier else "",  # N[13] — Plates
            "",  # O[14] — blank; BrokerAlpha gets =R*0.9 via formulas dict
            "",  # P[15] — formula =O*0.23 (BrokerAlpha only)
            "",  # Q[16] — formula =O+P   (BrokerAlpha only)
            turnover_netto if turnover_netto > 0 else "",  # R[17] — Turnover NETTO
            0,  # S[18] — VAT Turnover
            "",  # T[19] — Turnover BRUTTO (formula)
            "",  # U[20] — Delta (formula)
            "",  # V[21] — Invoice nr to client
            "",  # W[22] — Date sent
            "",  # X[23] — Date received
            client_type,  # Y[24] — Client pay type
            client_days,  # Z[25] — Client pay days
            "",  # AA[26] — Payment date (formula)
            "NO",  # AB[27] — Payment received
            "",  # AC[28] — intentionally empty
            "",  # AD[29] — Invoice nr from carrier
            "",  # AE[30] — Date invoice email
            "",  # AF[31] — Date originals received
            carrier_docs_type,  # AG[32] — Carrier pay type
            carrier_payment_days,  # AH[33] — Carrier pay days
            "",  # AI[34] — Carrier payment date (formula)
            "NO",  # AJ[35] — Paid to carrier
            # Total: 36 values → A(0)…Z(25), AA(26)…AJ(35) — matches range A:AJ
        ]

        actual = insert_row

        # ── Write row data ──────────────────────────────────────
        sheets_service.spreadsheets().values().update(
            spreadsheetId=LOADS_INFO_SHEET_ID,
            range=f"{sheet_name}!A{actual}:AJ{actual}",
            valueInputOption="RAW",
            body={"values": [placeholder_row]},
        ).execute()
        # Column C background: green for OWN ("-"), yellow for regular order nr
        _col_c_bg = (
            _rgb("#D9EAD3") if is_own else {"red": 1.0, "green": 1.0, "blue": 0.0}
        )
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=LOADS_INFO_SHEET_ID,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet.id,
                                "startRowIndex": actual - 1,
                                "endRowIndex": actual,
                                "startColumnIndex": 2,
                                "endColumnIndex": 3,
                            },
                            "cell": {
                                "userEnteredFormat": {"backgroundColor": _col_c_bg}
                            },
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    }
                ]
            },
        ).execute()

        # ── Formulas ─────────────────────────────────────────────
        formulas = {
            f"T{actual}": f"=R{actual}+S{actual}",
            f"AA{actual}": f"=X{actual}+Z{actual}",
            f"AI{actual}": f"=AH{actual}+AE{actual}",
        }
        if _is_brokeralpha:
            # U (DELTA) only for TC — carrier price known upfront (O=R*0.9)
            formulas[f"U{actual}"] = f"=R{actual}-O{actual}"
            formulas[f"O{actual}"] = f"=R{actual}*0,9"
            formulas[f"P{actual}"] = f"=O{actual}*0,23"
            formulas[f"Q{actual}"] = f"=O{actual}+P{actual}"
        if _is_polish:
            formulas[f"S{actual}"] = f"=R{actual}*0,23"
        else:
            formulas[f"S{actual}"] = 0

        formula_data = [
            {"range": f"{sheet_name}!{cell}", "values": [[formula]]}
            for cell, formula in formulas.items()
        ]
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=LOADS_INFO_SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": formula_data},
        ).execute()

        # ── Carrier colors on L, M, N + O/P/Q format (explicit batchUpdate) ──
        bg_requests = []
        _cc_upper = carrier_code.upper()
        if _cc_upper == "KWDL":
            _plates_upper = (license_plates or "").upper()
            hex_color = "C27BA0" if "BK" in _plates_upper else "D9D2E9"
        else:
            hex_color = CARRIER_COLORS.get(_cc_upper, "")
        if hex_color:
            h = hex_color
            bg_requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": worksheet.id,
                            "startRowIndex": actual - 1,
                            "endRowIndex": actual,
                            "startColumnIndex": 11,  # L
                            "endColumnIndex": 14,  # N+1 (L, M, N)
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {
                                    "red": int(h[0:2], 16) / 255,
                                    "green": int(h[2:4], 16) / 255,
                                    "blue": int(h[4:6], 16) / 255,
                                }
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )
        if carrier_netto > 0 and _fill_carrier:
            # O background #fcf9e7 for all orders with a known carrier price
            bg_requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": worksheet.id,
                            "startRowIndex": actual - 1,
                            "endRowIndex": actual,
                            "startColumnIndex": 14,  # O
                            "endColumnIndex": 15,
                        },
                        "cell": {
                            "userEnteredFormat": {"backgroundColor": _rgb("#fcf9e7")}
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )
            # EUR number format on O (and P/Q for BrokerAlpha formulas)
            bg_requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": worksheet.id,
                            "startRowIndex": actual - 1,
                            "endRowIndex": actual,
                            "startColumnIndex": 14,  # O
                            "endColumnIndex": 17
                            if _is_brokeralpha
                            else 15,  # Q+1 for TC, O only otherwise
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "NUMBER",
                                    "pattern": '#,##0.00" \u20ac"',
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )
        if bg_requests:
            try:
                sheets_service.spreadsheets().batchUpdate(
                    spreadsheetId=LOADS_INFO_SHEET_ID,
                    body={"requests": bg_requests},
                ).execute()
            except Exception as bg_err:
                log(f"⚠️ Color warning: {bg_err}")

        # ── PLN conversion: col R note + background override ─────────
        # Runs only when NBP rate was fetched successfully.
        if _nbp_result is not None:
            _r_bg = _rgb("#FFCCCC") if _nbp_result.is_approximate else _rgb("#E3FCF5")
            _nbp_requests = [
                # Cell note: conversion details for auditing
                {
                    "updateCells": {
                        "range": {
                            "sheetId": worksheet.id,
                            "startRowIndex": actual - 1,
                            "endRowIndex": actual,
                            "startColumnIndex": 17,  # col R
                            "endColumnIndex": 18,
                        },
                        "rows": [{"values": [{"note": _nbp_note_text}]}],
                        "fields": "note",
                    }
                },
                # Background: pale-red (#FFCCCC) if approximate, else standard (#E3FCF5)
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": worksheet.id,
                            "startRowIndex": actual - 1,
                            "endRowIndex": actual,
                            "startColumnIndex": 17,
                            "endColumnIndex": 18,
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": _r_bg}},
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                },
            ]
            try:
                sheets_service.spreadsheets().batchUpdate(
                    spreadsheetId=LOADS_INFO_SHEET_ID,
                    body={"requests": _nbp_requests},
                ).execute()
            except Exception as _nbp_fmt_err:
                log(f"⚠️ NBP note/color warning: {_nbp_fmt_err}")

        # ── Row formatting (alignment, font, dates) ───────────────
        try:
            _apply_row_formatting(
                sheets_service,
                LOADS_INFO_SHEET_ID,
                worksheet.id,
                actual,
                adr_value,
                carrier_code=carrier_code,
                is_brokeralpha=_is_brokeralpha,
                log_fn=log,
                license_plates=license_plates,
            )
        except Exception as fmt_err:
            log(f"⚠️ Formatting warning: {repr(fmt_err)}")
            log(traceback.format_exc())

        # Y/AG: bot writes "Scans"/"Originals" value only — manual chip dropdowns
        # in the sheet handle coloring automatically. No setDataValidation needed.

        log(f"✅ Loads Info: row {actual} written (Nr {next_nr}, order {col_c})")
        return True, next_nr, col_c

    except Exception as e:
        err_msg = str(e)
        if hasattr(e, "resp") and hasattr(e, "content"):
            err_msg = (
                f"HTTP {e.resp.status}: {e.content.decode('utf-8', errors='replace')}"
            )
        log(f"❌ Loads Info error: {err_msg}")
        return False, 0, our_order_number


import os


def refresh_approximate_rates(sheet_name: str, log_fn=None) -> int:
    """
    Scan *sheet_name* in Loads Info for rows where col R has pale-red background
    (#FFCCCC) — indicating a PLN→EUR conversion that used an approximate NBP rate.

    For each such row:
      - Re-fetch the NBP rate for (unloading_date - 1 business day) from col J.
      - If rate is no longer approximate → update col R value + note + restore
        standard background #E3FCF5.

    Returns count of rows updated.
    Called from app.py once per day (auto-mode daily trigger).
    """
    from nbp_rates import get_rate, build_note as _build_nbp_note, NbpRateError

    log = log_fn or print
    updated = 0

    try:
        gc, sheets_service, _, _ = _get_clients()
        spreadsheet = gc.open_by_key(LOADS_INFO_SHEET_ID)
        try:
            ws = spreadsheet.worksheet(sheet_name)
        except Exception:
            log(f"⚠️ refresh_approximate_rates: sheet '{sheet_name}' not found")
            return 0

        # Fetch all cell data including formatting for col R (index 17)
        result = (
            sheets_service.spreadsheets()
            .get(
                spreadsheetId=LOADS_INFO_SHEET_ID,
                ranges=[f"{sheet_name}!A:AJ"],
                includeGridData=True,
                fields="sheets.data.rowData.values(userEnteredValue,userEnteredFormat.backgroundColor,note)",
            )
            .execute()
        )

        _pale_red = _rgb("#FFCCCC")  # sentinel background meaning "approximate rate"

        rows_data = (
            result.get("sheets", [{}])[0].get("data", [{}])[0].get("rowData", [])
        )

        for row_idx, row in enumerate(rows_data):
            cells = row.get("values", [])
            if len(cells) < 18:
                continue  # row too short to have col R (index 17)

            r_cell = cells[17]
            bg = r_cell.get("userEnteredFormat", {}).get("backgroundColor", {})

            # Compare only rounded values to avoid float noise
            def _close(a, b):
                return abs((a or 0) - (b or 0)) < 0.01

            if not (
                _close(bg.get("red"), _pale_red["red"])
                and _close(bg.get("green"), _pale_red["green"])
                and _close(bg.get("blue"), _pale_red["blue"])
            ):
                continue  # not pale-red → skip

            # Extract unloading date from col J (index 9)
            j_cell = cells[9] if len(cells) > 9 else {}
            unload_str = str(
                j_cell.get("userEnteredValue", {}).get("stringValue", "")
            ).strip()
            if not unload_str:
                # Also try numberValue (Sheets stores dates as serial numbers sometimes)
                continue

            # Extract original PLN amount from the existing note
            note = r_cell.get("note", "")
            try:
                _pln_line = (
                    note.splitlines()[0].strip().replace(" ", "").replace(",", ".")
                )
                pln_amount = float(_pln_line.split("zl")[0].replace("zl", "").strip())
            except (ValueError, IndexError):
                log(f"⚠️ refresh row {row_idx + 1}: cannot parse PLN from note {note!r}")
                continue

            try:
                _unload_dt = datetime.strptime(unload_str, "%d.%m.%Y").date()
                nbp = get_rate(_unload_dt)
            except (ValueError, NbpRateError) as e:
                log(f"⚠️ refresh row {row_idx + 1}: rate fetch failed: {e}")
                continue

            if nbp.is_approximate:
                continue  # still approximate — nothing to update yet

            # Rate is now confirmed: update col R
            actual_row = row_idx + 1  # 1-based
            eur_value = round(pln_amount / nbp.rate, 2)
            note_text = _build_nbp_note(pln_amount, nbp)

            # Write new EUR value to R
            sheets_service.spreadsheets().values().update(
                spreadsheetId=LOADS_INFO_SHEET_ID,
                range=f"{sheet_name}!R{actual_row}",
                valueInputOption="RAW",
                body={"values": [[eur_value]]},
            ).execute()

            # Update note and restore standard background
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=LOADS_INFO_SHEET_ID,
                body={
                    "requests": [
                        {
                            "updateCells": {
                                "range": {
                                    "sheetId": ws.id,
                                    "startRowIndex": row_idx,
                                    "endRowIndex": row_idx + 1,
                                    "startColumnIndex": 17,
                                    "endColumnIndex": 18,
                                },
                                "rows": [{"values": [{"note": note_text}]}],
                                "fields": "note",
                            }
                        },
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": ws.id,
                                    "startRowIndex": row_idx,
                                    "endRowIndex": row_idx + 1,
                                    "startColumnIndex": 17,
                                    "endColumnIndex": 18,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "backgroundColor": _rgb("#E3FCF5")
                                    }
                                },
                                "fields": "userEnteredFormat.backgroundColor",
                            }
                        },
                    ]
                },
            ).execute()

            log(
                f"✅ refresh row {actual_row}: R updated {pln_amount} PLN → {eur_value} EUR "
                f"(table {nbp.table_nr})"
            )
            updated += 1

    except Exception as e:
        log(f"❌ refresh_approximate_rates error: {e}")

    return updated


def upload_to_drive(
    pdf_path: str,
    seq_nr: int = 0,
    company_name: str = "",
    client_order_number: str = "",
    log_fn=None,
    # legacy params kept for compatibility
    our_order_number: str = "",
    row_nr: int = 0,
) -> str:
    """
    Uploads a PDF to Drive. Returns webViewLink or ''.

    FIX #3 — Folder naming:
      Standard:      "{seq_nr} {company_name}"         e.g. "102 Delta-Spezialtransport GmbH"
      BrokerAlpha:  "{seq_nr} BrokerAlpha {client_order_number}"  e.g. "104 BrokerAlpha TC4000001"
    Always created inside the current month folder.
    """
    log = log_fn or print
    try:
        if not os.path.exists(pdf_path):
            log(f"⚠️ PDF not found, skipping Drive upload: {Path(pdf_path).name}")
            return ""

        _, _, drive_service, _ = _get_clients()
        now = datetime.now()

        # Determine folder name
        nr = seq_nr or row_nr or 0
        is_tc = "brokeralpha" in (company_name or "").lower() or str(
            client_order_number or ""
        ).upper().startswith("TC")
        if is_tc and client_order_number:
            folder_name = f"{nr} BrokerAlpha {client_order_number}"
        elif company_name:
            folder_name = f"{nr} {company_name}"
        else:
            # Fallback: use our_order_number (legacy)
            folder_name = f"{nr} {our_order_number}".strip()

        # Drive folder uses "MM MonthName" format, e.g. "04 Kwiecień"
        month_folder_name = f"{now.month:02d} {MONTH_NAMES[now.month]}"
        month_f = _get_or_create_folder(
            drive_service, DRIVE_ROOT_FOLDER_ID, month_folder_name
        )
        order_f = _get_or_create_folder(drive_service, month_f, folder_name)

        media = MediaFileUpload(str(pdf_path), mimetype="application/pdf")
        uploaded = (
            drive_service.files()
            .create(
                body={"name": Path(pdf_path).name, "parents": [order_f]},
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        link = uploaded.get("webViewLink", "")
        log(f"✅ Drive: {Path(pdf_path).name} → {folder_name}")
        return link

    except Exception as e:
        log(f"❌ Drive error: {repr(e)}")
        return ""


def create_gmail_draft(order_data: dict, log_fn=None) -> bool:
    """Creates a Gmail draft via IMAP APPEND (App Password)."""
    import imaplib
    import email.utils
    from config import EMAIL, EMAIL_PASSWORD

    log = log_fn or print
    try:
        email_payload = build_email(
            [
                {
                    "client_order_nr": order_data.get("client_order_number") or "",
                    "our_order_nr": order_data.get("our_order_number") or "",
                    "invoice_nr": order_data.get("invoice_number") or "",
                    "company_name": order_data.get("company_name") or "",
                    "invoice_email": order_data.get("invoice_email") or "",
                    "contact_email": order_data.get("contact_email") or "",
                    "payment_days": order_data.get("payment_days") or 0,
                    "payment_type": order_data.get("payment_type") or "",
                    "price_netto": order_data.get("price_netto") or 0,
                }
            ]
        )

        msg = MIMEMultipart()
        msg["To"] = email_payload.get("to", "")
        msg["Subject"] = email_payload.get("subject", "")
        msg["From"] = EMAIL
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg.attach(MIMEText(email_payload.get("body", ""), "plain", "utf-8"))

        raw_bytes = msg.as_bytes()
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, EMAIL_PASSWORD)
        import time

        mail.append(
            "[Gmail]/Drafts", "", imaplib.Time2Internaldate(time.time()), raw_bytes
        )
        mail.logout()

        log(f"✅ Gmail draft: {email_payload.get('subject', '')[:60]}")
        return True

    except Exception as e:
        log(f"❌ Gmail error: {repr(e)}")
        return False


def verify_invoice(parsed_invoice: dict) -> list[str]:
    """Verifies a carrier invoice against Loads Info. Returns list of issues (empty = OK)."""
    issues = []
    try:
        gc, _, _, _ = _get_clients()
        ws = _get_worksheet(gc.open_by_key(LOADS_INFO_SHEET_ID))
        rows = ws.get_all_values()

        target = str(parsed_invoice.get("order_number", "")).strip()
        if not target:
            return ["Order number not found in invoice"]

        for row in rows:
            if len(row) < 15:
                continue
            col_b = str(row[1]).strip()
            col_c = str(row[2]).strip()
            if target not in (col_b, col_c):
                continue

            try:
                sheet_amount = round(float(str(row[14]).replace(",", ".").strip()), 2)
                parsed_amount = round(
                    float(str(parsed_invoice.get("amount_netto", 0)).replace(",", ".")),
                    2,
                )
                if sheet_amount != parsed_amount:
                    issues.append(
                        f"Amount mismatch: sheet {sheet_amount} ≠ invoice {parsed_amount}"
                    )
            except Exception:
                issues.append("Could not compare amounts")

            sheet_date = str(row[9]).strip() if len(row) > 9 else ""
            invoice_date = str(parsed_invoice.get("service_date", "")).strip()
            if sheet_date and invoice_date and sheet_date != invoice_date:
                issues.append(
                    f"Unloading date mismatch: sheet {sheet_date} ≠ invoice {invoice_date}"
                )

            return issues

        issues.append(f"Order not found: {target}")
        return issues

    except Exception as e:
        return [f"Verification error: {e}"]


def update_loads_info_invoice(parsed_invoice: dict, log_fn=None) -> bool:
    """
    Finds the matching row in Loads Info by order_number (col C for YYYY-MM-NNNNN,
    col B for client order refs). Sheet is derived from the order_number month prefix.
    Writes:
      AD — invoice number from carrier
      AE — email received_at date (or today as fallback) in dd.MM.yyyy format
      AI — payment_deadline if present (overrides formula)
    Returns False if order_number is missing or row not found in any sheet.
    """
    log = log_fn or print
    try:
        gc, sheets_service, _, _ = _get_clients()
        spreadsheet = gc.open_by_key(LOADS_INFO_SHEET_ID)

        today = datetime.now().strftime("%d.%m.%Y")
        invoice_nr = str(parsed_invoice.get("invoice_number", "")).strip()
        order_number = str(parsed_invoice.get("order_number", "") or "").strip()
        payment_deadline = str(parsed_invoice.get("payment_deadline", "") or "").strip()

        if not order_number:
            log("⚠️ update_loads_info_invoice: order_number missing — cannot locate row")
            return False

        # AE date: prefer received_at from email over today (shows when invoice arrived)
        _received_raw = str(parsed_invoice.get("received_at") or "").strip().lstrip("'")
        ae_date = (
            _received_raw[:10]
            if len(_received_raw) >= 10
            else datetime.now().strftime("%d.%m.%Y")
        )

        # Sequence-only fallback: "2026-03-00128" → "128"
        # Needed for rows written by bot (col C = plain seq) vs full YYYY-MM-NNNNN format
        _seq_m = re.match(r"\d{4}-\d{2}-0*(\d+)$", order_number)
        _seq_only = _seq_m.group(1) if _seq_m else ""

        # ── Select worksheet: derive month from YYYY-MM-NNNNN order_number ──
        # "2026-03-00074" → month=3 → "Marzec"; fallback to current month.
        target_i = None
        worksheet = None
        sheet_name = None
        rows = []

        _ws_for_order = None  # resolved below; never use today() eagerly
        _is_own_order = order_number.startswith(
            "2026-"
        )  # YYYY-MM-NNNNN → col C; else col B

        def _select_sheet_by_month(mo: str, reason: str):
            """Try exact MONTH_NAMES match, then fuzzy tab search by month number or name."""
            import unicodedata

            def _norm(s: str) -> str:
                """Strip diacritics for accent-insensitive comparison (Kwiecień == Kwiecien)."""
                return "".join(
                    c
                    for c in unicodedata.normalize("NFD", s.lower())
                    if unicodedata.category(c) != "Mn"
                )

            nonlocal _ws_for_order
            title = MONTH_NAMES.get(int(mo), "")
            word_part = _norm(title.split()[-1]) if title else ""
            if title:
                try:
                    _ws_for_order = spreadsheet.worksheet(title)
                    log(f"🗂 Sheet selected: '{title}' ({reason})")
                    return
                except Exception:
                    pass
            for _cand in spreadsheet.worksheets():
                cand_l = _cand.title.lower()
                cand_n = _norm(_cand.title)
                if (
                    mo in _cand.title  # "04" in "04 Kwiecień"
                    or mo.lstrip("0") in cand_l  # "4" in "4. kwiecień"
                    or (word_part and word_part in cand_n)
                ):  # "kwiecien" in _norm("Kwiecień")
                    _ws_for_order = _cand
                    log(f"🗂 Sheet selected: '{_cand.title}' (fuzzy — {reason})")
                    return
            log(f"⚠️ No sheet found for month '{mo}' ({reason})")

        if order_number:
            log(f"🔎 order_number for sheet detection: {order_number!r}")
            _m = re.search(r"20\d{2}-(\d{2})-\d{5}", order_number)
            if _m:
                # YYYY-MM-NNNNN → month from order number — most reliable path
                _select_sheet_by_month(_m.group(1), "order_number month")
            else:
                # Client order number (ABCD10001, TC49xxx) — derive month from received_at
                _ra = str(parsed_invoice.get("received_at") or "").strip()
                _ra_m = re.match(r"\d{2}\.(\d{2})\.\d{4}", _ra)
                if _ra_m:
                    _select_sheet_by_month(_ra_m.group(1), "received_at month")

            # Last resort: fall back to current month only if both derivations failed
            if _ws_for_order is None:
                _ws_for_order = _get_worksheet(spreadsheet)
                log(
                    f"🗂 Sheet selected: '{_ws_for_order.title}' (current month fallback)"
                )

            ws_rows = _ws_for_order.get_all_values()
            _sample_b = [
                str(r[1]).strip() for r in ws_rows if len(r) > 1 and str(r[1]).strip()
            ][:3]
            _sample_c = [
                str(r[2]).strip() for r in ws_rows if len(r) > 2 and str(r[2]).strip()
            ][:3]
            log(
                f"🔍 Searching '{order_number}' in '{_ws_for_order.title}' | col B: {_sample_b} | col C: {_sample_c}"
            )

            def _row_matches(row):
                """Route by order type: our YYYY-MM-NNNNN → col C; client ref → col B."""
                col_c = str(row[2]).strip()
                col_b = str(row[1]).strip()
                if _is_own_order:
                    # Our order number: match full format or stripped-zero seq in col C
                    col_c_no_lead = re.sub(r"^0+", "", col_c)
                    return order_number == col_c or bool(
                        _seq_only and _seq_only == col_c_no_lead
                    )
                else:
                    # Client order number: match col B only
                    return order_number == col_b

            for i, row in enumerate(ws_rows):
                if len(row) < 3:
                    continue
                if _row_matches(row):
                    target_i = i
                    worksheet = _ws_for_order
                    sheet_name = _ws_for_order.title
                    rows = ws_rows
                    log(
                        f"🔗 Matched '{order_number}' → sheet '{sheet_name}' row {i + 1}"
                    )
                    break

            if worksheet is None:
                # Derived sheet miss — try ALL sheets before falling back to carrier search.
                log(f"⚠️ Not found in '{_ws_for_order.title}' — scanning all sheets")
                for _ws in spreadsheet.worksheets():
                    if _ws.id == _ws_for_order.id:
                        continue  # already checked
                    _all_rows = _ws.get_all_values()
                    for i, row in enumerate(_all_rows):
                        if len(row) < 3:
                            continue
                        if _row_matches(row):
                            target_i = i
                            worksheet = _ws
                            sheet_name = _ws.title
                            rows = _all_rows
                            log(
                                f"🔗 Matched '{order_number}' → sheet '{sheet_name}' row {i + 1} (all-sheets fallback)"
                            )
                            break
                    if worksheet is not None:
                        break

            if worksheet is None:
                log(f"⚠️ Order '{order_number}' not found in any sheet")
                return False

        actual_row = target_i + 1  # 1-based
        data = [
            {"range": f"{sheet_name}!AD{actual_row}", "values": [[invoice_nr]]},
            {"range": f"{sheet_name}!AE{actual_row}", "values": [[ae_date]]},
        ]
        # Write payment_deadline to col AI (index 34) — overrides =AH+AE formula with actual date
        if payment_deadline:
            data.append(
                {
                    "range": f"{sheet_name}!AI{actual_row}",
                    "values": [[payment_deadline]],
                }
            )

        # PLN→EUR: convert carrier invoice amount and write to col O (Carrier NETTO)
        _inv_currency = (parsed_invoice.get("currency") or "EUR").upper()
        try:
            _inv_amount = float(
                str(parsed_invoice.get("amount_netto") or 0)
                .replace(",", ".")
                .replace(" ", "")
            )
        except (ValueError, TypeError):
            _inv_amount = 0.0
        if _inv_currency == "PLN" and _inv_amount > 0:
            try:
                from nbp_rates import get_rate

                # Unloading date from matched Loads Info row (col J = index 9); fallback to today
                _row_data = rows[target_i] if rows and target_i is not None else []
                _unload_str = _row_data[9].strip() if len(_row_data) > 9 else ""
                if _unload_str:
                    _unload_dt = datetime.strptime(_unload_str, "%d.%m.%Y").date()
                else:
                    from datetime import date as _date_cls

                    _unload_dt = _date_cls.today()
                _nbp = get_rate(_unload_dt)
                _eur_amount = round(_inv_amount / _nbp.rate, 2)
                data.append(
                    {"range": f"{sheet_name}!O{actual_row}", "values": [[_eur_amount]]}
                )
                log(
                    f"   PLN→EUR (col O): {_inv_amount} PLN / {_nbp.rate} = {_eur_amount} EUR (table {_nbp.table_nr})"
                )
            except Exception as _pln_err:
                log(f"⚠️ PLN→EUR for col O failed: {_pln_err}")

        # AD and AI use RAW; AE uses USER_ENTERED so Sheets parses date without adding apostrophe
        ad_ai_data = [d for d in data if not d["range"].endswith(f"AE{actual_row}")]
        ae_data = [d for d in data if d["range"].endswith(f"AE{actual_row}")]
        if ad_ai_data:
            sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=LOADS_INFO_SHEET_ID,
                body={"valueInputOption": "RAW", "data": ad_ai_data},
            ).execute()
        if ae_data:
            sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=LOADS_INFO_SHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": ae_data},
            ).execute()

        # Format AD (col 29): no fill, CENTER, font 11
        # Format AE (col 30): #D9EAD3 fill, CENTER, font 10
        _sid = worksheet.id
        _row_idx = actual_row - 1  # 0-based

        def _fmt_cell(
            col_idx: int,
            bg_hex: str | None,
            font_size: int = 10,
            font_family: str | None = None,
        ) -> dict:
            """Build updateCells request. bg_hex=None → background not touched."""
            text_fmt: dict = {"fontSize": font_size}
            if font_family:
                text_fmt["fontFamily"] = font_family
            fmt = {"horizontalAlignment": "CENTER", "textFormat": text_fmt}
            fields = "userEnteredFormat.horizontalAlignment,userEnteredFormat.textFormat.fontSize"
            if font_family:
                fields += ",userEnteredFormat.textFormat.fontFamily"
            if bg_hex:
                fmt["backgroundColor"] = _rgb(bg_hex)
                fields += ",userEnteredFormat.backgroundColor"
            return {
                "updateCells": {
                    "rows": [{"values": [{"userEnteredFormat": fmt}]}],
                    "range": {
                        "sheetId": _sid,
                        "startRowIndex": _row_idx,
                        "endRowIndex": _row_idx + 1,
                        "startColumnIndex": col_idx,
                        "endColumnIndex": col_idx + 1,
                    },
                    "fields": fields,
                }
            }

        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=LOADS_INFO_SHEET_ID,
            body={
                "requests": [
                    _fmt_cell(29, None, font_size=11, font_family="Calibri"),
                    _fmt_cell(30, "#D9EAD3", font_size=11, font_family="Calibri"),
                ]
            },
        ).execute()

        written = f"AD={invoice_nr}, AE={ae_date}"
        if payment_deadline:
            written += f", AI={payment_deadline}"
        log(f"✅ Loads Info row {actual_row}: {written}")

        # If AG (index 32) = "Originals" → mark Payment Plan col K for this invoice
        matched_row = rows[target_i]
        ag_value = str(matched_row[32]).strip().lower() if len(matched_row) > 32 else ""
        if ag_value == "originals" and invoice_nr:
            try:
                pp_spreadsheet = gc.open_by_key(PAYMENT_PLAN_SHEET_ID)
                try:
                    pp_ws = pp_spreadsheet.worksheet("Payment plan")
                except Exception:
                    pp_ws = pp_spreadsheet.get_worksheet(0)
                pp_name = pp_ws.title
                pp_col_b = pp_ws.col_values(2)  # column B = invoice numbers
                if invoice_nr in pp_col_b:
                    pp_row = pp_col_b.index(invoice_nr) + 1  # 1-based
                    sheets_service.spreadsheets().values().update(
                        spreadsheetId=PAYMENT_PLAN_SHEET_ID,
                        range=f"{pp_name}!K{pp_row}",
                        valueInputOption="RAW",
                        body={"values": [["Originals check payment date"]]},
                    ).execute()
                    log(f"✅ Payment Plan row {pp_row}: K=Originals check payment date")
            except Exception as pp_err:
                log(f"⚠️ Could not update Payment Plan K: {pp_err}")

        return True

    except Exception as e:
        log(f"❌ update_loads_info_invoice error: {e}")
        return False


def write_our_invoice_nr(row: int, invoice_nr: str, log_fn=None) -> bool:
    """
    Writes our invoice number to column V (index 21) of a Loads Info row.
    Also applies light green background #B6D7A8 ("Light green 2") to that cell.

    Args:
        row:        1-based row number in the current month's sheet.
        invoice_nr: Invoice number string, e.g. "136/03/2026".
    """
    log = log_fn or print
    try:
        gc, sheets_service, _, _ = _get_clients()
        spreadsheet = gc.open_by_key(LOADS_INFO_SHEET_ID)
        worksheet = _get_worksheet(spreadsheet)
        sheet_name = worksheet.title
        sheet_id = worksheet.id

        # Write the value to col V
        sheets_service.spreadsheets().values().update(
            spreadsheetId=LOADS_INFO_SHEET_ID,
            range=f"{sheet_name}!V{row}",
            valueInputOption="RAW",
            body={"values": [[invoice_nr]]},
        ).execute()

        # Apply #B6D7A8 background + font 10 Calibri to V
        _v_color = "#B6D7A8"
        print(f"[SHEETS] col V background color: {_v_color}")
        r = row - 1  # 0-based row index
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=LOADS_INFO_SHEET_ID,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": r,
                                "endRowIndex": r + 1,
                                "startColumnIndex": 21,
                                "endColumnIndex": 22,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": _rgb(_v_color),
                                    "textFormat": {
                                        "fontSize": 11,
                                        "fontFamily": "Calibri",
                                    },
                                }
                            },
                            "fields": (
                                "userEnteredFormat.backgroundColor,"
                                "userEnteredFormat.textFormat.fontSize,"
                                "userEnteredFormat.textFormat.fontFamily"
                            ),
                        }
                    }
                ]
            },
        ).execute()

        log(f"✅ Loads Info row {row}: V={invoice_nr} (green)")
        return True

    except Exception as e:
        log(f"❌ write_our_invoice_nr error: {e}")
        return False


def write_to_payment_plan(parsed_invoice, log_fn=None) -> bool:
    """Adds a carrier invoice (or list of invoices) to Payment Plan."""
    if isinstance(parsed_invoice, list):
        return all(
            write_to_payment_plan(inv, log_fn=log_fn) for inv in parsed_invoice if inv
        )
    log = log_fn or print
    try:
        gc, sheets_service, _, _ = _get_clients()
        spreadsheet = gc.open_by_key(PAYMENT_PLAN_SHEET_ID)
        try:
            worksheet = spreadsheet.worksheet("Payment plan")
        except Exception:
            worksheet = spreadsheet.get_worksheet(0)
        sheet_name = worksheet.title

        payment_type = _normalize_payment_type(parsed_invoice.get("payment_type"))
        comment = "Originals check payment date" if payment_type == "Originals" else ""

        def _clean_amount(v):
            if not v:
                return ""
            s = (
                str(v)
                .replace("zł", "")
                .replace("PLN", "")
                .replace("€", "")
                .replace("EUR", "")
                .replace(",", ".")
                .replace(" ", "")
                .strip()
            )
            try:
                return round(float(s), 2)
            except Exception:
                return v

        currency = (parsed_invoice.get("currency") or "EUR").upper()
        if currency != "EUR":
            log(f"⚠️ Invoice currency is {currency} — writing amounts as-is")

        invoice_nr = parsed_invoice.get("invoice_number") or ""
        # Dedup: check if invoice already exists in column B (Faktura/Nota, 1-indexed = 2)
        existing_b = worksheet.col_values(2)  # column B (1-indexed)
        if invoice_nr and invoice_nr in existing_b:
            log(f"⚠️ Already exists: {invoice_nr}")
            return True

        netto = _clean_amount(parsed_invoice.get("amount_netto"))
        log(
            f"   Payment Plan: netto={netto!r}, invoice={parsed_invoice.get('invoice_number')!r}, "
            f"carrier={parsed_invoice.get('carrier_name')!r}"
        )

        # Resolve carrier name from CARRIERS config (AI output may have legal suffixes stripped)
        from config import CARRIERS

        raw_carrier = (parsed_invoice.get("carrier_name") or "").strip()
        carrier_name_resolved = raw_carrier
        raw_low = raw_carrier.lower()
        for info in CARRIERS.values():
            cfg = info.get("name", "").lower()
            if cfg and (cfg in raw_low or raw_low in cfg):
                carrier_name_resolved = info["name"]
                break

        payment_deadline = parsed_invoice.get("payment_deadline") or ""

        row = [
            carrier_name_resolved,  # A — Company
            invoice_nr,  # B — Faktura/Nota
            netto,  # C — Amount NETTO (EUR)
            "",  # D — formula =C{row}*0,23 written after append
            "",  # E — formula =C{row}+D{row} written after append
            payment_deadline,  # F — Payment term date (DD.MM.YYYY)
            "NO",  # G — Paid
            "",  # H — Payment date
            "",  # I — On time?
            "",  # J — =F-H formula added after append
            comment,  # K — Comments
        ]

        append_result = (
            sheets_service.spreadsheets()
            .values()
            .append(
                spreadsheetId=PAYMENT_PLAN_SHEET_ID,
                range=f"{sheet_name}!A3:K",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            )
            .execute()
        )

        actual = _actual_row_from_append(append_result, fallback=0)

        # Write D, E, J as formulas now that we know the row number
        if actual > 0:
            sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=PAYMENT_PLAN_SHEET_ID,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": [
                        {
                            "range": f"{sheet_name}!D{actual}",
                            "values": [[f"=C{actual}*0,23"]],
                        },
                        {
                            "range": f"{sheet_name}!E{actual}",
                            "values": [[f"=C{actual}+D{actual}"]],
                        },
                        {
                            "range": f"{sheet_name}!J{actual}",
                            "values": [[f"=F{actual}-H{actual}"]],
                        },
                    ],
                },
            ).execute()

        formatting_requests = [
            # Auto-fit columns A–K to content width
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 11,
                    }
                }
            },
        ]

        if actual > 0:
            # Strip ALL inherited formatting from the entire row A–K first.
            # INSERT_ROWS copies format from the row above — this resets it.
            formatting_requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": worksheet.id,
                            "startRowIndex": actual - 1,
                            "endRowIndex": actual,
                            "startColumnIndex": 0,
                            "endColumnIndex": 11,  # A(0)–K(10)
                        },
                        "cell": {"userEnteredFormat": {}},
                        "fields": (
                            "userEnteredFormat.backgroundColor,"
                            "userEnteredFormat.backgroundColorStyle"
                        ),
                    }
                }
            )

            # Col K: light red #FFCCCC only when "Originals" → text is dark/readable
            if comment:
                _light_red = {"red": 1.0, "green": 0.8, "blue": 0.8}
                formatting_requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet.id,
                                "startRowIndex": actual - 1,
                                "endRowIndex": actual,
                                "startColumnIndex": 10,
                                "endColumnIndex": 11,  # K(10)
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": _light_red,
                                    "backgroundColorStyle": {"rgbColor": _light_red},
                                    "horizontalAlignment": "CENTER",
                                    "textFormat": {"fontSize": 11},
                                }
                            },
                            "fields": (
                                "userEnteredFormat.backgroundColor,"
                                "userEnteredFormat.backgroundColorStyle,"
                                "userEnteredFormat.horizontalAlignment,"
                                "userEnteredFormat.textFormat.fontSize"
                            ),
                        }
                    }
                )

        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=PAYMENT_PLAN_SHEET_ID,
            body={"requests": formatting_requests},
        ).execute()

        log(f"✅ Payment Plan: {parsed_invoice.get('invoice_number') or ''}")
        return True

    except Exception as e:
        err = str(e)
        if "403" in err:
            log(
                "❌ Sheets 403 — no access to Payment Plan.\n"
                "Add service account as Editor in the Payment Plan spreadsheet."
            )
        else:
            log(f"❌ Payment Plan error: {e}")
        return False
