"""
executor.py

Task queue — processes orders in a background thread.

Task types:
  order          — transport order: Sheets + Drive + Gmail draft
  order_timocom  — same as order
  carrier_invoice — carrier invoice: verify + Payment Plan
"""

import threading
from pathlib import Path
from queue import Queue

from task import Task

task_queue = Queue()
_progress_callbacks: dict = {}   # task_id → callback(msg: str)


# ─────────────────────────────────────────────
# PROGRESS / LOG
# ─────────────────────────────────────────────

def register_progress_callback(task_id: str, fn):
    """Register a function for progress display in UI."""
    _progress_callbacks[task_id] = fn


def _log(task: Task, msg: str):
    """Log progress to console and UI callback if registered."""
    print(f"[{task.task_id}] {msg}")
    cb = _progress_callbacks.get(task.task_id)
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


# ─────────────────────────────────────────────
# MAIN DISPATCHER
# ─────────────────────────────────────────────

def execute_task(task: Task):
    """Execute one task based on its type."""
    task.status = "running"
    _log(task, f"▶️ Starting: {task.task_type} [{task.task_id}]")

    try:
        if task.task_type in ("order", "order_timocom"):
            _execute_order(task)
        elif task.task_type == "carrier_invoice":
            _execute_carrier_invoice(task)
        else:
            _log(task, f"⚠️ Unknown task type: {task.task_type}")

        task.status = "done"
        _log(task, "✅ Task completed successfully")

    except Exception as e:
        task.status = "error"
        task.error = str(e)
        _log(task, f"❌ Critical error: {e}")

    finally:
        _progress_callbacks.pop(task.task_id, None)


# ─────────────────────────────────────────────
# ORDER PROCESSING
# ─────────────────────────────────────────────

def _execute_order(task: Task):
    """
    Processes a transport order:
      1. Sheets — write_to_loads_info()
      2. Drive  — upload_to_drive() for each PDF
      3. Gmail  — create_gmail_draft()

    task.data expects:
      parsed        — dict from ai_parser.parse_order()
      our_order_nr  — str, e.g. "2026-03-00079"
      pdf_paths     — list[str]
      do_sheets     — bool
      do_drive      — bool
      do_gmail      — bool
    """
    from actions import write_to_loads_info, upload_to_drive, create_gmail_draft

    data         = task.data
    order        = data.get("parsed") or {}
    our_order_nr = data.get("our_order_nr", "")
    pdf_paths    = data.get("pdf_paths", [])
    do_sheets    = data.get("do_sheets", True)
    do_drive     = data.get("do_drive", True)
    do_gmail     = data.get("do_gmail", True)

    log = lambda msg: _log(task, msg)

    if not our_order_nr:
        raise ValueError("our_order_nr not specified")

    # Extract row_nr from our order number (last digits)
    # Format: "2026-03-00079" → 79
    try:
        row_nr = int(our_order_nr.split("-")[-1])
    except Exception:
        row_nr = 0

    # Find carrier by code in carrier_name or license_plates
    carrier_info = _find_carrier(order.get("carrier_name", ""),
                                 order.get("license_plates", ""))

    seq_nr = row_nr  # fallback if sheets skipped
    company_name = order.get("company_name", "")

    # ── 1. Google Sheets ──
    if do_sheets:
        log("📊 Writing to Loads Info...")
        result = write_to_loads_info(
            parsed_order=order,
            carrier_info=carrier_info,
            our_order_number=our_order_nr,
            sender_email=data.get("sender_email", ""),
            log_fn=log,
            total_minutes=task.duration / 60,
        )
        # write_to_loads_info returns (success, seq_nr, col_c)
        if isinstance(result, tuple):
            success, seq_nr, _ = result
        else:
            success = result
        if not success:
            log("⚠️ Sheets not written, continuing...")

    # ── 2. Google Drive ──
    if do_drive and pdf_paths:
        client_order_nr = order.get("client_order_number", "")
        for pdf_path in pdf_paths:
            log(f"📁 Uploading to Drive: {pdf_path}")
            upload_to_drive(
                pdf_path=pdf_path,
                seq_nr=seq_nr,
                company_name=company_name,
                client_order_number=client_order_nr,
                log_fn=log,
            )
    elif do_drive and not pdf_paths:
        log("📁 Drive: no PDFs to upload")

    # ── 3. Gmail draft ──
    if do_gmail:
        log("✉️ Creating Gmail draft...")
        draft_data = {
            **order,
            "our_order_number":    our_order_nr,
            "client_order_number": order.get("client_order_number", ""),
            "invoice_email":       order.get("invoice_email") or order.get("contact_email", ""),
        }
        create_gmail_draft(order_data=draft_data, log_fn=log)


# ─────────────────────────────────────────────
# CARRIER INVOICE PROCESSING
# ─────────────────────────────────────────────

def _execute_carrier_invoice(task: Task):
    """
    Processes a carrier invoice:
      1. Verifies amount and date via verify_invoice()
      2. Writes to Payment Plan via write_to_payment_plan()

    task.data expects:
      parsed    — dict from ai_parser.parse_invoice() (used when single PDF)
      pdf_paths — list[str] (when multiple PDFs, each is parsed separately)
    """
    from actions import verify_invoice, write_to_payment_plan, update_loads_info_invoice
    from ai_parser import parse_invoice

    data      = task.data
    parsed    = data.get("parsed") or {}
    pdf_paths = data.get("pdf_paths") or []
    body      = parsed.get("_body", "")
    log       = lambda msg: _log(task, msg)

    # Build list of parsed dicts — one per PDF when multiple attachments
    if len(pdf_paths) > 1:
        invoices_to_process = []
        for i, pdf_path in enumerate(pdf_paths, 1):
            log(f"Processing invoice {i} of {len(pdf_paths)}: {Path(pdf_path).name}")
            p = parse_invoice(body, "", [pdf_path])
            if p:
                invoices_to_process.append(p)
            else:
                log(f"⚠️ Could not parse {Path(pdf_path).name}, skipping")
    else:
        invoices_to_process = [parsed] if parsed else []

    for inv_parsed in invoices_to_process:
        invoice_nr = inv_parsed.get("invoice_number", "?")
        log(f"📄 Carrier invoice: {invoice_nr}")

        # ── 1. Verify against Loads Info ──
        log("🔍 Verifying amount and date in Loads Info...")
        issues = verify_invoice(inv_parsed)
        if issues:
            for issue in issues:
                log(f"⚠️ {issue}")
            log("⚠️ Discrepancies found — check manually!")
        else:
            log("✅ Verification passed — data matches")

        # ── Match to order ──
        client_nr, our_nr = _find_matching_order(inv_parsed)
        if our_nr or client_nr:
            log(f"🔗 Matched to order: {our_nr} (client: {client_nr})")
            task.data["matched_order_nr"]  = our_nr
            task.data["matched_client_nr"] = client_nr
        else:
            log("⚠️ No matching order found — check manually")

        # ── 2. Write to Payment Plan ──
        log("💳 Writing to Payment Plan...")
        success = write_to_payment_plan(inv_parsed, log_fn=log)
        if not success:
            log("⚠️ Payment Plan not written")

        # ── 3. Update Loads Info AD/AE ──
        log("📝 Updating Loads Info AD/AE...")
        # Inject email metadata so update_loads_info_invoice can use correct sheet + AE date
        received_at = data.get("received_at") or parsed.get("received_at") or ""
        inv_parsed["received_at"]  = received_at
        inv_parsed["order_number"] = inv_parsed.get("order_number") or data.get("matched_order_nr", "")
        print(f"DEBUG received_at injected: {received_at!r}, parsed received_at: {inv_parsed.get('received_at')!r}")
        update_loads_info_invoice(inv_parsed, log_fn=log)


# ─────────────────────────────────────────────
# HELPER: ORDER MATCH LOOKUP
# ─────────────────────────────────────────────

def _find_matching_order(parsed: dict) -> tuple[str, str]:
    """
    Searches Loads Info for the row matching invoice order_number (col B or C).
    Returns (client_order_nr, our_order_nr) or ('', '') if not found.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        from config import CREDENTIALS_PATH, LOADS_INFO_SHEET_ID
        from actions import SCOPES, _get_worksheet

        creds = Credentials.from_service_account_file(str(CREDENTIALS_PATH), scopes=SCOPES)
        gc = gspread.authorize(creds)
        worksheet = _get_worksheet(gc.open_by_key(LOADS_INFO_SHEET_ID))
        rows = worksheet.get_all_values()

        target = str(parsed.get("order_number", "")).strip()
        if not target:
            return "", ""

        for row in rows:
            if len(row) < 3:
                continue
            col_b = str(row[1]).strip()
            col_c = str(row[2]).strip()
            if target in (col_b, col_c):
                return col_b, col_c
    except Exception:
        pass
    return "", ""


# ─────────────────────────────────────────────
# HELPER: CARRIER LOOKUP
# ─────────────────────────────────────────────

def _find_carrier(carrier_name: str, plates: str) -> dict | None:
    """
    Searches config.CARRIERS by code in carrier_name or plates.
    Returns dict with payment rules or None if not found.

    Example codes: KWDL, KWD, LKW4, LKW9, PWT, OLA, OWN1, OWN2
    """
    try:
        from config import CARRIERS
    except (ImportError, AttributeError):
        return None

    combined = f"{carrier_name} {plates}".upper()

    for code, info in CARRIERS.items():
        if code in combined:
            # For OWN1/OWN2, override payment rules based on client terms
            if code in ("OWN1", "OWN2"):
                # We do not treat own trucks as carriers for payment rules
                return info
            if info.get("is_own"):
                return None
            return info

    return None


# ─────────────────────────────────────────────
# BACKGROUND WORKER
# ─────────────────────────────────────────────

def worker():
    """Background thread — takes tasks from the queue and executes them one by one."""
    while True:
        task = task_queue.get()
        try:
            execute_task(task)
        except Exception as e:
            print(f"[worker] Unexpected error: {e}")
        finally:
            task_queue.task_done()


# Start background thread on module import
threading.Thread(target=worker, daemon=True).start()
