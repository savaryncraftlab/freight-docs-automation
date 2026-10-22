"""
cmr_processor.py — Unified CMR processing pipeline.

Two input sources:
  1. Downloads folder watcher  — detects CMR files from WhatsApp, moves to bot_data/cmr/
  2. Telegram bot (telegram_cmr.py) — saves directly to bot_data/cmr/

Processing:
  - Match CMR to order (by order_number in filename or by unloading date)
  - Convert images to PDF
  - Merge multiple PDFs for same order
  - Upload to Google Drive folder
  - Mark col V yellow + "виставити" in Loads Info
  - Move processed files to bot_data/cmr/processed/
"""

import io
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from config import CMR_WATCH_FOLDER, DOWNLOADS_WATCH_FOLDER, MONTH_NAMES

# ─── CMR file recognition patterns ─────────────────────────────────
# These patterns identify CMR/unloading documents vs orders/invoices
_CMR_PATTERNS = [
    re.compile(r"^Unloading", re.IGNORECASE),
    re.compile(r"^Unloadinbg", re.IGNORECASE),  # common typo
    re.compile(r"^CMR[_\s]", re.IGNORECASE),
    re.compile(r"^POD[_\s]", re.IGNORECASE),
    re.compile(r"^Lieferschein", re.IGNORECASE),
    re.compile(r"^WhatsApp Image", re.IGNORECASE),
    re.compile(r"^WhatsApp Video", re.IGNORECASE),
    re.compile(r"^Документ", re.IGNORECASE),  # Ukrainian scan names (Viber/WhatsApp)
    re.compile(r"^Document\s*\d", re.IGNORECASE),
    re.compile(r"^Dokument", re.IGNORECASE),
    re.compile(r"^Scan[_\s]", re.IGNORECASE),
    re.compile(r"^IMG[_\-]", re.IGNORECASE),
    re.compile(r"^photo[_\-]", re.IGNORECASE),
]

# Files to NEVER treat as CMR (even if in Downloads)
_NOT_CMR_PATTERNS = [
    re.compile(r"^faktura", re.IGNORECASE),
    re.compile(r"^Purchase_", re.IGNORECASE),
    re.compile(r"zlecenie", re.IGNORECASE),
    re.compile(r"^rate_confirmation", re.IGNORECASE),
    re.compile(r"^invoice", re.IGNORECASE),
    re.compile(r"^rechnung", re.IGNORECASE),
]

# Supported CMR file extensions
_CMR_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".heic"}

# Date extraction from filenames
_DATE_DD_MM_YYYY = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")  # 02.04.2026
_DATE_YYYY_MM_DD = re.compile(r"(\d{4})-(\d{2})-(\d{2})")  # 2026-04-02

# Order number from Telegram bot filename: CMR_{order}_{time}.pdf
_TELEGRAM_CMR = re.compile(r"^CMR_(.+?)_\d{6}\.pdf$", re.IGNORECASE)


def is_cmr_file(filename: str) -> bool:
    """Check if a filename matches CMR/unloading document pattern."""
    name = Path(filename).name
    ext = Path(filename).suffix.lower()
    if ext not in _CMR_EXTENSIONS:
        return False
    # Reject known non-CMR files
    if any(p.search(name) for p in _NOT_CMR_PATTERNS):
        return False
    # Accept known CMR patterns
    if any(p.search(name) for p in _CMR_PATTERNS):
        return True
    return False


def extract_date_from_filename(filename: str) -> str | None:
    """
    Extract unloading date from CMR filename.

    Returns date as DD.MM.YYYY string, or None if not found.
    Examples:
      "Unloading 02.04.2026.pdf"           → "02.04.2026"
      "Unloading BE 02.04.2026.pdf"        → "02.04.2026"
      "WhatsApp Image 2026-04-02 at ..."   → "02.04.2026"
    """
    name = Path(filename).stem
    # Try DD.MM.YYYY first
    m = _DATE_DD_MM_YYYY.search(name)
    if m:
        return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    # Try YYYY-MM-DD (WhatsApp format)
    m = _DATE_YYYY_MM_DD.search(name)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    return None


def extract_order_from_filename(filename: str) -> str | None:
    """
    Extract order number from Telegram CMR filename.

    "CMR_TC400003_143022.pdf" → "TC400003"
    "CMR_2026-04-00059_091500.pdf" → "2026-04-00059"
    """
    m = _TELEGRAM_CMR.match(Path(filename).name)
    return m.group(1) if m else None


def _image_to_pdf(image_bytes: bytes) -> bytes:
    """Convert image bytes (JPEG/PNG) to single-page PDF."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150.0)
    return buf.getvalue()


def _merge_pdfs(pdf_paths: list[Path]) -> bytes:
    """Merge multiple PDF files into one. Returns merged PDF bytes."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for p in pdf_paths:
        reader = PdfReader(str(p))
        for page in reader.pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _parse_cmr_with_vision(file_path: Path, log_fn=None) -> dict | None:
    """Parse a scanned CMR document using GPT-4o-mini vision API."""
    log = log_fn or print
    try:
        import base64
        import json

        import pdfplumber
        from openai import OpenAI

        from config import OPENAI_API_KEY

        if not OPENAI_API_KEY:
            log("  No OPENAI_API_KEY — cannot use vision parsing")
            return None

        client = OpenAI(api_key=OPENAI_API_KEY)

        # Convert first page of PDF to JPEG
        with pdfplumber.open(str(file_path)) as pdf:
            if not pdf.pages:
                return None
            page = pdf.pages[0]
            img = page.to_image(resolution=200).original
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            img_b64 = base64.b64encode(buf.getvalue()).decode()

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Extract from this CMR/consignment note:\n"
                                '1. loading_place: country code + postal code (e.g. "BE 1000")\n'
                                '2. unloading_place: country code + postal code (e.g. "PL 00950")\n'
                                "3. loading_date: DD.MM.YYYY\n"
                                "4. delivery_date: DD.MM.YYYY (if visible)\n"
                                "5. license_plates: list of all vehicle registration plates\n"
                                "6. carrier_name: from box 16/17 or stamps\n"
                                "7. order_reference: any order/zlecenie number\n"
                                "8. sender_name: company in box 1\n"
                                "9. consignee_name: company in box 2\n\n"
                                "Return ONLY valid JSON, no markdown, no explanation."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                        },
                    ],
                }
            ],
            max_tokens=500,
        )

        raw = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        if raw.startswith("json"):
            raw = raw[4:]

        parsed = json.loads(raw.strip())
        log(
            f"  Vision parsed: plates={parsed.get('license_plates')}, "
            f"load={parsed.get('loading_place')}, unload={parsed.get('unloading_place')}"
        )
        return parsed

    except Exception as e:
        log(f"  Vision parsing error: {e}")
        return None


def _identify_carrier_by_plates(plates: list[str]) -> str | None:
    """Match license plates to carrier code from CARRIERS config."""
    from config import CARRIERS

    for plate in plates:
        plate_upper = plate.upper().replace(" ", "")
        for code, info in CARRIERS.items():
            config_plates = info.get("license_plates", "").upper().replace(" ", "")
            for config_plate in config_plates.split(","):
                config_plate = config_plate.strip().split("/")[0]  # First plate of pair
                if config_plate and config_plate in plate_upper:
                    return code
                # Also check if the plate appears anywhere in the config plates string
                if plate_upper in config_plates:
                    return code
    return None


def _find_orders_by_carrier_route(
    carrier_code: str,
    loading_postal: str,
    unloading_postal: str,
    date_str: str = "",
) -> list[dict]:
    """Search loads_cache by carrier code (col L) and route postal codes (col G/H)."""
    from loads_cache import _ALL_ROWS, _LOCK

    results = []
    with _LOCK:
        snapshot = list(_ALL_ROWS)

    carrier_upper = carrier_code.upper()
    # Normalize postal codes: remove spaces, dashes (may be None from vision API)
    load_norm = (loading_postal or "").replace(" ", "").replace("-", "").upper()
    unload_norm = (unloading_postal or "").replace(" ", "").replace("-", "").upper()

    for row in snapshot:
        row_l = (row.get("L") or "").strip().upper()
        if row_l != carrier_upper:
            continue

        row_g = (row.get("G") or "").replace(" ", "").replace("-", "").upper()
        row_h = (row.get("H") or "").replace(" ", "").replace("-", "").upper()

        # Check if postal codes match (partial match — our format might differ)
        load_match = load_norm in row_g or row_g in load_norm if load_norm else True
        unload_match = (
            unload_norm in row_h or row_h in unload_norm if unload_norm else True
        )

        if load_match and unload_match:
            # If date provided, also check
            if date_str:
                row_j = (row.get("J") or "").strip()
                if row_j and row_j != date_str:
                    continue
            results.append(row)

    return results


def _find_orders_by_date(unloading_date: str) -> list[dict]:
    """
    Search loads_cache for orders with matching unloading date (col J).

    Returns list of matching row dicts with keys: A, B, D, J, L, V, sheet_row, month.
    """
    from loads_cache import _ALL_ROWS, _LOCK

    results = []
    with _LOCK:
        snapshot = list(_ALL_ROWS)
    for row in snapshot:
        j = (row.get("J") or "").strip()
        if j == unloading_date:
            results.append(row)
    return results


def _find_order_by_number(order_number: str) -> dict | None:
    """Search loads_cache for order by client order number (col B) or our order number (col C)."""
    from loads_cache import _ALL_ROWS, _LOCK

    needle = order_number.strip().upper()
    with _LOCK:
        snapshot = list(_ALL_ROWS)
    for row in snapshot:
        b = (row.get("B") or "").strip().upper()
        c = (row.get("C") or "").strip().upper()
        if (b and b == needle) or (c and c == needle):
            return row
    return None


def _upload_cmr_to_drive(
    pdf_bytes: bytes,
    filename: str,
    order_row: dict,
    log_fn=None,
) -> str:
    """
    Upload CMR PDF to the order's Drive folder.

    Folder structure: 2026 / MM MonthName / {A} {D} / {filename}
    Returns webViewLink or empty string.
    """
    log = log_fn or print
    try:
        from actions import _get_clients, _get_or_create_folder
        from config import DRIVE_ROOT_FOLDER_ID

        _, _, drive_service, _ = _get_clients()
        now = datetime.now()

        # Month folder: "04 Kwiecień"
        month_name = f"{now.month:02d} {MONTH_NAMES[now.month]}"
        month_folder = _get_or_create_folder(
            drive_service, DRIVE_ROOT_FOLDER_ID, month_name
        )

        # Order folder: "{seq_nr} {company_name}"
        seq = str(order_row.get("A", "")).strip()
        company = str(order_row.get("D", "")).strip()
        order_folder_name = f"{seq} {company}".strip()

        if not order_folder_name or order_folder_name == " ":
            log(f"  CMR: cannot determine folder name for row {order_row}")
            return ""

        order_folder = _get_or_create_folder(
            drive_service, month_folder, order_folder_name
        )

        # Upload PDF
        import tempfile
        from googleapiclient.http import MediaFileUpload

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        try:
            media = MediaFileUpload(tmp_path, mimetype="application/pdf")
            uploaded = (
                drive_service.files()
                .create(
                    body={"name": filename, "parents": [order_folder]},
                    media_body=media,
                    fields="id,webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
            link = uploaded.get("webViewLink", "")
            log(f"  Drive: {filename} -> {order_folder_name}")
            return link
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        log(f"  Drive upload error: {e}")
        return ""


def _mark_col_v_vystavyty(order_row: dict, log_fn=None):
    """
    Mark col V in Loads Info: yellow background + text "виставити".

    Only marks if col V is currently empty.
    """
    log = log_fn or print
    current_v = (order_row.get("V") or "").strip()
    if current_v and current_v.lower() != "виставити":
        log(f"  Col V already has value: {current_v} — skipping")
        return

    sheet_row = order_row.get("sheet_row")
    month = order_row.get("month", "")
    if not sheet_row or not month:
        log("  Cannot mark col V: no sheet_row or month")
        return

    try:
        from actions import _get_clients
        from config import LOADS_INFO_SHEET_ID

        gc, sheets_service, _, _ = _get_clients()
        spreadsheet = gc.open_by_key(LOADS_INFO_SHEET_ID)
        ws = spreadsheet.worksheet(month)
        sheet_id = ws.id

        # Write "виставити" to col V
        sheets_service.spreadsheets().values().update(
            spreadsheetId=LOADS_INFO_SHEET_ID,
            range=f"{month}!V{sheet_row}",
            valueInputOption="RAW",
            body={"values": [["виставити"]]},
        ).execute()

        # Format: yellow background, font 10 Calibri, center
        r = sheet_row - 1  # 0-based
        yellow = {"red": 1.0, "green": 0.85, "blue": 0.4}  # ~FFD966 yellow
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
                                "startColumnIndex": 21,  # V = index 21
                                "endColumnIndex": 22,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": yellow,
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

        log(f"  Col V row {sheet_row}: yellow + 'виставити'")

    except Exception as e:
        log(f"  Col V marking error: {e}")


def process_cmr_file(file_path: Path, log_fn=None) -> bool:
    """
    Process a single CMR file: match to order, upload to Drive, mark Sheets.

    Returns True if file was successfully processed and moved to processed/.
    """
    log = log_fn or print
    processed_dir = Path(CMR_WATCH_FOLDER) / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)

    name = file_path.name
    ext = file_path.suffix.lower()
    log(f"Processing CMR: {name}")

    # Step 1: Read file and convert to PDF if image
    raw_bytes = file_path.read_bytes()
    if ext in (".jpg", ".jpeg", ".png"):
        try:
            pdf_bytes = _image_to_pdf(raw_bytes)
            log("  Converted image -> PDF")
        except Exception as e:
            log(f"  Image conversion error: {e}")
            return False
    elif ext == ".pdf":
        pdf_bytes = raw_bytes
    else:
        log(f"  Unsupported format: {ext}")
        return False

    # Step 2: Match to order
    order_row = None

    # Try by order number (Telegram bot format: CMR_{order}_{time}.pdf)
    order_nr = extract_order_from_filename(name)
    if order_nr:
        order_row = _find_order_by_number(order_nr)
        if order_row:
            log(
                f"  Matched by order number: {order_nr} -> row {order_row.get('sheet_row')}"
            )

    # Try by unloading date
    date_str = None  # Will be set by filename or vision for standard naming
    if not order_row:
        date_str = extract_date_from_filename(name)
        if date_str:
            candidates = _find_orders_by_date(date_str)
            if len(candidates) == 1:
                order_row = candidates[0]
                log(
                    f"  Matched by date {date_str}: {order_row.get('B')} ({order_row.get('D')})"
                )
            elif len(candidates) > 1:
                log(f"  Multiple orders for date {date_str}:")
                for c in candidates:
                    log(
                        f"    - row {c.get('sheet_row')}: {c.get('B')} / {c.get('D')} / {c.get('L')}"
                    )
                log("  Cannot auto-match by date alone — trying vision...")
            else:
                log(f"  No orders found for date {date_str} — trying vision...")
        else:
            log("  No date or order number in filename — trying vision...")

    # Try 3: Vision API — parse scanned CMR and match by carrier + route
    if not order_row:
        vision_data = _parse_cmr_with_vision(file_path, log_fn=log)
        if vision_data:
            # Try direct match by order reference
            order_ref = vision_data.get("order_reference")
            if order_ref:
                order_row = _find_order_by_number(order_ref)
                if order_row:
                    log(f"  Vision: matched by order ref: {order_ref}")

            # Try match by license plates + route
            if not order_row:
                plates = vision_data.get("license_plates") or []
                if isinstance(plates, str):
                    plates = [plates]
                carrier_code = _identify_carrier_by_plates(plates)
                if carrier_code:
                    log(f"  Vision: carrier = {carrier_code} (plates: {plates})")
                    load_place = vision_data.get("loading_place", "")
                    unload_place = vision_data.get("unloading_place", "")
                    load_date = vision_data.get("loading_date", "")
                    candidates = _find_orders_by_carrier_route(
                        carrier_code, load_place, unload_place, load_date
                    )
                    if len(candidates) == 1:
                        order_row = candidates[0]
                        log(f"  Vision: matched by carrier+route: {order_row.get('B')}")
                    elif len(candidates) > 1:
                        log(
                            f"  Vision: {len(candidates)} candidates for {carrier_code}:"
                        )
                        for c in candidates:
                            log(
                                f"    - row {c.get('sheet_row')}: {c.get('B')} / {c.get('D')}"
                            )
                    else:
                        log(
                            f"  Vision: no orders for {carrier_code} {load_place}->{unload_place}"
                        )

            # Update date for standard naming from vision data
            if order_row and vision_data.get("loading_date"):
                date_str = vision_data["loading_date"]

    if not order_row:
        log("  No matching order found")
        return False

    # Step 3: Rename to standard format: "Unloading DD.MM.YYYY.pdf"
    # date_str may come from filename (Try 2) or vision (Try 3); fallback to today
    if not date_str:
        date_str = datetime.now().strftime("%d.%m.%Y")
    standard_name = f"Unloading {date_str}.pdf"

    # Step 4: Upload to Drive
    link = _upload_cmr_to_drive(pdf_bytes, standard_name, order_row, log_fn=log)

    # Step 5: Mark col V yellow + "виставити"
    if link:
        _mark_col_v_vystavyty(order_row, log_fn=log)

    # Step 6: Move to processed
    dest = processed_dir / name
    if dest.exists():
        dest = processed_dir / f"{file_path.stem}_{int(time.time())}{file_path.suffix}"
    shutil.move(str(file_path), str(dest))
    log(f"  Done: {name} -> processed/")

    return True


class DownloadsWatcher:
    """
    Watches the Downloads folder for new CMR files.

    When detected: moves to bot_data/cmr/ for processing.
    Only moves files that match CMR naming patterns.
    """

    def __init__(self, poll_interval: int = 120, log_fn=None):
        self.downloads_dir = Path(DOWNLOADS_WATCH_FOLDER)
        self.cmr_dir = Path(CMR_WATCH_FOLDER)
        self.cmr_dir.mkdir(parents=True, exist_ok=True)
        self.poll_interval = poll_interval
        self.log = log_fn or print
        # Track known files to only process new ones
        self._known_files: set[str] = set()
        self._init_known_files()

    def _init_known_files(self):
        """Snapshot current Downloads contents so we only detect NEW files."""
        if self.downloads_dir.exists():
            for f in self.downloads_dir.iterdir():
                if f.is_file():
                    self._known_files.add(f.name)

    def _scan_for_new_cmr(self) -> list[Path]:
        """Find new CMR files in Downloads that weren't there before."""
        new_cmr = []
        if not self.downloads_dir.exists():
            return new_cmr
        for f in self.downloads_dir.iterdir():
            if not f.is_file():
                continue
            if f.name in self._known_files:
                continue
            self._known_files.add(f.name)
            if is_cmr_file(f.name):
                # Wait a moment for file to finish writing
                try:
                    size1 = f.stat().st_size
                    time.sleep(1)
                    size2 = f.stat().st_size
                    if size1 == size2 and size2 > 0:
                        new_cmr.append(f)
                except OSError:
                    continue
        return new_cmr

    def _move_to_cmr_folder(self, src: Path) -> Path | None:
        """Move a CMR file from Downloads to bot_data/cmr/."""
        dest = self.cmr_dir / src.name
        if dest.exists():
            dest = self.cmr_dir / f"{src.stem}_{int(time.time())}{src.suffix}"
        try:
            shutil.move(str(src), str(dest))
            self.log(f"CMR detected: {src.name} -> bot_data/cmr/")
            return dest
        except Exception as e:
            self.log(f"CMR move error: {src.name}: {e}")
            return None

    def poll_once(self) -> list[Path]:
        """Single poll cycle. Returns list of files moved to cmr/."""
        moved = []
        for cmr_file in self._scan_for_new_cmr():
            dest = self._move_to_cmr_folder(cmr_file)
            if dest:
                moved.append(dest)
        return moved


class CMRProcessor:
    """
    Main CMR processing loop.

    1. DownloadsWatcher detects new CMR files -> moves to bot_data/cmr/
    2. Scans bot_data/cmr/ for unprocessed files
    3. Processes each: match, convert, upload, mark
    """

    def __init__(self, poll_interval: int = 120, log_fn=None):
        self.cmr_dir = Path(CMR_WATCH_FOLDER)
        self.cmr_dir.mkdir(parents=True, exist_ok=True)
        self.poll_interval = poll_interval
        self.log = log_fn or print
        self.downloads_watcher = DownloadsWatcher(
            poll_interval=poll_interval, log_fn=self.log
        )
        self._processed_names: set[str] = set()
        self._retry_counts: dict[str, int] = {}
        self._max_retries = 3  # retry up to 3 times before giving up

    def _scan_cmr_folder(self) -> list[Path]:
        """Find unprocessed files in bot_data/cmr/ (not in processed/ subfolder)."""
        files = []
        for f in self.cmr_dir.iterdir():
            if not f.is_file():
                continue
            if f.name in self._processed_names:
                continue
            ext = f.suffix.lower()
            if ext in _CMR_EXTENSIONS:
                files.append(f)
        return files

    def run_once(self) -> int:
        """Single processing cycle. Returns number of files processed."""
        # Phase 1: Check Downloads for new CMR files
        self.downloads_watcher.poll_once()

        # Phase 2: Process files in bot_data/cmr/
        processed = 0
        for f in self._scan_cmr_folder():
            ok = process_cmr_file(f, log_fn=self.log)
            if ok:
                processed += 1
                self._processed_names.add(f.name)
                self._retry_counts.pop(f.name, None)
            else:
                # Retry up to 3 times (cache may not have been ready)
                retries = self._retry_counts.get(f.name, 0) + 1
                self._retry_counts[f.name] = retries
                if retries >= self._max_retries:
                    self._processed_names.add(f.name)
                    self.log(f"  {f.name}: giving up after {retries} attempts")
        return processed

    def run_forever(self):
        """Main polling loop — runs in background thread."""
        # Ensure loads_cache is initialized before first processing cycle
        try:
            from loads_cache import _ALL_ROWS, _LOCK

            with _LOCK:
                cached = len(_ALL_ROWS)
            if cached == 0:
                import loads_cache

                loads_cache.init()
                # Wait up to 15 seconds for cache to populate
                for _ in range(15):
                    time.sleep(1)
                    with _LOCK:
                        if len(_ALL_ROWS) > 0:
                            break
                with _LOCK:
                    cached = len(_ALL_ROWS)
                self.log(f"CMR Processor: loads_cache initialized ({cached} rows)")
        except Exception as e:
            self.log(f"CMR Processor: cache init warning: {e}")

        self.log(
            f"CMR Processor started (Downloads: {self.downloads_watcher.downloads_dir})"
        )
        while True:
            try:
                self.run_once()
            except Exception as e:
                self.log(f"CMR Processor error: {e}")
            time.sleep(self.poll_interval)


def start_cmr_processor(log_fn=None) -> threading.Thread:
    """Start the CMR processor in a background daemon thread."""
    processor = CMRProcessor(poll_interval=120, log_fn=log_fn)
    thread = threading.Thread(
        target=processor.run_forever, daemon=True, name="cmr_processor"
    )
    thread.start()
    return thread
