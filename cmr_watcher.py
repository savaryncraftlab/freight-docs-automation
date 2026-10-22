import time
import shutil
import threading
from pathlib import Path
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import CMR_WATCH_FOLDER, EMAIL
from actions import _get_oauth_creds


class CMRWatcher:
    def __init__(self, watch_folder: str, poll_interval: int = 120):
        self.watch_folder = Path(watch_folder)
        self.processed_folder = self.watch_folder / "processed"
        self.poll_interval = poll_interval
        self.creds = _get_oauth_creds()
        self.gmail_service = build("gmail", "v1", credentials=self.creds)
        self._ensure_folders()

    def _ensure_folders(self):
        self.watch_folder.mkdir(parents=True, exist_ok=True)
        self.processed_folder.mkdir(parents=True, exist_ok=True)

    def _list_pdfs(self):
        return list(self.watch_folder.glob("*.pdf"))

    def _extract_order_number(self, filename: str) -> Optional[str]:
        # Extract order number from filename like CMR_TC400002.pdf or cmr_2026-03-00087.pdf
        name = Path(filename).stem
        parts = name.split("_", 1)
        if len(parts) == 2:
            return parts[1]
        return None

    def _search_draft_by_order(self, order_number: str) -> Optional[str]:
        """
        Search Gmail drafts for a draft whose subject contains the order_number.
        Returns draft id or None if not found.
        """
        try:
            response = self.gmail_service.users().drafts().list(userId="me").execute()
            drafts = response.get("drafts", [])
            for draft in drafts:
                draft_id = draft.get("id")
                draft_detail = (
                    self.gmail_service.users()
                    .drafts()
                    .get(userId="me", id=draft_id)
                    .execute()
                )
                msg = draft_detail.get("message", {})
                headers = msg.get("payload", {}).get("headers", [])
                subject = ""
                for h in headers:
                    if h.get("name", "").lower() == "subject":
                        subject = h.get("value", "")
                        break
                if order_number in subject:
                    return draft_id
        except HttpError as e:
            print(f"⚠️ Gmail API error during draft search: {e}")
        return None

    def _attach_pdf_to_draft(self, draft_id: str, pdf_path: Path) -> bool:
        """
        Attach PDF file to existing Gmail draft by replacing its message with attachment.
        """
        import base64
        from email.mime.multipart import MIMEMultipart
        from email.mime.application import MIMEApplication
        from email.mime.text import MIMEText
        from email.utils import formatdate

        try:
            draft_detail = (
                self.gmail_service.users()
                .drafts()
                .get(userId="me", id=draft_id)
                .execute()
            )
            msg = draft_detail.get("message", {})
            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            subject = ""
            to = ""
            for h in headers:
                if h.get("name", "").lower() == "subject":
                    subject = h.get("value", "")
                elif h.get("name", "").lower() == "to":
                    to = h.get("value", "")

            # Create new MIME message with attachment
            mime_msg = MIMEMultipart()
            mime_msg["To"] = to or EMAIL
            mime_msg["Subject"] = subject
            mime_msg["Date"] = formatdate(localtime=True)

            # Add a simple text part
            mime_msg.attach(MIMEText("Attached CMR document.", "plain"))

            # Attach PDF
            with open(pdf_path, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="pdf")
                part.add_header(
                    "Content-Disposition", "attachment", filename=pdf_path.name
                )
                mime_msg.attach(part)

            raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()

            updated_msg = {
                "message": {
                    "raw": raw,
                    "threadId": msg.get("threadId"),
                }
            }
            self.gmail_service.users().drafts().update(
                userId="me", id=draft_id, body=updated_msg
            ).execute()
            return True
        except Exception as e:
            print(f"⚠️ Failed to attach PDF to draft: {e}")
            return False

    def _process_pdf(self, pdf_path: Path):
        order_number = self._extract_order_number(pdf_path.name)
        if not order_number:
            print(f"⚠️ Cannot extract order number from filename: {pdf_path.name}")
            return

        draft_id = self._search_draft_by_order(order_number)
        if draft_id:
            success = self._attach_pdf_to_draft(draft_id, pdf_path)
            if success:
                dest = self.processed_folder / pdf_path.name
                shutil.move(str(pdf_path), str(dest))
                print(f" CMR attached to draft: {order_number}")
            else:
                print(f"⚠️ Failed to attach CMR to draft: {order_number}")
        else:
            print(f" No matching draft for CMR: {pdf_path.name}")

    def run(self):
        while True:
            pdfs = self._list_pdfs()
            for pdf in pdfs:
                self._process_pdf(pdf)
            time.sleep(self.poll_interval)


def start_watcher():
    watcher = CMRWatcher(CMR_WATCH_FOLDER)
    thread = threading.Thread(target=watcher.run, daemon=True)
    thread.start()
    print(f"CMR watcher started, monitoring folder: {CMR_WATCH_FOLDER}")


if __name__ == "__main__":
    start_watcher()
    while True:
        time.sleep(60)
