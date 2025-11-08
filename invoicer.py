
# invoicer.py
import streamlit as st
import html
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from typing import List, Dict, Any

# PDF (ReportLab) imports
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_RIGHT, TA_LEFT
from reportlab.lib.styles import ParagraphStyle

# -----------------------------
# Utilities
# -----------------------------

DEC_QUANT = Decimal("0.01")  # 2 dp rounding, half-up

def d2(x) -> Decimal:
    """Convert to Decimal and quantize to 2 dp (half-up)."""
    return (Decimal(str(x))).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)

def compute_due_date(inv_date: date, terms_days: int) -> date:
    return inv_date + timedelta(days=int(terms_days))

def tax_label_options():
    # Presets plus "None" and "Custom"
    return ["None", "VAT", "Sales Tax", "GST", "Custom"]

def ensure_session():
    if "current_step" not in st.session_state:
        st.session_state.current_step = 0  # Start at Step 0
    if "profile" not in st.session_state:
        st.session_state.profile = {
            "legal_name": "",
            "trading_name": "",
            "address_lines": [""],
            "email": "",
            "phone": "",
            "company_number": "",
            "vat_number": "",
            "tax_id": "",
            "region": "",  # require manual selection
        }
    if "client" not in st.session_state:
        st.session_state.client = {
            "contact_name": "",
            "company_name": "",
            "address_lines": [""],
            "email": "",
            "po_reference": "",
            "notes": "",
        }
    if "invoice" not in st.session_state:
        st.session_state.invoice = {
            "invoice_number": "",
            "invoice_date": date.today(),
            "terms_days": None,  # Mandatory integer
            "currency": "GBP",   # Single currency per invoice
            "tax_label_mode": "None",
            "tax_label_custom": "",
            "tax_rate": 0.0,
        }
    # Use 'line_items' to avoid collision with dict.items()
    if "line_items" not in st.session_state:
        st.session_state.line_items = []  # Each: dict with basis, description, qty, rate
    if "payments" not in st.session_state:
        st.session_state.payments = {
            "accept_wise": False,
            "accept_stripe": False,
            "accept_paypal": False,
            "accept_bank": False,
            "paypal_text": "",
            "stripe_text": "",
            "wise_text": "",
            "bank_country": "UK",
            "bank_uk": {"account_name": "", "sort_code": "", "account_number": "", "iban": "", "bic": ""},
            "bank_us": {"account_name": "", "routing_number": "", "account_number": "", "ach_wire_notes": ""},
            "footer_notes": "",
        }

def set_step(n: int):
    st.session_state.current_step = n

def currency_symbol(code: str) -> str:
    return {"GBP": "£", "USD": "$", "EUR": "€"}.get(code, "")

def compute_totals(items: List[Dict[str, Any]], tax_rate_percent: float):
    subtotal = Decimal("0.00")
    for it in items:
        qty = d2(it.get("qty", 0))
        rate = d2(it.get("rate", 0))
        line_total = (qty * rate).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)  # per-line rounding
        subtotal += line_total
    subtotal = subtotal.quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
    tax_amount = (subtotal * Decimal(str(tax_rate_percent)) / Decimal("100")).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
    total = (subtotal + tax_amount).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
    return subtotal, tax_amount, total

def items_table_preview_html(items, currency):
    sym = currency_symbol(currency)
    rows = []
    for i, it in enumerate(items, start=1):
        qty = d2(it.get("qty", 0))
        rate = d2(it.get("rate", 0))
        line_total = (qty * rate).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
        rows.append(f"""
        <tr>
            <td style="padding:6px; border:1px solid #ddd;">{i}</td>
            <td style="padding:6px; border:1px solid #ddd;">{html.escape(it.get('basis',''))}</td>
            <td style="padding:6px; border:1px solid #ddd;">{html.escape(it.get('description',''))}</td>
            <td style="padding:6px; border:1px solid #ddd; text-align:right;">{qty}</td>
            <td style="padding:6px; border:1px solid #ddd; text-align:right;">{sym}{rate}</td>
            <td style="padding:6px; border:1px solid #ddd; text-align:right;">{sym}{line_total}</td>
        </tr>
        """)
    body = "\n".join(rows) if rows else f"""
        <tr><td colspan="6" style="padding:8px; border:1px solid #ddd; text-align:center;">No items</td></tr>
    """
    html_tbl = f"""
    <table style="border-collapse:collapse; width:100%; font-size:14px;">
        <thead>
            <tr>
                <th style="padding:6px; border:1px solid #ddd; text-align:left;">#</th>
                <th style="padding:6px; border:1px solid #ddd; text-align:left;">Basis</th>
                <th style="padding:6px; border:1px solid #ddd; text-align:left;">Description</th>
                <th style="padding:6px; border:1px solid #ddd; text-align:right;">Qty</th>
                <th style="padding:6px; border:1px solid #ddd; text-align:right;">Rate</th>
                <th style="padding:6px; border:1px solid #ddd; text-align:right;">Line Total</th>
            </tr>
        </thead>
        <tbody>{body}</tbody>
    </table>
    """
    return html_tbl

def render_preview_html():
    prof = st.session_state.profile
    cli  = st.session_state.client
    inv  = st.session_state.invoice
    pay  = st.session_state.payments
    items = st.session_state.line_items

    # Compute totals
    subtotal, tax_amount, total = compute_totals(items, inv.get("tax_rate") or 0.0)
    sym = currency_symbol(inv["currency"])

    # Determine tax label
    tl_mode = inv.get("tax_label_mode", "None")
    if tl_mode == "Custom":
        tax_label = inv.get("tax_label_custom", "").strip() or "Tax"
    elif tl_mode == "None":
        tax_label = None
    else:
        tax_label = tl_mode

    # Address helpers
    def fmt_addr(lines):
        ls = [l.strip() for l in (lines or []) if (l or "").strip()]
        return "<br/>".join(html.escape(x) for x in ls) if ls else ""

    # Payment section
    pay_lines = []
    if pay.get("accept_wise"):
        txt = pay.get("wise_text", "").strip()
        if txt:
            pay_lines.append(f"<strong>Wise:</strong> {html.escape(txt)}")
    if pay.get("accept_stripe"):
        txt = pay.get("stripe_text", "").strip()
        if txt:
            pay_lines.append(f"<strong>Stripe:</strong> {html.escape(txt)}")
    if pay.get("accept_paypal"):
        txt = pay.get("paypal_text", "").strip()
        if txt:
            pay_lines.append(f"<strong>PayPal:</strong> {html.escape(txt)}")
    if pay.get("accept_bank"):
        if pay.get("bank_country") == "UK":
            d = pay.get("bank_uk", {})
            pay_lines.append("<strong>Bank Transfer (UK)</strong>")
            if d.get("account_name"):  pay_lines.append(f"Account name: {html.escape(d['account_name'])}")
            if d.get("sort_code"):     pay_lines.append(f"Sort code: {html.escape(d['sort_code'])}")
            if d.get("account_number"):pay_lines.append(f"Account number: {html.escape(d['account_number'])}")
            if d.get("iban"):          pay_lines.append(f"IBAN: {html.escape(d['iban'])}")
            if d.get("bic"):           pay_lines.append(f"BIC: {html.escape(d['bic'])}")
        else:
            d = pay.get("bank_us", {})
            pay_lines.append("<strong>Bank Transfer (US)</strong>")
            if d.get("account_name"):    pay_lines.append(f"Account name: {html.escape(d['account_name'])}")
            if d.get("routing_number"):  pay_lines.append(f"Routing number: {html.escape(d['routing_number'])}")
            if d.get("account_number"):  pay_lines.append(f"Account number: {html.escape(d['account_number'])}")
            if d.get("ach_wire_notes"):  pay_lines.append(f"Notes: {html.escape(d['ach_wire_notes'])}")

    if pay.get("footer_notes"):
        pay_lines.append(html.escape(pay["footer_notes"]))

    pay_html = "<br/>".join(pay_lines) if pay_lines else "No payment instructions provided."

    # Items table
    items_html = items_table_preview_html(items, inv["currency"])

    # Totals panel
    totals_rows = [
        f"<tr><td style='padding:6px;'>Subtotal</td><td style='padding:6px; text-align:right;'>{sym}{subtotal}</td></tr>"
    ]
    if tax_label:
        tr = d2(inv.get("tax_rate") or 0.0)
        totals_rows.append(
            f"<tr><td style='padding:6px;'>{html.escape(tax_label)} ({tr}%)</td>"
            f"<td style='padding:6px; text-align:right;'>{sym}{tax_amount}</td></tr>"
        )
    totals_rows.append(
        f"<tr><td style='padding:6px; font-weight:bold;'>Total</td>"
        f"<td style='padding:6px; text-align:right; font-weight:bold;'>{sym}{total}</td></tr>"
    )
    totals_html = f"""
    <table style="border-collapse:collapse; width:100%; max-width:300px; float:right;">
        <tbody>
            {''.join(totals_rows)}
        </tbody>
    </table>
    <div style="clear:both;"></div>
    """

    # Header/meta layout
    inv_date_str = inv["invoice_date"].strftime("%Y-%m-%d")  # display can be adapted later
    due_str = ""
    if inv.get("terms_days") is not None:
        due_date = compute_due_date(inv["invoice_date"], int(inv["terms_days"]))
        due_str = due_date.strftime("%Y-%m-%d")

    header_html = f"""
    <div style="display:flex; justify-content:space-between; gap:20px; align-items:flex-start;">
      <div style="flex:1;">
        <div><strong>{html.escape(prof.get('legal_name',''))}</strong></div>
        <div>{html.escape(prof.get('trading_name',''))}</div>
        <div>{fmt_addr(prof.get('address_lines'))}</div>
        <div>{html.escape(prof.get('email',''))}</div>
        <div>{html.escape(prof.get('phone',''))}</div>
        <div>{('Company No: ' + html.escape(prof.get('company_number',''))) if prof.get('company_number') else ''}</div>
        <div>{('VAT No: ' + html.escape(prof.get('vat_number',''))) if prof.get('vat_number') else ''}</div>
        <div>{('Tax ID: ' + html.escape(prof.get('tax_id',''))) if prof.get('tax_id') else ''}</div>
      </div>
      <div style="text-align:right; min-width:220px;">
        <div><strong>Invoice</strong></div>
        <div>Invoice No: {html.escape(inv.get('invoice_number',''))}</div>
        <div>Invoice Date: {inv_date_str}</div>
        <div>Terms (days): {html.escape(str(inv.get('terms_days') or ''))}</div>
        <div>Due Date: {html.escape(due_str)}</div>
        <div>Currency: {html.escape(inv.get('currency',''))}</div>
      </div>
    </div>
    <hr/>
    <div>
      <div><strong>Bill To</strong></div>
      <div>{html.escape(cli.get('contact_name',''))}</div>
      <div>{html.escape(cli.get('company_name',''))}</div>
      <div>{fmt_addr(cli.get('address_lines'))}</div>
      <div>{html.escape(cli.get('email',''))}</div>
      <div>{('PO/Ref: ' + html.escape(cli.get('po_reference',''))) if cli.get('po_reference') else ''}</div>
      <div>{html.escape(cli.get('notes',''))}</div>
    </div>
    <br/>
    """

    html_doc = f"""
    <div style="font-family: Arial, sans-serif; font-size:14px; color:#000;">
      {header_html}
      {items_html}
      <br/>
      {totals_html}
      <br/>
      <div>
        <strong>Payment Instructions</strong><br/>
        {pay_html}
      </div>
    </div>
    """
    return html_doc

# -----------------------------
# PDF generation (ReportLab)
# -----------------------------

def build_pdf_bytes():
    prof = st.session_state.profile
    cli  = st.session_state.client
    inv  = st.session_state.invoice
    items = st.session_state.line_items
    pay  = st.session_state.payments

    subtotal, tax_amount, total = compute_totals(items, inv.get("tax_rate") or 0.0)
    sym = currency_symbol(inv["currency"])

    tl_mode = inv.get("tax_label_mode", "None")
    if tl_mode == "Custom":
        tax_label = inv.get("tax_label_custom", "").strip() or "Tax"
    elif tl_mode == "None":
        tax_label = None
    else:
        tax_label = tl_mode

    inv_date_str = inv["invoice_date"].strftime("%Y-%m-%d")
    due_str = ""
    if inv.get("terms_days") is not None:
        due_str = compute_due_date(inv["invoice_date"], int(inv["terms_days"])).strftime("%Y-%m-%d")

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=16*mm, bottomMargin=16*mm)
    styles = getSampleStyleSheet()
    styleN = styles["Normal"]
    styleB = styles["Heading4"]
    styleRight = ParagraphStyle("right", parent=styleN, alignment=TA_RIGHT)
    styleLeft  = ParagraphStyle("left", parent=styleN, alignment=TA_LEFT)

    story = []

    # Header
    header_table_data = [
        [
            Paragraph(f"<b>{html.escape(prof.get('legal_name',''))}</b>", styleN),
            Paragraph("<b>Invoice</b>", styleRight),
        ],
        [
            Paragraph(f"{html.escape(prof.get('trading_name',''))}", styleN),
            Paragraph(f"Invoice No: {html.escape(inv.get('invoice_number',''))}", styleRight),
        ],
        [
            Paragraph("<br/>".join([*(html.escape(l) for l in (prof.get('address_lines') or []) if l)]), styleN),
            Paragraph(f"Invoice Date: {inv_date_str}", styleRight),
        ],
        [
            Paragraph(f"{html.escape(prof.get('email',''))}" + (f" | {html.escape(prof.get('phone',''))}" if prof.get('phone') else ""), styleN),
            Paragraph(f"Terms (days): {html.escape(str(inv.get('terms_days') or ''))}", styleRight),
        ],
        [
            Paragraph(
                f"{('Company No: ' + html.escape(prof.get('company_number'))) if prof.get('company_number') else ''} "
                f"{('  VAT No: ' + html.escape(prof.get('vat_number'))) if prof.get('vat_number') else ''} "
                f"{('  Tax ID: ' + html.escape(prof.get('tax_id'))) if prof.get('tax_id') else ''}",
                styleN
            ),
            Paragraph(f"Due Date: {html.escape(due_str)}", styleRight),
        ],
        [
            Paragraph("", styleN),
            Paragraph(f"Currency: {html.escape(inv.get('currency',''))}", styleRight),
        ],
    ]
    header_table = Table(header_table_data, colWidths=[100*mm, 70*mm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6))

    story.append(Paragraph("<b>Bill To</b>", styleB))
    bill_to_lines = []
    for line in [
        html.escape(cli.get("contact_name","")),
        html.escape(cli.get("company_name","")),
        *[html.escape(l) for l in (cli.get("address_lines") or []) if l],
        html.escape(cli.get("email","")),
        f"PO/Ref: {html.escape(cli.get('po_reference',''))}" if cli.get("po_reference") else "",
        html.escape(cli.get("notes","")),
    ]:
        if line:
            bill_to_lines.append(Paragraph(line, styleN))
    story.extend(bill_to_lines or [Paragraph("—", styleN)])
    story.append(Spacer(1, 8))

    # Items table
    tbl_header = ["#", "Basis", "Description", "Qty", "Rate", "Line Total"]
    tbl_data = [tbl_header]
    for i, it in enumerate(items, start=1):
        qty = d2(it.get("qty", 0))
        rate = d2(it.get("rate", 0))
        total_line = (qty * rate).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
        tbl_data.append([
            str(i),
            it.get("basis",""),
            it.get("description",""),
            f"{qty}",
            f"{sym}{rate}",
            f"{sym}{total_line}",
        ])
    if len(tbl_data) == 1:
        tbl_data.append(["", "", "No items", "", "", ""])

    table = Table(tbl_data, repeatRows=1, colWidths=[10*mm, 25*mm, 80*mm, 20*mm, 25*mm, 30*mm])
    table.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("BACKGROUND", (0,0), (-1,0), colors.whitesmoke),
        ("ALIGN", (3,1), (3,-1), "RIGHT"),
        ("ALIGN", (4,1), (5,-1), "RIGHT"),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
    ]))
    story.append(table)
    story.append(Spacer(1, 8))

    # Totals
    totals_rows = [
        ["Subtotal", f"{sym}{subtotal}"],
    ]
    if tax_label:
        tr = d2(inv.get("tax_rate") or 0.0)
        totals_rows.append([f"{tax_label} ({tr}%)", f"{sym}{tax_amount}"])
    totals_rows.append(["Total", f"{sym}{total}"])

    totals_tbl = Table(totals_rows, colWidths=[40*mm, 30*mm])
    totals_tbl.setStyle(TableStyle([
        ("ALIGN", (1,0), (1,-1), "RIGHT"),
        ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
    ]))
    totals_wrap = Table([[totals_tbl]], colWidths=[170*mm])
    totals_wrap.setStyle(TableStyle([("ALIGN", (0,0), (-1,-1), "RIGHT")]))
    story.append(totals_wrap)
    story.append(Spacer(1, 8))

    # Payment instructions
    story.append(Paragraph("<b>Payment Instructions</b>", styleB))
    payment_lines = []

    def add_line(txt):
        if txt:
            payment_lines.append(Paragraph(html.escape(txt), styleN))

    if pay.get("accept_wise"):
        add_line("Wise:")
        add_line(pay.get("wise_text",""))
    if pay.get("accept_stripe"):
        add_line("Stripe:")
        add_line(pay.get("stripe_text",""))
    if pay.get("accept_paypal"):
        add_line("PayPal:")
        add_line(pay.get("paypal_text",""))
    if pay.get("accept_bank"):
        if pay.get("bank_country") == "UK":
            uk = pay.get("bank_uk", {})
            add_line("Bank Transfer (UK)")
            add_line(f"Account name: {uk.get('account_name','')}")
            add_line(f"Sort code: {uk.get('sort_code','')}")
            add_line(f"Account number: {uk.get('account_number','')}")
            if uk.get("iban"): add_line(f"IBAN: {uk.get('iban')}")
            if uk.get("bic"):  add_line(f"BIC: {uk.get('bic')}")
        else:
            us = pay.get("bank_us", {})
            add_line("Bank Transfer (US)")
            add_line(f"Account name: {us.get('account_name','')}")
            add_line(f"Routing number: {us.get('routing_number','')}")
            add_line(f"Account number: {us.get('account_number','')}")
            if us.get("ach_wire_notes"): add_line(f"Notes: {us.get('ach_wire_notes')}")

    if pay.get("footer_notes"):
        add_line(pay.get("footer_notes"))

    if not payment_lines:
        payment_lines = [Paragraph("No payment instructions provided.", styleN)]

    story.extend(payment_lines)

    doc.build(story)
    buf.seek(0)
    return buf.read()

# -----------------------------
# UI helpers
# -----------------------------

def edit_address_lines(state_dict: dict, field: str, label_prefix: str, max_lines: int = 6):
    """Dynamic address line editor with stable keys and proper resizing inside forms."""
    lines = state_dict.get(field) or [""]
    # stable counter key per prefix
    count = st.number_input(
        f"{label_prefix} address lines (count)",
        min_value=1, max_value=max_lines, value=len(lines),
        step=1, key=f"{label_prefix}_addr_count"
    )
    # Adjust length BEFORE rendering inputs
    if count > len(lines):
        lines += [""] * (count - len(lines))
    elif count < len(lines):
        lines = lines[:count]
    # Render inputs with stable keys
    for i in range(count):
        lines[i] = st.text_input(
            f"{label_prefix} address line {i+1}",
            value=lines[i],
            key=f"{label_prefix}_addr_{i}"
        )
    state_dict[field] = lines

# -----------------------------
# UI Steps
# -----------------------------

def step0():
    st.header("Step 0 — Upload last invoice (optional)")
    st.caption("Prefill from a prior invoice (Part B) to be added later.")
    st.file_uploader("Upload a prior invoice PDF produced by this app (ignored in Part A).", type=["pdf"])
    if st.button("Continue"):
        set_step(1)

def step1():
    st.header("Step 1 — Your details")

    # 1) Region selection outside form for immediate re-render
    region = st.selectbox(
        "Region (affects formatting and tax fields)",
        ["Select…", "UK", "US", "EU"],
        index=0,
        key="region_select"
    )
    st.session_state.profile["region"] = "" if region == "Select…" else region

    # Update incompatible fields when region changes
    prev_region = st.session_state.get("_prev_region")
    if prev_region and prev_region != region:
        if region == "US":
            st.session_state.profile.pop("vat_number", None)
        elif region in ("UK", "EU"):
            st.session_state.profile.pop("tax_id", None)
    st.session_state["_prev_region"] = region

    # 2) Gate the rest of the form until region is chosen
    if region == "Select…" or not region:
        st.info("Please select your region to continue.")
        return

    prof = st.session_state.profile
    with st.form("form_step1"):
        prof["legal_name"]    = st.text_input("Legal name (required)", prof.get("legal_name",""))
        prof["trading_name"]  = st.text_input("Trading/stage name (optional)", prof.get("trading_name",""))

        edit_address_lines(prof, "address_lines", "Your")

        prof["email"]          = st.text_input("Email (required)", prof.get("email",""))
        prof["phone"]          = st.text_input("Phone", prof.get("phone",""))
        prof["company_number"] = st.text_input("Company number", prof.get("company_number",""))

        # Conditional tax fields by region
        if region in ("UK", "EU"):
            prof["vat_number"] = st.text_input("VAT number", prof.get("vat_number",""))
        elif region == "US":
            prof["tax_id"] = st.text_input("Tax ID / EIN (optional)", prof.get("tax_id",""))

        submitted = st.form_submit_button("Continue to Step 2")
    if submitted:
        # Minimal validation
        if not prof["legal_name"].strip():
            st.error("Legal name is required.")
            return
        if not prof["email"].strip():
            st.error("Email is required.")
            return
        if not any((l or "").strip() for l in prof.get("address_lines", [])):
            st.error("Provide at least one address line.")
            return
        set_step(2)

def step2():
    st.header("Step 2 — Client details")
    cli = st.session_state.client

    with st.form("form_step2"):
        cli["contact_name"] = st.text_input("Contact person", cli.get("contact_name",""))
        cli["company_name"] = st.text_input("Company name", cli.get("company_name",""))

        edit_address_lines(cli, "address_lines", "Client")

        cli["email"]        = st.text_input("Client email", cli.get("email",""))
        cli["po_reference"] = st.text_input("PO / Reference", cli.get("po_reference",""))
        cli["notes"]        = st.text_area("Client notes (shown on invoice)", cli.get("notes",""))
        submitted = st.form_submit_button("Continue to Step 3")
    if submitted:
        if not (cli["contact_name"].strip() or cli["company_name"].strip()):
            st.error("Provide at least a contact person or a company name.")
            return
        if not any((l or "").strip() for l in cli.get("address_lines", [])):
            st.error("Provide at least one client address line.")
            return
        set_step(3)

def step3():
    st.header("Step 3 — Items and invoice metadata")
    inv = st.session_state.invoice

    with st.form("form_step3"):
        # Invoice metadata
        inv["invoice_number"] = st.text_input("Invoice number (required)", inv.get("invoice_number",""))
        inv["invoice_date"]   = st.date_input("Invoice date", inv.get("invoice_date", date.today()))
        terms_str = st.text_input("Payment terms (days, required)", "" if inv.get("terms_days") is None else str(inv.get("terms_days")))
        inv["currency"] = st.selectbox("Currency (single per invoice)", ["GBP","USD","EUR"], index=["GBP","USD","EUR"].index(inv.get("currency","GBP")))

        # Tax
        inv["tax_label_mode"] = st.selectbox("Tax label", tax_label_options(), index=tax_label_options().index(inv.get("tax_label_mode","None")))
        if inv["tax_label_mode"] == "Custom":
            inv["tax_label_custom"] = st.text_input("Custom tax label", inv.get("tax_label_custom",""))
        inv["tax_rate"] = st.number_input("Tax rate (%)", min_value=0.0, max_value=100.0, value=float(inv.get("tax_rate", 0.0)), step=0.5)

        # Items editor
        st.subheader("Line items")
        add_col1, add_col2 = st.columns([1,1])
        new_basis = add_col1.selectbox("Basis", ["per job", "per line", "per word", "per finished hour", "per session", "per hour"])
        new_desc  = add_col2.text_input("Description")

        cols = st.columns([1,1,1])
        new_qty  = cols[0].number_input("Quantity", min_value=0.0, value=0.0, step=0.25, format="%.2f")
        new_rate = cols[1].number_input("Rate",     min_value=0.0, value=0.0, step=1.0,  format="%.2f")
        add_clicked = cols[2].form_submit_button("Add item")

        items = st.session_state.line_items

        # Render current items with remove buttons
        for idx, it in enumerate(items):
            c1, c2, c3, c4, c5 = st.columns([1,2,2,2,1])
            c1.write(f"{idx+1}")
            c2.write(it.get("basis",""))
            c3.write(it.get("description",""))
            c4.write(f"Qty {d2(it.get('qty',0))} × Rate {currency_symbol(inv['currency'])}{d2(it.get('rate',0))}")
            if c5.form_submit_button(f"Remove {idx}", use_container_width=True):
                st.session_state.line_items.pop(idx)
                st.experimental_rerun()

        # Compute and show running totals
        subtotal, tax_amount, total = compute_totals(items, inv.get("tax_rate") or 0.0)
        st.write(f"Subtotal: {currency_symbol(inv['currency'])}{subtotal}")
        if inv.get("tax_label_mode") != "None":
            label = inv.get("tax_label_custom","Tax") if inv.get("tax_label_mode") == "Custom" else inv.get("tax_label_mode")
            st.write(f"{label} ({d2(inv.get('tax_rate') or 0.0)}%): {currency_symbol(inv['currency'])}{tax_amount}")
        st.write(f"Total: {currency_symbol(inv['currency'])}{total}")

        # Navigation
        nav_left, nav_right = st.columns([1,1])
        back_clicked   = nav_left.form_submit_button("Back to Step 2")
        continue_click = nav_right.form_submit_button("Continue to Step 4")

    # Handle form intents
    if add_clicked:
        st.session_state.line_items.append({
            "basis": new_basis,
            "description": new_desc.strip(),
            "qty": float(new_qty),
            "rate": float(new_rate),
        })
        st.experimental_rerun()

    if back_clicked:
        set_step(2)

    if continue_click:
        if not inv["invoice_number"].strip():
            st.error("Invoice number is required.")
            return
        try:
            terms_days = int((terms_str or "").strip())
            if terms_days < 0:
                raise ValueError
            inv["terms_days"] = terms_days
        except Exception:
            st.error("Payment terms (days) must be a non-negative integer.")
            return
        if not st.session_state.line_items:
            st.error("Add at least one line item.")
            return
        set_step(4)

def step4():
    st.header("Step 4 — Payment options")
    pay = st.session_state.payments

    with st.form("form_step4"):
        st.write("Select accepted payment methods and provide details as needed.")
        cols = st.columns([1,1,1,1])
        pay["accept_wise"]   = cols[0].checkbox("Wise", value=pay.get("accept_wise", False))
        pay["accept_stripe"] = cols[1].checkbox("Stripe", value=pay.get("accept_stripe", False))
        pay["accept_paypal"] = cols[2].checkbox("PayPal", value=pay.get("accept_paypal", False))
        pay["accept_bank"]   = cols[3].checkbox("Direct Payment (Bank)", value=pay.get("accept_bank", False))

        if pay["accept_wise"]:
            pay["wise_text"] = st.text_area("Wise (instructions/links)", pay.get("wise_text",""))
        if pay["accept_stripe"]:
            pay["stripe_text"] = st.text_area("Stripe (instructions/links)", pay.get("stripe_text",""))
        if pay["accept_paypal"]:
            pay["paypal_text"] = st.text_area("PayPal (instructions/links)", pay.get("paypal_text",""))
        if pay["accept_bank"]:
            pay["bank_country"] = st.selectbox("Bank country", ["UK", "US"], index=["UK","US"].index(pay.get("bank_country","UK")))
            if pay["bank_country"] == "UK":
                uk = pay.get("bank_uk", {})
                uk["account_name"]   = st.text_input("UK Account name", uk.get("account_name",""))
                uk["sort_code"]      = st.text_input("UK Sort code", uk.get("sort_code",""))
                uk["account_number"] = st.text_input("UK Account number", uk.get("account_number",""))
                uk["iban"]           = st.text_input("UK IBAN (optional)", uk.get("iban",""))
                uk["bic"]            = st.text_input("UK BIC (optional)", uk.get("bic",""))
                pay["bank_uk"] = uk
            else:
                us = pay.get("bank_us", {})
                us["account_name"]    = st.text_input("US Account name", us.get("account_name",""))
                us["routing_number"]  = st.text_input("US Routing number", us.get("routing_number",""))
                us["account_number"]  = st.text_input("US Account number", us.get("account_number",""))
                us["ach_wire_notes"]  = st.text_area("US ACH/Wire notes (optional)", us.get("ach_wire_notes",""))
                pay["bank_us"] = us

        pay["footer_notes"] = st.text_area("Footer notes (optional, shown on invoice)", pay.get("footer_notes",""))

        submitted = st.form_submit_button("Continue to Step 5")
    if submitted:
        # Minimal rule: require at least one payment method or some text
        has_any = pay.get("accept_wise") or pay.get("accept_stripe") or pay.get("accept_paypal") or pay.get("accept_bank") or bool(pay.get("footer_notes"))
        if not has_any:
            st.error("Provide at least one payment method or footer note.")
            return
        set_step(5)

def step5():
    st.header("Step 5 — Preview & export")
    html_doc = render_preview_html()
    st.markdown(html_doc, unsafe_allow_html=True)

    st.write("---")
    c1, c2, c3, c4 = st.columns([1,1,1,1])
    if c1.button("Return to Step 1"):
        set_step(1)
    if c2.button("Return to Step 2"):
        set_step(2)
    if c3.button("Return to Step 3"):
        set_step(3)
    if c4.button("Return to Step 4"):
        set_step(4)

    # Generate PDF
    pdf_bytes = build_pdf_bytes()
    filename = (st.session_state.invoice.get("invoice_number") or "Invoice") + ".pdf"
    st.download_button("Download PDF", data=pdf_bytes, file_name=filename, mime="application/pdf")

# -----------------------------
# Main
# -----------------------------
def main():
    st.set_page_config(page_title="Invoice Builder (MVP - Part A)", layout="centered")
    ensure_session()
    step = st.session_state.current_step

    st.title("Invoice Builder (MVP — Part A)")

    if step == 0:
        step0()
    elif step == 1:
        step1()
    elif step == 2:
        step2()
    elif step == 3:
        step3()
    elif step == 4:
        step4()
    else:
        step5()

if __name__ == "__main__":
    main()
