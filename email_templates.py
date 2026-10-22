
"""
email_templates.py

Email templates modelled on typical client emails.

Three template types:
  1. STANDARD     — standard (Gamma, most clients)
  2. SKONTO       — with skonto discount condition (BETA LOGISTIK, AA/BB/CC/DD series)
  3. BROKERALPHA — batch email with multiple invoices

Usage:
  from email_templates import build_email
  result = build_email(orders, template="standard")
  # result = {"subject": ..., "body": ..., "to": ...}
"""



# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _is_polish_company(company_name: str) -> bool:
    """Returns True if company name contains Polish legal-form markers."""
    markers = ["sp. z o.o.", "s.a.", "sp.k.", "sp.j.", "s.k.a.", "spółka", "spolka"]
    lower = (company_name or "").lower()
    return any(m in lower for m in markers)


# ─────────────────────────────────────────────
# TEMPLATE DETECTION BY CLIENT
# ─────────────────────────────────────────────

def detect_template(company_name: str, client_order_nr: str) -> str:
    """
    Determines which template to use based on company name or order number.

    Returns: "brokeralpha" | "skonto" | "standard"
    """
    company_lower = (company_name or "").lower()
    order_lower = (client_order_nr or "").lower()

    # BrokerAlpha — always batch (even for 1 invoice)
    if "brokeralpha" in company_lower or order_lower.startswith("tc"):
        return "brokeralpha"

    # BETA LOGISTIK — always skonto
    if "beta logistik" in company_lower:
        return "skonto"

    # AA / BB / CC / DD series — skonto (transport order / contract format)
    if any(f"-{code}-" in (client_order_nr or "").upper() for code in
           ["AA", "BB", "CC", "DD"]):
        return "skonto"

    # Standard for all others
    return "standard"


# ─────────────────────────────────────────────
# TEMPLATE 1: STANDARD
# Example: Invoice nr 71/03/2026 and POD for the transport order 100001
# Clients: Gamma, most clients
# ─────────────────────────────────────────────

def build_standard(
    invoice_nr: str,
    client_order_nr: str,
    payment_days: str | int,
    payment_type: str,   # "scans" or "originals"
    invoice_email: str,
    company_name: str = "",
    nip: str = "",
) -> dict:

    def _is_polish_company(company_name: str, nip: str) -> bool:
        """Returns True if company is Polish based on NIP prefix."""
        if nip and nip.upper().startswith("PL"):
            return True
        return False

    is_polish = _is_polish_company(company_name, nip)
    is_originals = "original" in str(payment_type).lower()
    doc_text = "originals" if is_originals else "scans of the POD's"
    pay_text = ("upon receipt of the original documents"
                if is_originals
                else "by scans of POD in PDF file")

    if is_polish:
        subject = f"Faktura i dokumenty za zamówienie transportowe nr {client_order_nr} // ACME Sp. z o.o."
        body = (
            f"Szanowni Państwo,\n\n"
            f"w załączeniu przesyłam fakturę elektroniczną nr <b>{invoice_nr}</b> oraz skany \n"
            f"dokumentów rozładunkowych dotyczących zamówienia nr <b>{client_order_nr}</b>.\n\n"
            f"Zgodnie z umową, termin płatności wynosi <b style='background:yellow'>{payment_days} dni</b> \n"
            f"od daty dostarczenia dokumentów, a płatność zostanie dokonana na podstawie skanów \n"
            f"dokumentu odbioru (POD) w formacie PDF.\n\n"
            f"Bardzo proszę o potwierdzenie, czy wszystko jest w porządku oraz na jaką datę \n"
            f"mają Państwo zaplanowaną płatność?\n\n"
            f"Z poważaniem,\n"
            f"Anna Lewandowska\n"
            f"ACME Sp. z o.o."
        )
    else:
        subject = f"Invoice nr and POD for the transport order {client_order_nr} // ACME Sp. z o.o."
        body = (
            f"Dear Sir or Madam,\n\n"
            f"Please find attached the electronic invoice no. <b>{invoice_nr}</b> and scans \n"
            f"of the unloading documents related to order no. <b>{client_order_nr}</b>.\n\n"
            f"In accordance with the agreement, the payment term is <b style='background:yellow'>{payment_days} days</b> \n"
            f"from the date of delivery of the documents, and the payment will be made based on \n"
            f"the scanned proof of delivery (POD) documents in PDF format.\n\n"
            f"Could you please confirm that everything is in order and inform us of the scheduled payment date?\n\n"
            f"Kind regards,\n"
            f"Anna Lewandowska\n"
            f"ACME Sp. z o.o."
        )

    return {
        "subject": subject,
        "body": body,
        "to": invoice_email,
        "template": "standard",
    }


# ─────────────────────────────────────────────
# TEMPLATE 2: SKONTO
# Example: Invoice for Transport order No. 2026-AA-10001 // ACME Sp. z o.o.
# Clients: BETA LOGISTIK (skonto@beta-logistik.example), AA/BB/CC/DD series
# ─────────────────────────────────────────────

def build_skonto(
    invoice_nr: str,
    client_order_nr: str,
    payment_days: str | int,
    price_eur: float,
    skonto_pct: float,       # e.g. 3.0
    invoice_email: str,
) -> dict:
    discounted = round(price_eur * (1 - skonto_pct / 100), 2)
    skonto_days = int(payment_days)

    subject = f"Invoice for Transport order No. {client_order_nr} // ACME Sp. z o.o."

    body = (
        f"Dear Sir or Madam,\n\n"
        f"Please find attached our invoice No. {invoice_nr} "
        f"and the scans of the POD's for the order number {client_order_nr}.\n\n"
        f"Payment terms: {skonto_days} days – {skonto_pct:.0f}% Skonto\n"
        f"{price_eur:.2f} euro - {skonto_pct:.0f}% = {discounted:.2f} euro total price\n\n"
        f"Please let me know if all is ok and the payment term starts to count?\n\n"
        f"Kind regards,\n"
        f"Anna Lewandowska\n"
        f"ACME Sp. z o.o."
    )

    return {
        "subject": subject,
        "body": body,
        "to": invoice_email,
        "template": "skonto",
        "skonto_pct": skonto_pct,
        "discounted_price": discounted,
    }


# ─────────────────────────────────────────────
# TEMPLATE 3: BROKERALPHA (batch)
# Example subject: Faktury BrokerAlpha
# Client: BrokerAlpha → invoices@brokeralpha.example
# ─────────────────────────────────────────────

def build_brokeralpha(orders: list[dict]) -> dict:
    """
    Batch email for BrokerAlpha — multiple invoices in one email.

    Each order dict must have:
      - client_order_nr:  "TC400011"
      - our_order_nr:     "2026-03-00046"
      - invoice_nr:       "45/03/2026"

    Row format:
      TC400011   2026-03-00046   – Invoice No. 45/03/2026

    Subject:
      - 1 order:   "Faktury BrokerAlpha 2026-03-00046"
      - multiple:  "Faktury BrokerAlpha 2026-03-00046+2026-03-00047+..."
                   (if > 4, show first 3 + "...")
    """
    if not orders:
        raise ValueError("At least one order is required")

    # Subject
    our_nrs = [o.get("our_order_nr", "") for o in orders]
    if len(our_nrs) == 1:
        subject = f"Faktury BrokerAlpha {our_nrs[0]}"
    elif len(our_nrs) <= 4:
        subject = "Faktury BrokerAlpha " + "+".join(our_nrs)
    else:
        subject = "Faktury BrokerAlpha " + "+".join(our_nrs[:3]) + "+..."

    # Order table
    lines = []
    for o in orders:
        client_nr = o.get("client_order_nr", "")
        our_nr = o.get("our_order_nr", "")
        inv_nr = o.get("invoice_nr", "")
        lines.append(f"{client_nr}   {our_nr}   – Invoice No. {inv_nr}")

    orders_block = "\n".join(lines)

    body = (
        f"Dear Sir or Madam,\n\n"
        f"Please find attached the invoices for the following orders:\n\n"
        f"{orders_block}\n\n"
        f"The invoices are attached below.\n\n"
        f"Please confirm receipt and let me know if everything is in order.\n\n"
        f"Kind regards,\n"
        f"Anna Lewandowska\n"
        f"ACME Sp. z o.o."
    )

    return {
        "subject": subject,
        "body": body,
        "to": "invoices@brokeralpha.example",
        "template": "brokeralpha",
        "order_count": len(orders),
    }


# ─────────────────────────────────────────────
# MAIN FUNCTION
# ─────────────────────────────────────────────

def build_email(
    orders: list[dict],
    template: str = "auto",
    skonto_pct: float = 3.0,
) -> dict:
    """
    Main function. Builds an email automatically or using the specified template.

    orders — list of dicts with fields:
      client_order_nr, our_order_nr, invoice_nr,
      company_name, invoice_email,
      payment_days, payment_type,
      price_netto  (required for skonto)

    template:
      "auto"         — detect automatically by client
      "standard"     — standard template
      "skonto"       — with skonto discount
      "brokeralpha" — batch for TC

    Returns dict: subject, body, to, template
    """
    if not orders:
        raise ValueError("Orders list is empty")

    first = orders[0]

    # Detect template
    if template == "auto":
        template = detect_template(
            first.get("company_name", ""),
            first.get("client_order_nr", ""),
        )

    # BrokerAlpha — always batch regardless of count
    if template == "brokeralpha":
        return build_brokeralpha(orders)

    # For other templates use the first order
    # (if multiple and not BrokerAlpha — send separate emails)
    invoice_nr = first.get("invoice_nr", "")
    client_order_nr = first.get("client_order_nr", "")
    payment_days = first.get("payment_days", 30)
    payment_type = first.get("payment_type", "scans")
    invoice_email = first.get("invoice_email", "") or first.get("contact_email", "")
    price_netto = float(first.get("price_netto") or 0)

    if template == "skonto":
        return build_skonto(
            invoice_nr=invoice_nr,
            client_order_nr=client_order_nr,
            payment_days=payment_days,
            price_eur=price_netto,
            skonto_pct=skonto_pct,
            invoice_email=invoice_email,
        )

    # standard (default)
    return build_standard(
        invoice_nr=invoice_nr,
        client_order_nr=client_order_nr,
        payment_days=payment_days,
        payment_type=payment_type,
        invoice_email=invoice_email,
    )


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":

    # Test 1: standard (Gamma)
    r1 = build_email([{
        "company_name": "Gamma Int. Transporte GmbH",
        "client_order_nr": "100001",
        "our_order_nr": "2026-03-00079",
        "invoice_nr": "71/03/2026",
        "invoice_email": "buchhaltung@gamma-transporte.example",
        "payment_days": 30,
        "payment_type": "scans",
        "price_netto": 700,
    }])
    print("=== STANDARD ===")
    print(f"Subject: {r1['subject']}")
    print(r1['body'])

    print()

    # Test 2: skonto (BETA LOGISTIK)
    r2 = build_email([{
        "company_name": "BETA LOGISTIK AUSTRIA GmbH",
        "client_order_nr": "2026-AA-10001",
        "our_order_nr": "2026-03-00060",
        "invoice_nr": "69/03/2026",
        "invoice_email": "skonto@beta-logistik.example",
        "payment_days": 3,
        "payment_type": "scans",
        "price_netto": 775,
    }])
    print("=== SKONTO ===")
    print(f"Subject: {r2['subject']}")
    print(r2['body'])

    print()

    # Test 3: BrokerAlpha (batch, 3 invoices)
    tc_orders = [
        {"client_order_nr": "TC400011", "our_order_nr": "2026-03-00046",
         "invoice_nr": "45/03/2026", "company_name": "BrokerAlpha",
         "invoice_email": "invoices@brokeralpha.example", "payment_days": 14,
         "payment_type": "scans", "price_netto": 289},
        {"client_order_nr": "TC400012", "our_order_nr": "2026-03-00047",
         "invoice_nr": "46/03/2026", "company_name": "BrokerAlpha",
         "invoice_email": "invoices@brokeralpha.example", "payment_days": 14,
         "payment_type": "scans", "price_netto": 310},
        {"client_order_nr": "TC400013", "our_order_nr": "2026-03-00048",
         "invoice_nr": "47/03/2026", "company_name": "BrokerAlpha",
         "invoice_email": "invoices@brokeralpha.example", "payment_days": 14,
         "payment_type": "scans", "price_netto": 260},
    ]
    r3 = build_email(tc_orders, template="brokeralpha")
    print("=== BROKERALPHA (3 invoices) ===")
    print(f"Subject: {r3['subject']}")
    print(r3['body'])
