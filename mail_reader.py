"""
mail_reader.py

Reads emails from INBOX and the "logistics" folder.
Filters:
  - only from info@acme-forwarding.example and dispo@acme-forwarding.example
  - only from the last 7 days
  - max 50 emails at a time
  - Fwd: are working emails, NOT skipped
  - emails are NEVER moved, deleted, or flagged — read-only access
"""

import email
import imaplib
from email.header import decode_header
from pathlib import Path
from datetime import datetime, timedelta

from config import EMAIL, EMAIL_PASSWORD
from storage import add_email

BASE_DIR = Path(__file__).resolve().parent
ATTACHMENTS_DIR = BASE_DIR / "bot_data" / "attachments"
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_SENDERS = [
    "info@acme-forwarding.example",
    "dispo@acme-forwarding.example",
    "operator@acme-forwarding.example",
    "operator2@acme-forwarding.example",
]
IGNORED_SENDERS = []
DAYS_BACK = 45
BATCH_LIMIT = 50

# Source folders: Inbox + logistics grey


SOURCE_FOLDERS = [
    "[Gmail]/&BCMEQQRP- &BD8EPgRIBEIEMA-",  # All Mail (UTF-7 encoded) — contains every label/folder
]
# Subject keywords that identify a carrier invoice (Polish FV/, German Rechnung, etc.)
_INVOICE_KEYWORDS = ("fv/", "rechnung", "faktura vat", "faktura ")

# Our own outgoing emails forwarded back — must NOT be processed as carrier invoices
# Matched case-insensitively against subject; if any hits → skip without RFC822 download
_OWN_SUBJECT_SKIP = (
    "invoice and documents for transport order",  # our standard subject template to clients
    "// acme sp. z o.o.",  # our company signature appended to subject
)
# Markers in email body (checked case-insensitively) that identify our outgoing emails
_OWN_BODY_SKIP = (
    "please find attached the electronic invoice no.",  # our invoice email opening line
    "kind regards,\nacme sp. z o.o.",  # our email signature block
    "operator@acme-forwarding.example",  # operator's personal mailbox in Fwd originals
)

# PDF filename keywords for filtering attachments
_PDF_ORDER_KEYWORDS = (
    "purchase",
    "tc4",
    "tc5",
    "zlecenie",
    "order",
    "auftrag",
    "spedycyjne",
)
_PDF_INVOICE_KEYWORDS = ("faktura", "fv", "rechnung", "invoice")


def _is_carrier_invoice(subject: str) -> bool:
    s = subject.lower()
    return any(k in s for k in _INVOICE_KEYWORDS)


def _decode_mime(value):
    if not value:
        return ""
    try:
        parts = decode_header(value)
        decoded = []
        for part, enc in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(enc or "utf-8", errors="ignore"))
            else:
                decoded.append(str(part))
        return "".join(decoded).strip()
    except Exception:
        return value


def _extract_plain_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="ignore"
                    )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
    return ""


def _save_pdf_attachments(msg, email_uid):
    pdf_paths = []
    for part in msg.walk():
        filename = _decode_mime(part.get_filename())
        if not filename or not filename.lower().endswith(".pdf"):
            continue
        file_bytes = part.get_payload(decode=True)
        if not file_bytes:
            continue
        safe_filename = filename.replace("/", "-").replace("\\", "-")
        path = ATTACHMENTS_DIR / f"{email_uid}_{safe_filename}"
        with open(path, "wb") as f:
            f.write(file_bytes)
        pdf_paths.append(str(path))
    return pdf_paths


def _filter_pdfs(pdf_paths: list, email_type: str) -> list:
    """
    Filters PDF attachments by filename keywords.
    If only 1 PDF — always return it as-is.
    email_type: "order" or "invoice"
    """
    if len(pdf_paths) <= 1:
        return pdf_paths

    keywords = _PDF_INVOICE_KEYWORDS if email_type == "invoice" else _PDF_ORDER_KEYWORDS
    matched = [
        p for p in pdf_paths if any(kw in Path(p).name.lower() for kw in keywords)
    ]
    return matched if matched else pdf_paths


def _select_folder(mail, folder):
    try:
        status, _ = mail.select(f'"{folder}"', readonly=True)
        if status == "OK":
            print(f"📂 Opened folder: {folder}")
            return True
    except Exception:
        pass
    print(f"⚠️ Could not open folder: {folder}")
    return False


def _process_folder(
    mail,
    folder,
    since_date,
    processed_uids,
    saved,
    limit,
    existing_uids: set | None = None,
) -> tuple:
    """Processes one folder. Returns (updated_saved_count, list_of_new_email_dicts).

    Two-phase fetch: cheap header-only check first, full RFC822 only for new emails.
    existing_uids — set of Message-ID strings already in emails.json (built once by caller).
    """
    new_emails = []
    if not _select_folder(mail, folder):
        return saved, new_emails

    for sender_email in ALLOWED_SENDERS:
        if saved >= limit:
            break

        criteria = f'(SINCE {since_date} FROM "{sender_email}")'
        status, messages = mail.search(None, criteria)

        if status != "OK" or not messages[0]:
            print(f"  ℹ️ No emails from {sender_email} in {folder}")
            continue

        email_ids = messages[0].split()
        print(f"  📨 Found {len(email_ids)} emails from {sender_email} in {folder}")

        for email_id in email_ids[-(limit - saved) :]:
            uid_raw = (
                email_id.decode() if isinstance(email_id, bytes) else str(email_id)
            )
            # Unique key = folder + uid (to avoid mixing INBOX uid with another folder)
            uid_key = f"{folder}:{uid_raw}"
            if uid_key in processed_uids:
                continue
            processed_uids.add(uid_key)

            # ── Phase 1: header-only fetch (MESSAGE-ID + SUBJECT) — cheap duplicate/skip check ──
            if existing_uids is not None:
                _, hdr_data = mail.fetch(
                    email_id, "(BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT)])"
                )
                if hdr_data and hdr_data[0]:
                    hdr_msg = email.message_from_bytes(hdr_data[0][1])
                    early_msg_id = (hdr_msg.get("Message-ID") or "").strip()
                    early_subj = _decode_mime(hdr_msg.get("Subject", "")).lower()
                    # Skip already-saved emails
                    if early_msg_id and early_msg_id in existing_uids:
                        print(f"  ⏭ Already saved (header check): {early_msg_id[:60]}")
                        continue
                    # Skip our own outgoing emails forwarded back — no RFC822 needed
                    if any(pat in early_subj for pat in _OWN_SUBJECT_SKIP):
                        print(f"  ⏭ Our outgoing (subject skip): {early_subj[:60]}")
                        continue

            # ── Phase 2: full RFC822 — only for emails not yet in storage ──
            _, msg_data = mail.fetch(email_id, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            subject = _decode_mime(msg.get("Subject", ""))
            sender = _decode_mime(msg.get("From", ""))
            body = _extract_plain_body(msg)

            # Own emails — skip
            if any(s in sender.lower() for s in IGNORED_SENDERS):
                print(f"  ⏭ Own email: {subject[:50]}")
                continue

            # Our sent client invoices (legacy pattern) — skip
            if "Invoice nr" in subject and "POD" in subject:
                print(f"  ⏭ Our invoice (legacy): {subject[:50]}")
                continue

            # Our outgoing emails forwarded back — subject check (covers Phase 1 miss when
            # existing_uids=None) or body check for Fwd'd originals from the operator's mailbox
            if any(pat in subject.lower() for pat in _OWN_SUBJECT_SKIP):
                print(f"  ⏭ Our outgoing (subject): {subject[:60]}")
                continue
            _body_lower = body.lower()
            if any(marker in _body_lower for marker in _OWN_BODY_SKIP):
                print(f"  ⏭ Our outgoing (body marker): {subject[:60]}")
                continue

            # Use Message-ID as stable uid for deduplication
            msg_id = msg.get("Message-ID", uid_key).strip()
            email_type = "invoice" if _is_carrier_invoice(subject) else "order"
            pdf_paths = _filter_pdfs(_save_pdf_attachments(msg, uid_raw), email_type)

            added = add_email(
                {
                    "subject": subject,
                    "from": sender,
                    "body": body,
                    "pdf_paths": pdf_paths,
                    "email_uid": msg_id,
                    "doc_type": email_type,
                    "status": "new",
                    "received_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
                }
            )

            if added:
                saved += 1
                new_emails.append(
                    {
                        "subject": subject,
                        "from": sender,
                        "email_uid": msg_id,
                        "doc_type": email_type,
                    }
                )
                print(f"  ✅ Saved: {subject[:60]}")
            else:
                print(f"  ♻️ Duplicate: {subject[:60]}")

            # After saving — classify and parse. Wrapped in try/except so
            # one AI failure does not abort the rest of the IMAP loop.
            try:
                from ai_parser import classify_email, parse_order, parse_invoice
                from storage import load_emails, save_emails

                all_emails = load_emails()
                changed = False
                for em in all_emails:
                    parsed = em.get("parsed")
                    if not parsed or parsed == {}:
                        subj = em.get("subject", "")
                        body_text = em.get("body", "")
                        sender_email = em.get("from", "")
                        pdfs = em.get("pdf_paths", [])
                        classification = classify_email(
                            subj, body_text, "", sender_email
                        )
                        if classification == "order":
                            parsed_data = parse_order(
                                subj,
                                body_text,
                                "order",
                                pdfs,
                                from_email=sender_email,
                            )
                        elif classification == "carrier_invoice":
                            parsed_data = parse_invoice(body_text, "", pdfs)
                        else:
                            parsed_data = {}
                        em["doc_type"] = classification
                        em["parsed"] = parsed_data
                        changed = True
                if changed:
                    save_emails(all_emails)
            except Exception as _parse_exc:
                print(f"  ⚠️ Parse error (email still saved): {_parse_exc}")

    return saved, new_emails


def check_mail_once() -> list:
    import threading
    from ai_parser import parse_order, parse_invoice
    from storage import save_emails, load_emails

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(EMAIL, EMAIL_PASSWORD)

    since_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%d-%b-%Y")
    saved = 0
    processed_uids = set()
    new_emails = []

    # Build once — used for cheap header-only duplicate check in _process_folder
    existing_uids = {em.get("email_uid", "") for em in load_emails()}

    folders_to_process = SOURCE_FOLDERS

    for folder in folders_to_process:
        if saved >= BATCH_LIMIT:
            break
        saved, folder_new = _process_folder(
            mail,
            folder,
            since_date,
            processed_uids,
            saved,
            BATCH_LIMIT,
            existing_uids=existing_uids,
        )
        new_emails.extend(folder_new)

    mail.logout()

    # Перевірка при завантаженні: якщо parsed пустий, але doc_type відомий, запускаємо повторний парсинг
    def reparse_missing():
        all_emails = load_emails()
        changed = False
        for em in all_emails:
            old_parsed = em.get("parsed") or {}
            doc_type = em.get("doc_type", "")
            invoice_ok = (
                doc_type == "carrier_invoice"
                and old_parsed.get("invoice_number")
                and old_parsed.get("amount_netto") not in (None, 0, 0.0)
                and old_parsed.get("carrier_name")
            )
            order_ok = doc_type == "order" and old_parsed.get("client_order_number")
            if not (invoice_ok or order_ok) and doc_type in (
                "order",
                "carrier_invoice",
            ):
                old_key = (
                    old_parsed.get("invoice_number")
                    if doc_type == "carrier_invoice"
                    else old_parsed.get("client_order_number")
                )
                subj = em.get("subject", "")
                body = em.get("body", "")
                pdfs = em.get("pdf_paths", [])
                if doc_type == "order":
                    parsed_data = parse_order(
                        subj,
                        body,
                        doc_type,
                        pdfs,
                        from_email=em.get("from", ""),
                    )
                else:
                    parsed_data = parse_invoice(body, "", pdfs)
                em["parsed"] = parsed_data
                # If key field appeared that was missing → reset status so task re-runs
                new_key = (parsed_data or {}).get(
                    "invoice_number"
                    if doc_type == "carrier_invoice"
                    else "client_order_number"
                )
                if not old_key and new_key and em.get("status") == "done":
                    em["status"] = "new"
                changed = True
        if changed:
            save_emails(all_emails)

    threading.Thread(target=reparse_missing, daemon=True).start()

    print(f"📬 Total saved: {saved} emails")
    return new_emails
