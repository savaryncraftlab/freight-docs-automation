from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

# ─── AI ───────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # optional fallback

# ─── Email ────────────────────────────────────
EMAIL = os.getenv("EMAIL", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_FOLDER = os.getenv("EMAIL_FOLDER", "logistics")

# ─── Google Sheets ────────────────────────────
LOADS_INFO_SHEET_ID = os.getenv("LOADS_INFO_SHEET_ID", "")
PAYMENT_PLAN_SHEET_ID = os.getenv("PAYMENT_PLAN_SHEET_ID", "")

# ─── Google Drive ─────────────────────────────
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID", "")

# ─── Paths ────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
DATA_FILE = BASE_DIR / "bot_data" / "emails.json"
ATTACHMENTS_DIR = BASE_DIR / "bot_data" / "attachments"
CMR_WATCH_FOLDER = BASE_DIR / "bot_data" / "cmr"
INVOICE_WATCH_FOLDER = BASE_DIR / "bot_data" / "invoices_out"

# ─── Telegram (optional) ───────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── WhatsApp CMR watcher ─────────────────────
# Folder where WhatsApp Desktop saves downloaded files (usually ~/Downloads)
DOWNLOADS_WATCH_FOLDER = Path(
    os.getenv("DOWNLOADS_WATCH_FOLDER", str(Path.home() / "Downloads"))
)

# ─── Month names for Drive folders ───────────
MONTH_NAMES = {
    1: "Styczeń",
    2: "Luty",
    3: "Marzec",
    4: "Kwiecień",
    5: "Maj",
    6: "Czerwiec",
    7: "Lipiec",
    8: "Sierpień",
    9: "Wrzesień",
    10: "Październik",
    11: "Listopad",
    12: "Grudzień",
}

# ─── Carrier payment rules ────────────────────
# payment_days_rule:  "fixed_14" | "fixed_30" | "own_conditional"
# payment_type_rule:  "scans" | "own_conditional"
#
# own_conditional (OWN1/OWN2) — derived from client terms:
#   client 60 days scans          → carrier 30 days originals
#   client 45 days scans          → carrier 30 days scans
#   client 45–60 days originals   → carrier 30 days originals
CARRIERS = {
    "KWDL": {
        "payment_days_rule": "fixed_14",
        "payment_type_rule": "scans",
        "name": "Kowalski Trans Jan Kowalski",
        "license_plates": "BK0001AA/BK0002X, LU0001A/LU0002B",
    },
    "KWD": {
        "payment_days_rule": "fixed_14",
        "payment_type_rule": "scans",
        "name": "Kowalski Trans Jan Kowalski",
        "license_plates": "DW0001A",
    },
    "LKW4": {
        "payment_days_rule": "fixed_30",
        "payment_type_rule": "scans",
        "name": "Adam Nowak Transport Usługi",
        "license_plates": "WPI00001/WX0001A",
    },
    "LKW9": {
        "payment_days_rule": "fixed_30",
        "payment_type_rule": "scans",
        "name": "Adam Nowak Transport Usługi",
        "license_plates": "WA0001A/WX0002B",
    },
    "PWT": {
        "payment_days_rule": "fixed_30",
        "payment_type_rule": "scans",
        "name": "PW-TRANS Piotr Wiśniewski",
        "license_plates": "WX00001/DX00001",
    },
    "OLA": {
        "payment_days_rule": "fixed_30",
        "payment_type_rule": "scans",
        "name": "FHU Ola Speed Marek Zieliński",
        "license_plates": "KNS0001A/KNS0002B",
    },
    "OWN1": {
        "payment_days_rule": "own_conditional",
        "payment_type_rule": "own_conditional",
        "name": "ACME",
        "license_plates": "WX0001M/WX0001YA",
    },
    "OWN2": {
        "payment_days_rule": "own_conditional",
        "payment_type_rule": "own_conditional",
        "name": "ACME",
        "license_plates": "WX0002M/WX0002YA",
    },
    "THETA": {
        "payment_days_rule": "fixed_45",
        "payment_type_rule": "scans",
        "name": "THETA HOLDING SP. Z O.O.",
        "license_plates": "CTR00001/SP001",
    },
}
