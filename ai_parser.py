"""
ai_parser.py

AI парсинг ордерів і фактур.
Основний провайдер: Groq (llama-3.3-70b-versatile) — безкоштовний, швидкий.
Fallback: OpenAI (gpt-4o-mini) — якщо Groq вичерпав токени (429/RateLimitError).

Щоб увімкнути fallback — додай в config.py:
  OPENAI_API_KEY = "sk-..."
Якщо ключа немає — fallback мовчки пропускається.
"""

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
import re
from pathlib import Path

import pdfplumber
from groq import Groq, RateLimitError as GroqRateLimitError

from config import GROQ_API_KEY

# OpenAI fallback — підключаємо тільки якщо є ключ
try:
    from config import OPENAI_API_KEY as _OPENAI_KEY
    from openai import OpenAI as _OpenAI, RateLimitError as _OpenAIRateLimitError

    _openai_client = _OpenAI(api_key=_OPENAI_KEY) if _OPENAI_KEY else None
except (ImportError, AttributeError):
    _openai_client = None
    _OpenAIRateLimitError = Exception  # заглушка

_groq_client = Groq(api_key=GROQ_API_KEY)

# ─────────────────────────────────────────────
# ПРАВИЛА КЛАСИФІКАЦІЇ ЛИСТІВ
# ─────────────────────────────────────────────

# subject (lowercase) → carrier_invoice
CARRIER_INVOICE_SUBJECT_KEYWORDS = [
    "f-ra",
    "f-re",
    "fra ",
    "rechnung",
    "faktura",
    "fv/",
    "invoice and documents",
]

# body (lowercase) → carrier_invoice
# IMPORTANT: "netto:" and "brutto:" removed — too generic, appear in orders/contracts too
CARRIER_INVOICE_BODY_KEYWORDS = [
    "do zapłaty",
    "zur zahlung",
    "invoice number",
    "nr faktury",
    "faktura nr",
    "termin zapłaty",
    "termin płatności",
]

# subject (lowercase) → ignore
IGNORE_SUBJECT_PREFIXES = ["re:"]
IGNORE_SUBJECT_COMBOS = [("invoice nr", "pod")]  # обидва слова присутні → ignore


# ─────────────────────────────────────────────
# ВНУТРІШНІ ХЕЛПЕРИ
# ─────────────────────────────────────────────


def _extract_json(content: str) -> dict | None:
    """Витягує JSON з відповіді моделі — навіть якщо є ```json обгортка."""
    content = (content or "").strip()
    content = re.sub(r"^```json\s*", "", content, flags=re.IGNORECASE)
    content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except Exception:
        pass
    # Fallback: шукаємо JSON блок всередині тексту
    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _call_groq(prompt: str) -> str | None:
    """Викликає Groq. Повертає текст або None якщо помилка.
    Re-raises GroqRateLimitError so _call_with_fallback() can switch to OpenAI."""
    try:
        response = _groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return response.choices[0].message.content
    except GroqRateLimitError:
        raise  # bubble up to _call_with_fallback() → OpenAI fallback
    except Exception as e:
        print(f"❌ Groq call error: {e}")
        return None


def _call_openai(prompt: str) -> str | None:
    """Викликає OpenAI gpt-4o-mini як fallback."""
    if not _openai_client:
        return None
    try:
        response = _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ OpenAI call error: {e}")
        return None


def _call_with_fallback(prompt: str) -> str | None:
    """
    Спочатку пробує Groq.
    Якщо Groq повертає RateLimitError (429) — автоматично перемикається на OpenAI.
    Логує який провайдер використовується.
    """
    try:
        result = _call_groq(prompt)
        return result
    except GroqRateLimitError:
        print("Use OpenAI")
        if not _openai_client:
            print("❌  OpenAI ключ не налаштований. Додай OPENAI_API_KEY в config.py")
            return None
        try:
            result = _call_openai(prompt)
            print("✅  OpenAI відповів успішно")
            return result
        except _OpenAIRateLimitError:
            print("❌  OpenAI теж вичерпав ліміт")
            return None
        except Exception as e:
            print(f"❌  OpenAI помилка: {e}")
            return None
    except Exception as e:
        print(f"❌  Groq помилка: {e}")
        return None


def _ocr_pdf_text(pdf_paths: list) -> str:
    """
    OCR fallback for scanned PDFs that pdfplumber returns < 100 chars from.
    Requires: PyMuPDF (fitz), pytesseract, Pillow.
    On Windows, set pytesseract.pytesseract.tesseract_cmd to the .exe path.
    """
    try:
        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image
        import io
    except ImportError:
        print("⚠️ OCR libraries not installed — pip install PyMuPDF pytesseract Pillow")
        return ""

    pytesseract.pytesseract.tesseract_cmd = (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    )
    ocr_text = ""
    for path in pdf_paths:
        if not os.path.exists(path):
            continue
        try:
            doc = fitz.open(path)
            for page in doc:
                pix = page.get_pixmap(dpi=200)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                ocr_text += pytesseract.image_to_string(img, lang="pol+eng") + "\n"
            doc.close()
            print(f"📝 OCR raw text (first 500 chars):\n{ocr_text[:500]}")
        except Exception as e:
            print(f"⚠️ OCR error on {path}: {e}")
    return ocr_text


def _ocr_pdf2image_fallback(pdf_paths: list) -> str:
    """
    Secondary OCR fallback using pdf2image + pytesseract.
    Used when PyMuPDF OCR also fails or returns < 100 chars.
    Requires: pdf2image, pytesseract, Pillow, and poppler in PATH.
    """
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        print("⚠️ pdf2image not installed — pip install pdf2image")
        return ""

    pytesseract.pytesseract.tesseract_cmd = (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    )
    ocr_text = ""
    for path in pdf_paths:
        if not os.path.exists(path):
            continue
        try:
            images = convert_from_path(path, dpi=200)
            for img in images:
                ocr_text += pytesseract.image_to_string(img, lang="pol+eng") + "\n"
            print(f"📝 pdf2image OCR: {len(ocr_text)} chars from {path}")
        except Exception as e:
            print(f"⚠️ pdf2image OCR error on {path}: {e}")
    return ocr_text


def _extract_pdf_text(pdf_paths: list | None) -> str:
    """Витягує текст з PDF файлів. Повертає порожній рядок якщо нічого немає."""
    pdf_text = ""
    if not pdf_paths:
        return pdf_text
    for path in pdf_paths:
        if not os.path.exists(path):
            continue
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        pdf_text += text + "\n"
        except Exception as e:
            print(f"❌ PDF read error {path}: {e}")
    return pdf_text


# ─────────────────────────────────────────────
# BROKERALPHA PDF PRE-PROCESSOR
# ─────────────────────────────────────────────

# Country name → ISO code mapping for BrokerAlpha address blocks
_TC_COUNTRY_MAP = {
    "netherlands": "NL",
    "holland": "NL",
    "nederland": "NL",
    "germany": "DE",
    "deutschland": "DE",
    "poland": "PL",
    "polska": "PL",
    "belgium": "BE",
    "belgique": "BE",
    "belgie": "BE",
    "france": "FR",
    "austria": "AT",
    "czechia": "CZ",
    "czech": "CZ",
    "tsjechie": "CZ",
    "slovakia": "SK",
    "slovensko": "SK",
    "italy": "IT",
    "italia": "IT",
    "spain": "ES",
    "hungary": "HU",
    "romania": "RO",
    "sweden": "SE",
    "denmark": "DK",
    "finland": "FI",
    "norway": "NO",
    "switzerland": "CH",
    "schweiz": "CH",
    "luxembourg": "LU",
    "latvia": "LV",
    "lithuania": "LT",
    "estonia": "EE",
    "bulgaria": "BG",
    "croatia": "HR",
}

# Pattern to match all country names in a line (handles "Netherlands Netherlands")
_TC_COUNTRY_PATTERN = re.compile(
    r"\b(netherlands|holland|nederland|germany|deutschland|poland|polska|"
    r"belgium|belgique|belgie|france|austria|czechia|czech|tsjechie|"
    r"slovakia|slovensko|italy|italia|spain|hungary|romania|sweden|"
    r"denmark|finland|norway|switzerland|schweiz|luxembourg|"
    r"latvia|lithuania|estonia|bulgaria|croatia)\b",
    re.IGNORECASE,
)

# Unified postal code pattern for TC PDF line scanning — matches either:
#   5-digit format: German/PL "10115"
#   4-digit + optional single space + 2-letter suffix: Dutch/BE "1011AB" or "3011 CD" or "1011 AB"
# Groups: (five_digit, four_plus_letters) — exactly one will be non-empty per match.
# NOTE: \s? (0 or 1 space) is intentional — TC PDFs use at most one space inside NL codes.
_TC_POSTAL_RE = re.compile(r"\b(\d{5})\b|\b(\d{4}\s?[A-Z]{2})\b")


def _parse_brokeralpha_pdf(pdf_text: str) -> dict:
    """
    Pre-process BrokerAlpha 'Zamówienie zakupu TCxxxxxx' PDF format with regex.

    BrokerAlpha PDFs have two-column address blocks that pdfplumber merges into
    single lines. Regex is more reliable than AI for this fixed format.
    Returns a dict of fields to override the AI result.
    """
    result = {}

    # Order number: "Zamówienie zakupu : TC400003" or "Zamówienie zakupu TC400003"
    # TC numbers always have 5+ digits — prevents false matches like "TC62" from non-TC orders
    m = re.search(r"Zamówienie zakupu\s*:?\s*(TC\d{5,})", pdf_text)
    if m:
        result["client_order_number"] = m.group(1)

    # Loading date: "Data zamówienia : 23-03-2026" → "23.03.2026"
    m = re.search(r"Data zamówienia\s*:\s*(\d{2})-(\d{2})-(\d{4})", pdf_text)
    if m:
        result["loading_date"] = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
        result["unloading_date"] = result[
            "loading_date"
        ]  # TC has no separate unload date

    # Price: "€ 82.00" — appears on the vehicle dimensions table row, not the header
    m = re.search(r"€\s*(\d+(?:[.,]\d+)?)", pdf_text)
    if m:
        result["price_netto"] = float(m.group(1).replace(",", "."))

    # Addresses — scan lines after "Pochodzenie pojazdu" header
    # Both origin and destination columns are merged side-by-side per line
    lines = pdf_text.splitlines()
    origin_idx = next(
        (i for i, ln in enumerate(lines) if "Pochodzenie pojazdu" in ln), None
    )

    if origin_idx is not None:
        loading_postal = loading_country = None
        unloading_postal = unloading_country = None

        for line in lines[origin_idx + 1 : origin_idx + 10]:
            # Unified postal extraction — handles German (5-digit) and Dutch/BE (4-digit + letters)
            # on the same line in any combination, preserving left-to-right column order.
            # e.g. "10115 Berlin 1011AB Amsterdam" → ["10115", "1011AB"]
            # e.g. "3011 CD Rotterdam 1011 AB Amsterdam" → ["3011 CD", "1011 AB"]
            # BUG 4 fix: group(2) captures full NL/BE code including letter suffix (e.g. "1011 AB")
            if not (loading_postal and unloading_postal):
                postals = [
                    m.group(1) or m.group(2) for m in _TC_POSTAL_RE.finditer(line)
                ]
                if len(postals) >= 2:
                    loading_postal, unloading_postal = postals[0], postals[1]
                elif len(postals) == 1 and loading_postal is None:
                    loading_postal = postals[0]

            # Country names — may appear twice on same line: "Netherlands Netherlands"
            if not (loading_country and unloading_country):
                hits = _TC_COUNTRY_PATTERN.findall(line)
                if len(hits) >= 2:
                    loading_country = _TC_COUNTRY_MAP.get(hits[0].lower())
                    unloading_country = _TC_COUNTRY_MAP.get(hits[1].lower())
                elif len(hits) == 1 and loading_country is None:
                    loading_country = _TC_COUNTRY_MAP.get(hits[0].lower(), "?")
                    unloading_country = loading_country  # same country for both

        if loading_postal and loading_country:
            result["loading_places"] = [f"{loading_country} {loading_postal}"]
        if unloading_postal and unloading_country:
            result["unloading_places"] = [f"{unloading_country} {unloading_postal}"]

    # Fixed values for all BrokerAlpha orders
    result["company_name"] = "BrokerAlpha"
    result["payment_days"] = 14
    result["payment_type"] = "scans"
    result["invoice_email"] = "invoices@brokeralpha.example"

    return result


# ─────────────────────────────────────────────
# INVOICE EMAIL EXTRACTION
# ─────────────────────────────────────────────

# Keywords that signal an invoice-related email address in the body (EN/PL/DE)
_INVOICE_EMAIL_KEYWORDS = [
    # English
    r"send\s+invoices?\s+to",
    r"invoices?\s+to",
    r"billing\s+email",
    r"invoices?\s+(?:should\s+be\s+)?sent?\s+to",
    r"send\s+to",
    # Polish
    r"faktur[ęey]\s+(?:proszę\s+)?(?:wyślij|przesłać|wysłać|kierować)\s+(?:na)?",
    r"faktur[ęey]\s+na(?:\s+adres)?",
    r"faktury?\s+na(?:\s+adres)?",
    r"przesłać\s+faktur[ęey]",
    # German
    r"rechnung(?:en)?\s+an",
    r"rechnung(?:en)?\s+senden\s+an",
    r"rechnungs[\-\s]*e?mail",
]

_EMAIL_RE = re.compile(r"[\w\.\-\+]+@[\w\.\-]+\.\w{2,}")


def extract_invoice_email(body: str, from_email: str = "") -> str | None:
    """
    Extract the email address where the client wants invoices sent.

    Priority:
      1. Keyword match in body (EN/PL/DE) — email on same or next line
      2. Fallback to sender email (from_email)
      3. None if nothing found

    BrokerAlpha is handled separately in parse_order() — always uses
    invoices@brokeralpha.example regardless of what this function returns.
    """
    if not body:
        return from_email or None

    lines = body.splitlines()

    for i, line in enumerate(lines):
        line_low = line.lower()
        for kw_pattern in _INVOICE_EMAIL_KEYWORDS:
            if re.search(kw_pattern, line_low, re.IGNORECASE):
                # Found keyword — look for email on same line first
                email_match = _EMAIL_RE.search(line)
                if email_match:
                    found = email_match.group(0)
                    print(f"Invoice email found in body: {found}")
                    return found
                # Check next line if email not on same line
                if i + 1 < len(lines):
                    email_match = _EMAIL_RE.search(lines[i + 1])
                    if email_match:
                        found = email_match.group(0)
                        print(f"Invoice email found in body (next line): {found}")
                        return found

    # No keyword match — return None so caller falls back to contact_email (col E)
    return None


# ─────────────────────────────────────────────
# ПУБЛІЧНІ ФУНКЦІЇ
# ─────────────────────────────────────────────


def classify_email(
    subject: str, body: str, pdf_text: str, sender_email: str = ""
) -> str:
    """
    Класифікує лист як "order", "carrier_invoice" або "ignore".

    Спочатку перевіряє прості правила (без AI).
    Якщо правила не дають чіткої відповіді — викликає Groq.
    """
    import re

    subj = (subject or "").strip()
    body_low = (body or "").lower()
    sender_low = (sender_email or "").lower()

    # Hard-ignore: replies are never new orders — must check BEFORE stripping prefix.
    # "Re: Zlecenie 123" → clean_subj = "zlecenie 123" which would wrongly match ORDER keywords.
    if re.match(r"^re:\s*", subj, re.IGNORECASE):
        return "ignore"

    # strip reply/forward prefixes before keyword matching
    clean_subj = re.sub(r"^(fwd?|fw|wg|re):\s*", "", subj, flags=re.IGNORECASE).lower()

    # Жорсткі правила - IGNORE
    if "noreply" in sender_low:
        return "ignore"
    if "make.com" in sender_low:
        return "ignore"
    if "newsletter" in sender_low:
        return "ignore"
    if "artlist.io" in sender_low:
        return "ignore"
    if clean_subj.startswith("odp:"):
        return "ignore"

    # Our outgoing invoices to clients — always ignore before any carrier_invoice check
    if "// acme" in clean_subj:
        return "ignore"
    # Match both "zamówienie" (UTF-8) and "zamowienie" (ASCII-mangled MIME decode)
    if "faktura i dokumenty za zam" in clean_subj:
        return "ignore"
    if "faktura elektroniczna nr" in clean_subj and "acme" in sender_low:
        return "ignore"
    # Reply/forward of "faktury" with no transport context → ignore
    subj_low = subj.lower()
    if re.match(r"^re:\s*faktury\b", subj_low):
        return "ignore"
    if re.match(r"^fwd?:\s*faktury\b", subj_low):
        return "ignore"
    # If carrier name in body first line is ACME — ignore (our own truck, not a client order)
    _first_line_low = (
        (body or "").splitlines()[0].lower() if (body or "").strip() else ""
    )
    if "acme" in _first_line_low:
        return "ignore"

    # Жорсткі правила - CARRIER_INVOICE
    if "cmr+fv" in clean_subj:
        return "carrier_invoice"
    if "f-ra" in clean_subj:
        return "carrier_invoice"
    if "faktury od" in clean_subj:
        return "carrier_invoice"

    # --- ignore ---
    for prefix in IGNORE_SUBJECT_PREFIXES:
        if clean_subj.startswith(prefix):
            return "ignore"
    for word_a, word_b in IGNORE_SUBJECT_COMBOS:
        if word_a in clean_subj and word_b in clean_subj:
            return "ignore"

    # --- carrier_invoice (SUBJECT only — body check comes after order keywords) ---
    for kw in CARRIER_INVOICE_SUBJECT_KEYWORDS:
        if kw in clean_subj:
            return "carrier_invoice"

    # --- order (subject keywords) — checked BEFORE carrier_invoice body keywords ---
    # Reason: body often contains "netto:", "brutto:" in orders too, which would
    # misclassify orders as carrier_invoice if body check came first.
    ORDER_SUBJECT_KEYWORDS = [
        "zlecenie",
        "zlecenia",
        "zamówienie",
        "auftrag",
        "transport order",
        "frachtvertrag",
        "epsilontrans",
        "timocom",
        "ladeauftrag",
        "dokumenty do zlecenia",
        "zlecenie przewozowe",
        "position:",
        "umowa przewozu",
        "umowa transportu",
        "fracht wurde angenommen",
    ]
    if any(kw in clean_subj for kw in ORDER_SUBJECT_KEYWORDS):
        return "order"
    # "ZL/00001/..." — Polish "Zlecenie" abbreviation
    if re.match(r"zl/\d", clean_subj):
        return "order"
    # Pure order number in subject (5+ digits) with carrier code in body → order
    if re.match(r"^\d{5,}$", clean_subj):
        return "order"

    # --- carrier_invoice (BODY keywords — more specific, no false positives) ---
    for kw in CARRIER_INVOICE_BODY_KEYWORDS:
        if kw in body_low:
            return "carrier_invoice"

    # --- order (body has known carrier code in first lines → dispatched order) ---
    _BODY_CARRIER_CODES = {
        "KWD",
        "KWDL",
        "LKW4",
        "LKW9",
        "PWT",
        "PWTRANS",
        "OLA",
        "OWN1",
        "OWN2",
    }
    for _bline in (body or "").splitlines()[:5]:
        _bnorm = _bline.strip().upper().replace("-", "").replace(" ", "")
        for _bc in sorted(_BODY_CARRIER_CODES, key=len, reverse=True):
            if _bnorm.startswith(_bc):
                return "order"

    # --- order (має PDF вкладення) ---
    if pdf_text and pdf_text.strip():
        # Але код перевізника в першому рядку не означає автоматично order
        # Перевіряємо спочатку subject на ключові слова invoice
        invoice_kw_found = any(
            kw in clean_subj for kw in CARRIER_INVOICE_SUBJECT_KEYWORDS
        )
        if invoice_kw_found:
            return "carrier_invoice"
        return "order"

    # --- невизначено → Groq ---

    # Завантажуємо приклади з файлу classifier_examples.json
    examples_path = Path("classifier_examples.json")
    examples = []
    if examples_path.exists():
        try:
            with examples_path.open("r", encoding="utf-8") as f:
                examples = json.load(f)
        except Exception:
            examples = []

    if not isinstance(examples, list):
        examples = []

    # Формуємо few-shot приклади для prompt
    example_lines = []
    for ex in examples[:9]:  # максимум 9 прикладів
        ex_type = ex.get("result", "")
        ex_subj = ex.get("subject", "").replace("\n", " ").strip()
        ex_first_line = ex.get("first_line", "").replace("\n", " ").strip()
        if ex_type == "ignore":
            example_lines.append(
                f"EXAMPLE - ignore: Subject: '{ex_subj}', Result: ignore"
            )
        else:
            example_lines.append(
                f"EXAMPLE - {ex_type}: Subject: '{ex_subj}', First line: '{ex_first_line}', Result: {ex_type}"
            )

    few_shot_text = "\n".join(example_lines)

    prompt = (
        "Classify this logistics email into exactly one category: order, carrier_invoice, or ignore.\n"
        "Rules:\n"
        "- carrier_invoice: email is an invoice/bill from a carrier requesting payment\n"
        "- order: email is a transport order request with cargo details\n"
        "- ignore: reply, confirmation, or unrelated message\n"
        "- If subject contains 'F-RA' or 'CMR+FV'  always carrier_invoice\n"
        "- If subject starts with 'Re:' or 'Odp:'  always ignore\n"
        "- If from noreply@ or make.com  always ignore\n"
        "- Carrier code in first line does NOT automatically mean order  check subject for invoice keywords first\n"
        "Reply with ONLY one word: order, carrier_invoice, or ignore.\n\n"
        f"{few_shot_text}\n\n"
        f"Subject: {subject}\n"
        f"First line: {(body or '').splitlines()[0] if body else ''}\n"
    )
    result = _call_with_fallback(prompt)
    if result:
        result = result.strip().lower()
        if result in ("order", "carrier_invoice", "ignore"):
            return result
    return "ignore"


def parse_order(
    subject: str,
    body: str,
    doc_type: str = "",
    pdf_paths: list | None = None,
    *,
    from_email: str = "",
) -> dict | None:
    """
    Парсить транспортний ордер.

    Особливості:
    - company_name = клієнт, НІКОЛИ не Acme
    - BrokerAlpha → invoice_email = invoices@brokeralpha.example
    - Умови оплати з тіла листа важливіші за PDF
    - order_timocom: маршрут з теми, перевізник з першого рядка тіла
    - Перевізник зазвичай написаний в ПЕРШОМУ рядку тіла листа:
      "KWDL Kowalski Trans LU0001A/LU0002B" або "LKW4 WPI00001/WX0001A"
    """
    # Витягуємо carrier_code, payment_days, payment_type, carrier_price з перших 3 рядків тіла
    carrier_codes = {
        "KWD",
        "KWDL",
        "LKW4",
        "LKW9",
        "LKW-4",
        "LKW-9",
        "PWT",
        "OLA",
        "OWN1",
        "OWN2",
        "OWN-1",
        "OWN-2",
        "PW",
        "TRANS",
    }
    lines = (body or "").splitlines()[:10]

    # Detect carrier from first 10 lines — "PW Trans" may appear after forwarded headers
    CARRIER_ALIASES = {
        "LKW4": "LKW4",
        "LKW9": "LKW9",
        "OWN1": "OWN1",
        "OWN2": "OWN2",
        "KWD": "KWD",
        "KWDL": "KWDL",
        "OLA": "OLA",
        "PWT": "PWT",
        "PWTRANS": "PWT",
    }
    # Check longer aliases first so "KWDL..." matches KWDL, not KWD
    detected_carrier = ""
    for _line in lines:
        _norm = _line.strip().upper().replace("-", "").replace(" ", "")
        for _alias in sorted(CARRIER_ALIASES.keys(), key=len, reverse=True):
            if _norm.startswith(_alias):
                detected_carrier = CARRIER_ALIASES[_alias]
                break
        if detected_carrier:
            break

    result_overrides = {}  # collects pre-extracted fields before Groq call

    # Extract license plates from body first line
    first_line = (body or "").strip().split("\n")[0].strip()
    tokens = first_line.split()
    import re as _re

    _plate_pat = _re.compile(r"^[A-Z0-9]{4,12}(/[A-Z0-9]{4,12})?$")
    _plates = [
        t
        for t in tokens
        if _plate_pat.match(t.upper()) and t.upper() not in CARRIER_ALIASES
    ]
    if _plates:
        result_overrides["license_plates"] = "/".join(_plates)

    carrier_code = None
    payment_days = None
    payment_type = None
    carrier_price = None

    for line in lines:
        tokens = line.strip().split()
        # carrier_code — перший токен що відповідає відомому коду
        for token in tokens:
            if token.upper() in carrier_codes:
                carrier_code = token.upper()
                break
        # payment_days — "30 days", "45 dni", "60 Tage"
        m = re.search(r"(\d+)\s*(days|dni|Tage)", line, re.IGNORECASE)
        if m:
            payment_days = int(m.group(1))
        # payment_type
        if re.search(r"scans|skany", line, re.IGNORECASE):
            payment_type = "scans"
        elif re.search(r"originals|oryginaly", line, re.IGNORECASE):
            payment_type = "originals"
        # carrier_price — "1800eur", "1800.50 EUR", "1.800,00 EUR", "1800 PLN", "1800,00 zł"
        m_price = re.search(
            r"([\d]+(?:[.,]\d+)?)\s*(?:eur|pln|z[łl])",
            line,
            re.IGNORECASE,
        )
        if m_price:
            raw_price = m_price.group(1).replace(",", ".")
            try:
                carrier_price = float(raw_price)
            except ValueError:
                pass

    # Filter out "terms & conditions" PDFs — they add noise and confuse the AI.
    # Nordic Road sends AVB (Allgemeine Vertragsbedingungen), Eta Trans sends OWU / Regulamin, etc.
    _NOISE_PDF_PATTERNS = _re.compile(
        r"(?i)(AVB|AGB|OWU|regulamin|warunki|vertragsbedingungen|bedingungen"
        r"|terms.?and.?conditions|ogolne.?warunki)",
    )
    _filtered_pdfs = []
    for _pdf in (pdf_paths or []):
        _basename = _pdf.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        if _NOISE_PDF_PATTERNS.search(_basename):
            print(f"PDF FILTER: skipping terms/conditions file: {_basename}")
        else:
            _filtered_pdfs.append(_pdf)
    # Fallback: if ALL PDFs were filtered out, use originals (better than nothing)
    if not _filtered_pdfs and pdf_paths:
        _filtered_pdfs = list(pdf_paths)

    # Витягуємо текст з PDF файлів
    pdf_text = _extract_pdf_text(_filtered_pdfs)

    # Fix garbled Polish postal codes from pdfplumber table extraction.
    # pdfplumber sometimes splits "00-950" as "-0-0-950" or "0 0-950" when extracting tables.
    if pdf_text:
        # Pattern: "-X-X-XXX" → "XX-XXX" (e.g. "-0-0-950" → "00-950")
        pdf_text = _re.sub(r"-(\d)-(\d)-(\d{3})\b", r"\1\2-\3", pdf_text)
        # Pattern: "X X-XXX" → "XX-XXX" (e.g. "0 0-950" → "00-950")
        pdf_text = _re.sub(r"\b(\d) (\d)-(\d{3})\b", r"\1\2-\3", pdf_text)

    # Detect BrokerAlpha PDF and pre-extract fields with regex before calling AI.
    # Regex is more reliable than AI for TC format because:
    # 1. Price "€ xx.xx" is on a different line than "Cena zakupu" header
    # 2. Address columns are merged side-by-side per line by pdfplumber
    tc_override = {}
    if pdf_text and "Zamówienie zakupu" in pdf_text and "Cena zakupu" in pdf_text:
        tc_override = _parse_brokeralpha_pdf(pdf_text)

    prompt = f"""You extract logistics transport order data. Return ONLY valid JSON, no other text.

CRITICAL RULES for company_name:
- company_name is the TRANSPORT BROKER / FORWARDER / SPEDITOR who hired ACME to perform this transport
- Look for this company in labelled fields: "Zleceniodawca", "Auftraggeber", "Spedycja", "Speditor", "Order placed by", "Dispatched by", "Forwarder", "Freight forwarder"
- The broker appears in the DOCUMENT HEADER or in a labelled "Zleceniodawca / Auftraggeber" block, usually with an order/reference number next to it
- NEVER return the cargo owner, manufacturer, retailer, or the company located at the loading/unloading address — those are NOT our client
- NEVER return retail chains or factories (KAUFLAND, IKEA, LIDL, ALDI, and similar) — these are cargo owners at loading/unloading points, not brokers
- ACME, "Acme sp. z o.o.", "ACME Sp. Z o.o.", "Acme" is ALWAYS us — the carrier/executor (ZLECENIOBIORCA). NEVER return it as company_name
- If you see ACME in the ZLECENIOBIORCA / executor position — ignore it, look for the OTHER company in ZLECENIODAWCA position
- Return the other company's full legal name exactly as written. Never shorten or abbreviate (return "Nordic Road A/S", not "Nordic")
- Never return null if any other company name is visible in the document — look harder

RULES:
- If client is BrokerAlpha (order starts with TC): invoice_email = "invoices@brokeralpha.example"
- Payment info from EMAIL BODY overrides PDF (body is more reliable)

LOADING/UNLOADING PLACES — CRITICAL:
- Format MUST be: "CC POSTAL" where CC = 2-letter country code, POSTAL = postal/ZIP code
- NEVER return a city name — always find the postal code in the document
- Valid examples: "DE 20095", "PL 00-001", "NL 1011 AB", "AT 5020", "IT 20121"
- INVALID examples: "DE Hamburg", "PL Warszawa", "AT Salzburg" — NEVER return these
- INVALID: single/two-letter country code with no postal, e.g. "D", "I", "A", "PL" — NEVER return these
- If postal code not found in document → return null, do not guess
- Multi-stop routes: if the order has MULTIPLE loading or unloading stops, return ALL of them in the array
  Example: loading_places: ["DE 10115", "AT 1010"], unloading_places: ["DE 80331", "DE 50667"]
  NEVER return only the first stop if multiple are listed in the document
- Look in SUBJECT line FIRST — it often contains the route, e.g. "NL - NL", "Leipzig(DE) - Dresden(DE)", "D-55 Mainz --> DE Berlin"
- For Netherlands (NL) and Belgium (BE): postal code is ALWAYS 4 digits + space + 2 letters, e.g. "1011 AB" — NEVER strip the letters
- Polish (PL) postal codes: format XX-XXX (e.g. 00-950, 00-001, 02-001). PDF extraction may garble them — look for 5-digit patterns near city names
- NEVER return an empty array — if postal not found, return null
- loading_places and unloading_places MUST be DIFFERENT locations. NEVER put the same value in both.
- loading_places = where cargo is PICKED UP (Miejsce załadunku / Beladestelle / Loading / Abholung / Odbiór / Laden).
  unloading_places = where cargo is DELIVERED (Miejsce rozładunku / Entladestelle / Unloading / Zustellung / Dostawa / Entladen).
- Slovak postal codes: "SK-811 01" → return "SK 81101". Strip dashes and spaces between digits.
- Old Italian format: "I-20121" → return "IT 20121". Convert "I-" prefix to "IT".
  NEVER swap these. If unsure — look for explicit section labels, not just order of appearance in the document.
- loading_places and unloading_places must contain ALL stops as a list. Return ALL postal codes visible for that direction, not just the first one.
  Example: loading_places: ["DE 20095", "DE 20097"] for a two-pickup route.
- NEVER use "?" as a postal code or country code — return null if unsure

CLIENT ORDER NUMBER:
- client_order_number is the order/reference number that the CLIENT uses to identify this transport
- Common labels: "Zlecenie", "Zlecenie nr", "Auftrag", "Ladeauftrag", "Transportauftrag", "Order", "Reference", "Tour-Number", "Zamówienie", "Zlecenie transportowe", "Zlecenie przewozowe", "Zlecenie spedycyjne"
- Examples of valid formats: "ZL00001/03/26", "Ladeauftrag 11/10001", "Transportauftrag 10001", "Tour-Number: F202600001", "KP/ABC/26-04/001", "520000001", "1001/S/26", "ABCD26000001/XY"
- ALWAYS extract the COMPLETE reference including slashes, letters, and numbers — NEVER truncate at "/" or strip suffixes
- Look in SUBJECT line first, then in the PDF body
- NEVER return null if any reference number is visible in the document

PRICE:
- Search for amounts in BOTH EUR and PLN in both BODY and PDF text
- Keywords to look for: "Vergütung", "wynagrodzenie", "rate", "freight", "netto", "brutto", "kwota", "Betrag", "fracht", "transport", "wynagrodzenie za", "stawka", "cena"
- EUR formats: "1800 EUR", "1800.00 EUR", "EUR 1800", "1.800,00 EUR", "1800,00 EUR", "€ 1800"
- PLN formats: "1800 PLN", "1800,00 PLN", "1800 zł", "1800,00 zł", "PLN 1800"
- European number format: "1.800,00" means 1800.00 (dot=thousands separator, comma=decimal)
- If multiple amounts found, take the one labeled as transport price or the largest
- Set currency field to "EUR" or "PLN" matching the price found
- NEVER return 0.0 — if no price found, return null

PAYMENT DAYS:
- Look for a number directly before or after: "days", "dni", "Tage", "Tag"
- Also recognize patterns: "net 30", "30 days net", "płatność 30"
- If client is BrokerAlpha → payment_days = 14 always
- NEVER return 0 — if unsure, return null

CARRIER:
- The FIRST LINE of the email body usually contains the carrier info
- Format examples: "KWDL Kowalski Trans LU0001A/LU0002B" or "LKW4 WPI00001/WX0001A Ivan Petrenko"
- Extract the first word as truck_type (carrier code), and the full company name as carrier_name
- If carrier code found (KWDL/KWD/LKW4/PWT/OLA/OWN1/OWN2) extract the name after the code
- carrier_name: short trading name only, max 3 words, no legal suffixes like Sp. z o.o. / GmbH / Ltd

- payment_type: "scans" or "originals" only
- if field not found return null, never return 0 or empty string
- If doc_type is "order_timocom": route comes from SUBJECT line, carrier from BODY first line

BROKERALPHA PDF FORMAT ("Zamówienie zakupu TCxxxxxx"):
- client_order_number: value after "Zamówienie zakupu :" e.g. "TC400003"
- loading_places: from "Pochodzenie pojazdu" block — country code + full postal code including letter suffix if present
  Example: "1011 AB Amsterdam, Netherlands" → ["NL 1011 AB"]
- unloading_places: from "Miejsce docelowe pojazdu" block — same format
  Example: "3011 CD Rotterdam, Netherlands" → ["NL 3011 CD"]
- price_netto: number from "Cena zakupu : € xxx.xx"
- loading_date: from "Data zamówienia : DD-MM-YYYY", convert to DD.MM.YYYY
- unloading_date: same as loading_date if not specified separately
- company_name: "BrokerAlpha" always for this format
- payment_days: 14 always for BrokerAlpha
- payment_type: "scans" always for BrokerAlpha

# Explicit extraction rules for known formats:
# - PWT: carrier_name = "PWT", license_plates format "PWxxxxxx", payment_type "scans"
# - KWDL: carrier_name = "Kowalski Trans", license_plates format "LUxxxxxx", payment_type "scans"
# - LKW4: carrier_name = "LKW4 Transport", license_plates format "WPIxxxxxx", payment_type "originals"
# - OLA: carrier_name = "Ola Transport", license_plates format "KNSxxxxxx", payment_type "scans"
# - OWN1/OWN2: carrier_name = "Acme Own Truck", license_plates empty, payment_type "scans"
# - BrokerAlpha: company_name = "BrokerAlpha", invoice_email = "invoices@brokeralpha.example", payment_days = 14, payment_type = "scans"

# Polish Zlecenie (EPSILON, ZETA, etc.):
# - client_order_number: extract the FULL order number exactly as written, including slashes and letters
#   Examples: "Z/62/ABC/4/2026/XY" → "Z/62/ABC/4/2026/XY", "ZT/2026/04/123" → "ZT/2026/04/123"
#   NEVER extract only the numeric part — ALWAYS return the complete reference string
#   NEVER prepend "TC" — TC prefix is ONLY for BrokerAlpha "Zamówienie zakupu TCxxxxxx" format
# - company_name: extract full legal name from subject or body (Zleceniodawca)
# - loading_places and unloading_places: extract country code + postal from body
# - payment_days: extract from body phrases like "30 dni", "45 dni", "60 dni"
# - payment_type: "scans" or "originals" based on keywords in body
# - carrier_name: extract from first line of body if matches known carriers
# - license_plates: extract plate numbers from body if present
# - adr: "YES" if hazardous goods mentioned in body

Country codes: Netherlands/Holland → NL, Germany/Deutschland → DE, Poland/Polska → PL,
Belgium → BE, France → FR, Austria → AT, Czech/Czechia → CZ, Slovakia → SK

ADDITIONAL RULES:
- If the order contains ADR hazardous goods, set "adr" to "YES"
- If license plates are present, extract them as a string in "license_plates"
- If contact email is found in the body, extract it as "contact_email"
- Dates should be in format "DD.MM.YYYY"
- If any field is missing or unclear, return null for that field

{f"CARRIER_OVERRIDE: truck_type is already confirmed as '{detected_carrier}'. Use this value exactly — do not guess from PDF." if detected_carrier else ""}
DOC_TYPE: {doc_type}
SUBJECT: {subject}
BODY (first 2500 chars):
{(body or "")[:2500]}
PDF TEXT (first 2000 chars):
{pdf_text[:2000]}

Return exactly this JSON:
{{
  "client_order_number": null,
  "company_name": null,
  "contact_email": null,
  "invoice_email": null,
  "loading_places": null,
  "unloading_places": null,
  "loading_date": null,
  "unloading_date": null,
  "carrier_name": null,
  "license_plates": null,
  "price_netto": null,
  "currency": null,
  "payment_days": null,
  "payment_type": null,
  "adr": "NO",
  "truck_type": null
}}"""

    content = _call_with_fallback(prompt)
    result = _extract_json(content) if content else None

    # Якщо AI повернув null для цих полів — підставляємо витягнуті значення з тіла листа
    if isinstance(result, dict):
        if detected_carrier:
            result["truck_type"] = (
                detected_carrier  # hard override — first line is authoritative
            )
        elif not result.get("truck_type") and carrier_code:
            result["truck_type"] = carrier_code
        if not result.get("payment_days") and payment_days is not None:
            result["payment_days"] = payment_days
        if not result.get("payment_type") and payment_type:
            result["payment_type"] = payment_type
        if not result.get("price_netto") and carrier_price is not None:
            result["price_netto"] = carrier_price
        # Підставляємо carrier_name з конфіга якщо є truck_type
        from config import CARRIERS

        truck_type = (result.get("truck_type") or "").upper().strip()
        if truck_type in ("PW", "PW TRANS", "PW-TRANS"):
            truck_type = "PWT"
        if truck_type in CARRIERS:
            carrier_cfg = CARRIERS[truck_type]
            result["carrier_name"] = carrier_cfg.get("name", result.get("carrier_name"))
            # If license_plates not found by AI use first plate from config
            if not result.get("license_plates") and carrier_cfg.get("license_plates"):
                result["license_plates"] = (
                    carrier_cfg["license_plates"].split(",")[0].strip()
                )

    # Guard: reject fake TC-prefixed order numbers (e.g. "TC62" from "Z/62/ABC/...")
    # Real TC numbers always have 5+ digits (TC400002, TC400003, etc.)
    if isinstance(result, dict):
        _con = (result.get("client_order_number") or "").strip()
        _tc_fake = re.match(r"^TC(\d{1,4})$", _con)
        if _tc_fake:
            print(
                f"WARNING: AI returned fake TC order number '{_con}' — "
                f"stripped TC prefix, raw='{_tc_fake.group(1)}'"
            )
            # Try to recover full order number from PDF/body text
            # Look for "Z/NN/..." pattern (EPSILON, ZETA style)
            _full_ref = re.search(
                r"[A-Z]{1,3}/\d{1,4}/[A-Z]{2,5}/\d{1,2}/\d{4}/[A-Z]{1,3}",
                f"{(subject or '')} {(body or '')[:3000]} {pdf_text[:2000]}",
            )
            if _full_ref:
                result["client_order_number"] = _full_ref.group(0)
                print(f"  → recovered full order number: '{_full_ref.group(0)}'")
            else:
                result["client_order_number"] = _con.replace("TC", "")

    # Guard: AI often truncates order numbers — returns "068" instead of
    # "KP/ABC/26-04/001" or "62" instead of "Z/62/ABC/4/2026/XY".
    # Recover the full reference from the original text.
    if isinstance(result, dict):
        _ai_nr = (result.get("client_order_number") or "").strip()
        if _ai_nr:
            _haystack = f"{(subject or '')} {(body or '')[:3000]} {pdf_text[:2000]}"
            _recovered = None

            if "/" not in _ai_nr:
                # Case 1: AI returned fragment without slashes — find full ref
                # containing this fragment with slashes around it
                # Matches: "KP/ABC/26-04/001", "Z/62/ABC/4/2026/XY", "1001/S/26"
                _ref_pat = re.search(
                    r"([\w-]{1,10}/[\w/-]*?" + re.escape(_ai_nr) + r"[\w/-]*)",
                    _haystack,
                )
                if _ref_pat:
                    _candidate = _ref_pat.group(1).strip()
                    # Only use if longer than AI result and contains slashes
                    if len(_candidate) > len(_ai_nr) and "/" in _candidate:
                        _recovered = _candidate

                # Case 2: AI result + suffix pattern (ZL00001 → ZL00001/03/26)
                if not _recovered:
                    _suffix_pat = re.search(
                        re.escape(_ai_nr) + r"(/[\w/-]+)",
                        _haystack,
                    )
                    if _suffix_pat:
                        _candidate = _ai_nr + _suffix_pat.group(1)
                        if len(_candidate) > len(_ai_nr):
                            _recovered = _candidate

            if _recovered:
                # Strip trailing punctuation/whitespace
                _recovered = _recovered.rstrip(" .,;:")
                print(f"  → order number recovered: '{_ai_nr}' → '{_recovered}'")
                result["client_order_number"] = _recovered

    # Apply BrokerAlpha regex overrides — regex is authoritative for TC format fields.
    # These override AI output because AI misreads the merged two-column PDF layout.
    if tc_override:
        if not isinstance(result, dict):
            result = {}
        for key, val in tc_override.items():
            if val is not None:
                result[key] = val

    # BUG 1 fix: remove "?" placeholders from place lists
    if isinstance(result, dict):
        for field in ("loading_places", "unloading_places"):
            places = result.get(field)
            if isinstance(places, list):
                result[field] = [p for p in places if p and p != "?"]
        # If loading == unloading (AI hallucination), clear unloading so dispatcher notices
        if result.get("loading_places") and result.get("loading_places") == result.get(
            "unloading_places"
        ):
            result["unloading_places"] = []

        # BUG 7 fix: restore NL/BE 4-digit+letter suffix truncated by AI.
        # AI sometimes returns "NL 1011" — scan original text for "1011 AB" and patch.
        _nl_scan = re.compile(r"\b(\d{4})\s([A-Z]{2})\b")
        _scan_text = f"{subject or ''} {(body or '')[:3000]} {pdf_text[:1000]}"
        _nl_map = {
            _m.group(1): f"{_m.group(1)} {_m.group(2)}"
            for _m in _nl_scan.finditer(_scan_text)
        }
        if _nl_map:
            for _f in ("loading_places", "unloading_places"):
                _places = result.get(_f)
                if not isinstance(_places, list):
                    continue
                _patched = []
                for _p in _places:
                    # Match "CC 1011" (country code + 4 digits only, no letter suffix yet)
                    _pm = re.match(r"^([A-Z]{2})\s+(\d{4})$", (_p or "").strip())
                    if _pm and _pm.group(2) in _nl_map:
                        _patched.append(f"{_pm.group(1)} {_nl_map[_pm.group(2)]}")
                    else:
                        _patched.append(_p)
                result[_f] = _patched

    # Normalize old-style country codes returned by AI: "I-20121" → "IT 20121", "SK-811 01" → "SK 81101"
    for _pf in ("loading_places", "unloading_places"):
        _raw = result.get(_pf)
        if not isinstance(_raw, list):
            continue
        _fixed = []
        for _p in _raw:
            _ps = (_p or "").strip()
            # "I-20121" or "I 20121" → "IT 20121"
            _m = _re.match(r"^I[- ](\d{5})\b", _ps)
            if _m:
                _ps = f"IT {_m.group(1)}"
            # "SK-811 01" → "SK 81101" (strip dash and space between digit groups)
            _m = _re.match(r"^SK[- ]?(\d{3})\s*(\d{2})\b", _ps)
            if _m:
                _ps = f"SK {_m.group(1)}{_m.group(2)}"
            _fixed.append(_ps)
        result[_pf] = _fixed

    # Strip trailing letter suffixes (NL format) and street names from postal codes.
    # "NL 1011 AB" → "NL 1011",  "NL 3011 Damrak" → "NL 3011",  "DE 10115 Hauptstraße" → "DE 10115"
    def _strip_street(place: str) -> str:
        """Remove letter suffix (NL) and street names that follow the postal code digits."""
        place = place.strip()
        # NL: strip 1-2 uppercase letter suffix after 4-digit code
        place = _re.sub(r"(\bNL \d{4})\s+[A-Z]{1,2}\b.*", r"\1", place)
        # Any country: strip anything (street name etc.) after the postal digits block
        place = _re.sub(r"([A-Z]{2}\s+\d{4,5}(?:-\d{3})?)\s+\S.*", r"\1", place)
        return place.strip()

    for _sf in ("loading_places", "unloading_places"):
        _sp = result.get(_sf)
        if isinstance(_sp, list):
            result[_sf] = [_strip_street(p) for p in _sp if p]

    # Guard: drop invalid place entries — city names or bare country codes
    _CITY_PAT = _re.compile(r"^[A-Z]{2}\s+[A-Z][a-z]")  # "DE Hamburg"
    _COUNTRY_ONLY = _re.compile(r"^[A-Z]{1,2}$")  # "D", "I", "A", "PL"
    for _pf in ("loading_places", "unloading_places"):
        _raw = result.get(_pf)
        # AI sometimes returns a string instead of a list — wrap it so join() doesn't split chars
        if isinstance(_raw, str) and _raw.strip():
            _raw = [_raw.strip()]
            result[_pf] = _raw
        if not isinstance(_raw, list):
            continue
        _cleaned = []
        for _place in _raw:
            _ps = str(_place).strip() if _place else ""
            if not _ps:
                continue
            if _COUNTRY_ONLY.match(_ps):
                print(f"WARNING: country-only code in {_pf}: {_ps!r} — dropped")
            elif _CITY_PAT.match(_ps):
                print(
                    f"WARNING: city name instead of postal in {_pf}: {_ps!r} — dropped"
                )
            else:
                _cleaned.append(_place)
        # Deduplicate while preserving order (AI sometimes repeats the same stop)
        _seen = set()
        _deduped = []
        for _p in _cleaned:
            _key = (_p or "").strip().upper()
            if _key not in _seen:
                _seen.add(_key)
                _deduped.append(_p)
        result[_pf] = _deduped if _deduped else None

    # Normalize place strings: strip trailing letter suffixes (NL) and street/city names.
    # Keeps only "CC NNNN" or "CC NNNNN(-NN)?" — prevents "NL 1011 AB" or "DE 10115 Hauptstraße".
    _POSTAL_KEEP = _re.compile(r"^([A-Z]{2} \d{4,6}(?:-\d{2,4})?)")
    for _nf in ("loading_places", "unloading_places"):
        _raw_list = result.get(_nf)
        if not isinstance(_raw_list, list):
            continue
        _normed = []
        for _entry in _raw_list:
            _pm = _POSTAL_KEEP.match((_entry or "").strip())
            _normed.append(_pm.group(1) if _pm else _entry)
        result[_nf] = _normed if _normed else None

    # Guard: ACME is never the client — clear if AI returned our own name
    _ACME_NAMES = {
        "acme",
        "acme",
        "acme sp. z o.o.",
        "acme sp z o o",
    }
    _cn = (result.get("company_name") or "").lower().strip()
    # Normalise punctuation for comparison
    _cn_norm = _re.sub(r"[.\s]+", " ", _cn).strip()
    if _cn_norm in _ACME_NAMES or _cn_norm.startswith("acme"):
        print(
            f"WARNING: company_name was '{result['company_name']}' (our own company) — cleared, needs manual check"
        )
        result["company_name"] = None

    # Guard: known retail/cargo brands are never transport brokers — warn if returned
    # These appear as cargo owners at loading points, not as order issuers
    _CARGO_BRAND_FRAGMENTS = {
        "kaufland",
        "ikea",
        "lidl",
        "aldi",
        "rewe",
        "metro",
        "carrefour",
        "penny",
    }
    _cn_check = (result.get("company_name") or "").lower()
    if any(brand in _cn_check for brand in _CARGO_BRAND_FRAGMENTS):
        print(
            f"WARNING: company_name='{result['company_name']}' looks like a cargo owner (retail chain) — possible wrong extraction, needs manual check"
        )

    # Guard: carrier_name must not be the same as company_name (client).
    # AI sometimes confuses client with carrier when no carrier code is in the body.
    _carrier_raw = (result.get("carrier_name") or "").lower().strip()
    _client_raw = (result.get("company_name") or "").lower().strip()
    def _norm_company(s: str) -> str:
        """Strip legal suffixes for fuzzy company name comparison."""
        return _re.sub(r"\b(gmbh|sp\.?\s*z\s*o\.?\s*o\.?|sp\.?\s*k\.?|s\.?r\.?l\.?|a/?s|co\.?\s*kg|ag|se)\b", "", s).strip().rstrip(".")

    if _carrier_raw and _client_raw:
        if _norm_company(_carrier_raw) == _norm_company(_client_raw) or _carrier_raw in _client_raw or _client_raw in _carrier_raw:
            print(
                f"WARNING: carrier_name '{result['carrier_name']}' matches company_name '{result['company_name']}' — cleared"
            )
            result["carrier_name"] = None

    # Normalize numeric fields — AI sometimes returns "500,00" (string) instead of 500.0 (float).
    # This causes downstream issues: Google Sheets gets a string, formulas break.
    def _parse_price(raw: object) -> float | None:
        """Convert price string to float. Handles DE/PL format '1.200,50' → 1200.5."""
        if isinstance(raw, (int, float)):
            return float(raw)
        if not isinstance(raw, str):
            return None
        s = raw.strip()
        # Remove currency symbols and whitespace
        s = _re.sub(r"[€$£PLNeEuUrR\s]", "", s)
        if not s:
            return None
        # Detect European format: "1.200,50" (dot=thousands, comma=decimal)
        if "," in s and "." in s:
            last_dot = s.rfind(".")
            last_comma = s.rfind(",")
            if last_comma > last_dot:
                # Comma is decimal separator: "1.200,50"
                s = s.replace(".", "").replace(",", ".")
            else:
                # Dot is decimal separator: "1,200.50"
                s = s.replace(",", "")
        elif "," in s:
            # Only comma: "500,00" → decimal separator
            s = s.replace(",", ".")
        # Remove any remaining thousands separators (spaces)
        s = s.replace(" ", "")
        try:
            return float(s)
        except ValueError:
            print(f"WARNING: could not parse price {raw!r} as float")
            return None

    for _price_field in ("price_netto", "carrier_price", "carrier_netto"):
        result[_price_field] = _parse_price(result.get(_price_field))

    # Normalize payment_days — same issue: "30" → 30
    _pd = result.get("payment_days")
    if isinstance(_pd, str):
        try:
            result["payment_days"] = int(_pd.strip())
        except ValueError:
            result["payment_days"] = None

    # Invoice email: use body keyword extraction, fallback to sender
    # BrokerAlpha always overrides to invoices@brokeralpha.example (set by AI prompt)
    _is_tc = (result.get("company_name") or "").lower().startswith("brokeralpha")
    if _is_tc:
        result["invoice_email"] = "invoices@brokeralpha.example"
    elif not result.get("invoice_email"):
        result["invoice_email"] = extract_invoice_email(body, from_email)

    return result


def _extract_amounts(text: str) -> tuple:
    """
    Extracts (netto, vat, brutto, currency) from invoice text using regex.
    EUR takes priority over PLN — handles dual-currency invoices (OLA SPEED etc.).
    Returns (None, None, None, None) if nothing found.
    """

    def _num(s: str) -> float | None:
        try:
            return float(s.replace(" ", "").replace(",", "."))
        except (ValueError, AttributeError):
            return None

    # STEP 1 — try EUR (multiple patterns, highest priority wins)
    _eur_brutto_patterns = [
        r"Do\s+zap[l\u0142t][a\u0105q]ty[:\s]*([\d\s]+[,\.]\d{2})\s*EUR",  # Do zapłaty: X EUR
        r"(?i)do\s+\w{3,}\s+([\d][\d ]+[,\.]\d{2})\s*EUR",  # OCR-tolerant: any "Do <word> X EUR"
        r"Warto[s\u015b][c\u0107]\s+brutto\s+([\d\s]+[,\.]\d{2})\s*EUR",  # Wartość brutto X EUR
    ]
    brutto_eur = None
    for _pat in _eur_brutto_patterns:
        _m = re.search(_pat, text, re.IGNORECASE)
        if _m:
            brutto_eur = _num(_m.group(1))
            if brutto_eur:
                break
    if brutto_eur:
        netto = vat = None
        # Netto from EUR block
        _mn = re.search(
            r"Warto[s\u015b][c\u0107]\s+netto\s+([\d\s]+[,\.]\d{2})\s*EUR",
            text,
            re.IGNORECASE,
        ) or re.search(
            r"Warto\u015bci bez podatku \[EUR\][^\n]*([\d\s]+[,\.]\d{2})", text
        )
        if _mn:
            netto = _num(_mn.group(1))
        _mv = re.search(
            r"Warto\u015b\u0107 podatku \[EUR\][^\n]*([\d\s]+[,\.]\d{2})", text
        )
        if _mv:
            vat = _num(_mv.group(1))
        return netto, vat, brutto_eur, "EUR"

    # STEP 2 — try PLN with OCR-tolerant patterns (ł→l/t, ą→a/q, ś→s)
    # OCR commonly misreads: zapłaty→zaptaty, zł→zt/zl, Wartość→Wartosc
    _brutto_pln_patterns = [
        r"Do\s+zap[l\u0142t][a\u0105q]ty[:\s]*([\d\s]+[,\.]\d{2})\s*z[l\u0142t]",
        r"Do\s+zap[l\u0142t][a\u0105q]ty[:\s]*([\d\s]+[,\.]\d{2})(?!\s*EUR)",  # no-currency fallback — never capture EUR line
        r"Razem[:\s]*([\d\s]+[,\.]\d{2})\s*z[l\u0142t]",
        r"RAZEM[:\s]*([\d\s]+[,\.]\d{2})(?!\s*EUR)",
    ]
    brutto = None
    for pat in _brutto_pln_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            brutto = _num(m.group(1))
            if brutto:
                break
    if brutto:
        netto = vat = None
        # Netto: OCR-tolerant variants of "Wartość netto" and summary pipe row
        _netto_pln_patterns = [
            r"Warto[s\u015b][s\u015b][c\u0107]?\s+netto\s+([\d\s]+[,\.]\d{2})",
            r"Netto\s+([\d\s]+[,\.]\d{2})\s*z[l\u0142t]",
            r"([\d][\d\s]+,\d{2})\s*z[l\u0142t]\s*\|",
        ]
        for pat in _netto_pln_patterns:
            mn = re.search(pat, text, re.IGNORECASE)
            if mn:
                netto = _num(mn.group(1))
                if netto:
                    break
        # VAT: summary pipe row "23% | X zł | Y zł"
        ms = re.search(
            r"\d+%\s*\|\s*([\d\s]+,\d{2})\s*z[l\u0142t]\s*\|\s*([\d\s]+,\d{2})\s*z[l\u0142t]",
            text,
            re.IGNORECASE,
        )
        if ms:
            netto = netto or _num(ms.group(1))
            vat = _num(ms.group(2))
        return netto, vat, brutto, "PLN"

    return None, None, None, None


def parse_invoice(
    body: str, pdf_text: str = "", pdf_paths: list | None = None
) -> dict | None:
    """
    Парсить фактуру від перевізника (carrier invoice).
    """
    # Extract text from PDF files; fall back to OCR for scanned PDFs
    pdf_text = _extract_pdf_text(pdf_paths) or pdf_text

    if len(pdf_text.strip()) < 100 and pdf_paths:
        _fname_log = Path(pdf_paths[0]).name if pdf_paths else "?"
        print(
            f"📄 OCR triggered for {_fname_log}, text length={len(pdf_text.strip())} — trying PyMuPDF OCR"
        )
        ocr_text = _ocr_pdf_text(pdf_paths)
        if len(ocr_text.strip()) >= 100:
            print(f"✅ PyMuPDF OCR extracted {len(ocr_text)} chars")
            pdf_text = ocr_text
        else:
            print(
                f"⚠️ PyMuPDF OCR yielded {len(ocr_text.strip())} chars — trying pdf2image fallback"
            )
            ocr_text2 = _ocr_pdf2image_fallback(pdf_paths)
            if len(ocr_text2.strip()) >= 100:
                print(f"✅ pdf2image OCR extracted {len(ocr_text2)} chars")
                pdf_text = ocr_text2
            else:
                print("❌ Cannot parse scan — manual entry required")
            return {
                "invoice_number": None,
                "carrier_name": None,
                "order_number": None,
                "amount_netto": None,
                "vat": None,
                "amount_brutto": None,
                "currency": None,
                "service_date": None,
                "payment_deadline": None,
                "payment_days": None,
                "payment_type": None,
            }

    prompt = f"""You extract carrier invoice data from logistics. Return ONLY valid JSON, no other text.

RULES:
- invoice_number: extract the invoice/faktura number. Look for:
  * "Faktura FA 7/3/26" → extract "FA 7/3/26"
  * "Faktura numer 48/03/2026" → extract "48/03/2026"
  * "Faktura F/DD/MM/YY/XXX" → extract "F/DD/MM/YY/XXX"
  * "Fakturanr 110/02/2026" → extract "110/02/2026"  (no space between Faktura and nr)
  * "FV/YYYY/MM/NNN", "Nr faktury:", "Faktura nr", "Faktura VAT" followed by the number
  * Return only the number part (text after "Faktura"/"Fakturanr" keyword), not the keyword itself
- order_number: look for any of these labels followed by a value:
  * "Zlecenie spedycyjne 2026-03-00074" → "2026-03-00074"
  * "Numer zlecenia: 2026-03-00145" / "Nr zlecenia:"
  * "Transport order 2026-03-00096" → "2026-03-00096"
  * "Uwagi: Transport order XXXX" / "Uwagi:" / "zamówienie nr"
  Return the order number value only, not the label.
- carrier_name: extract from "Sprzedawca:" block ONLY — the company issuing the invoice.
  NEVER return "ACME" or any name containing "Acme" — ACME Sp. z o.o. is always
  the buyer (Nabywca), not the seller. Email signatures with "ACME" are not the carrier.
  If only ACME is found → return null.
- currency: return "EUR" if invoice amounts are in euros, "PLN" if in Polish złoty.
  If both PLN and EUR are present (dual-currency invoice), use EUR — return "EUR".
- amount_netto, vat, amount_brutto: CRITICAL — NET price for ONE transport order only.
  * amount_netto = NETTO (before VAT) for the single order matching order_number. NEVER the invoice total.
  * If invoice lists multiple orders (multiple "Zlecenie"/"Transport order" entries):
    find the row/line matching order_number and return ONLY that row's netto. Return null if unsure.
  * amount_brutto = amount_netto + VAT for that same single order row.
  * NEVER return BRUTTO as amount_netto.
  If both PLN and EUR are present in the same PDF, use the EUR column/table only.
  For PLN-only invoices (amounts in "zł"), extract PLN values.
  Look for "Do zapłaty: X EUR", "Wartość netto X EUR", table labeled "[EUR]", or amounts followed by "EURO"/"EUR".
  Example: "Do zapłaty: 1 250,00 EUR" → amount_brutto = 1250.0, currency = "EUR"
  Example: "1200 EURO" in service description → amount_netto = 1200.0
  "Wartości bez podatku [EUR]" row → amount_netto; "Wartość podatku [EUR]" → vat; "Należność ogółem: X EUR" → amount_brutto
  Polish format "1 210,00" → 1210.0 (remove spaces, replace comma with dot)
- service_date: date the transport service was completed, in DD.MM.YYYY format.
  "Data zakończenia dostawy: 2026-03-17" → "17.03.2026"
  "Data sprzedaży: 2026-03-18" → "18.03.2026"
  "GTU 13 Data wykonania usługi 17.03.2026" → "17.03.2026"
  Convert YYYY-MM-DD → DD.MM.YYYY.
- payment_deadline: deadline date for payment, in DD.MM.YYYY format.
  "Termin zapłaty do: 20.04.2026" → "20.04.2026"
  "Termin płatności: 2026-04-03" → "03.04.2026"
  Convert YYYY-MM-DD → DD.MM.YYYY.
- payment_days: calculate as integer days between invoice date and payment_deadline. Null if either missing.
- if field not found return null, never return 0 or empty string

BODY (first 1000 chars):
{(body or "")[:1000]}
PDF TEXT (first 2000 chars):
{pdf_text[:2000]}

Return exactly this JSON:
{{
  "invoice_number": null,
  "carrier_name": null,
  "order_number": null,
  "amount_netto": null,
  "vat": null,
  "amount_brutto": null,
  "currency": "EUR",
  "service_date": null,
  "payment_deadline": null,
  "payment_days": null,
  "payment_type": null
}}"""

    content = _call_with_fallback(prompt)
    result = _extract_json(content) if content else None
    if not result:
        result = {}

    _full = (pdf_text or "") + "\n" + (body or "")

    # Detect currency from payment-context patterns only (not exchange-rate lines like "1 EUR = 4,25 PLN")
    _has_eur_payment = bool(
        re.search(
            r"(Do\s+zap\S+ty.*EUR|Waluta\s*:\s*EUR|nale\S+no\S+.*EUR)",
            _full,
            re.IGNORECASE,
        )
    )
    _has_pln_payment = bool(
        re.search(
            r"(Do\s+zap\S+ty.*z[l\u0142t]|Razem.*z[l\u0142t])",
            _full,
            re.IGNORECASE,
        )
    )
    # Filename is a reliable hint (e.g. "faktura_PLN.pdf")
    _fname = Path(pdf_paths[0]).name.upper() if pdf_paths else ""
    if "PLN" in _fname:
        _detected_currency = "PLN"
    elif "EUR" in _fname or "\u20ac" in _fname:
        _detected_currency = "EUR"
    elif _has_eur_payment and not _has_pln_payment:
        _detected_currency = "EUR"
    elif _has_pln_payment:
        _detected_currency = "PLN"
    else:
        _detected_currency = None  # unknown — keep AI result

    r_netto, r_vat, r_brutto, r_currency = _extract_amounts(_full)

    if r_currency == "EUR":
        # EUR amounts confirmed by regex: override AI (fixes dual-currency AI picking PLN)
        result["amount_brutto"] = r_brutto
        result["currency"] = "EUR"
        if r_netto is not None:
            result["amount_netto"] = r_netto
        if r_vat is not None:
            result["vat"] = r_vat
    elif r_currency == "PLN":
        # PLN amounts found: always set currency (even if AI already filled amount_brutto as EUR)
        result["currency"] = "PLN"
        if r_brutto is not None and not result.get("amount_brutto"):
            result["amount_brutto"] = r_brutto
        if r_netto is not None and not result.get("amount_netto"):
            result["amount_netto"] = r_netto
        if r_vat is not None and not result.get("vat"):
            result["vat"] = r_vat

    # Final text-based currency override — only when regex found nothing (don't overwrite confirmed EUR)
    if _detected_currency and r_currency != "EUR":
        result["currency"] = _detected_currency

    # Carrier-level override: KWDL (Kowalski Trans / Kowalski) always invoices in EUR.
    # Also re-extracts EUR amounts in case PLN values were captured by earlier fallbacks.
    _cname = (result.get("carrier_name") or "").lower()
    if "kowalski" in _cname or "kowalski" in _cname:
        result["currency"] = "EUR"

        def _num_eur(s: str) -> float | None:
            """Parse European-format number string to float."""
            try:
                return float(s.replace(" ", "").replace(",", "."))
            except (ValueError, AttributeError):
                return None

        # Re-extract EUR brutto — "Do zapłaty X EUR" or "Wartość brutto X EUR"
        for _ep in [
            r"(?i)do\s+\w{3,}\s+([\d][\d ]+[,\.]\d{2})\s*EUR",
            r"Warto[s\u015b][c\u0107]\s+brutto\s+([\d\s]+[,\.]\d{2})\s*EUR",
        ]:
            _em = re.search(_ep, _full, re.IGNORECASE)
            if _em:
                _v = _num_eur(_em.group(1))
                if _v:
                    result["amount_brutto"] = _v
                    break

        # Re-extract EUR netto — "Wartość netto X EUR"
        _enm = re.search(
            r"Warto[s\u015b][c\u0107]\s+netto\s+([\d\s]+[,\.]\d{2})\s*EUR",
            _full,
            re.IGNORECASE,
        )
        if _enm:
            _v = _num_eur(_enm.group(1))
            if _v:
                result["amount_netto"] = _v

    # "1051 EURO" or "1051,00 EURO" inline in service description → netto.
    # Skip payment/summary lines to avoid matching brutto totals.
    _SKIP_LINE_PAT = re.compile(
        r"(?i)^\s*(?:do\s+zap|razem|s\u0142ownie|w\s+tym|nale\u017cno\u015b\u0107)",
        re.UNICODE,
    )
    if not result.get("amount_netto"):
        _ep = re.compile(r"(?<!\d)(\d{3,5}(?:[,.]\d{2})?)\s*EURO?\b")
        for _line in _full.splitlines():
            if _SKIP_LINE_PAT.match(_line):
                continue
            _m = _ep.search(_line)
            if _m:
                try:
                    result["amount_netto"] = float(
                        _m.group(1).replace(" ", "").replace(",", ".")
                    )
                    if not result.get("currency"):
                        result["currency"] = "EUR"
                    break
                except ValueError:
                    pass

    print(f"🔍 pdf_text length after OCR: {len(pdf_text)}")
    print(f"🔍 amount_netto after regex: {result.get('amount_netto')}")
    print(f"🔍 currency after regex: {result.get('currency')}")

    # PLN-format structural fallbacks (only when AI missed the field)
    if not result.get("invoice_number"):
        for _ip in [
            r"Faktura\s+(F/[\w/]+)",  # "Faktura F/03/2026/XX"
            r"Fakturanr\s+(\d[\d/]+)",  # "Fakturanr 110/02/2026"
            r"(?:Nr\s+faktury|Faktura\s+nr|Faktura\s+VAT)[:\s]+([\w/][\w/\s.-]*?)(?:\s{2,}|\n|$)",
        ]:
            _m = re.search(_ip, _full, re.IGNORECASE | re.MULTILINE)
            if _m:
                result["invoice_number"] = _m.group(1).strip()
                break

    if not result.get("order_number"):
        for _op in [
            r"Numer zlecenia[:\s]+(20\d{2}-\d{2}-\d{5})",
            r"Transport order\s+(20\d{2}-\d{2}-\d{5})",
            r"Uwagi.*?(20\d{2}-\d{2}-\d{5})",
        ]:
            _m = re.search(_op, _full, re.IGNORECASE)
            if _m:
                result["order_number"] = _m.group(1)
                break

    if not result.get("carrier_name"):
        _m = re.search(r"(Transport Us\u0142ugi Adam Nowak|Adam Nowak)", _full)
        if _m:
            result["carrier_name"] = _m.group(1)

    # Multi-order invoice fix: when the PDF contains more than one distinct order number,
    # use _extract_multi_order_amounts() to build an exact order→amount map and look up
    # the AI-identified order_number.  This handles:
    #   - bot too LOW: AI read only the first line amount instead of the right one
    #   - bot too HIGH: AI read the grand "Do zapłaty" total instead of per-order netto
    _order_re_mo = re.compile(r"\b20\d{2}-\d{2}-\d{5}\b")
    _unique_orders_mo = set(_order_re_mo.findall(_full))
    if len(_unique_orders_mo) > 1:
        _target_order = result.get("order_number")
        _multi_map = _extract_multi_order_amounts(_full)
        if _target_order and _target_order in _multi_map:
            _per_order = _multi_map[_target_order]
            print(
                f"[parse_invoice] multi-order ({len(_unique_orders_mo)} orders): "
                f"matched {_target_order} → amount_netto={_per_order}"
            )
            result["amount_netto"] = _per_order
            result["amount_brutto"] = (
                None  # can't isolate brutto for one order reliably
            )
            result["vat"] = None
        elif _target_order:
            print(
                f"[parse_invoice] multi-order: order {_target_order!r} not in regex map "
                f"{list(_multi_map.keys())[:5]} — clearing amount_netto"
            )
            result["amount_netto"] = None
            result["amount_brutto"] = None
            result["vat"] = None
        # No order_number from AI → fall through to the >5000 guard below

    # Guard: if amount still looks like an invoice total (>5000) AND multiple order numbers
    # found → clear rather than write wrong value (catches cases where multi-order map
    # had no match and AI returned the brutto total).
    _amount = result.get("amount_netto")
    if _amount and _amount > 5000:
        if len(_unique_orders_mo) > 1:
            print(
                f"⚠️  amount_netto={_amount} with {len(_unique_orders_mo)} order numbers in PDF "
                f"— possible invoice total instead of per-order amount → cleared"
            )
            result["amount_netto"] = None
            result["amount_brutto"] = None
            result["vat"] = None

    return result if result else None


def _extract_multi_order_amounts(text: str) -> dict:
    """
    For multi-order invoices: build {order_number: amount_netto} from raw PDF text.

    Scans for each 2026-MM-NNNNN order number then finds the first EUR/EURO amount
    that follows it within 500 characters (non-greedy, handles multi-line blocks).
    First match per order number wins — prevents double-counting.

    Returns {} when no order-amount pairs are found.
    """
    _ORDER_AMOUNT_RE = re.compile(
        r"\b(20\d{2}-\d{2}-\d{5})\b"  # order number
        r".{0,500}?"  # intervening text (non-greedy)
        r"(?<!\d)(\d{3,5}(?:[,.]\d{2})?)\s*EURO?\b",  # amount + EUR or EURO keyword
        re.IGNORECASE | re.DOTALL,
    )
    result: dict = {}
    for m in _ORDER_AMOUNT_RE.finditer(text):
        order_nr = m.group(1)
        if order_nr in result:
            continue  # first EUR value after each order number wins
        try:
            result[order_nr] = float(m.group(2).replace(",", "."))
        except ValueError:
            pass
    return result


def parse_invoice_nowak(pdf_text: str, invoice_number: str | None = None) -> list[dict]:
    """
    Parse a Adam Nowak (LKW4/LKW9) invoice that lists multiple transport orders.

    Each order appears as:
      USŁUGA TRANSPORTOWA W/G ZLECENIA 2026-02-00069 Z DNIA ... FRACHT route NNNN EUR

    Returns a list of dicts, one per order:
      {"invoice_number": str|None, "order_number": str, "amount_netto": float,
       "carrier_name": "Adam Nowak Transport", "currency": "EUR"}

    Falls back to empty list if no matches found.
    """
    _ORDER_PAT = re.compile(
        r"ZLECENIA\s+(2026-\d{2}-\d{5})"  # order number
        r".*?"  # any text (route, date, etc.)
        r"(\d{3,4}(?:[,.]\d{2})?)\s*EUR",  # amount + EUR
        re.IGNORECASE | re.DOTALL,
    )

    results = []
    for m in _ORDER_PAT.finditer(pdf_text):
        order_nr = m.group(1)
        raw_amount = m.group(2).replace(",", ".").replace(" ", "")
        try:
            amount = float(raw_amount)
        except ValueError:
            amount = None
        results.append(
            {
                "invoice_number": invoice_number,
                "order_number": order_nr,
                "amount_netto": amount,
                "carrier_name": "Adam Nowak Transport",
                "currency": "EUR",
                "vat": None,
                "amount_brutto": None,
                "service_date": None,
                "payment_deadline": None,
                "payment_days": None,
                "payment_type": "scans",
            }
        )

    if results:
        print(f"[parse_invoice_nowak] found {len(results)} orders in invoice")
    return results


_INVOICE_HEADER_RE = re.compile(r"Faktura\s+(FA|F/)", re.IGNORECASE)


def parse_invoice_multipage(pdf_path: str) -> list:
    """
    Reads a PDF page by page and returns a list of parsed invoice dicts.
    Use when a single PDF may contain multiple invoices (e.g. PWT batch PDFs).
    Only pages that contain a "Faktura FA" or "Faktura F/" header are parsed.
    Falls back to single parse_invoice() call if no invoice header is detected.
    """
    results = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages_with_invoices = [
                page.extract_text() or ""
                for page in pdf.pages
                if _INVOICE_HEADER_RE.search(page.extract_text() or "")
            ]
    except Exception as e:
        print(f"[parse_invoice_multipage] PDF read error: {e}")
        return results

    if not pages_with_invoices:
        # No recognizable invoice header — fall back to whole-PDF parse
        single = parse_invoice("", "", [pdf_path])
        return [single] if single else []

    for i, page_text in enumerate(pages_with_invoices, 1):
        print(
            f"[parse_invoice_multipage] parsing page-invoice {i}/{len(pages_with_invoices)}"
        )
        parsed = parse_invoice("", page_text, None)
        if parsed:
            results.append(parsed)
    return results


_INVOICE_PAGE_KW = re.compile(
    r"Faktura|Nale\u017cno\u015b\u0107|Sprzedawca|invoice number", re.IGNORECASE
)


def parse_invoice_pages(pdf_path: str) -> list:
    """
    Opens a single PDF and parses each page that contains invoice keywords.
    Uses a PWT-optimised prompt (Polish structure, EUR table, Sprzedawca block).
    Does NOT call parse_invoice() — uses _call_with_fallback() directly.
    Returns a list of parsed invoice dicts, one per invoice page.
    """
    results = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            for i, page in enumerate(pdf.pages, 1):
                page_text = page.extract_text() or ""
                if not _INVOICE_PAGE_KW.search(page_text):
                    continue
                print(f"[parse_invoice_pages] {pdf_path} page {i}/{total}")
                prompt = f"""Extract carrier invoice data from this Polish logistics invoice page.
Return ONLY valid JSON, no other text.

LAYOUT: The page has two company blocks at the top.
  LEFT  block = "Sprzedawca:" — this is the CARRIER (seller). Use this for carrier_name.
  RIGHT block = "Nabywca:"   — this is ACME Sp. z o.o. (buyer). IGNORE for carrier_name.
ACME Sp. z o.o. is ALWAYS the buyer (Nabywca). NEVER return any name containing "Acme" or "ACME" as carrier_name. Return null if only ACME found.

RULES:
- carrier_name: from "Sprzedawca:" block LEFT side ONLY. e.g. "PW-TRANS Piotr Wiśniewski"
- invoice_number: text immediately after "Faktura" keyword. e.g. "Faktura FA 7/3/26" → "FA 7/3/26"
- order_number: value after "Zlecenie spedycyjne" in service description. e.g. "Zlecenie spedycyjne 2026-03-00074" → "2026-03-00074"
- amount_netto: from EUR table ONLY. Look for row "Wartości obliczone z cen bez podatku [EUR]" or "Wartości bez podatku [EUR]". NEVER use PLN table values.
- vat: from EUR table ONLY ("Wartość podatku [EUR]"). Polish carriers always charge 23% VAT.
- amount_brutto: "Należność ogółem" EUR value only. e.g. "Należność ogółem: 1 300,00 EUR" → 1300.0
- currency: always "EUR"
- service_date: from "Data wykonania usługi" or "Data sprzedaży" in DD.MM.YYYY format. Convert YYYY-MM-DD → DD.MM.YYYY.
- payment_deadline: from "Termin zapłaty do: DD.MM.YYYY" → "DD.MM.YYYY". Convert YYYY-MM-DD if needed.
- payment_type: "scans"
- if field not found return null, never return 0 or empty string

PAGE TEXT:
{page_text[:3000]}

Return exactly this JSON:
{{
  "invoice_number": null,
  "carrier_name": null,
  "order_number": null,
  "amount_netto": null,
  "vat": null,
  "amount_brutto": null,
  "currency": "EUR",
  "service_date": null,
  "payment_deadline": null,
  "payment_days": null,
  "payment_type": "scans"
}}"""
                content = _call_with_fallback(prompt)
                parsed = _extract_json(content) if content else None
                if parsed:
                    # Guard: reject ACME as carrier — it is always the buyer (Nabywca)
                    if "acme" in (parsed.get("carrier_name") or "").lower():
                        parsed["carrier_name"] = None

                    # Regex fallback for invoice_number — AI occasionally misses "FA 7/3/26"
                    if not parsed.get("invoice_number"):
                        _m = re.search(r"Faktura\s+(FA\s+\d+/\d+/\d+)", page_text)
                        if _m:
                            parsed["invoice_number"] = _m.group(1).strip()

                    # Regex fallback for order_number — "Zlecenie spedycyjne 20YY-MM-NNNNN"
                    if not parsed.get("order_number"):
                        _m = re.search(
                            r"[Zz]lecenie spedycyjne\s+(20\d{2}-\d{2}-\d{5})", page_text
                        )
                        if _m:
                            parsed["order_number"] = _m.group(1)

                    # Regex fallback for amount_brutto from EUR block
                    # "Należność ogółem: 1 300,00 EUR" or "Należność ogółem 1 300,00 EUR"
                    if not parsed.get("amount_brutto"):
                        _m = re.search(
                            r"Nale\u017cno\u015b\u0107 og\u00f3\u0142em[:\s]+([\d\s]+[,]\d{2})\s*EUR",
                            page_text,
                        )
                        if _m:
                            try:
                                parsed["amount_brutto"] = float(
                                    _m.group(1).replace(" ", "").replace(",", ".")
                                )
                            except ValueError:
                                pass

                    # Hard-override currency — EUR column only, never PLN
                    parsed["currency"] = "EUR"

                    if not parsed.get("invoice_number"):
                        print(
                            f"[parse_invoice_pages] ❌ invoice_number extraction failed — skipping page {i}/{total}"
                        )
                        continue

                    results.append(parsed)
    except Exception as e:
        print(f"[parse_invoice_pages] error on {pdf_path}: {e}")
    return results


def parse_invoice_per_page(pdf_paths: list) -> list:
    """
    Opens each PDF in pdf_paths page by page and calls parse_invoice() on each page.
    Returns a flat list of parsed invoice dicts.
    Use when a PDF batch has one invoice per page (e.g. PWT sends 30 invoices in one PDF).
    Pages with no extractable text are skipped.
    """
    results = []
    for pdf_path in pdf_paths or []:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                total = len(pdf.pages)
                for i, page in enumerate(pdf.pages, 1):
                    page_text = page.extract_text() or ""
                    if not page_text.strip():
                        continue
                    print(f"[parse_invoice_per_page] {pdf_path} page {i}/{total}")
                    parsed = parse_invoice("", page_text, None)
                    if parsed:
                        results.append(parsed)
        except Exception as e:
            print(f"[parse_invoice_per_page] error on {pdf_path}: {e}")
    return results
