"""
saldeo_client.py

SaldeoSMART API v3.0 client for ACME Sp. z o.o.
REST XML API — auth via MD5 signature, payloads gzip+base64 encoded.

Config (.env):
    SALDEO_BASE_URL          = https://saldeo-test.brainshare.pl  (or production URL)
    SALDEO_USERNAME          = user@acme-forwarding.example
    SALDEO_API_TOKEN         = <token from SaldeoSMART → Ustawienia → API>
    SALDEO_COMPANY_PROGRAM_ID = <numeric ID>

API method codes used:
    SS04  — GET  /api/xml/3.0/company/list      list companies (auth test)
    SSK06 — POST /api/xml/3.0/invoice/add       create invoice
    SSK08 — POST /api/xml/3.0/invoice/listbyid  fetch invoice PDF
"""

import base64
import gzip
import hashlib
import logging
import os
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

_BASE_URL = os.getenv("SALDEO_BASE_URL", "https://saldeo-test.brainshare.pl").rstrip("/")
_USERNAME = os.getenv("SALDEO_USERNAME", "")
_API_TOKEN = os.getenv("SALDEO_API_TOKEN", "")
_COMPANY_PROGRAM_ID = os.getenv("SALDEO_COMPANY_PROGRAM_ID", "")

# ── Logging ───────────────────────────────────────────────────────────────────

_log_path = os.path.join(os.path.dirname(__file__), "saldeo_log.txt")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("saldeo")


# ── Exceptions ────────────────────────────────────────────────────────────────

class SaldeoAPIError(Exception):
    """Raised for all SaldeoSMART API errors (bad status, error codes, HTTP errors)."""
    def __init__(self, message: str, error_code: str = "", status_code: int = 0):
        super().__init__(message)
        self.error_code  = error_code
        self.status_code = status_code


# ── Core helpers ──────────────────────────────────────────────────────────────

def _build_sig(params: dict, api_token: str) -> str:
    """Build MD5 signature per SaldeoSMART v3.0 spec.

    Steps:
    1. Sort params alphabetically by key.
    2. Join as 'key=value&key2=value2'.
    3. URL-encode the entire joined string (custom SaldeoSMART encoding).
    4. Append api_token (not encoded).
    5. Return lowercase MD5 hex of the UTF-8 bytes.
    """
    sorted_items = sorted(params.items(), key=lambda x: x[0])
    joined = "&".join(f"{k}={v}" for k, v in sorted_items)
    # SaldeoSMART custom encoding: encode everything except unreserved chars
    encoded = urllib.parse.quote(joined, safe="")
    to_hash = encoded + api_token
    return hashlib.md5(to_hash.encode("utf-8")).hexdigest()


def _encode_xml(xml_str: str) -> str:
    """gzip-compress XML string, then base64-encode → return ASCII string.

    SaldeoSMART requires the xml_data POST parameter to be encoded this way.
    """
    compressed = gzip.compress(xml_str.encode("utf-8"))
    return base64.b64encode(compressed).decode("ascii")


def _decode_response(response_text: str) -> dict:
    """Parse SaldeoSMART XML response into a plain dict.

    Returns:
        {
            "STATUS":     "ok" | "error",
            "ERROR_CODE": "" | "ERR_XXX",
            "ERROR_MSG":  "",
            "data":       {tag: text, ...}   (all other child elements)
        }

    Raises SaldeoAPIError if STATUS != "ok".
    """
    try:
        root = ET.fromstring(response_text.strip())
    except ET.ParseError as exc:
        raise SaldeoAPIError(f"Invalid XML response: {exc}\nRaw: {response_text[:500]}")

    result: dict = {
        "STATUS":     "",
        "ERROR_CODE": "",
        "ERROR_MSG":  "",
        "data":       {},
    }

    for child in root:
        tag = child.tag.upper()
        if tag == "STATUS":
            result["STATUS"] = (child.text or "").strip().lower()
        elif tag == "ERROR_CODE":
            result["ERROR_CODE"] = (child.text or "").strip()
        elif tag == "ERROR_MSG":
            result["ERROR_MSG"] = (child.text or "").strip()
        else:
            # Nested elements → store as sub-dict; simple text → store as string
            if len(child):
                result["data"][child.tag] = {
                    sub.tag: (sub.text or "").strip() for sub in child
                }
            else:
                result["data"][child.tag] = (child.text or "").strip()

    return result


def _post(endpoint: str, xml_str: str, extra_params: Optional[dict] = None,
          retries: int = 3) -> dict:
    """Send a signed POST request to the given endpoint.

    Args:
        endpoint:     e.g. "api/xml/3.0/invoice/add"
        xml_str:      raw XML payload (will be gzip+base64 encoded)
        extra_params: any params beyond username/company_program_id
        retries:      number of attempts on 5xx HTTP errors

    Returns parsed response dict from _decode_response().
    """
    xml_encoded = _encode_xml(xml_str)

    # Base params (always sent)
    params = {
        "username":           _USERNAME,
        "company_program_id": _COMPANY_PROGRAM_ID,
        "xml_data":           xml_encoded,
    }
    if extra_params:
        params.update(extra_params)

    # Build signature over all params
    sig = _build_sig(params, _API_TOKEN)
    params["sig"] = sig

    url = f"{_BASE_URL}/{endpoint}"
    logger.debug("POST %s | params keys: %s", url, list(params.keys()))

    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, data=params, timeout=30)
            logger.debug("Response [%d]: %s", resp.status_code, resp.text[:300])

            if resp.status_code >= 500:
                logger.warning("5xx on attempt %d/%d: %d", attempt, retries, resp.status_code)
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise SaldeoAPIError(
                    f"HTTP {resp.status_code} after {retries} attempts",
                    status_code=resp.status_code,
                )

            if resp.status_code != 200:
                raise SaldeoAPIError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )

            parsed = _decode_response(resp.text)
            if parsed["STATUS"] != "ok":
                raise SaldeoAPIError(
                    f"API error {parsed['ERROR_CODE']}: {parsed['ERROR_MSG']}",
                    error_code=parsed["ERROR_CODE"],
                )
            return parsed

        except SaldeoAPIError:
            raise
        except Exception as exc:
            last_exc = exc
            logger.error("Request attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)

    raise SaldeoAPIError(f"All {retries} attempts failed: {last_exc}")


def _get(endpoint: str, extra_params: Optional[dict] = None) -> dict:
    """Send a signed GET request (no xml_data — used for list/query endpoints)."""
    params = {
        "username":           _USERNAME,
        "company_program_id": _COMPANY_PROGRAM_ID,
    }
    if extra_params:
        params.update(extra_params)

    sig = _build_sig(params, _API_TOKEN)
    params["sig"] = sig

    url = f"{_BASE_URL}/{endpoint}"
    logger.debug("GET %s | params: %s", url, list(params.keys()))

    resp = requests.get(url, params=params, timeout=30)
    logger.debug("Response [%d]: %s", resp.status_code, resp.text[:300])

    if resp.status_code != 200:
        raise SaldeoAPIError(f"HTTP {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code)

    parsed = _decode_response(resp.text)
    if parsed["STATUS"] != "ok":
        raise SaldeoAPIError(
            f"API error {parsed['ERROR_CODE']}: {parsed['ERROR_MSG']}",
            error_code=parsed["ERROR_CODE"],
        )
    return parsed


# ── XML builders ─────────────────────────────────────────────────────────────

def _build_invoice_xml(order_data: dict) -> str:
    """Build SSK06 invoice XML from order_data dict.

    Expected keys in order_data:
        company_name      str   full legal name
        nip               str   tax ID (without dashes/spaces), "" for foreign
        address           str   street + number (may include city/zip)
        amount_netto      float netto price in EUR
        vat_rate          int   default 23
        description       str   line item description (default: "Usługa transportowa")
        loading_place     str   e.g. "DE 20095"
        unloading_place   str   e.g. "PL 00-100"
        issue_date        str   DD.MM.YYYY or YYYY-MM-DD
        sale_date         str   DD.MM.YYYY or YYYY-MM-DD (= unloading date)
        payment_days      int   e.g. 30
        our_order_number  str   e.g. "2026-04-00059"

    Returns UTF-8 XML string.
    """
    def _fmt_date(raw: str) -> str:
        """Normalise date to YYYY-MM-DD for Saldeo."""
        raw = raw.strip()
        if len(raw) == 10 and raw[2] == ".":          # DD.MM.YYYY
            d, m, y = raw.split(".")
            return f"{y}-{m}-{d}"
        return raw  # assume already YYYY-MM-DD

    vat_rate   = int(order_data.get("vat_rate") or 23)
    netto      = float(order_data.get("amount_netto") or 0)
    vat_amount = round(netto * vat_rate / 100, 2)
    brutto     = round(netto + vat_amount, 2)

    loading   = order_data.get("loading_place", "")
    unloading = order_data.get("unloading_place", "")
    route_str = f"{loading} → {unloading}".strip(" →")

    base_desc  = order_data.get("description") or "Usługa transportowa"
    order_nr   = order_data.get("our_order_number", "")
    line_desc  = f"{base_desc}" + (f", nr {order_nr}" if order_nr else "") + (f" ({route_str})" if route_str else "")

    issue_date = _fmt_date(order_data.get("issue_date") or date.today().strftime("%d.%m.%Y"))
    sale_date  = _fmt_date(order_data.get("sale_date") or issue_date)
    pay_days   = int(order_data.get("payment_days") or 30)
    pay_date   = (datetime.strptime(issue_date, "%Y-%m-%d") + timedelta(days=pay_days)).strftime("%Y-%m-%d")

    root = ET.Element("ORDERS")
    order = ET.SubElement(root, "ORDER")

    ET.SubElement(order, "SERIES_CODE").text  = "FV"        # Invoice series code
    ET.SubElement(order, "ISSUE_DATE").text   = issue_date
    ET.SubElement(order, "SELL_DATE").text    = sale_date
    ET.SubElement(order, "PAYMENT_METHOD").text = "transfer"
    ET.SubElement(order, "PAYMENT_DATE").text = pay_date
    ET.SubElement(order, "CURRENCY").text     = "EUR"
    ET.SubElement(order, "COMMENTS").text     = f"Zlecenie nr {order_nr}" if order_nr else ""

    # Contractor block
    contractor = ET.SubElement(order, "CONTRACTOR")
    ET.SubElement(contractor, "NAME").text    = order_data.get("company_name", "")
    ET.SubElement(contractor, "NIP").text     = (order_data.get("nip") or "").replace("-", "").replace(" ", "")
    ET.SubElement(contractor, "STREET").text  = order_data.get("address", "")

    # Line items
    lines = ET.SubElement(order, "LINES")
    line  = ET.SubElement(lines, "LINE")
    ET.SubElement(line, "NAME").text         = line_desc
    ET.SubElement(line, "PKWiU").text        = "52.29.1"    # Transport support services
    ET.SubElement(line, "QUANTITY").text     = "1"
    ET.SubElement(line, "UNIT").text         = "szt."
    ET.SubElement(line, "NETTO_PRICE").text  = f"{netto:.2f}"
    ET.SubElement(line, "VAT_RATE").text     = str(vat_rate)
    ET.SubElement(line, "NETTO_VALUE").text  = f"{netto:.2f}"
    ET.SubElement(line, "VAT_VALUE").text    = f"{vat_amount:.2f}"
    ET.SubElement(line, "BRUTTO_VALUE").text = f"{brutto:.2f}"

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def _build_listbyid_xml(invoice_id: str) -> str:
    """Build SSK08 XML to fetch invoice by ID."""
    root  = ET.Element("ORDERS")
    order = ET.SubElement(root, "ORDER")
    ET.SubElement(order, "INVOICE_ID").text = str(invoice_id)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


# ── Public API ────────────────────────────────────────────────────────────────

def list_companies() -> list[dict]:
    """SS04 — List companies available for this account.

    Returns list of dicts with company info.  Used for connection test.
    """
    parsed = _get("api/xml/3.0/company/list")
    companies = parsed["data"].get("COMPANIES", {})
    if isinstance(companies, dict):
        # Single company returned as dict
        return [companies]
    if isinstance(companies, list):
        return companies
    return []


def create_invoice(order_data: dict) -> str:
    """SSK06 — Create an invoice in SaldeoSMART.

    Args:
        order_data: see _build_invoice_xml() for expected keys.

    Returns:
        invoice_id (str) assigned by SaldeoSMART.

    Raises:
        SaldeoAPIError on any API or HTTP error.
    """
    xml_str = _build_invoice_xml(order_data)
    logger.info("create_invoice: order=%s client=%s netto=%.2f",
                order_data.get("our_order_number"),
                order_data.get("company_name"),
                float(order_data.get("amount_netto") or 0))
    logger.debug("Invoice XML: %s", xml_str)

    parsed = _post("api/xml/3.0/invoice/add", xml_str)

    # Extract invoice_id from response data
    invoice_id = (
        parsed["data"].get("INVOICE_ID")
        or parsed["data"].get("invoice_id")
        or parsed["data"].get("ID")
        or ""
    )
    # Sometimes nested under INVOICE element
    if not invoice_id and isinstance(parsed["data"].get("INVOICE"), dict):
        invoice_id = parsed["data"]["INVOICE"].get("INVOICE_ID", "")

    if not invoice_id:
        logger.warning("create_invoice: no invoice_id in response: %s", parsed["data"])

    logger.info("create_invoice: created id=%s", invoice_id)
    return str(invoice_id)


def get_invoice_pdf(invoice_id: str) -> bytes:
    """SSK08 — Fetch invoice as PDF bytes.

    Args:
        invoice_id: the id returned by create_invoice().

    Returns:
        PDF content as bytes.

    Raises:
        SaldeoAPIError if PDF data not present in response.
    """
    xml_str = _build_listbyid_xml(invoice_id)
    logger.info("get_invoice_pdf: id=%s", invoice_id)

    parsed = _post("api/xml/3.0/invoice/listbyid", xml_str)

    # SaldeoSMART returns PDF as base64-encoded string in PDF_FILE or FILE_CONTENT
    pdf_b64 = (
        parsed["data"].get("PDF_FILE")
        or parsed["data"].get("FILE_CONTENT")
        or parsed["data"].get("PDF")
        or ""
    )
    if not pdf_b64 and isinstance(parsed["data"].get("INVOICE"), dict):
        pdf_b64 = parsed["data"]["INVOICE"].get("PDF_FILE", "")

    if not pdf_b64:
        raise SaldeoAPIError(
            f"No PDF data in response for invoice_id={invoice_id}. "
            f"Response data keys: {list(parsed['data'].keys())}"
        )

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception as exc:
        raise SaldeoAPIError(f"Failed to decode PDF base64: {exc}") from exc

    logger.info("get_invoice_pdf: received %d bytes for id=%s", len(pdf_bytes), invoice_id)
    return pdf_bytes


def add_contractor(company_data: dict) -> str:
    """Add or update a contractor in SaldeoSMART.

    Args:
        company_data: dict with keys: name, nip, address, zip_code, city, country, email

    Returns:
        contractor_id (str)
    """
    root       = ET.Element("CONTRACTORS")
    contractor = ET.SubElement(root, "CONTRACTOR")
    ET.SubElement(contractor, "NAME").text     = company_data.get("name", "")
    ET.SubElement(contractor, "NIP").text      = (company_data.get("nip") or "").replace("-", "").replace(" ", "")
    ET.SubElement(contractor, "STREET").text   = company_data.get("address", "")
    ET.SubElement(contractor, "ZIP_CODE").text = company_data.get("zip_code", "")
    ET.SubElement(contractor, "CITY").text     = company_data.get("city", "")
    ET.SubElement(contractor, "COUNTRY").text  = company_data.get("country", "PL")
    ET.SubElement(contractor, "EMAIL").text    = company_data.get("email", "")

    xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
    parsed  = _post("api/xml/3.0/contractor/add", xml_str)

    contractor_id = (
        parsed["data"].get("CONTRACTOR_ID")
        or parsed["data"].get("ID")
        or ""
    )
    logger.info("add_contractor: id=%s name=%s", contractor_id, company_data.get("name"))
    return str(contractor_id)


def get_contractors() -> list[dict]:
    """Return list of contractors (clients) registered in SaldeoSMART."""
    parsed = _get("api/xml/3.0/contractor/list")
    contractors = parsed["data"].get("CONTRACTORS", {})
    if isinstance(contractors, dict):
        return [contractors]
    if isinstance(contractors, list):
        return contractors
    return []
