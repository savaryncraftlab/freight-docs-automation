"""
screenshot.py — opens the real UI with fictional demo data and saves screenshots.

Nothing is sent anywhere: mail, Google Sheets/Drive and AI calls are not used.
Background watchers are switched off and the e-mail list comes from demo/emails.json.

Usage:
  python demo/screenshot.py
Result:
  docs/screenshot-order.png, docs/screenshot-invoice.png
"""

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import storage
import loads_cache
import cmr_watcher
import invoice_checker

# Work on a temporary copy so the demo file never changes
_tmp = Path(tempfile.mkdtemp()) / "emails.json"
shutil.copy(ROOT / "demo" / "emails.json", _tmp)
storage.DATA_FILE = _tmp

# No Google Sheets in demo: duplicate check finds nothing, watchers do not start
loads_cache.init = lambda: None
loads_cache.is_duplicate = lambda _nr: None
cmr_watcher.start_watcher = lambda: None
invoice_checker.start_watcher = lambda: None

import app as ui  # noqa: E402  (import after the patches above)

DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)


def main():
    qapp = QApplication(sys.argv)
    win = ui.MainWindow()
    win.resize(1440, 900)
    win.show()
    mail = win.mail_tab

    def shot_order():
        mail.list_widget.setCurrentRow(0)
        QTimer.singleShot(400, lambda: _save("screenshot-order.png", shot_invoice))

    def shot_invoice():
        mail._set_filter("Invoices")
        mail.list_widget.setCurrentRow(0)
        QTimer.singleShot(400, lambda: _save("screenshot-invoice.png", qapp.quit))

    def _save(name, then):
        path = DOCS / name
        win.grab().save(str(path))
        print(f"saved {path}")
        then()

    QTimer.singleShot(800, shot_order)
    qapp.exec()


if __name__ == "__main__":
    main()
