"""
telegram_cmr.py — Telegram bot that receives CMR photos from drivers.

Flow:
  1. Driver sends photo to Telegram bot with caption = order number
     (e.g., "TC400002" or "2026-04-00059")
  2. Bot downloads photo → converts to PDF → saves to bot_data/cmr/
  3. Existing CMRWatcher picks it up and attaches to Gmail draft

Uses only `requests` (no python-telegram-bot dependency).
Requires TELEGRAM_BOT_TOKEN in .env.
"""

import io
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from config import TELEGRAM_BOT_TOKEN, CMR_WATCH_FOLDER

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_POLL_INTERVAL = 10  # seconds between polls


def _get_updates(offset: int = 0, timeout: int = 30) -> list:
    """Long-poll Telegram for new messages."""
    try:
        resp = requests.get(
            f"{_API_BASE}/getUpdates",
            params={"offset": offset, "timeout": timeout},
            timeout=timeout + 5,
        )
        data = resp.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception as e:
        print(f"⚠️ Telegram poll error: {e}")
        return []


def _download_file(file_id: str) -> bytes | None:
    """Download a file from Telegram by file_id."""
    try:
        resp = requests.get(f"{_API_BASE}/getFile", params={"file_id": file_id})
        data = resp.json()
        if not data.get("ok"):
            return None
        file_path = data["result"]["file_path"]
        dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        dl_resp = requests.get(dl_url, timeout=30)
        dl_resp.raise_for_status()
        return dl_resp.content
    except Exception as e:
        print(f"⚠️ Telegram download error: {e}")
        return None


def _send_reply(chat_id: int, text: str):
    """Send a text reply to the user."""
    try:
        requests.post(
            f"{_API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def _photo_to_pdf(image_bytes: bytes) -> bytes:
    """Convert image bytes to a single-page PDF."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    pdf_buffer = io.BytesIO()
    img.save(pdf_buffer, format="PDF", resolution=150.0)
    return pdf_buffer.getvalue()


def _extract_order_number(caption: str) -> str | None:
    """
    Extract order number from message caption.

    Accepts:
      - "TC400002"
      - "2026-04-00059"
      - "Z/62/ABC/4/2026/XY"
      - Plain number like "59" (will be used as-is)
    """
    text = (caption or "").strip()
    if not text:
        return None
    # Take first non-empty line, first token
    first_token = text.split()[0].strip()
    # Remove any surrounding punctuation
    first_token = first_token.strip(".,;:!?()[]{}\"'")
    return first_token if first_token else None


def _process_update(update: dict, log_fn=None):
    """Process a single Telegram update — extract photo/document, save as CMR PDF."""
    log = log_fn or print
    msg = update.get("message", {})
    if not msg:
        return

    chat_id = msg.get("chat", {}).get("id")
    caption = msg.get("caption", "")

    # Determine file_id: photo (highest res) or document
    file_id = None
    is_photo = False
    if msg.get("photo"):
        # photo is a list of PhotoSize objects — last one is highest resolution
        file_id = msg["photo"][-1]["file_id"]
        is_photo = True
    elif msg.get("document"):
        file_id = msg["document"]["file_id"]
        # If it's already a PDF, no conversion needed
        is_photo = not (msg["document"].get("mime_type", "") == "application/pdf")

    if not file_id:
        return  # not a photo or document — ignore

    order_nr = _extract_order_number(caption)
    if not order_nr:
        _send_reply(
            chat_id,
            "⚠️ Напиши номер ордера в підпис до фото (caption).\nНаприклад: TC400002",
        )
        log(f"⚠️ Telegram CMR: photo without caption from chat {chat_id}")
        return

    # Download file
    file_bytes = _download_file(file_id)
    if not file_bytes:
        _send_reply(chat_id, f"❌ Не вдалось завантажити файл для {order_nr}")
        return

    # Convert to PDF if it's a photo
    if is_photo:
        try:
            pdf_bytes = _photo_to_pdf(file_bytes)
        except Exception as e:
            _send_reply(chat_id, f"❌ Помилка конвертації фото: {e}")
            log(f"❌ Telegram CMR: photo conversion error: {e}")
            return
    else:
        pdf_bytes = file_bytes

    # Save to CMR watch folder
    cmr_dir = Path(CMR_WATCH_FOLDER)
    cmr_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H%M%S")
    filename = f"CMR_{order_nr}_{timestamp}.pdf"
    out_path = cmr_dir / filename

    with open(out_path, "wb") as f:
        f.write(pdf_bytes)

    _send_reply(chat_id, f"✅ CMR збережено: {filename}\nОрдер: {order_nr}")
    log(f"✅ Telegram CMR: saved {filename} ({len(pdf_bytes)} bytes)")


def run_telegram_cmr(log_fn=None):
    """
    Main polling loop — runs forever in a background thread.
    Receives photos/documents from Telegram, converts to PDF, saves to bot_data/cmr/.
    """
    log = log_fn or print

    if not TELEGRAM_BOT_TOKEN:
        log("⚠️ Telegram CMR: TELEGRAM_BOT_TOKEN not set — skipping")
        return

    log(f"🤖 Telegram CMR bot started (polling every {_POLL_INTERVAL}s)")
    offset = 0

    while True:
        try:
            updates = _get_updates(offset=offset, timeout=_POLL_INTERVAL)
            for upd in updates:
                _process_update(upd, log_fn=log)
                offset = upd["update_id"] + 1
        except Exception as e:
            log(f"⚠️ Telegram CMR loop error: {e}")
            time.sleep(5)


def start_telegram_cmr(log_fn=None):
    """Start the Telegram CMR bot in a background daemon thread."""
    thread = threading.Thread(target=run_telegram_cmr, args=(log_fn,), daemon=True)
    thread.start()
    return thread
