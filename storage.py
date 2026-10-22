"""
storage.py — зберігання листів у локальному JSON файлі.
"""

import json
from datetime import datetime
from pathlib import Path
from config import DATA_FILE


def _data_path() -> Path:
    base = Path(__file__).resolve().parent
    p = base / DATA_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def load_emails() -> list:
    path = _data_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    patched = False
    for em in data:
        if "received_at" not in em:
            em["received_at"] = "unknown"
            patched = True
    if patched:
        save_emails(data)
    return data


def save_emails(data: list):
    path = _data_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_email(item: dict):
    data = load_emails()
    # Перевірка дублів по email_uid
    uid = item.get("email_uid")
    if uid and any(e.get("email_uid") == uid for e in data):
        return False   # вже є
    item.setdefault("received_at", datetime.now().strftime("%d.%m.%Y %H:%M"))
    data.append(item)
    save_emails(data)
    return True
