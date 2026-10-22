"""
app.py — Main UI on PyQt6.

Sidebar navigation:
  1. Emails    — email list, details, AI parsing, processing
  2. Dashboard — today's summary, overdue payments, pending tasks
  3. Log       — real-time task progress
  4. Settings  — connection testing

Usage:
  python main.py
"""

import sys
import threading
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QPushButton,
    QDoubleSpinBox,
    QCheckBox,
    QTextEdit,
    QGroupBox,
    QStatusBar,
    QMessageBox,
    QFormLayout,
    QTableWidget,
    QTableWidgetItem,
    QFrame,
    QScrollArea,
    QSizePolicy,
)
from PyQt6.QtCore import (
    Qt,
    pyqtSignal,
    QObject,
    QTimer,
    QSize,
    QMetaObject,
    QPointF,
    QRectF,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QBrush

from storage import load_emails, save_emails
from executor import task_queue, register_progress_callback
from task import Task
import cmr_watcher
import invoice_checker
from config import CMR_WATCH_FOLDER, INVOICE_WATCH_FOLDER


# ─── Design Tokens (02 Charcoal Teal) ────────
_BG_BASE = "#0a0e1a"  # deep navy-charcoal (main bg)
_BG_SURFACE = "#101828"  # sidebar / right panel
_BG_CARD = "#151f32"  # email cards & data cards
_BG_HOVER = "#1c2a42"  # hover state
_BG_ACTIVE = "#172e40"  # selected / active state
_BORDER = "#1c2640"  # subtle border
_BORDER_HI = "#283d58"  # highlighted border
_ACCENT = "#14b8a6"  # teal — primary accent
_ACCENT_SOFT = "#0d9488"  # darker teal
_SUCCESS = "#10b981"  # emerald green
_WARNING = "#f59e0b"  # amber
_DANGER = "#ef4444"  # red
_GOLD = "#d4a843"  # prices & financial amounts
_TEXT = "#e2e8f0"  # primary text
_TEXT_SEC = "#94a3b8"  # secondary text
_TEXT_MUTED = "#4b5e7a"  # muted / tertiary text

# ─── Carrier badge styles (bg, text color, optional border) ─────────────────
# Regular carriers: solid colored background + dark text (high contrast)
# OWN1/OWN2: OUTLINE style (dark bg + colored border + colored text)
#              → instantly signals "OWN VEHICLE" vs hired carrier
_CARRIER_BADGE: dict[str, dict] = {
    "KWD":  {"bg": "#D9D2E9", "fg": "#2d1b69"},          # lavender → dark violet text
    "KWDL": {"bg": "#C27BA0", "fg": "#ffffff"},           # pink     → white text
    "LKW9": {"bg": "#FFD966", "fg": "#4a3000"},           # yellow   → dark brown text
    "LKW4": {"bg": "#BF9000", "fg": "#ffffff"},           # gold     → white text
    "OLA": {"bg": "#9FC5E8", "fg": "#0d2b4a"},           # sky blue → dark navy text
    "PWT":  {"bg": "#CFE2F3", "fg": "#0d2b4a"},           # pale blue→ dark navy text
    "OWN1":{"bg": "#1c1c2e", "fg": "#E69138", "border": "#E69138"},  # outline orange
    "OWN2":{"bg": "#1c1c2e", "fg": "#F6B26B", "border": "#F6B26B"},  # outline peach
}
# Keep _CARRIER_COLORS for backward compat (used in actions.py indirectly via config)
_CARRIER_COLORS = {k: v["bg"] for k, v in _CARRIER_BADGE.items()}


def _carrier_badge_html(code: str) -> str:
    """Return styled HTML <span> for the carrier code badge."""
    style = _CARRIER_BADGE.get(code)
    if not style:
        # Unknown carrier: neutral grey badge
        return (
            f"<span style='color:#e2e8f0;font-weight:700;padding:3px 10px;"
            f"background:#334155;border-radius:5px;"
            f"font-family:\"Segoe UI Black\",\"Arial Black\",Arial,sans-serif;"
            f"font-size:11px;letter-spacing:1px;'>{code}</span>"
        )
    bg = style["bg"]
    fg = style["fg"]
    border_css = (
        f"border:1.5px solid {style['border']};"
        if "border" in style else "border:none;"
    )
    return (
        f"<span style='color:{fg};font-weight:900;padding:3px 10px;"
        f"background:{bg};border-radius:5px;{border_css}"
        f"font-family:\"Segoe UI Black\",\"Arial Black\",Arial,sans-serif;"
        f"font-size:11px;letter-spacing:1px;'>{code}</span>"
    )

# ─── Font Families ────────────────────────────
# TAG    → bold uppercase badges (Segoe UI: most readable on Windows at all sizes)
# BODY   → main UI text (Segoe UI: clean, ClearType-optimized)
# HEADER → detail panel titles (Georgia: classic serif, excellent screen rendering)
# MONO   → metadata, timestamps (Cascadia Mono: modern, wide glyphs, very clear)
_FONT_TAG = "'Segoe UI', 'Arial', 'Helvetica', sans-serif"
_FONT_BODY = "'Segoe UI', 'Helvetica Neue', 'Arial', sans-serif"
_FONT_HEADER = "'Georgia', 'Cambria', 'Times New Roman', serif"
_FONT_MONO = "'Cascadia Mono', 'Consolas', 'Courier New', monospace"

STYLE = f"""
/* ===== BASE ===== */
QMainWindow {{ background: {_BG_BASE}; }}
QWidget {{ color: {_TEXT}; font-family: {_FONT_BODY}; font-size: 13px; }}

/* ===== LISTS ===== */
QListWidget {{
    background: transparent; border: none; outline: none;
}}
QListWidget::item {{
    padding: 4px 6px; margin: 1px 0; border-radius: 6px; color: {_TEXT};
}}
QListWidget::item:hover {{ background: {_BG_HOVER}; }}
QListWidget::item:selected {{ background: {_BG_ACTIVE}; }}

/* ===== BUTTONS ===== */
QPushButton {{
    background: {_BG_CARD}; color: {_TEXT}; border: 1px solid {_BORDER};
    border-radius: 8px; padding: 7px 18px; font-weight: 600; font-family: {_FONT_BODY};
}}
QPushButton:hover {{ background: {_BG_HOVER}; border-color: {_ACCENT}; }}
QPushButton:pressed {{ background: {_BG_BASE}; }}
QPushButton:disabled {{ color: {_TEXT_MUTED}; background: {_BG_SURFACE}; border-color: {_BG_SURFACE}; }}

/* ===== INPUTS ===== */
QTextEdit, QLineEdit {{
    background: {_BG_SURFACE}; border: 1px solid {_BORDER}; border-radius: 6px;
    padding: 8px; color: {_TEXT}; selection-background-color: {_BG_ACTIVE}; font-family: {_FONT_BODY};
}}
QTextEdit:focus, QLineEdit:focus {{ border-color: {_ACCENT}; }}

/* ===== SCROLLBAR (macOS-style) ===== */
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 4px 1px; }}
QScrollBar::handle:vertical {{ background: rgba(148,163,184,0.2); border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(148,163,184,0.4); }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}

/* ===== GROUPS ===== */
QGroupBox {{
    border: 1px solid {_BORDER}; border-radius: 8px; margin-top: 16px;
    padding: 20px 12px 12px 12px; font-weight: 600; color: {_TEXT_SEC};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 16px; padding: 0 6px; }}

/* ===== SPLITTER ===== */
QSplitter::handle {{ background: {_BORDER}; width: 1px; }}

/* ===== TABLE ===== */
QTableWidget {{ background: {_BG_SURFACE}; border: 1px solid {_BORDER}; border-radius: 6px; gridline-color: {_BORDER}; }}
QTableWidget::item {{ padding: 6px 10px; font-family: {_FONT_BODY}; }}
QHeaderView::section {{ background: {_BG_CARD}; border: none; border-bottom: 1px solid {_BORDER}; padding: 8px 10px; font-weight: 600; color: {_TEXT_SEC}; }}

/* ===== CHECKBOX ===== */
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 2px solid {_BORDER_HI}; background: {_BG_SURFACE}; }}
QCheckBox::indicator:hover {{ border-color: {_ACCENT}; }}
QCheckBox::indicator:checked {{ background: {_ACCENT}; border-color: {_ACCENT}; }}

/* ===== COMBOBOX ===== */
QComboBox {{
    background: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;
    padding: 5px 12px; color: {_TEXT}; min-height: 24px; font-family: {_FONT_BODY};
}}
QComboBox:hover {{ border-color: {_ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {_BG_CARD}; border: 1px solid {_BORDER}; color: {_TEXT};
    selection-background-color: {_BG_ACTIVE};
}}

/* ===== SPINBOX ===== */
QDoubleSpinBox {{
    background: {_BG_CARD}; border: 1px solid {_BORDER}; border-radius: 6px;
    padding: 4px 8px; color: {_TEXT};
}}

/* ===== STATUS BAR ===== */
QStatusBar {{ background: {_BG_BASE}; border-top: 1px solid {_BORDER}; color: {_TEXT_SEC}; font-size: 12px; padding: 4px 12px; font-family: {_FONT_MONO}; }}
"""


# Loads Info column indices (0-based, A=0)
_C = {
    "nr": 0,  # A
    "client_order_nr": 1,  # B
    "our_order_nr": 2,  # C
    "company": 3,  # D
    "email": 4,  # E
    "manager": 5,  # F
    "loading_place": 6,  # G
    "unloading_place": 7,  # H
    "loading_date": 8,  # I
    "unloading_date": 9,  # J
    "adr": 10,  # K
    "truck_type": 11,  # L
    "carrier_name": 12,  # M
    "plates": 13,  # N
    "carrier_netto": 14,  # O
    "turnover_netto": 17,  # R
    "invoice_nr_client": 21,  # V
    "date_sent": 22,  # W
    "date_received": 23,  # X
    "client_pay_type": 24,  # Y
    "client_pay_days": 25,  # Z
    "client_pay_date": 26,  # AA
    "payment_received": 27,  # AB  "NO"/"YES"
    "dispatch_date": 28,  # AC
    "invoice_nr_carrier": 29,  # AD
    "date_invoice_email": 30,  # AE
    "date_originals": 31,  # AF
    "carrier_pay_type": 32,  # AG
    "carrier_pay_days": 33,  # AH
    "carrier_pay_date": 34,  # AI
    "paid_to_carrier": 35,  # AJ  "NO"/"YES"
}

STATUS_COLORS = {
    "new": _SUCCESS,
    "processing": _WARNING,
    "done": _TEXT_MUTED,
    "error": _DANGER,
    "ignored": "#334155",
}

DOC_TYPE_LABELS = {
    "order": "ORDER",
    "order_timocom": "TIMOCOM",
    "carrier_invoice": "INVOICE",
    "client_invoice": "CLIENT INV",
    "ignore": "IGNORE",
}

DOC_TYPE_BADGE_COLORS = {
    "order": _SUCCESS,
    "order_timocom": "#10b981",
    "carrier_invoice": _WARNING,
    "client_invoice": _ACCENT,
    "ignore": _TEXT_MUTED,
}

DOC_TYPE_BORDER_COLORS = {
    "order": _SUCCESS,
    "order_timocom": _SUCCESS,
    "carrier_invoice": _WARNING,
    "ignore": _TEXT_MUTED,
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────


def _classify_doc_type(subject: str, sender: str) -> str:
    """Quick classification of email type by subject and sender."""
    s = subject.lower()
    clean = s.replace("fwd:", "").replace("fw:", "").strip()

    if s.strip().startswith("re:"):
        return "ignore"
    if "invoice nr" in s and "pod" in s:
        return "ignore"
    if any(
        x in clean
        for x in [
            "documents accepted",
            "invoice accepted",
            "documents uploaded",
            "payment confirmed",
            "now upload invoice",
        ]
    ):
        return "ignore"
    if "timocom" in clean or "transport offer" in clean:
        return "order_timocom"
    if any(
        x in clean
        for x in [
            "faktura",
            "rechnung",
            "gutschrift",
            "cmr+fv",
            "cmr + fv",
            "zlecenie transportowe",
        ]
    ):
        return "carrier_invoice"
    if any(
        x in clean
        for x in [
            "zlecenie",
            "transportauftrag",
            "transport order",
            "loading order",
            "zamówienie",
            "auftrag",
            "tc48",
            "tc49",
            "tc50",
            "tc45",
            "tc46",
            "tc47",
        ]
    ):
        return "order"
    if clean.startswith("zamówienie - tc") or "tc4" in clean or "tc5" in clean:
        return "order"
    if "faktury brokeralpha" in clean or "faktury tc" in clean:
        return "carrier_invoice"
    return "order"


def _parse_sheet_date(s: str):
    """Parse DD.MM.YYYY -> datetime or None."""
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y")
    except Exception:
        return None


def _col(row: list, key: str) -> str:
    """Extract column value by key name from a Sheets row."""
    idx = _C.get(key, -1)
    if idx < 0 or idx >= len(row):
        return ""
    return str(row[idx]).strip()


# ─────────────────────────────────────────────
# PARSED DATA CARD (HTML)
# ─────────────────────────────────────────────


def _format_parsed_card(parsed: dict, doc_type: str) -> str:
    """Build rich HTML card for parsed order/invoice data."""
    if not parsed or not isinstance(parsed, dict):
        return f"<p style='color:{_TEXT_MUTED}; padding:16px'>No data parsed yet</p>"

    def _fmt_places(places):
        if not places:
            return ""
        return " → ".join(str(p) for p in places)

    def _fmt_date(d):
        if not d:
            return "—"
        return str(d).replace("-", ".")

    from config import CARRIERS as _CARRIERS

    carrier_code = (parsed.get("truck_type") or "").upper().strip()
    if carrier_code in _CARRIERS:
        carrier_display = _CARRIERS[carrier_code]["name"]
    else:
        carrier_display = (parsed.get("carrier_name") or "").strip()

    lp = _fmt_places(parsed.get("loading_places") or [])
    up = _fmt_places(parsed.get("unloading_places") or [])

    _row_style = f"border-bottom: 1px solid {_BORDER};"
    _label_style = (
        f"color:{_TEXT_MUTED}; padding:10px 14px 10px 0; width:110px; "
        f"font-size:11px; text-transform:uppercase; {_row_style}"
    )
    _value_style = f"color:{_TEXT}; padding:10px 0; font-size:13px; {_row_style}"
    _price_style = (
        f"color:{_GOLD}; font-size:16px; font-weight:700; "
        f"font-family:Consolas,monospace; {_row_style}"
    )

    if doc_type in ("order", "order_timocom"):
        _price = parsed.get("price_netto", "")
        _cprice = parsed.get("carrier_price", "")
        return f"""
<div style='font-family:Segoe UI; font-size:13px; padding:4px 0;'>
  <table style='border-collapse:collapse; width:100%;'>
    <tr><td style='{_label_style}'>Client</td>
        <td style='{_value_style} font-weight:600;'>{parsed.get("company_name", "—")}</td></tr>
    <tr><td style='{_label_style}'>Carrier</td>
        <td style='{_value_style}'>{_carrier_badge_html(carrier_code) if carrier_code else "—"} <span style='color:{_TEXT_SEC};font-family:Consolas;'>{carrier_display}</span></td></tr>
    <tr><td style='{_label_style}'>Route</td>
        <td style='{_value_style} font-weight:600; color:{_WARNING}; font-family:Consolas;'>{lp} → {up}</td></tr>
    <tr><td style='{_label_style}'>Loading</td>
        <td style='{_value_style} font-family:Consolas;'>{_fmt_date(parsed.get("loading_date", ""))}</td></tr>
    <tr><td style='{_label_style}'>Unloading</td>
        <td style='{_value_style} font-family:Consolas;'>{_fmt_date(parsed.get("unloading_date", ""))}</td></tr>
    <tr><td style='{_label_style}'>Client price</td>
        <td style='{_price_style}'>{_price} EUR</td></tr>
    <tr><td style='{_label_style}'>Carrier price</td>
        <td style='{_value_style} color:{_TEXT_SEC}; font-family:Consolas;'>{_cprice} EUR</td></tr>
    <tr><td style='{_label_style}'>Payment</td>
        <td style='{_value_style} font-family:Consolas;'>{parsed.get("payment_days", "")} days / {parsed.get("payment_type", "")}</td></tr>
  </table>
</div>
"""
    elif doc_type == "carrier_invoice":
        _cur = parsed.get("currency") or "EUR"
        return f"""
<div style='font-family:Segoe UI; font-size:13px; padding:4px 0;'>
  <table style='border-collapse:collapse; width:100%;'>
    <tr><td style='{_label_style}'>Carrier</td>
        <td style='{_value_style} font-weight:600;'>{parsed.get("carrier_name", "—")}</td></tr>
    <tr><td style='{_label_style}'>Invoice</td>
        <td style='{_value_style} font-weight:700; color:{_ACCENT}; font-family:Consolas;'>{parsed.get("invoice_number", "—")}</td></tr>
    <tr><td style='{_label_style}'>Netto</td>
        <td style='{_price_style}'>{parsed.get("amount_netto", "—")} {_cur}</td></tr>
    <tr><td style='{_label_style}'>Brutto</td>
        <td style='{_value_style} color:{_TEXT_SEC}; font-family:Consolas;'>{parsed.get("amount_brutto", "—")} {_cur}</td></tr>
    <tr><td style='{_label_style}'>Service</td>
        <td style='{_value_style} font-family:Consolas;'>{parsed.get("service_date", "—")}</td></tr>
    <tr><td style='{_label_style}'>Order</td>
        <td style='{_value_style} color:{_WARNING}; font-family:Consolas;'>{parsed.get("order_number", "—")}</td></tr>
  </table>
</div>
"""
    else:
        return f"<p style='color:{_TEXT_MUTED}'>—</p>"


# ─────────────────────────────────────────────
# SIGNALS
# ─────────────────────────────────────────────


class Signals(QObject):
    """Cross-thread signals for UI updates."""

    log_message = pyqtSignal(str)
    status_update = pyqtSignal(str)
    mail_checked = pyqtSignal(int)  # new email count
    task_finished = pyqtSignal(str, str)  # task_id, "done"|"error"
    check_done = pyqtSignal()  # check_mail finished
    email_parsed = pyqtSignal(int)  # idx of parsed email
    dashboard_data = pyqtSignal(dict)  # async dashboard refresh result
    test_result = pyqtSignal(str)  # settings test label update
    reparse_done = pyqtSignal(int)  # count of re-parsed emails


# ─────────────────────────────────────────────
# MAIL TAB
# ─────────────────────────────────────────────


class MailTab(QWidget):
    """Email list + detail panel with processing controls."""

    def __init__(self, signals: Signals):
        super().__init__()
        self.signals = signals
        self.emails: list = []
        self._visible_emails: list = []
        self._task_email_map: dict[str, int] = {}

        self._build_ui()

        # Init duplicate-detection cache in background
        import loads_cache as _lc

        threading.Thread(target=_lc.init, daemon=True, name="loads_cache_init").start()

        self.reload()

        self.signals.task_finished.connect(self._on_task_finished)
        self.signals.check_done.connect(self._check_done)
        self.signals.email_parsed.connect(self._on_email_parsed)
        self.signals.mail_checked.connect(lambda _: self.reload())
        self.signals.reparse_done.connect(self._on_reparse_done)

    def _build_ui(self):
        """Construct the email tab layout."""
        main = QHBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ══════════════════════════════════════════
        # LEFT PANEL — email list
        # ══════════════════════════════════════════
        left = QWidget()
        left.setStyleSheet(f"background: {_BG_BASE};")
        lv = QVBoxLayout(left)
        lv.setSpacing(8)
        lv.setContentsMargins(12, 12, 8, 12)

        # ── Filter tabs (All | Orders | Invoices) ──
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        self._filter_buttons: list[QPushButton] = []
        self._active_filter = "All"
        for label in ("All", "Orders", "Invoices"):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(label == "All")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background:transparent; color:{_TEXT};
                    border:1px solid {_TEXT_SEC}; border-radius:8px;
                    padding:8px 22px; font-weight:600; font-size:13px;
                    font-family: {_FONT_BODY};
                }}
                QPushButton:checked {{
                    background:{_ACCENT}; color:#ffffff;
                    border-color:{_ACCENT};
                }}
                QPushButton:hover:!checked {{
                    background:{_BG_HOVER};
                    border-color:{_TEXT};
                    color:#ffffff;
                }}
            """)
            btn.clicked.connect(lambda checked, lbl=label: self._set_filter(lbl))
            filter_row.addWidget(btn)
            self._filter_buttons.append(btn)

        filter_row.addStretch()

        self.btn_check = QPushButton("Check Mail")
        self.btn_check.setStyleSheet(f"""
            QPushButton {{
                background:{_TEXT}; color:{_BG_BASE};
                border:1px solid {_TEXT}; border-radius:8px;
                padding:8px 22px; font-weight:700; font-size:12px;
                font-family: {_FONT_BODY};
            }}
            QPushButton:hover {{
                background:#cbd5e1; border-color:#cbd5e1;
            }}
            QPushButton:disabled {{ color:{_TEXT_MUTED}; background:{_BG_SURFACE}; border-color:{_BORDER}; }}
        """)
        self.btn_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_check.clicked.connect(self.check_mail)
        filter_row.addWidget(self.btn_check)

        lv.addLayout(filter_row)

        # ── Auto-mode controls (hidden — backend only) ──
        self.chk_auto_check = QCheckBox("Auto")
        self.chk_auto_check.setChecked(False)
        self.chk_auto_check.hide()
        self.chk_auto_check.stateChanged.connect(self._on_auto_check_toggled)

        self.spin_auto_interval = QDoubleSpinBox()
        self.spin_auto_interval.setRange(1, 60)
        self.spin_auto_interval.setValue(5)
        self.spin_auto_interval.setSuffix(" min")
        self.spin_auto_interval.setSingleStep(1)
        self.spin_auto_interval.hide()
        self.spin_auto_interval.valueChanged.connect(self._on_auto_interval_changed)

        self.btn_auto_toggle = QPushButton("OFF")
        self.btn_auto_toggle.setCheckable(True)
        self.btn_auto_toggle.hide()
        self.btn_auto_toggle.clicked.connect(self._on_auto_toggle)

        self.lbl_auto_countdown = QLabel("")
        self.lbl_auto_countdown.hide()

        self.btn_check_invoices = QPushButton("Invoices")
        self.btn_check_invoices.hide()
        self.btn_check_invoices.clicked.connect(self.on_check_invoices)

        # ── Row 2: Date filters + Re-parse ──
        date_row = QHBoxLayout()
        date_row.setSpacing(6)

        self._date_filter_buttons: list[QPushButton] = []
        self._active_date_filter = "All"
        for dlabel in ("All", "Today", "3 days", "Week"):
            dbtn = QPushButton(dlabel)
            dbtn.setCheckable(True)
            dbtn.setChecked(dlabel == "All")
            dbtn.setCursor(Qt.CursorShape.PointingHandCursor)
            dbtn.setFixedHeight(28)
            dbtn.setStyleSheet(f"""
                QPushButton {{
                    background:transparent; color:{_TEXT_SEC};
                    border:1px solid {_BORDER}; border-radius:6px;
                    padding:4px 12px; font-size:11px; font-weight:600;
                    font-family:{_FONT_BODY};
                }}
                QPushButton:checked {{
                    background:{_BG_ACTIVE}; color:{_TEXT};
                    border-color:{_ACCENT};
                }}
                QPushButton:hover:!checked {{
                    background:{_BG_HOVER}; color:{_TEXT};
                }}
            """)
            dbtn.clicked.connect(lambda checked, dl=dlabel: self._set_date_filter(dl))
            date_row.addWidget(dbtn)
            self._date_filter_buttons.append(dbtn)

        date_row.addStretch()

        self.btn_reparse_all = QPushButton("Re-parse")
        self.btn_reparse_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reparse_all.setFixedHeight(28)
        self.btn_reparse_all.setStyleSheet(f"""
            QPushButton {{
                background:transparent; color:{_WARNING};
                border:1px solid {_WARNING}50; border-radius:6px;
                padding:4px 14px; font-size:11px; font-weight:600;
                font-family:{_FONT_BODY};
            }}
            QPushButton:hover {{
                background:{_WARNING}15; border-color:{_WARNING};
            }}
            QPushButton:disabled {{
                color:{_TEXT_MUTED}; border-color:{_BORDER};
            }}
        """)
        self.btn_reparse_all.clicked.connect(self.reparse_all)
        date_row.addWidget(self.btn_reparse_all)

        lv.addLayout(date_row)

        # ── Separator ──
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{_BORDER};")
        lv.addWidget(sep)

        # ── Email list ──
        self.list_widget = QListWidget()
        self.list_widget.setMinimumWidth(440)
        self.list_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.list_widget.setStyleSheet("""
            QListWidget { background:transparent; border:none; outline:none; }
            QListWidget::item { border:none; padding:3px 4px; margin:2px 0; background:transparent; }
            QListWidget::item:hover { background:transparent; }
            QListWidget::item:selected { background:transparent; }
        """)
        self.list_widget.currentRowChanged.connect(self.on_select)
        lv.addWidget(self.list_widget)

        # ── Count label ──
        self.lbl_count = QLabel("0 emails")
        self.lbl_count.setStyleSheet(
            f"color:{_TEXT_MUTED}; font-size:12px; padding:4px 0;"
        )
        lv.addWidget(self.lbl_count)

        # Countdown label (used by auto mode)
        self.lbl_countdown = QLabel("")
        self.lbl_countdown.setStyleSheet(f"color:{_ACCENT}; font-size:11px;")
        self.lbl_countdown.hide()

        splitter.addWidget(left)

        # ══════════════════════════════════════════
        # RIGHT PANEL — detail + actions
        # ══════════════════════════════════════════
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setStyleSheet(f"background:{_BG_SURFACE};")

        right = QWidget()
        right.setStyleSheet(f"background:{_BG_SURFACE};")
        rv = QVBoxLayout(right)
        rv.setSpacing(12)
        rv.setContentsMargins(20, 20, 20, 20)

        # Subject — elegant serif header
        self.lbl_subject = QLabel("Select an email")
        self.lbl_subject.setWordWrap(True)
        self.lbl_subject.setStyleSheet(
            f"color:{_TEXT}; font-family:{_FONT_HEADER}; font-size:20px; font-weight:700;"
        )
        rv.addWidget(self.lbl_subject)

        # From + date (Consolas/Space Mono — monospace metadata)
        self.lbl_from = QLabel("")
        self.lbl_from.setStyleSheet(
            f"color:{_TEXT_SEC}; font-family:{_FONT_MONO}; font-size:12px;"
        )
        rv.addWidget(self.lbl_from)

        # Parsed data card
        self.txt_parsed = QTextEdit()
        self.txt_parsed.setReadOnly(True)
        self.txt_parsed.setMaximumHeight(260)
        self.txt_parsed.setStyleSheet(f"""
            QTextEdit {{
                background:{_BG_CARD}; border:1px solid {_BORDER}; border-radius:8px;
                padding:12px; font-size:13px; color:{_TEXT};
            }}
        """)
        rv.addWidget(self.txt_parsed)

        # Processing options
        opts = QGroupBox("Processing")
        opts.setStyleSheet(f"""
            QGroupBox {{ border:1px solid {_BORDER}; border-radius:8px; margin-top:14px; padding:18px 12px 10px; color:{_TEXT_SEC}; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:14px; padding:0 6px; }}
        """)
        opts_layout = QHBoxLayout(opts)
        opts_layout.setSpacing(16)

        self.chk_sheets = QCheckBox("Sheets")
        self.chk_drive = QCheckBox("Drive")
        self.chk_gmail = QCheckBox("Gmail")
        self.chk_payment_plan = QCheckBox("Payment Plan")
        self.chk_loads_info = QCheckBox("Loads Info")

        for cb in (
            self.chk_sheets,
            self.chk_drive,
            self.chk_gmail,
            self.chk_payment_plan,
            self.chk_loads_info,
        ):
            cb.setChecked(True)

        opts_layout.addWidget(self.chk_sheets)
        opts_layout.addWidget(self.chk_drive)
        opts_layout.addWidget(self.chk_payment_plan)
        opts_layout.addWidget(self.chk_loads_info)
        opts_layout.addStretch()

        self.chk_payment_plan.hide()
        self.chk_loads_info.hide()
        rv.addWidget(opts)

        # Collapsible email body
        self.body_toggle = QPushButton("Email body")
        self.body_toggle.setCheckable(True)
        self.body_toggle.setChecked(False)
        self.body_toggle.setStyleSheet(f"""
            QPushButton {{ text-align:left; font-weight:600; color:{_ACCENT}; border:none; padding:6px 0; background:transparent; font-size:12px; }}
            QPushButton:checked {{ color:{_SUCCESS}; }}
        """)
        self.body_toggle.clicked.connect(self._toggle_body)
        rv.addWidget(self.body_toggle)

        self.txt_body = QTextEdit()
        self.txt_body.setReadOnly(True)
        self.txt_body.setMaximumHeight(140)
        self.txt_body.setVisible(False)
        self.txt_body.setStyleSheet(f"""
            QTextEdit {{ background:{_BG_CARD}; border:1px solid {_BORDER}; border-radius:6px; font-family:Consolas,monospace; font-size:12px; color:{_TEXT_SEC}; padding:8px; }}
        """)
        rv.addWidget(self.txt_body)

        # Carrier invoice verification panel
        self.verif_group = QGroupBox("Invoice Verification")
        self.verif_group.setVisible(False)
        self.verif_group.setStyleSheet(f"""
            QGroupBox {{
                border:1px solid {_BORDER}; border-radius:8px;
                margin-top:14px; padding:18px 14px 10px;
                color:{_TEXT_SEC}; font-family:{_FONT_BODY}; font-size:13px;
                background:{_BG_CARD};
            }}
            QGroupBox::title {{
                subcontrol-origin:margin; left:14px; padding:0 6px;
                color:{_ACCENT}; font-weight:700;
            }}
        """)
        verif_layout = QFormLayout(self.verif_group)
        verif_layout.setSpacing(8)
        verif_layout.setContentsMargins(10, 12, 10, 8)

        self.verif_invoice_nr = QLabel("")
        self.verif_carrier = QLabel("")
        self.verif_amount_netto = QLabel("")
        self.verif_amount_brutto = QLabel("")
        self.verif_service_date = QLabel("")
        self.verif_matched_order = QLabel("")
        self.verif_matched_price = QLabel("")
        self.verif_unloading_date = QLabel("")
        self.verif_status = QLabel("")

        # Labels = muted small mono, Values = bright body font
        _lbl_style = (
            f"color:{_TEXT_MUTED}; font-family:{_FONT_MONO}; font-size:11px; "
            f"font-weight:600; padding-right:12px;"
        )
        _val_style = (
            f"color:{_TEXT}; font-family:{_FONT_BODY}; font-size:13px; font-weight:600;"
        )
        _val_accent = f"color:{_ACCENT}; font-family:{_FONT_MONO}; font-size:13px; font-weight:700;"
        _val_gold = (
            f"color:{_GOLD}; font-family:{_FONT_MONO}; font-size:14px; font-weight:700;"
        )

        for lbl_text, widget, style in [
            ("Invoice #:", self.verif_invoice_nr, _val_accent),
            ("Carrier:", self.verif_carrier, _val_style),
            ("Netto:", self.verif_amount_netto, _val_gold),
            ("Brutto:", self.verif_amount_brutto, _val_style),
            ("Service date:", self.verif_service_date, _val_style),
            ("Matched order:", self.verif_matched_order, _val_accent),
            ("Carrier price:", self.verif_matched_price, _val_gold),
            ("Unloading:", self.verif_unloading_date, _val_style),
            ("Status:", self.verif_status, _val_accent),
        ]:
            widget.setStyleSheet(style)
            row_label = QLabel(lbl_text)
            row_label.setStyleSheet(_lbl_style)
            verif_layout.addRow(row_label, widget)

        self.btn_verif_confirm = QPushButton("Confirm & Write")
        self.btn_verif_override = QPushButton("Override & Write")
        self.btn_verif_confirm.setEnabled(False)
        self.btn_verif_override.setEnabled(False)
        self.btn_verif_confirm.clicked.connect(self._on_confirm_payment_plan)
        self.btn_verif_override.clicked.connect(self._on_override_payment_plan)
        verif_layout.addRow(self.btn_verif_confirm, self.btn_verif_override)

        rv.addWidget(self.verif_group)
        rv.addStretch()

        right_scroll.setWidget(right)

        # ── Action buttons (fixed at bottom — outlined style) ──
        self.btn_process = QPushButton("Start Processing")
        self.btn_process.setStyleSheet(f"""
            QPushButton {{
                background:transparent; color:{_TEXT};
                border:1px solid {_BORDER_HI}; border-radius:10px;
                padding:12px 44px; font-weight:700; font-size:13px;
                font-family:{_FONT_BODY};
            }}
            QPushButton:hover {{
                background:{_ACCENT}15; border-color:{_ACCENT};
                color:{_TEXT};
            }}
            QPushButton:disabled {{ color:{_TEXT_MUTED}; border-color:{_BORDER}; }}
        """)
        self.btn_process.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_process.clicked.connect(self.process_current)
        self.btn_process.setEnabled(False)

        self.btn_ignore = QPushButton("Skip")
        self.btn_ignore.setStyleSheet(f"""
            QPushButton {{
                background:transparent; color:{_TEXT};
                border:1px solid {_BORDER_HI}; border-radius:10px;
                padding:12px 28px; font-weight:700; font-size:13px;
                font-family:{_FONT_BODY};
            }}
            QPushButton:hover {{
                background:{_BG_HOVER}; border-color:{_TEXT_SEC};
            }}
        """)
        self.btn_ignore.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ignore.clicked.connect(self.ignore_current)

        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setStyleSheet(f"""
            QPushButton {{
                background:transparent; color:{_TEXT_SEC};
                border:1px solid {_BORDER_HI}; border-radius:10px;
                padding:12px 28px; font-weight:700; font-size:13px;
                font-family:{_FONT_BODY};
            }}
            QPushButton:hover {{
                background:{_DANGER}18; color:{_DANGER};
                border-color:{_DANGER};
            }}
        """)
        self.btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete.clicked.connect(self.delete_current)

        self.lbl_duplicate = QLabel("")
        self.lbl_duplicate.setWordWrap(True)
        self.lbl_duplicate.setVisible(False)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 8, 20, 12)
        btn_row.setSpacing(10)
        btn_row.addWidget(self.btn_process)
        btn_row.addWidget(self.btn_ignore)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_delete)

        right_wrapper = QWidget()
        right_wrapper.setStyleSheet(f"background:{_BG_SURFACE};")
        right_wrapper_layout = QVBoxLayout(right_wrapper)
        right_wrapper_layout.setContentsMargins(0, 0, 0, 0)
        right_wrapper_layout.setSpacing(0)
        right_wrapper_layout.addWidget(right_scroll)
        right_wrapper_layout.addWidget(self.lbl_duplicate)
        right_wrapper_layout.addLayout(btn_row)

        splitter.addWidget(right_wrapper)
        splitter.setSizes([480, 620])
        left.setMinimumWidth(440)
        splitter.setStretchFactor(0, 40)
        splitter.setStretchFactor(1, 60)
        main.addWidget(splitter)

        # Auto-mode timer (1-second tick)
        self.auto_timer = QTimer()
        self.auto_timer.setInterval(1000)
        self.auto_timer.timeout.connect(self._auto_tick)
        self.auto_seconds_left = 0
        self._auto_check_running = False

    # ── Auto Mode ───────────────────────────────

    def _on_auto_toggle(self, checked: bool):
        if checked:
            self.btn_auto_toggle.setText("ON")
            self.start_auto_mode()
        else:
            self.btn_auto_toggle.setText("OFF")
            self.stop_auto_mode()

    def start_auto_mode(self):
        self.auto_seconds_left = 0
        self._auto_tick()

    def stop_auto_mode(self):
        self.auto_timer.stop()
        self.lbl_countdown.setText("")
        self.signals.status_update.emit("Auto mode stopped")

    def _auto_tick(self):
        if self.auto_seconds_left > 0:
            self.auto_seconds_left -= 1
            mins, secs = divmod(self.auto_seconds_left, 60)
            self.lbl_auto_countdown.setText(f"{mins}:{secs:02d}")
            self.signals.status_update.emit(f"Auto — next in {mins}:{secs:02d}")
        else:
            if self._auto_check_running:
                return
            self._auto_check_running = True
            self.lbl_auto_countdown.setText("...")
            self.signals.status_update.emit("Auto-check running...")
            self._auto_check_mail()

    def _auto_check_mail(self):
        interval_secs = int(self.spin_auto_interval.value() * 60)

        def run():
            try:
                from mail_reader import check_mail_once

                count = len(check_mail_once())
                self.signals.log_message.emit(
                    f"Auto-check: {count} new email(s)"
                    if count > 0
                    else "Auto-check: no new emails"
                )

                from ai_parser import classify_email, parse_order, parse_invoice
                from pdf_reader import extract_pdf_text
                from storage import load_emails, save_emails

                all_emails = load_emails()
                parsed_count = 0
                for em in all_emails:
                    if em.get("status") != "new":
                        continue
                    if em.get("parsed") and em.get("doc_type"):
                        continue
                    subject = em.get("subject", "")
                    body = em.get("body", "")
                    pdf_text = ""
                    for p in em.get("pdf_paths", []):
                        try:
                            pdf_text += extract_pdf_text(p) + "\n"
                        except Exception:
                            pass
                    doc_type = classify_email(
                        subject, body, pdf_text, em.get("from", "")
                    )
                    em["doc_type"] = doc_type
                    if doc_type in ("order", "order_timocom"):
                        em["parsed"] = parse_order(
                            subject, body, doc_type, em.get("pdf_paths", [])
                        )
                    elif doc_type == "carrier_invoice":
                        em["parsed"] = parse_invoice(body, "", em.get("pdf_paths", []))
                    else:
                        em["parsed"] = {}
                    parsed_count += 1
                    self.signals.log_message.emit(
                        f"  Parsed [{doc_type}]: {subject[:50]}"
                    )

                if parsed_count:
                    save_emails(all_emails)
                    self.signals.log_message.emit(
                        f"Auto-parse: {parsed_count} email(s) parsed"
                    )

                self.signals.mail_checked.emit(count)
            except Exception as e:
                import traceback

                self.signals.log_message.emit(
                    f"Auto-check error: {e}\n{traceback.format_exc()}"
                )
            finally:
                self.auto_seconds_left = interval_secs
                self._auto_check_running = False
                QMetaObject.invokeMethod(
                    self.auto_timer, "start", Qt.ConnectionType.QueuedConnection
                )

        threading.Thread(target=run, daemon=True).start()

    def _toggle_body(self):
        visible = self.body_toggle.isChecked()
        self.txt_body.setVisible(visible)

    def _auto_parse_and_process_new(self):
        all_emails = load_emails()
        new_indices = [
            i
            for i, e in enumerate(all_emails)
            if e.get("status") == "new"
            or (e.get("status") == "processing" and not e.get("sheet_row"))
        ]

        running_uids: set = set(self._task_email_map.values())
        queued_order_numbers: set = set()

        for idx in new_indices:
            em = all_emails[idx]
            email_uid = em.get("email_uid") or str(idx)

            if email_uid in running_uids:
                continue

            try:
                from ai_parser import classify_email, parse_order, parse_invoice
                from pdf_reader import extract_pdf_text

                subject = em.get("subject", "")
                body = em.get("body", "")
                pdf_text = ""
                for p in em.get("pdf_paths", []):
                    try:
                        pdf_text += extract_pdf_text(p) + "\n"
                    except Exception:
                        pass

                doc_type = classify_email(subject, body, pdf_text, em.get("from", ""))
                em["doc_type"] = doc_type

                if doc_type in ("order", "order_timocom"):
                    parsed = parse_order(
                        subject, body, doc_type, em.get("pdf_paths", [])
                    )
                elif doc_type == "carrier_invoice":
                    parsed = parse_invoice(body, "", em.get("pdf_paths", []))
                else:
                    parsed = None

                em["parsed"] = parsed

                if doc_type in ("order", "order_timocom") and parsed:
                    client_nr = (parsed.get("client_order_number") or "").strip()
                    if client_nr:
                        if client_nr in queued_order_numbers:
                            em["status"] = "duplicate"
                            all_emails[idx] = em
                            save_emails(all_emails)
                            self.signals.log_message.emit(
                                f"[Auto] Duplicate (same batch): {client_nr}"
                            )
                            continue
                        try:
                            from loads_cache import is_duplicate

                            dup = is_duplicate(client_nr)
                            if dup:
                                em["status"] = "duplicate"
                                all_emails[idx] = em
                                save_emails(all_emails)
                                self.signals.log_message.emit(
                                    f"[Auto] Duplicate in Sheets row {dup.get('sheet_row')}: {client_nr}"
                                )
                                continue
                        except Exception as cache_err:
                            self.signals.log_message.emit(
                                f"[Auto] Cache check failed ({cache_err})"
                            )

                em["status"] = "processing"
                all_emails[idx] = em
                save_emails(all_emails)

                our_nr = em.get("our_order_nr", "") or f"auto-{idx}"

                _parsed_with_body = dict(parsed or {})
                _parsed_with_body["_body"] = em.get("body", "")
                task = Task(
                    task_type=doc_type,
                    data={
                        "parsed": _parsed_with_body,
                        "our_order_nr": our_nr,
                        "pdf_paths": em.get("pdf_paths", []),
                        "do_sheets": True,
                        "do_drive": True,
                        "do_gmail": True,
                        "sender_email": em.get("from", ""),
                    },
                    duration_minutes=5.0,
                )
                self._task_email_map[task.task_id] = email_uid
                running_uids.add(email_uid)

                if doc_type in ("order", "order_timocom") and parsed:
                    client_nr = (parsed.get("client_order_number") or "").strip()
                    if client_nr:
                        queued_order_numbers.add(client_nr)

                task.duration = 0

                def _make_cb(t, i):
                    def progress_cb(msg: str):
                        self.signals.log_message.emit(f"[Auto] {msg}")
                        if "Task completed" in msg:
                            self.signals.task_finished.emit(t.task_id, "done")
                        elif "Critical" in msg:
                            self.signals.task_finished.emit(t.task_id, "error")

                    return progress_cb

                register_progress_callback(task.task_id, _make_cb(task, idx))
                task_queue.put(task)
                self.signals.log_message.emit(f"[Auto] Task queued: {our_nr}")

            except Exception as e:
                self.signals.log_message.emit(f"[Auto] Error on email {idx}: {e}")

        self.reload()

    # ── Mail Check ──────────────────────────────

    def _set_filter(self, label: str):
        """Switch the active filter tab and reload the email list."""
        self._active_filter = label
        for btn in self._filter_buttons:
            btn.setChecked(btn.text() == label)
        self.reload()

    def _set_date_filter(self, label: str):
        """Switch date filter (All / Today / 3 days / Week) and reload."""
        self._active_date_filter = label
        for btn in self._date_filter_buttons:
            btn.setChecked(btn.text() == label)
        self.reload()

    def check_mail(self):
        self.btn_check.setEnabled(False)
        self.btn_check.setText("Checking...")

        def run():
            try:
                from mail_reader import check_mail_once

                count = len(check_mail_once())
                self.signals.mail_checked.emit(count)
                self.signals.log_message.emit(f"{count} new email(s) found")
            except Exception as e:
                self.signals.log_message.emit(f"Mail check error: {e}")
            finally:
                self.signals.check_done.emit()

        threading.Thread(target=run, daemon=True).start()

    def _on_auto_check_toggled(self, state):
        if state == Qt.CheckState.Checked.value:
            self.auto_seconds_left = int(self.spin_auto_interval.value() * 60)
            self.auto_timer.start()
            self.lbl_auto_countdown.show()
        else:
            self.auto_timer.stop()
            self.lbl_auto_countdown.hide()

    def _on_auto_interval_changed(self, value):
        pass

    def _check_done(self):
        self.btn_check.setEnabled(True)
        self.btn_check.setText("Check Mail")
        self.reload()

    # ── List & Selection ────────────────────────

    def reload(self):
        """Reload email list from storage and rebuild the list widget."""
        self.emails = load_emails()
        self.list_widget.clear()
        self.verif_group.setVisible(False)
        new_count = 0

        filter_text = getattr(self, "_active_filter", "All")

        # ── Type filter ──
        type_filtered = []
        for em in self.emails:
            doc_type = em.get("doc_type", "").lower()
            if filter_text == "Orders" and doc_type not in (
                "order",
                "order_timocom",
            ):
                continue
            if filter_text == "Invoices" and doc_type != "carrier_invoice":
                continue
            type_filtered.append(em)

        # ── Date filter ──
        date_filter = getattr(self, "_active_date_filter", "All")
        if date_filter == "All":
            filtered_emails = type_filtered
        else:
            from datetime import datetime, timedelta

            now = datetime.now()
            if date_filter == "Today":
                cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elif date_filter == "3 days":
                cutoff = now - timedelta(days=3)
            else:  # Week
                cutoff = now - timedelta(days=7)

            filtered_emails = []
            for em in type_filtered:
                received = em.get("received_at", "")
                if not received or received == "unknown":
                    filtered_emails.append(em)  # show emails without date
                    continue
                try:
                    em_date = datetime.strptime(received, "%d.%m.%Y %H:%M")
                    if em_date >= cutoff:
                        filtered_emails.append(em)
                except ValueError:
                    filtered_emails.append(em)  # unparseable date → show anyway

        try:
            from loads_cache import is_duplicate as _is_dup
        except Exception:

            def _is_dup(_):
                return None

        def _in_cache(em: dict) -> bool:
            parsed = em.get("parsed") or {}
            nr = parsed.get("client_order_number", "")
            return bool(nr and _is_dup(nr))

        visible = [
            e
            for e in filtered_emails
            if e.get("status") not in ("done", "duplicate")
            and e.get("doc_type") != "ignore"
            and not _in_cache(e)
        ]
        self._visible_emails = visible

        for em in visible:
            status = em.get("status", "new")
            doc_type = em.get("doc_type", "").lower()

            sender_raw = em.get("from", "")
            sender_name = (
                sender_raw.split("<")[0].strip().strip('"').strip()
                if "<" in sender_raw
                else sender_raw.strip()
            )
            if not sender_name:
                sender_name = "Unknown"
            subj = em.get("subject", "(no subject)")
            received_at = em.get("received_at", "")

            # Determine visual colors
            if status in ("processing", "done", "error"):
                dot_color = STATUS_COLORS.get(status, _TEXT_MUTED)
            else:
                dot_color = DOC_TYPE_BORDER_COLORS.get(doc_type, _TEXT_MUTED)

            badge_text = DOC_TYPE_LABELS.get(doc_type, "")
            if not badge_text:
                if not doc_type or doc_type == "unknown":
                    lower_subj = subj.lower()
                    if "invoice" in lower_subj:
                        badge_text = "INVOICE"
                        doc_type = "carrier_invoice"
                    elif "ignore" in lower_subj or "re:" in lower_subj:
                        badge_text = "IGNORE"
                        doc_type = "ignore"
                    else:
                        badge_text = "ORDER"
                        doc_type = "order"
                else:
                    badge_text = "?"
            badge_color = DOC_TYPE_BADGE_COLORS.get(doc_type, _TEXT_MUTED)

            # Build the card widget
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 88))

            card = QWidget()
            card.setStyleSheet(f"""
                QWidget {{
                    background:{_BG_CARD}; border-radius:10px;
                    border:1px solid {_BORDER};
                    border-left:4px solid {dot_color};
                }}
                QWidget:hover {{
                    background:{_BG_HOVER};
                    border-color:{_BORDER_HI};
                    border-left:4px solid {dot_color};
                }}
            """)

            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(16, 10, 14, 10)
            card_layout.setSpacing(12)

            # Text area (type label + subject + sender)
            text_w = QWidget()
            text_w.setStyleSheet("background:transparent; border:none;")
            text_l = QVBoxLayout(text_w)
            text_l.setContentsMargins(0, 0, 0, 0)
            text_l.setSpacing(5)

            # Type badge — colored pill with white text, very readable
            lbl_type = QLabel(f"  {badge_text}  ")
            lbl_type.setStyleSheet(
                f"color:#ffffff; font-family:{_FONT_TAG}; font-size:9px; "
                f"font-weight:700; letter-spacing:0.8px; "
                f"background:{badge_color}; border:none; border-radius:3px; "
                f"padding:2px 4px;"
            )
            lbl_type.setFixedHeight(18)
            lbl_type.setMaximumWidth(80)

            # Subject — clean body font
            lbl_subj = QLabel(subj)
            lbl_subj.setStyleSheet(
                f"color:{_TEXT}; font-family:{_FONT_BODY}; font-weight:600; "
                f"font-size:13px; background:transparent; border:none;"
            )
            lbl_subj.setWordWrap(False)
            lbl_subj.setToolTip(subj)

            # Sender — monospace metadata, smaller
            lbl_meta = QLabel(f"{sender_name}  |  ACME")
            lbl_meta.setStyleSheet(
                f"color:{_TEXT_MUTED}; font-family:{_FONT_MONO}; font-size:10px; "
                f"background:transparent; border:none;"
            )
            lbl_meta.setFixedHeight(16)

            text_l.addWidget(lbl_type)
            text_l.addWidget(lbl_subj)
            text_l.addWidget(lbl_meta)
            text_w.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            card_layout.addWidget(text_w, 1)

            # Time column on the right (Space Mono / Consolas)
            time_str = ""
            if received_at and len(received_at) > 10:
                time_str = received_at[11:16]  # "14:35"
            elif received_at:
                time_str = received_at[:10]
            lbl_time = QLabel(time_str)
            lbl_time.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            lbl_time.setStyleSheet(
                f"color:{_TEXT_MUTED}; font-family:{_FONT_MONO}; font-size:11px; "
                f"background:transparent; border:none;"
            )
            lbl_time.setFixedWidth(50)
            card_layout.addWidget(lbl_time)

            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, card)

            if status == "new":
                new_count += 1

        self.lbl_count.setText(f"{len(visible)} emails · {new_count} new")
        self.lbl_count.setStyleSheet(
            f"color:{_TEXT_MUTED}; font-family:{_FONT_MONO}; font-size:11px; padding:4px 0;"
        )

    def on_select(self, idx: int):
        """Handle email selection — show detail panel."""
        if idx < 0 or idx >= len(self._visible_emails):
            return
        em = self._visible_emails[idx]

        self.lbl_subject.setText(em.get("subject", ""))
        self.lbl_from.setText(
            f"From: {em.get('from', '')}  ·  {em.get('received_at', '')}"
        )
        body_text = em.get("body", "")[:500]
        self.txt_body.setPlainText(body_text)
        self.txt_body.setVisible(False)
        self.body_toggle.setChecked(False)

        parsed = em.get("parsed")
        doc_type = em.get("doc_type", "order")

        if isinstance(parsed, str):
            import json

            try:
                parsed = json.loads(parsed)
            except Exception:
                parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed = {k: (v if v is not None else "") for k, v in parsed.items()}

        if parsed and doc_type == "carrier_invoice":
            payment_days = parsed.get("payment_days")
            if payment_days == 0:
                parsed["payment_days"] = 14

        # Show/hide checkboxes based on doc_type
        if doc_type in ("order", "order_timocom"):
            self.chk_sheets.show()
            self.chk_drive.show()
            self.chk_payment_plan.hide()
            self.chk_loads_info.hide()
        elif doc_type == "carrier_invoice":
            self.chk_sheets.hide()
            self.chk_drive.hide()
            self.chk_payment_plan.show()
            self.chk_loads_info.show()
        else:
            self.chk_sheets.show()
            self.chk_drive.show()
            self.chk_payment_plan.hide()
            self.chk_loads_info.hide()

        for cb in [
            self.chk_sheets,
            self.chk_drive,
            self.chk_gmail,
            self.chk_payment_plan,
            self.chk_loads_info,
        ]:
            if cb.isVisible():
                cb.setChecked(True)

        if parsed:

            def fix_date_str(s):
                if isinstance(s, str):
                    return s.replace("-", ".")
                return s

            for key in (
                "loading_date",
                "unloading_date",
                "service_date",
                "payment_days",
            ):
                if key in parsed:
                    if key == "payment_days" and parsed[key] == 0:
                        parsed[key] = 14
                    else:
                        parsed[key] = fix_date_str(parsed[key])

            if doc_type in ("order", "order_timocom"):
                client_nr = parsed.get("client_order_number", "")
                if client_nr:
                    from loads_cache import is_duplicate

                    dup = is_duplicate(client_nr)
                    if dup:
                        self.lbl_duplicate.setText(
                            f"Already in sheet row {dup['sheet_row']} — {dup['our_order_nr']}"
                        )
                        self.lbl_duplicate.setStyleSheet(
                            f"background:{_WARNING}; color:#000; padding:8px 16px; border-radius:6px; font-weight:600; font-size:12px;"
                        )
                        self.lbl_duplicate.setVisible(True)
                    else:
                        self.lbl_duplicate.setVisible(False)
                else:
                    self.lbl_duplicate.setVisible(False)

            self.txt_parsed.setHtml(_format_parsed_card(parsed, doc_type))
            self.btn_process.setEnabled(
                doc_type in ("order", "order_timocom", "carrier_invoice")
            )
            if doc_type == "carrier_invoice":
                self._show_verification_panel(parsed)
            else:
                self.verif_group.setVisible(False)
        else:
            self.txt_parsed.setPlainText("— not parsed —")
            self.btn_process.setEnabled(False)
            self.verif_group.setVisible(False)

    # ── AI Parse ────────────────────────────────

    def parse_current(self):
        idx = self.list_widget.currentRow()
        if idx < 0 or idx >= len(self._visible_emails):
            return
        em = self._visible_emails[idx]

        def run():
            try:
                from ai_parser import classify_email, parse_order, parse_invoice
                from pdf_reader import extract_pdf_text

                subject = em.get("subject", "")
                body = em.get("body", "")
                pdf_text = ""
                for p in em.get("pdf_paths", []):
                    try:
                        pdf_text += extract_pdf_text(p) + "\n"
                    except Exception:
                        pass

                doc_type = em.get("doc_type") or classify_email(
                    subject, body, pdf_text, em.get("from", "")
                )

                if doc_type in ("order", "order_timocom"):
                    parsed = parse_order(
                        subject, body, doc_type, em.get("pdf_paths", [])
                    )
                elif doc_type == "carrier_invoice":
                    parsed = parse_invoice(body, pdf_text, em.get("pdf_paths", []))
                else:
                    parsed = {}

                uid = em.get("email_uid") or str(idx)
                all_emails = load_emails()
                for i, e in enumerate(all_emails):
                    if (e.get("email_uid") or str(i)) == uid:
                        all_emails[i]["parsed"] = parsed
                        all_emails[i]["doc_type"] = doc_type
                        break
                save_emails(all_emails)

                self.signals.log_message.emit(f"Parsed as [{doc_type}]: {subject[:50]}")
            except Exception as e:
                self.signals.log_message.emit(f"Parse error: {e}")
            finally:
                self.signals.email_parsed.emit(idx)

        threading.Thread(target=run, daemon=True).start()

    def _on_email_parsed(self, idx: int):
        self.reload()
        if 0 <= idx < self.list_widget.count():
            self.list_widget.setCurrentRow(idx)

    # ── Re-parse All ─────────────────────────────

    def reparse_all(self):
        self.btn_reparse_all.setEnabled(False)
        self.btn_reparse_all.setText("Parsing...")

        def run():
            try:
                from ai_parser import classify_email, parse_order, parse_invoice
                from pdf_reader import extract_pdf_text

                all_emails = load_emails()
                count = 0
                for em in all_emails:
                    old_parsed = em.get("parsed") or {}
                    doc_type = em.get("doc_type", "")
                    already_good = (
                        old_parsed
                        and doc_type
                        and not (
                            doc_type == "carrier_invoice"
                            and (
                                not old_parsed.get("invoice_number")
                                or not old_parsed.get("amount_netto")
                                or old_parsed.get("amount_netto") == 0
                            )
                        )
                        and not (
                            doc_type == "order"
                            and not old_parsed.get("client_order_number")
                        )
                    )
                    if already_good:
                        continue

                    old_key = (
                        old_parsed.get("invoice_number")
                        if doc_type == "carrier_invoice"
                        else old_parsed.get("client_order_number")
                    )

                    subject = em.get("subject", "")
                    body = em.get("body", "")
                    pdf_text = ""
                    for p in em.get("pdf_paths", []):
                        try:
                            pdf_text += extract_pdf_text(p) + "\n"
                        except Exception:
                            pass

                    doc_type = classify_email(
                        subject, body, pdf_text, em.get("from", "")
                    )
                    em["doc_type"] = doc_type

                    if doc_type in ("order", "order_timocom"):
                        pdfs = em.get("pdf_paths", [])
                        parsed = parse_order(subject, body, doc_type, pdfs)
                    elif doc_type == "carrier_invoice":
                        pdfs = em.get("pdf_paths", [])
                        parsed = parse_invoice(body, "", pdfs)
                    else:
                        parsed = None

                    em["parsed"] = parsed

                    new_key = (parsed or {}).get(
                        "invoice_number"
                        if doc_type == "carrier_invoice"
                        else "client_order_number"
                    )
                    if not old_key and new_key and em.get("status") == "done":
                        em["status"] = "new"
                        self.signals.log_message.emit(f"  Status reset: {subject[:40]}")

                    count += 1
                    self.signals.log_message.emit(
                        f"  Re-parsed [{doc_type}]: {subject[:50]}"
                    )

                save_emails(all_emails)
                self.signals.log_message.emit(f"Re-parse done: {count} email(s)")
                self.signals.reparse_done.emit(count)
            except Exception as e:
                import traceback

                self.signals.log_message.emit(
                    f"Re-parse error: {e}\n{traceback.format_exc()}"
                )
                self.signals.reparse_done.emit(0)

        threading.Thread(target=run, daemon=True).start()

    def _on_reparse_done(self, count: int):
        self.btn_reparse_all.setEnabled(True)
        self.btn_reparse_all.setText("Re-parse")
        self.reload()

    def on_check_invoices(self):
        """Scan invoices_out folder and process each PDF."""
        from invoice_checker import process_invoice
        from pathlib import Path

        folder = Path(__file__).resolve().parent / "bot_data" / "invoices_out"
        pdfs = [f for f in folder.glob("*.pdf") if f.is_file()]

        if not pdfs:
            QMessageBox.information(
                self, "Invoices", "No PDF files in bot_data/invoices_out/"
            )
            return

        results = []
        for pdf in pdfs:
            r = process_invoice(str(pdf))
            results.append(r)

        ok = [r for r in results if r["status"] == "ok"]
        errors = [r for r in results if r["status"] == "error"]
        not_found = [r for r in results if r["status"] == "not_found"]
        already = [r for r in results if r["status"] == "already_written"]

        msg = f"Processed: {len(results)} invoices\n"
        msg += f"OK: {len(ok)}\n"
        if already:
            msg += f"Already written: {len(already)}\n"
        if not_found:
            msg += f"Not found: {len(not_found)}\n"
        if errors:
            msg += f"Errors: {len(errors)}\n"

        for r in ok:
            msg += f"\n  {r.get('invoice_number')} — {r.get('client_name')}"
        for r in not_found:
            msg += f"\n  Not found: {r.get('order_number')}"
        for r in errors:
            msg += f"\n  Error: {r.get('error_message', '')[:80]}"

        QMessageBox.information(self, "Invoice Check Results", msg)

    # ── Process ─────────────────────────────────

    def process_current(self):
        idx = self.list_widget.currentRow()
        if idx < 0 or idx >= len(self._visible_emails):
            return
        em = self._visible_emails[idx]
        parsed = em.get("parsed")
        doc_type = em.get("doc_type", "order")
        our_nr = em.get("our_order_nr", "") or f"auto-{idx}"

        _parsed_with_body = dict(parsed or {})
        _parsed_with_body["_body"] = em.get("body", "")
        _parsed_with_body["received_at"] = em.get("received_at", "")
        task = Task(
            task_type=doc_type,
            data={
                "parsed": _parsed_with_body,
                "our_order_nr": our_nr,
                "pdf_paths": em.get("pdf_paths", []),
                "do_sheets": self.chk_sheets.isChecked(),
                "do_drive": self.chk_drive.isChecked(),
                "do_gmail": self.chk_gmail.isChecked(),
                "sender_email": em.get("from", ""),
            },
            duration_minutes=5.0,
        )
        email_uid = em.get("email_uid") or str(idx)
        self._task_email_map[task.task_id] = email_uid

        task.duration = 0

        all_emails = load_emails()
        for i, e in enumerate(all_emails):
            if (e.get("email_uid") or str(i)) == email_uid:
                all_emails[i]["status"] = "processing"
                all_emails[i]["our_order_nr"] = our_nr
                save_emails(all_emails)
                break

        def progress_cb(msg: str):
            self.signals.log_message.emit(msg)
            if "Loads Info:" in msg and ", order " in msg:
                try:
                    order_nr = msg.split(", order ")[-1].rstrip(")")
                    if order_nr and order_nr != "-":
                        self.signals.log_message.emit(
                            f"Order number assigned: {order_nr}"
                        )
                except Exception:
                    pass
            if "Task completed" in msg:
                self.signals.task_finished.emit(task.task_id, "done")
            elif "Critical" in msg:
                self.signals.task_finished.emit(task.task_id, "error")

        register_progress_callback(task.task_id, progress_cb)
        task_queue.put(task)
        self.signals.log_message.emit(f"Task queued: {our_nr or doc_type}")
        self.reload()

    def _on_task_finished(self, task_id: str, status: str):
        """Save final task status back to emails.json and refresh the list."""
        uid = self._task_email_map.pop(task_id, None)
        found_idx = None
        if uid is not None:
            all_emails = load_emails()
            for i, em in enumerate(all_emails):
                if (em.get("email_uid") or str(i)) == uid:
                    all_emails[i]["status"] = status
                    save_emails(all_emails)
                    found_idx = i
                    if status == "done":
                        try:
                            from loads_cache import append_row as _append

                            parsed = em.get("parsed") or {}
                            our_nr = em.get("our_order_nr", "")
                            _append(parsed, our_nr)
                        except Exception as _e:
                            print(f"[loads_cache] append_row error: {_e}")
                    break
        self.reload()
        if found_idx is not None and 0 <= found_idx < self.list_widget.count():
            self.list_widget.setCurrentRow(found_idx)

    # ── Ignore / Delete ─────────────────────────

    def ignore_current(self):
        idx = self.list_widget.currentRow()
        if idx < 0 or idx >= len(self._visible_emails):
            return
        uid = self._visible_emails[idx].get("email_uid") or str(idx)
        all_emails = load_emails()
        for i, e in enumerate(all_emails):
            if (e.get("email_uid") or str(i)) == uid:
                all_emails[i]["status"] = "ignored"
                save_emails(all_emails)
                break
        self.reload()

    def delete_current(self):
        idx = self.list_widget.currentRow()
        if idx < 0 or idx >= len(self._visible_emails):
            return
        uid = self._visible_emails[idx].get("email_uid") or str(idx)
        all_emails = load_emails()
        for i, e in enumerate(all_emails):
            if (e.get("email_uid") or str(i)) == uid:
                del all_emails[i]
                save_emails(all_emails)
                break
        self.reload()

    # ── Verification Panel ───────────────────────

    def _on_confirm_payment_plan(self):
        """Write matched carrier invoice data to Payment Plan."""
        idx = self.list_widget.currentRow()
        if idx < 0 or idx >= len(self._visible_emails):
            return
        em = self._visible_emails[idx]
        parsed = em.get("parsed") or {}
        try:
            from actions import write_to_payment_plan

            write_to_payment_plan(
                parsed, log_fn=lambda m: self.signals.log_message.emit(m)
            )
            self.signals.log_message.emit("Payment Plan updated (confirmed)")
        except Exception as e:
            self.signals.log_message.emit(f"Payment Plan error: {e}")

    def _on_override_payment_plan(self):
        """Write carrier invoice data to Payment Plan (override)."""
        idx = self.list_widget.currentRow()
        if idx < 0 or idx >= len(self._visible_emails):
            return
        em = self._visible_emails[idx]
        parsed = em.get("parsed") or {}
        try:
            from actions import write_to_payment_plan

            write_to_payment_plan(
                parsed, log_fn=lambda m: self.signals.log_message.emit(m)
            )
            self.signals.log_message.emit("Payment Plan updated (override)")
        except Exception as e:
            self.signals.log_message.emit(f"Payment Plan error: {e}")

    def _show_verification_panel(self, parsed: dict):
        self.verif_group.setVisible(True)
        self.verif_invoice_nr.setText(str(parsed.get("invoice_number", "—")))
        self.verif_carrier.setText(str(parsed.get("carrier_name", "—")))
        self.verif_amount_netto.setText(str(parsed.get("amount_netto", "—")))
        self.verif_amount_brutto.setText(str(parsed.get("amount_brutto", "—")))
        self.verif_service_date.setText(str(parsed.get("service_date", "—")))
        self.verif_matched_order.setText(str(parsed.get("order_number", "—")))
        self.verif_matched_price.setText("—")
        self.verif_unloading_date.setText("—")
        self.verif_status.setText("Ready to verify")
        self.btn_verif_confirm.setEnabled(True)
        self.btn_verif_override.setEnabled(True)


# ─────────────────────────────────────────────
# DASHBOARD TAB
# ─────────────────────────────────────────────


class DashboardTab(QWidget):
    """Dashboard with stat cards, attention table, and unprocessed emails."""

    _data_ready = pyqtSignal(dict)

    def __init__(self, signals: Signals):
        super().__init__()
        self.signals = signals
        self._build_ui()
        self._data_ready.connect(self._apply_data)

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"background:{_BG_BASE};")

        content = QWidget()
        content.setStyleSheet(f"background:{_BG_BASE};")
        layout = QVBoxLayout(content)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # Header
        header = QHBoxLayout()
        lbl = QLabel("Dashboard")
        lbl.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color:{_TEXT};")

        self.lbl_updated = QLabel("Not loaded")
        self.lbl_updated.setStyleSheet(f"color:{_TEXT_MUTED}; font-size:12px;")

        btn_refresh = QPushButton("Refresh")
        btn_refresh.setFixedWidth(100)
        btn_refresh.clicked.connect(self.refresh)
        header.addWidget(lbl)
        header.addStretch()
        header.addWidget(self.lbl_updated)
        header.addWidget(btn_refresh)
        layout.addLayout(header)

        # ── Stat cards ──
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)

        def _card(title, obj_name, accent_color):
            frame = QFrame()
            frame.setStyleSheet(f"""
                QFrame {{
                    background:{_BG_CARD}; border:1px solid {_BORDER};
                    border-radius:10px; border-left:3px solid {accent_color};
                }}
            """)
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(16, 14, 16, 14)

            lbl_title = QLabel(title)
            lbl_title.setStyleSheet(
                f"color:{_TEXT_MUTED}; font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:1px;"
            )

            lbl_val = QLabel("—")
            lbl_val.setFont(QFont("Segoe UI", 26, QFont.Weight.Bold))
            lbl_val.setStyleSheet(f"color:{accent_color};")

            fl.addWidget(lbl_title)
            fl.addWidget(lbl_val)
            setattr(self, obj_name, lbl_val)
            return frame

        cards_row.addWidget(_card("Orders Today", "card_orders", _SUCCESS))
        cards_row.addWidget(_card("Invoices", "card_invoices", _WARNING))
        cards_row.addWidget(_card("Pending", "card_pending", _DANGER))
        cards_row.addWidget(_card("Time Saved (min)", "card_time", _ACCENT))
        layout.addLayout(cards_row)

        # ── Needs Attention ──
        lbl_attention = QLabel("Needs Attention")
        lbl_attention.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        lbl_attention.setStyleSheet(f"color:{_WARNING};")
        layout.addWidget(lbl_attention)

        self.tbl_needs_attention = QTableWidget()
        self.tbl_needs_attention.setColumnCount(5)
        self.tbl_needs_attention.setHorizontalHeaderLabels(
            ["Nr", "Company / Carrier", "Order Nr", "Type", "Date"]
        )
        self.tbl_needs_attention.horizontalHeader().setStretchLastSection(True)
        self.tbl_needs_attention.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.tbl_needs_attention.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.tbl_needs_attention.setMaximumHeight(400)
        self.tbl_needs_attention.setStyleSheet(f"""
            QTableWidget {{ background:{_BG_SURFACE}; border:1px solid {_BORDER}; border-radius:8px; }}
            QTableWidget::item {{ padding:8px; }}
            QHeaderView::section {{ background:{_BG_CARD}; border:none; border-bottom:1px solid {_BORDER}; padding:8px; color:{_TEXT_SEC}; font-weight:600; }}
        """)
        layout.addWidget(self.tbl_needs_attention)

        # ── Unprocessed emails ──
        lbl_unfiled = QLabel("Unprocessed Emails")
        lbl_unfiled.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        lbl_unfiled.setStyleSheet(f"color:{_ACCENT};")
        layout.addWidget(lbl_unfiled)

        self.lst_unfiled = QListWidget()
        self.lst_unfiled.setMaximumHeight(120)
        self.lst_unfiled.setStyleSheet(f"""
            QListWidget {{ background:{_BG_SURFACE}; border:1px solid {_BORDER}; border-radius:8px; }}
            QListWidget::item {{ padding:6px 12px; color:{_TEXT}; }}
        """)
        layout.addWidget(self.lst_unfiled)

        layout.addStretch()
        scroll.setWidget(content)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def refresh(self):
        """Load Sheets data in background."""
        self.lbl_updated.setText("Loading...")

        def run():
            result = {"error": None, "rows": [], "emails": []}
            try:
                from actions import _get_clients
                from config import LOADS_INFO_SHEET_ID

                gc, _, _, _ = _get_clients()
                ws = gc.open_by_key(LOADS_INFO_SHEET_ID).get_worksheet(0)
                result["rows"] = ws.get_all_values()
            except Exception as e:
                result["error"] = str(e)
            result["emails"] = load_emails()
            self._data_ready.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def _apply_data(self, result: dict):
        if result.get("error"):
            self.lbl_updated.setText(f"Error: {result['error'][:60]}")
            return

        rows = result["rows"]
        today = datetime.now()
        today_str = today.strftime("%d.%m.%Y")

        orders_today = 0
        invoices_today = 0
        needs_attention_rows = []

        for row in rows[3:]:
            if len(row) < 30:
                continue

            dispatch = _col(row, "dispatch_date")
            inv_email = _col(row, "date_invoice_email")
            company = _col(row, "company")
            our_nr = _col(row, "our_order_nr")
            nr = _col(row, "nr")
            inv_client = _col(row, "invoice_nr_client")
            cmr_received = _col(row, "date_received")

            if dispatch == today_str:
                orders_today += 1
            if inv_email == today_str:
                invoices_today += 1

            if not inv_client:
                needs_attention_rows.append(
                    {
                        "nr": nr,
                        "party": company,
                        "order": our_nr,
                        "type": "Invoice not sent",
                        "date": dispatch,
                    }
                )
            if not cmr_received:
                needs_attention_rows.append(
                    {
                        "nr": nr,
                        "party": company,
                        "order": our_nr,
                        "type": "CMR not received",
                        "date": dispatch,
                    }
                )

        self.card_orders.setText(str(orders_today))
        self.card_invoices.setText(str(invoices_today))
        self.card_pending.setText(str(len(needs_attention_rows)))
        time_saved = orders_today * 15 + invoices_today * 10
        self.card_time.setText(str(time_saved))

        self.tbl_needs_attention.setRowCount(len(needs_attention_rows))
        for i, r in enumerate(needs_attention_rows):
            vals = [r["nr"], r["party"], r["order"], r["type"], r["date"]]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setForeground(QColor(_TEXT_SEC))
                self.tbl_needs_attention.setItem(i, c, item)

        self.lst_unfiled.clear()
        for em in result["emails"]:
            if em.get("status") == "new":
                self.lst_unfiled.addItem(em.get("subject", "(no subject)"))

        self.lbl_updated.setText(f"Updated {today.strftime('%H:%M')}")
        self.signals.log_message.emit(
            f"Dashboard: {orders_today} orders · {len(needs_attention_rows)} attention items"
        )


# ─────────────────────────────────────────────
# LOG TAB
# ─────────────────────────────────────────────


class LogTab(QWidget):
    """Execution log with timestamped, color-coded entries."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        top = QHBoxLayout()
        lbl = QLabel("Execution Log")
        lbl.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color:{_TEXT};")

        btn_clear = QPushButton("Clear")
        btn_clear.setFixedWidth(80)
        btn_clear.clicked.connect(self._clear)
        top.addWidget(lbl)
        top.addStretch()
        top.addWidget(btn_clear)
        layout.addLayout(top)

        self.txt = QTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setFont(QFont("Consolas", 11))
        self.txt.setStyleSheet(f"""
            QTextEdit {{
                background:{_BG_SURFACE}; border:1px solid {_BORDER}; border-radius:8px;
                padding:12px; color:{_TEXT_SEC};
            }}
        """)
        layout.addWidget(self.txt)

    def _clear(self):
        self.txt.clear()

    def append(self, msg: str):
        """Add a timestamped, color-coded log entry."""
        ts = datetime.now().strftime("%H:%M:%S")
        if (
            "completed" in msg.lower()
            or "ok" in msg.lower()
            or "assigned" in msg.lower()
        ):
            color = _SUCCESS
        elif (
            "error" in msg.lower() or "fail" in msg.lower() or "critical" in msg.lower()
        ):
            color = _DANGER
        elif "warning" in msg.lower() or "duplicate" in msg.lower():
            color = _WARNING
        else:
            color = _TEXT_SEC
        ts_html = f"<span style='color:{_TEXT_MUTED}'>{ts}</span>"
        self.txt.append(f"{ts_html}  <span style='color:{color}'>{msg}</span>")
        sb = self.txt.verticalScrollBar()
        sb.setValue(sb.maximum())


# ─────────────────────────────────────────────
# SETTINGS TAB
# ─────────────────────────────────────────────


class SettingsTab(QWidget):
    """Connection testing and background watcher status."""

    def __init__(self, signals: Signals):
        super().__init__()
        self.signals = signals
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        lbl = QLabel("Settings")
        lbl.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color:{_TEXT};")
        layout.addWidget(lbl)

        # Connection tests
        grp = QGroupBox("Connections")
        grp.setStyleSheet(f"""
            QGroupBox {{ border:1px solid {_BORDER}; border-radius:8px; margin-top:12px; padding:20px 14px 14px; color:{_TEXT_SEC}; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:14px; padding:0 6px; }}
        """)
        grp_layout = QVBoxLayout(grp)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        for label, fn in [
            ("Sheets", self._test_sheets),
            ("Drive", self._test_drive),
            ("Gmail", self._test_gmail),
            ("Groq", self._test_groq),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(fn)
            btn_row.addWidget(btn)
        btn_row.addStretch()

        grp_layout.addLayout(btn_row)
        self.lbl_test = QLabel("Click a button to test")
        self.lbl_test.setStyleSheet(
            f"color:{_TEXT_MUTED}; font-size:12px; padding:4px 0;"
        )
        grp_layout.addWidget(self.lbl_test)
        layout.addWidget(grp)
        self.signals.test_result.connect(self.lbl_test.setText)

        # CMR watcher
        grp_cmr = QGroupBox("CMR Folder Watcher")
        grp_cmr.setStyleSheet(f"""
            QGroupBox {{ border:1px solid {_BORDER}; border-radius:8px; margin-top:8px; padding:18px 14px 12px; color:{_TEXT_SEC}; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:14px; padding:0 6px; }}
        """)
        grp_cmr_layout = QVBoxLayout(grp_cmr)
        lbl_cmr_path = QLabel(f"Path: {CMR_WATCH_FOLDER}")
        lbl_cmr_path.setStyleSheet(f"color:{_TEXT_MUTED}; font-size:12px;")
        grp_cmr_layout.addWidget(lbl_cmr_path)
        self.lbl_cmr_status = QLabel("Starting...")
        self.lbl_cmr_status.setStyleSheet(f"color:{_TEXT_SEC}; font-size:12px;")
        grp_cmr_layout.addWidget(self.lbl_cmr_status)
        layout.addWidget(grp_cmr)

        # Invoice checker
        grp_inv = QGroupBox("Invoice Checker")
        grp_inv.setStyleSheet(f"""
            QGroupBox {{ border:1px solid {_BORDER}; border-radius:8px; margin-top:8px; padding:18px 14px 12px; color:{_TEXT_SEC}; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:14px; padding:0 6px; }}
        """)
        grp_inv_layout = QVBoxLayout(grp_inv)
        lbl_inv_path = QLabel(f"Path: {INVOICE_WATCH_FOLDER}")
        lbl_inv_path.setStyleSheet(f"color:{_TEXT_MUTED}; font-size:12px;")
        grp_inv_layout.addWidget(lbl_inv_path)
        self.lbl_inv_status = QLabel("Starting...")
        self.lbl_inv_status.setStyleSheet(f"color:{_TEXT_SEC}; font-size:12px;")
        grp_inv_layout.addWidget(self.lbl_inv_status)
        layout.addWidget(grp_inv)

        layout.addStretch()

    def _test_sheets(self):
        def fn():
            from actions import _get_clients
            from config import LOADS_INFO_SHEET_ID

            _, sheets, _, _ = _get_clients()
            sheets.spreadsheets().get(spreadsheetId=LOADS_INFO_SHEET_ID).execute()

        self._run_test("Sheets", fn)

    def _test_drive(self):
        def fn():
            from actions import _get_clients

            _, _, drive, _ = _get_clients()
            drive.files().list(pageSize=1).execute()

        self._run_test("Drive", fn)

    def _test_gmail(self):
        def fn():
            import imaplib
            from config import EMAIL, EMAIL_PASSWORD

            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(EMAIL, EMAIL_PASSWORD)
            mail.logout()

        self._run_test("Gmail (IMAP)", fn)

    def _test_groq(self):
        def fn():
            from ai_parser import _call_groq

            result = _call_groq("Reply with the single word: ok")
            if not result:
                raise ValueError("Empty response")

        self._run_test("Groq", fn)

    def _run_test(self, name: str, fn):
        self.signals.test_result.emit(f"Testing {name}...")

        def run():
            try:
                fn()
                self.signals.log_message.emit(f"{name}: connected")
                self.signals.test_result.emit(f"{name}: OK")
            except Exception as e:
                self.signals.log_message.emit(f"{name}: {repr(e)}")
                self.signals.test_result.emit(f"{name}: error")

        threading.Thread(target=run, daemon=True).start()


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────


class Sidebar(QWidget):
    """Left navigation sidebar — compact, all 4 items visible without scroll."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(200)
        self.setStyleSheet(
            f"background:{_BG_SURFACE}; border-right:1px solid {_BORDER};"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Logo area (compact) ──
        logo_w = QWidget()
        logo_w.setStyleSheet("background:transparent;")
        logo_l = QHBoxLayout(logo_w)
        logo_l.setContentsMargins(16, 16, 16, 10)
        logo_l.setSpacing(10)

        # "A" circle badge
        logo_circle = QLabel("A")
        logo_circle.setFixedSize(36, 36)
        logo_circle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_circle.setStyleSheet(f"""
            QLabel {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {_ACCENT}, stop:1 {_SUCCESS});
                color:#fff; border-radius:18px;
                font-family:{_FONT_TAG}; font-size:18px; font-weight:700;
                border:none;
            }}
        """)
        logo_l.addWidget(logo_circle)

        title_w = QWidget()
        title_w.setStyleSheet("background:transparent; border:none;")
        title_l = QVBoxLayout(title_w)
        title_l.setContentsMargins(0, 0, 0, 0)
        title_l.setSpacing(1)

        title = QLabel("ACME")
        title.setStyleSheet(
            f"color:{_ACCENT}; font-family:{_FONT_TAG}; font-size:14px; "
            f"font-weight:700; letter-spacing:1.5px; border:none; background:transparent;"
        )
        subtitle = QLabel("Logistics Bot")
        subtitle.setStyleSheet(
            f"color:{_TEXT_MUTED}; font-family:{_FONT_MONO}; font-size:9px; "
            f"border:none; background:transparent;"
        )

        title_l.addWidget(title)
        title_l.addWidget(subtitle)
        logo_l.addWidget(title_w)
        logo_l.addStretch()
        layout.addWidget(logo_w)

        # Separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{_BORDER};")
        layout.addWidget(sep)

        # ── Nav buttons (no QListWidget — direct QPushButtons for precise sizing) ──
        self._nav_buttons: list[QPushButton] = []
        nav_container = QWidget()
        nav_container.setStyleSheet("background:transparent; border:none;")
        nav_layout = QVBoxLayout(nav_container)
        nav_layout.setContentsMargins(8, 8, 8, 8)
        nav_layout.setSpacing(2)

        nav_items = ["Emails", "Dashboard", "Log", "Settings"]
        for i, name in enumerate(nav_items):
            btn = QPushButton(f"  {name}")
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(40)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background:transparent; color:{_TEXT_SEC};
                    border:none; border-radius:8px; border-left:3px solid transparent;
                    text-align:left; padding-left:14px;
                    font-family:{_FONT_BODY}; font-size:13px; font-weight:600;
                }}
                /* ── macOS dock glow on hover ── */
                QPushButton:hover:!checked {{
                    background: qlineargradient(x1:0, y1:0.5, x2:1, y2:0.5,
                        stop:0 rgba(20,184,166,0.10),
                        stop:0.4 rgba(20,184,166,0.14),
                        stop:0.8 rgba(20,184,166,0.06),
                        stop:1 transparent);
                    color:{_TEXT}; border-left:3px solid rgba(20,184,166,0.35);
                }}
                /* ── Selected: teal accent ── */
                QPushButton:checked {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 rgba(20,184,166,0.18), stop:1 transparent);
                    color:{_TEXT}; border-left:3px solid {_ACCENT};
                }}
            """)
            btn.clicked.connect(lambda checked, idx=i: self._on_nav_click(idx))
            nav_layout.addWidget(btn)
            self._nav_buttons.append(btn)

        nav_layout.addStretch()
        layout.addWidget(nav_container, 1)

        # Version at bottom
        ver = QLabel("v2.3")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setStyleSheet(
            f"color:{_TEXT_MUTED}; font-family:{_FONT_MONO}; font-size:9px; "
            f"padding:8px; border:none; background:transparent;"
        )
        layout.addWidget(ver)

        # Public interface for MainWindow
        self._current_idx = 0
        self._row_changed_callback = None

    def _on_nav_click(self, idx: int):
        """Handle nav button click — update checked state and emit change."""
        for i, btn in enumerate(self._nav_buttons):
            btn.setChecked(i == idx)
        self._current_idx = idx
        if self._row_changed_callback:
            self._row_changed_callback(idx)

    def add_item(self, text: str):
        """Compatibility method — items are already created in __init__."""
        pass

    def current_row(self) -> int:
        return self._current_idx

    def set_current_row(self, row: int):
        if 0 <= row < len(self._nav_buttons):
            self._on_nav_click(row)


# ─────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────


class MainWindow(QMainWindow):
    """Application main window with sidebar navigation."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ACME Logistics Bot")
        self.setMinimumSize(1280, 800)
        self.setStyleSheet(STYLE)

        self.signals = Signals()

        self.sidebar = Sidebar()
        self.stack = QStackedWidget()

        self.mail_tab = MailTab(self.signals)
        self.dashboard_tab = DashboardTab(self.signals)
        self.log_tab = LogTab()
        self.settings_tab = SettingsTab(self.signals)

        for tab in (
            self.mail_tab,
            self.dashboard_tab,
            self.log_tab,
            self.settings_tab,
        ):
            self.stack.addWidget(tab)

        # Connect sidebar navigation — uses callback instead of QListWidget signal
        self.sidebar._row_changed_callback = self._on_tab_change
        self.sidebar.set_current_row(0)

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.sidebar)

        content = QWidget()
        content.setStyleSheet(f"background:{_BG_BASE};")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self.stack)
        main_layout.addWidget(content)

        central = QWidget()
        central.setLayout(main_layout)
        self.setCentralWidget(central)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        self.signals.log_message.connect(self._on_log)
        self.signals.status_update.connect(self.status_bar.showMessage)
        self.signals.mail_checked.connect(self._on_mail_checked)

        self._start_cmr_watcher()
        self._start_invoice_checker()

    def _on_tab_change(self, idx: int):
        self.stack.setCurrentIndex(idx)
        if idx == 1:
            self.dashboard_tab.refresh()

    def _on_log(self, msg: str, count: int = 0):
        self.log_tab.append(msg)
        self.status_bar.showMessage(msg[:100])

    def _on_mail_checked(self, count: int):
        self.mail_tab.reload()
        self.status_bar.showMessage(
            f"{count} new email(s) received" if count else "No new emails"
        )

    def _start_cmr_watcher(self):
        self.settings_tab.lbl_cmr_status.setTextFormat(Qt.TextFormat.RichText)
        self.settings_tab.lbl_cmr_status.setText(
            f"<span style='color:{_SUCCESS}'>Watching</span>"
        )

        def run():
            try:
                cmr_watcher.start_watcher()
            except Exception as e:
                self.signals.log_message.emit(f"CMR watcher error: {e}")

        threading.Thread(target=run, daemon=True).start()

    def _start_invoice_checker(self):
        self.settings_tab.lbl_inv_status.setTextFormat(Qt.TextFormat.RichText)
        self.settings_tab.lbl_inv_status.setText(
            f"<span style='color:{_SUCCESS}'>Watching</span>"
        )

        def run():
            try:
                invoice_checker.start_watcher()
            except Exception as e:
                self.signals.log_message.emit(f"Invoice checker error: {e}")

        threading.Thread(target=run, daemon=True).start()


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────


def run():
    """Launch the application."""
    app = QApplication(sys.argv)
    app.setApplicationName("ACME Bot")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
