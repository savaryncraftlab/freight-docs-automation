import re
import time
import base64
import threading
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pdfplumber

from config import (
    DRIVE_ROOT_FOLDER_ID,
    INVOICE_WATCH_FOLDER,
    LOADS_INFO_SHEET_ID,
    MONTH_NAMES,
)
from actions import _get_clients, write_our_invoice_nr

# Company name keywords that route invoice email to a central invoices address
BROKERALPHA_CLIENTS = ["brokeralpha", "alpha wheels"]

# Minimal email validation — rejects empty strings and values without @domain.tld
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Ensure required output folders exist at module load
Path("bot_data/invoices_out/processed").mkdir(parents=True, exist_ok=True)
Path("bot_data/invoices_out/errors").mkdir(parents=True, exist_ok=True)

# ── Order number normalization (used by both extract and lookup) ─────────
ORDER_PREFIXES = [
    r"numer\s+zlecenia\s+transportowego\s+",
    r"zamówienie\s+zakupu\s+",
    r"zlecenie\s+dla\s+przewo[zź]nika\s+",
    r"zlecenie\s+przewozowe\s+",
    r"zlecenie\s+transportowe\s+",
    r"beförderungsvertrag:\s*",
    r"umowa\s+przewozu\s+",
    r"wykonanie\s+us[lł]ugi\s+transportowej:\s*",
    r"loading\s+order\s*\|\s*ladeauftrag\s+",
    r"transportauftrag\s+vom:\s*\S+\s*",
    r"transportauftrag:\s*(ID\s*)?",
    r"transportauftrag\s+",
    r"tour[- ]?number:\s*",
    r"für\s+tour\s+",
    r"for\s+tour\s+",
    r"order\s+number[:\s]+",
    r"order\s+no[.:]?\s*",
    r"order:\s*",
    r"nr:\s*",
    r"nr\s+",
    r"invoice\s*/\s*rechnung\s+",
    r"invoice/rechnung\s+",
]
ORDER_SUFFIXES = [
    r"\s+z\s+dnia\s+\d{2}[\.\-]\d{2}[\.\-]\d{4}$",
    r"\s+z\s+dnia\s+\d{4}[\.\-]\d{2}[\.\-]\d{2}$",
]


def strip_order_prefix(text: str) -> str:
    """Remove known label prefixes and date suffixes from order-number string."""
    result = text.strip()
    for pat in ORDER_PREFIXES:
        cleaned = re.sub(pat, "", result, flags=re.IGNORECASE).strip()
        if cleaned != result:
            result = cleaned
            break
    for pat in ORDER_SUFFIXES:
        result = re.sub(pat, "", result, flags=re.IGNORECASE).strip()
    return result


def find_loads_info_row(order_number: str) -> dict | None:
    """
    Search Loads Info sheet (worksheet "Marzec") for an exact match in column B.

    We use column B (index 1) which stores the client's order number (e.g. "TC400003").
    Returns a dict with selected columns or None if not found.

    Args:
        order_number: Exact string to match in column B (e.g. "TC400003").

    Returns:
        dict with row_index and column values, or None.
    """
    if not order_number or not order_number.strip():
        return None

    # Normalize: strip known prefixes/suffixes (e.g. "ZLECENIE DLA PRZEWOŹNIKA 1001/S/26" → "1001/S/26")
    order_number = strip_order_prefix(order_number)

    try:
        gspread_client, _, _, _ = _get_clients()
        spreadsheet = gspread_client.open_by_key(LOADS_INFO_SHEET_ID)
        # Use dynamic month name from config so this works year-round
        sheet_name = MONTH_NAMES.get(datetime.now().month, "")
        try:
            worksheet = (
                spreadsheet.worksheet(sheet_name)
                if sheet_name
                else spreadsheet.get_worksheet(0)
            )
        except Exception:
            worksheet = spreadsheet.get_worksheet(0)
        # Fetch all rows as lists — empty cells come back as ""
        all_rows = worksheet.get_all_values()
    except Exception as e:
        print(f"[LOADS LOOKUP] Sheets error: {e}")
        return None

    row_index = None
    for i, row in enumerate(all_rows):
        # Column B = index 1; pad short rows so index access never raises
        col_b = row[1].strip() if len(row) > 1 else ""
        if col_b == order_number:
            row_index = i + 1  # 1-based row number (row 1 = header)
            break

    print(f"[LOADS LOOKUP] order='{order_number}' -> row {row_index or 'NOT FOUND'}")

    if row_index is None:
        return None

    # Re-use the matched row (i is still in scope from the loop above)
    def _safe_float(val: str) -> float:
        """Parse European-formatted number: '1 300,00' or '1300.00' → float."""
        try:
            return float(val.strip().replace(" ", "").replace(",", "."))
        except (ValueError, AttributeError):
            return 0.0

    def _safe_int(val: str) -> int:
        """Extract only digit characters and parse as int."""
        digits = re.sub(r"\D", "", val.strip() if val else "")
        return int(digits) if digits else 0

    def _safe_str(val: str) -> str:
        return val.strip() if val else ""

    # Column indexes (0-based): A=0, B=1, D=3, E=4, R=17, V=21, Z=25, AG=32
    def _col(row: list, idx: int) -> str:
        return row[idx].strip() if len(row) > idx else ""

    return {
        "row_index": row_index,
        "col_a": _safe_str(_col(row, 0)),
        "col_b": _safe_str(_col(row, 1)),
        "col_d": _safe_str(_col(row, 3)),
        "col_e": _safe_str(_col(row, 4)),
        "col_r": _safe_float(_col(row, 17)),
        "col_v": _safe_str(_col(row, 21)),
        "col_z": _safe_int(_col(row, 25)),
        "col_ag": _col(row, 32).lower().strip(),
    }


def build_invoice_email(invoice_data: dict, row_data: dict) -> dict:
    """
    Build subject and body for an invoice email based on language and document type.

    Language is detected from client NIP: PL prefix → Polish, otherwise English.
    Doc type controls whether body mentions scans or originals.

    Args:
        invoice_data: Output of extract_invoice_data() — needs invoice_number, client_nip.
        row_data:     Output of find_loads_info_row() — needs col_b, col_z, col_ag.

    Returns:
        dict with keys: subject, body, lang, doc_type.
    """
    client_nip = invoice_data.get("client_nip") or ""
    lang = "pl" if client_nip.upper().startswith("PL") else "en"

    doc_type = (row_data.get("col_ag") or "scans").lower()

    invoice = invoice_data.get("invoice_number") or ""
    order = row_data.get("col_b") or ""
    days = str(row_data.get("col_z") or "30")
    short_order = order  # always use full order number in subject

    # ── Subject ───────────────────────────────────────────────────────────────
    if lang == "pl":
        subject = (
            f"Faktura i dokumenty za zamówienie transportowe nr {short_order}"
            f" // ACME Sp. z o.o."
        )
    else:
        subject = (
            f"Invoice and documents for transport order no. {short_order}"
            f" // ACME Sp. z o.o."
        )

    # ── Body — 4 HTML variants ────────────────────────────────────────────────
    _hl = '<span style="background-color:#FACC15;font-weight:bold;">'
    _hl_end = "</span>"

    if lang == "pl" and doc_type == "scans":
        body = (
            f"Szanowni Pa\u0144stwo,<br><br>"
            f"w za\u0142\u0105czeniu przesy\u0142am faktur\u0119 elektroniczn\u0105 nr <b>{invoice}</b> "
            f"oraz skany dokument\u00f3w roz\u0142adunkowych dotycz\u0105cych zam\u00f3wienia nr <b>{order}</b>.<br><br>"
            f"Zgodnie z umow\u0105, termin p\u0142atno\u015bci wynosi "
            f"{_hl}{days} dni{_hl_end} "
            f"od daty dostarczenia dokument\u00f3w, a p\u0142atno\u015b\u0107 zostanie dokonana na podstawie "
            f"skan\u00f3w dokumentu odbioru (POD) w formacie PDF.<br><br>"
            f"Bardzo prosz\u0119 o potwierdzenie, czy wszystko jest w porz\u0105dku oraz na jak\u0105 "
            f"dat\u0119 maj\u0105 Pa\u0144stwo zaplanowany p\u0142atno\u015b\u0107?<br><br>"
            f"Z powa\u017caniem,<br>ACME Sp. z o.o."
        )

    elif lang == "pl" and doc_type == "originals":
        body = (
            f"Szanowni Pa\u0144stwo,<br><br>"
            f"w za\u0142\u0105czeniu przesy\u0142am faktur\u0119 elektroniczn\u0105 nr <b>{invoice}</b> "
            f"dotycz\u0105c\u0105 zam\u00f3wienia nr <b>{order}</b>.<br><br>"
            f"Um\u00f3wiony termin p\u0142atno\u015bci wynosi "
            f"{_hl}{days} dni{_hl_end} "
            f"od otrzymania orygina\u0142\u00f3w. Orygina\u0142y dokument\u00f3w postaramy si\u0119 "
            f"wys\u0142a\u0107 do Pa\u0144stwa poczt\u0105 jak najszybciej.<br><br>"
            f"Z powa\u017caniem,<br>ACME Sp. z o.o."
        )

    elif lang == "en" and doc_type == "scans":
        body = (
            f"Dear Sir or Madam,<br><br>"
            f"please find attached the electronic invoice no. <b>{invoice}</b> and scans of the "
            f"unloading documents related to order no. <b>{order}</b>.<br><br>"
            f"In accordance with the agreement, the payment term is "
            f"{_hl}{days} days{_hl_end} "
            f"from the date of delivery of the documents, and the payment will be made based on "
            f"the scanned proof of delivery (POD) documents in PDF format.<br><br>"
            f"Could you please confirm that everything is in order and inform me of the scheduled "
            f"payment date?<br><br>"
            f"Kind regards,<br>ACME Sp. z o.o."
        )

    else:  # en + originals
        body = (
            f"Dear Sir or Madam,<br><br>"
            f"please find attached the electronic invoice no. <b>{invoice}</b> related to order "
            f"no. <b>{order}</b>.<br><br>"
            f"The agreed payment term is "
            f"{_hl}{days} days{_hl_end} "
            f"from the receipt of the original documents. We will do our best to send the "
            f"originals by post as soon as possible.<br><br>"
            f"Kind regards,<br>ACME Sp. z o.o."
        )

    return {
        "subject": subject,
        "body": body,
        "lang": lang,
        "doc_type": doc_type,
    }


def extract_invoice_data(pdf_path: str) -> dict:
    """
    Extract structured data from a ACME outgoing invoice PDF.

    Supports three language variants (PL/DE/EN) with the same layout.
    Returns None for any field that cannot be found — never raises.

    Args:
        pdf_path: Absolute or relative path to the invoice PDF.

    Returns:
        dict with keys: invoice_number, order_number, amount_netto,
        currency, client_nip, client_name, invoice_date.
    """
    result = {
        "invoice_number": None,
        "order_number": None,
        "amount_netto": None,
        "currency": None,
        "client_nip": None,
        "client_name": None,
        "invoice_date": None,
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Concatenate all pages into a single list of lines for scanning
            lines = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                lines.extend(text.splitlines())
    except Exception as e:
        print(f"[INVOICE PARSE] Failed to open PDF {pdf_path}: {e}")
        print(f"[INVOICE PARSE] {result}")
        return result

    # ── helpers ───────────────────────────────────────────────────────────

    def next_nonempty(idx: int) -> str:
        """Return the first non-blank line after position idx."""
        for j in range(idx + 1, len(lines)):
            stripped = lines[j].strip()
            if stripped:
                return stripped
        return ""

    def parse_amount(text: str):
        """
        Convert '1 300,00 EUR' or '2 100,00' to (1300.0, 'EUR'|None).
        Handles both European (1.300,00) and plain (1300.00) formats.
        """
        # Try to capture a number with optional thousands separators and a 3-letter currency
        m = re.search(r"([\d\s\.]+[,\.][\d]{2})\s*([A-Z]{3})?", text)
        if not m:
            return None, None
        raw = m.group(1)
        cur = m.group(2) or None
        # Normalise: remove spaces, then swap comma→dot (European decimal)
        raw = raw.replace(" ", "").replace(".", "").replace(",", ".")
        try:
            return float(raw), cur
        except ValueError:
            return None, None

    # ── scan lines ────────────────────────────────────────────────────────

    currency_from_context = None  # fallback when amount line has no currency code

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()

        # ── Invoice number ────────────────────────────────────────────────
        # Matches: "Nr 136/03/2026"  "Nr. 135/03/2026"  "No. 138/03/2026"
        # Require digits/slashes so stray words like "Name" are never captured.
        if result["invoice_number"] is None:
            m = re.search(r"(?:Nr\.?|No\.)\s+(\d+/\d+/\d+)", line, re.IGNORECASE)
            if m:
                result["invoice_number"] = m.group(1)

        # ── Invoice date ──────────────────────────────────────────────────
        # Labels: PL "data wystawienia" / DE "Ausstellungsdatum" / EN "date of issue"
        # Layout A: label line contains the date (same or next line)
        # Layout B: dates are on the line ABOVE the label
        #   e.g. "26-03-2026 26-03-2026\ndata wystawienia data dostawy/..."
        if result["invoice_date"] is None:
            if re.search(
                r"data\s+wystawienia|ausstellungsdatum|rechnungsdatum|date\s+of\s+issue|invoice\s+date",
                line,
                re.IGNORECASE,
            ):
                # Try same line first, then next line (Layout A)
                m = re.search(r"\d{2}[-./]\d{2}[-./]\d{4}", line)
                if not m:
                    m = re.search(r"\d{2}[-./]\d{2}[-./]\d{4}", next_nonempty(i))
                # Fallback: look at the line above (Layout B)
                if not m and i > 0:
                    m = re.search(r"\d{2}[-./]\d{2}[-./]\d{4}", lines[i - 1])
                if m:
                    result["invoice_date"] = m.group(0)

        # ── Client NIP ────────────────────────────────────────────────────
        # Labels: "NIP:" / "USt-IdNr:" / "VAT ID:"
        # DE invoices put both seller and buyer NIP on the SAME line:
        #   "USt-IdNr: PL0000000000 USt-IdNr: NL 000000000B01"
        # Use findall to collect all matches, then skip our own NIP.
        OUR_NIP = "PL0000000000"
        if result["client_nip"] is None:
            # NIP/VAT values can contain letters mid-value: "NL 000000000B01"
            # Capture everything up to the next label or end of line.
            all_nips = re.findall(
                r"(?:NIP|USt-IdNr|VAT\s*ID)[:\s]+([\w\s]+?)(?=\s+(?:NIP|USt-IdNr|VAT\s*ID)|$)",
                line,
                re.IGNORECASE,
            )
            for raw_nip in all_nips:
                candidate_nip = re.sub(r"\s+", "", raw_nip).strip()
                if candidate_nip and candidate_nip != OUR_NIP:
                    result["client_nip"] = candidate_nip
                    break

        # ── Client name ───────────────────────────────────────────────────
        # "Nabywca:" / "Käufer:" / "Buyer:" may appear:
        #   - at line start followed by name on the NEXT line
        #   - mid-line: "Sprzedawca: Foo  Nabywca: Bar" / "Verkäufer: Foo  Käufer: Bar"
        # Use negative lookbehind to avoid matching "Käufer" inside "Verkäufer".
        # Stop extraction at address/NIP boundaries to avoid capturing too much.
        if result["client_name"] is None:
            if re.search(
                r"(?<![A-Za-z])(Nabywca|Käufer|Buyer)\s*:", line, re.IGNORECASE
            ):
                # Try inline first: stop at Adres/Address/Adresse or NIP/USt/VAT keywords
                m = re.search(
                    r"(?<![A-Za-z])(?:Nabywca|Käufer|Buyer)\s*:\s*(.+?)(?:\s+(?:Adres(?:se)?|Address|NIP|USt|VAT)\s*[:\s]|$)",
                    line,
                    re.IGNORECASE,
                )
                if m and m.group(1).strip():
                    result["client_name"] = m.group(1).strip()
                else:
                    # Fall back to next non-empty line (original layout)
                    candidate = next_nonempty(i)
                    if candidate:
                        result["client_name"] = candidate

        # ── Amount netto ──────────────────────────────────────────────────
        # Labels: PL "Wartość netto" / DE "Gesamt Netto" / EN "Net value"
        # Note: "Nettowert" in DE is a TABLE COLUMN HEADER — skip it, use "Gesamt Netto"
        # which appears in the summary section where the actual total is on the next line.
        if result["amount_netto"] is None:
            if re.search(
                r"wartość\s+netto|gesamt\s+netto|net\s+value", line, re.IGNORECASE
            ):
                # Amount may be on the same line or the next
                amount_line = line
                m = re.search(r"[\d\s,\.]+[,\.][\d]{2}", amount_line)
                if not m:
                    amount_line = next_nonempty(i)
                val, cur = parse_amount(amount_line)
                if val is not None:
                    result["amount_netto"] = val
                    if cur:
                        result["currency"] = cur
                        currency_from_context = cur

        # ── Currency from context (total/brutto line) ─────────────────────
        # Pick up EUR/PLN if the amount line had no currency suffix
        if currency_from_context is None:
            m = re.search(r"\b(EUR|PLN|USD|GBP)\b", line)
            if m:
                currency_from_context = m.group(1)

        # ── Order number (comment / reference field) ──────────────────────
        # Labels: "Uwaga:" / "Kommentar:" / "Comment:"
        if result["order_number"] is None:
            if re.search(r"^(Uwaga|Kommentar|Comment)\s*:", line, re.IGNORECASE):
                # Comment may be inline: "Uwaga: ZLECENIE TRANSPORTOWE 11/03.2026 AB"
                inline = re.sub(
                    r"^(Uwaga|Kommentar|Comment)\s*:\s*", "", line, flags=re.IGNORECASE
                ).strip()
                comment_text = inline if inline else next_nonempty(i)
                if comment_text:
                    result["order_number"] = strip_order_prefix(comment_text)

    # Apply currency fallback if amount was found but currency tag was missing
    if result["currency"] is None and currency_from_context:
        result["currency"] = currency_from_context

    # encode to ASCII with backslash-replace so German/Polish chars don't crash CP1251 terminal
    safe = str(result).encode("ascii", errors="backslashreplace").decode("ascii")
    print(f"[INVOICE PARSE] {safe}")
    return result


def process_invoice(pdf_path: str) -> dict:
    """
    Full pipeline for a single outgoing invoice PDF.

    Steps:
      1. Extract invoice data from PDF.
      2. Find the matching row in Loads Info sheet by order_number.
      3. Skip if invoice number already written to col V.
      4. Build email subject + body.
      5. Write invoice number (col V) and today's date (col W) to Sheets.
      6. Find the order's Drive folder and download PDFs from it.
      7. Create a Gmail draft with invoice PDF + Drive PDFs attached.
      8. Move the source PDF to processed/.

    Never raises — always returns a status dict.

    Args:
        pdf_path: Absolute or relative path to the invoice PDF to process.

    Returns:
        dict with "status" key; see inline comments for all possible shapes.
    """
    pdf_path = Path(pdf_path)
    errors_dir = pdf_path.parent / "errors"
    processed_dir = pdf_path.parent / "processed"
    errors_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    # Track whether we already moved the file so the except block doesn't double-move
    _moved = False

    def _move_to_errors():
        nonlocal _moved
        if not _moved and pdf_path.exists():
            dest = errors_dir / pdf_path.name
            if dest.exists():
                dest.unlink()  # overwrite existing file in errors/
            pdf_path.rename(dest)
            _moved = True

    try:
        _draft_created = False
        _draft_skip_msg = None  # set when draft is intentionally skipped

        # ── Step 0: Sanitize filename — strip duplicate-number suffixes ────────
        # Windows sometimes appends " (2)", "-(3)", "_(2)_" when saving duplicates.
        # Pattern matches any combo of spaces/dashes/underscores + (N) / _N_ before .pdf
        original_name = pdf_path.name
        clean_stem = re.sub(
            r"[\s_-]*[\(_]+\d+[\)_]+(?=\.pdf$)", "", original_name, flags=re.IGNORECASE
        )
        if clean_stem != original_name:
            clean_path = pdf_path.parent / clean_stem
            if not clean_path.exists():
                pdf_path.rename(clean_path)
                pdf_path = clean_path
                print(f"[RENAME] {original_name} -> {clean_stem}")
            else:
                print(f"[RENAME] skipped — {clean_stem} already exists")

        # ── Step 1: Extract invoice data ──────────────────────────────────────
        invoice_data = extract_invoice_data(str(pdf_path))

        if not invoice_data.get("order_number"):
            _move_to_errors()
            return {"status": "error", "error_message": "order_number not found"}

        order_number = invoice_data["order_number"]

        # ── Step 2: Find Loads Info row ───────────────────────────────────────
        row_data = find_loads_info_row(order_number)

        if row_data is None:
            _move_to_errors()
            return {"status": "not_found", "order_number": order_number}

        # ── Step 3: Skip if already written ───────────────────────────────────
        col_v = row_data.get("col_v") or ""
        if col_v not in ("", "виставити"):
            return {"status": "already_written", "col_v": col_v}

        # ── Step 4: Build email ────────────────────────────────────────────────
        # Detect BrokerAlpha early — affects email template AND Drive attachment logic
        col_d = (row_data.get("col_d") or "").lower()
        is_brokeralpha = any(kw in col_d for kw in BROKERALPHA_CLIENTS)

        invoice_number = invoice_data.get("invoice_number") or ""

        if is_brokeralpha:
            # BrokerAlpha gets a dedicated HTML template — no build_invoice_email()
            tc_subject = f"Faktura BrokerAlpha {invoice_number} // ACME Sp. z o.o."
            tc_body = (
                "Witam,<br><br>"
                "W za\u0142\u0105czeniu przesy\u0142am faktury za us\u0142ugi transportowe:<br><br>"
                f"<ul><li><b>{invoice_number}</b></li></ul><br>"
                "W razie pyta\u0144 pozostaj\u0119 do dyspozycji.<br><br>"
                "Pozdrawiam serdecznie / Kind regards,<br>"
                "Anna Lewandowska"
            )
            email_data = {
                "subject": tc_subject,
                "body": tc_body,
                "lang": "pl",
                "doc_type": "tc",
            }
        else:
            email_data = build_invoice_email(invoice_data, row_data)

        # ── Step 5: Write to Sheets (col V + col W) ───────────────────────────
        write_our_invoice_nr(
            row=row_data["row_index"],
            invoice_nr=invoice_data["invoice_number"] or "",
        )

        _, sheets_svc, drive_service, gmail_service = _get_clients()
        sheet_ws_name = MONTH_NAMES.get(
            datetime.now().month, ""
        )  # same worksheet used by find_loads_info_row

        # Col W (date sent) is written only for BrokerAlpha — other clients fill it manually
        if is_brokeralpha:
            today_str = datetime.now().strftime("%d.%m.%Y")

            # Need sheet_id for batchUpdate — open the worksheet to get it
            _gc, _, _, _ = _get_clients()
            _ws = _gc.open_by_key(LOADS_INFO_SHEET_ID).worksheet(sheet_ws_name)
            _sheet_id = _ws.id

            sheets_svc.spreadsheets().values().update(
                spreadsheetId=LOADS_INFO_SHEET_ID,
                range=f"{sheet_ws_name}!W{row_data['row_index']}",
                valueInputOption="RAW",
                body={"values": [[today_str]]},
            ).execute()

            # Format col W: font 10 Calibri, center, #D9EAD3 background (Light green 3)
            _r = row_data["row_index"] - 1  # 0-based
            _green = {
                "red": 217 / 255,
                "green": 234 / 255,
                "blue": 211 / 255,
            }  # #D9EAD3
            sheets_svc.spreadsheets().batchUpdate(
                spreadsheetId=LOADS_INFO_SHEET_ID,
                body={
                    "requests": [
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": _sheet_id,
                                    "startRowIndex": _r,
                                    "endRowIndex": _r + 1,
                                    "startColumnIndex": 22,  # W = index 22 (0-based)
                                    "endColumnIndex": 23,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "backgroundColor": _green,
                                        "horizontalAlignment": "CENTER",
                                        "textFormat": {
                                            "fontSize": 11,
                                            "fontFamily": "Calibri",
                                        },
                                    }
                                },
                                "fields": (
                                    "userEnteredFormat.backgroundColor,"
                                    "userEnteredFormat.horizontalAlignment,"
                                    "userEnteredFormat.textFormat.fontSize,"
                                    "userEnteredFormat.textFormat.fontFamily"
                                ),
                            }
                        }
                    ]
                },
            ).execute()

        # ── Steps 6-7: Find Drive folder and download PDFs ────────────────────
        # BrokerAlpha gets only the invoice PDF — no Drive attachments needed
        drive_files = []  # list of (filename, bytes)

        if is_brokeralpha:
            print("[INVOICE] BrokerAlpha — skipping Drive attachments")
        else:
            folder_id = None
            col_a = str(row_data.get("col_a") or "").strip()

            folders_result = (
                drive_service.files()
                .list(
                    q=(
                        f"'{DRIVE_ROOT_FOLDER_ID}' in parents"
                        " and mimeType='application/vnd.google-apps.folder'"
                        " and trashed=false"
                    ),
                    fields="files(id, name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )

            for folder in folders_result.get("files", []):
                if col_a and folder["name"].startswith(col_a + " "):
                    folder_id = folder["id"]
                    break

            if not folder_id:
                print(
                    f"[INVOICE] Drive folder not found for col_a='{col_a}' — skipping attachments"
                )
            else:
                pdfs_result = (
                    drive_service.files()
                    .list(
                        q=(
                            f"'{folder_id}' in parents"
                            " and mimeType='application/pdf'"
                            " and trashed=false"
                        ),
                        fields="files(id, name)",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )

                for f in pdfs_result.get("files", []):
                    content = drive_service.files().get_media(fileId=f["id"]).execute()
                    drive_files.append((f["name"], content))
                    print(
                        f"[INVOICE] Downloaded from Drive: {f['name']} ({len(content)} bytes)"
                    )

        # ── Step 8: Create Gmail draft ─────────────────────────────────────────
        if is_brokeralpha:
            to_email = "invoices@brokeralpha.example"
            # HTML body in alternative part, PDF in outer mixed container
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(email_data["body"], "html", "utf-8"))

            outer = MIMEMultipart("mixed")
            outer["To"] = to_email
            outer["Bcc"] = "dispo@acme-forwarding.example"
            outer["Subject"] = email_data["subject"]
            outer.attach(alt)

            with open(pdf_path, "rb") as fh:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(fh.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition", f"attachment; filename={pdf_path.name}"
                )
                outer.attach(part)

            msg_to_send = outer

            raw = base64.urlsafe_b64encode(msg_to_send.as_bytes()).decode()
            try:
                gmail_service.users().drafts().create(
                    userId="me",
                    body={"message": {"raw": raw}},
                ).execute()
                _draft_created = True
            except Exception as e:
                print(f"[TC ERROR] {type(e).__name__}: {e}")
        else:
            # HTML email with invoice PDF + Drive files
            to_email = (row_data.get("col_e") or "").strip()

            if not EMAIL_RE.match(to_email):
                print(
                    f"[GMAIL WARNING] Invalid or empty email: '{to_email}' — skipping draft"
                )
                _draft_skip_msg = f"No valid email in col E: '{to_email}'"
            else:
                alt = MIMEMultipart("alternative")
                alt.attach(MIMEText(email_data["body"], "html", "utf-8"))

                outer = MIMEMultipart("mixed")
                outer["To"] = to_email
                outer["Subject"] = email_data["subject"]
                outer.attach(alt)

                with open(pdf_path, "rb") as fh:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(fh.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition", f"attachment; filename={pdf_path.name}"
                    )
                    outer.attach(part)

                for fname, content in drive_files:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(content)
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition", f"attachment; filename={fname}"
                    )
                    outer.attach(part)

                raw = base64.urlsafe_b64encode(outer.as_bytes()).decode()
                try:
                    gmail_service.users().drafts().create(
                        userId="me",
                        body={"message": {"raw": raw}},
                    ).execute()
                    print(f"[INVOICE] Gmail draft created for {order_number}")
                    _draft_created = True
                except Exception as e:
                    print(f"[GMAIL ERROR] {type(e).__name__}: {e}")

        # ── Step 9: Move PDF to processed/ ────────────────────────────────────
        pdf_path.rename(processed_dir / pdf_path.name)
        _moved = True

        # ── Step 10: Return success ────────────────────────────────────────────
        return {
            "status": "ok",
            "invoice_number": invoice_data.get("invoice_number"),
            "order_number": invoice_data.get("order_number"),
            "client_name": invoice_data.get("client_name"),
            "amount_netto": invoice_data.get("amount_netto"),
            "currency": invoice_data.get("currency"),
            "lang": email_data["lang"],
            "doc_type": email_data["doc_type"],
            "drive_files_attached": len(drive_files),
            "draft_created": _draft_created,
            "error_message": _draft_skip_msg,
        }

    except Exception as e:
        _move_to_errors()
        print(f"[INVOICE] process_invoice failed: {e}")
        return {"status": "error", "error_message": str(e)}


class InvoiceChecker:
    def __init__(self, watch_folder: str, poll_interval: int = 30):
        self.watch_folder = Path(watch_folder)
        self.processed_folder = self.watch_folder / "processed"
        self.poll_interval = poll_interval
        self._ensure_folders()

    def _ensure_folders(self):
        self.watch_folder.mkdir(parents=True, exist_ok=True)
        self.processed_folder.mkdir(parents=True, exist_ok=True)

    def _list_pdfs(self):
        return list(self.watch_folder.glob("*.pdf"))

    def _process_pdf(self, pdf_path: Path):
        """Process a single invoice PDF through the full pipeline."""
        print(f"Invoice found: {pdf_path.name}")
        result = process_invoice(str(pdf_path))
        print(f"[InvoiceChecker] result={result.get('status')} for {pdf_path.name}")

    def run(self):
        while True:
            for pdf in self._list_pdfs():
                self._process_pdf(pdf)
            time.sleep(self.poll_interval)


def start_watcher():
    checker = InvoiceChecker(INVOICE_WATCH_FOLDER)
    thread = threading.Thread(target=checker.run, daemon=True)
    thread.start()
    print(f"Invoice checker started, monitoring folder: {INVOICE_WATCH_FOLDER}")
