
# invoicer.py — Part A + Part B integrated
# Streamlit invoice builder MVP with PDF export + self-prefill from prior exported PDFs

import streamlit as st
import streamlit.components.v1 as components
import html
import uuid
import io
from datetime import date, timedelta, datetime
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from typing import List, Dict, Any, Optional
from pdfminer.high_level import extract_text

# PDF (ReportLab) fallback renderer
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Flowable
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle

from urllib.parse import urlencode
from xml.sax.saxutils import escape as xml_escape

# For Part B: embed / extract snapshot
import base64, json, zlib, re
try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

INVOICER_META_TAG = "INVOICER_V2"

# ---- Persistence (autosave keyed by user key) ----

@st.cache_resource
def get_store():
    return {}

def build_snapshot() -> Dict[str, Any]:
    return {
        "version": 2,
        "saved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "profile": st.session_state.get("profile", {}),
        "client": st.session_state.get("client", {}),
        "invoice": st.session_state.get("invoice", {}),
        "line_items": st.session_state.get("line_items", []),
        "payments": st.session_state.get("payments", {}),
        "current_step": st.session_state.get("current_step", -1),
        "your_addr_count": st.session_state.get("your_addr_count", 1),
        "client_addr_count": st.session_state.get("client_addr_count", 1),
    }

def hydrate_from_snapshot(s: Dict[str, Any]):
    st.session_state.profile = s.get("profile", {})
    st.session_state.client = s.get("client", {})
    st.session_state.invoice = s.get("invoice", {})
    st.session_state.line_items = s.get("line_items", [])
    st.session_state.payments = s.get("payments", {})
    st.session_state.current_step = s.get("current_step", -1)
    st.session_state.your_addr_count = s.get("your_addr_count", 1)
    st.session_state.client_addr_count = s.get("client_addr_count", 1)

def save_snapshot():
    userkey = st.session_state.get("userkey", "").strip()
    if not userkey:
        return
    get_store()[userkey] = build_snapshot()

def load_snapshot(userkey: str) -> Optional[Dict[str, Any]]:
    return get_store().get(userkey)

# ---- Importing ----

# ---- Importing / regex ----
ENC_KV_TOKEN_SPLIT = re.compile(r'[ \t\r\n]*&[ \t\r\n]*')
ENC_KV_PAIR = re.compile(r'^(enc_[a-z0-9_]+)=(.*)$', re.IGNORECASE)

def extract_enc_payload_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Extract all enc_* key=value pairs from a PDF, tolerating PDF line wraps.
    Returns a single '&'-joined string like 'enc_a=...&enc_b=...'.
    """
    raw = extract_text(io.BytesIO(pdf_bytes)) or ""

    # 1) Unescape HTML entities so '&amp;' becomes '&' before any splitting
    txt = html.unescape(raw)

    # 2) Normalise NBSP and collapse line breaks that split identifiers/words
    #    e.g., 'enc_invoi\nce_date' → 'enc_invoice_date'
    txt = txt.replace('\u00A0', ' ')

    # 3) Find all enc_* pairs, allowing any content (incl. newlines) in the value
    #    until the next '& enc_' boundary or end of text.
    #    - Key:   enc_[a-z0-9_]+  (case-insensitive)
    #    - Value: non-greedy, up to next '& enc_' (with optional spaces/newlines) or end
    # Allow newlines inside keys and between pairs. Key may be split by PDF line-wraps.
    pair_re = re.compile(
        r'(?i)(enc_[a-z0-9_]+(?:\s*\n\s*[a-z0-9_]+)*)\s*=\s*(.*?)(?=(?:\s*&\s*enc_[a-z0-9_]+(?:\s*\n\s*[a-z0-9_]+)*\s*=)|$)',
        re.DOTALL
    )
    
    pairs = []
    for m in pair_re.finditer(txt):
        k = m.group(1)
        v = m.group(2)
    
        # KEY: remove any whitespace/newlines that occurred due to PDF wrapping
        k = re.sub(r'\s+', '', k)
    
        # VALUE: preserve spaces; convert any newline(s) (with surrounding spaces) to a single space
        v = v.replace('\r\n', '\n').replace('\r', '\n')
        v = re.sub(r'\s*\n\s*', ' ', v)
        v = v.strip()
    
        pairs.append(f"{k}={v}")
    
    return "&".join(pairs)

def parse_enc_payload_to_dict(payload_line: str) -> dict:
    """Parse 'enc_key=value&…' into dict; normalise 'NIL'→''."""
    result = {}
    if not payload_line:
        return result
    for tok in ENC_KV_TOKEN_SPLIT.split(payload_line):
        if not tok:
            continue
        m = ENC_KV_PAIR.match(tok.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        # De-escape &amp; from storage and convert NIL→''
        v = v.replace("&amp;", "&")
        result[k] = "" if v.strip().upper() == "NIL" else v
    return result

def _to_bool(x):
    if isinstance(x, bool):
        return x
    s = (x or "").strip().lower()
    return s in {"1","true","yes","y","on"}

def _addr_list(enc: dict, prefix: str) -> list[str]:
    """Collect address lines in numeric order for prefix like 'enc_trading_address_line_'."""
    keys = [k for k in enc if k.startswith(prefix)]
    keys.sort(key=lambda k: int(k.rsplit("_", 1)[1]))
    return [enc.get(k, "") for k in keys]

def _inc_invoice_number(s: str) -> str:
    """
    Increment the trailing number while preserving any prefix and zero padding.
    e.g., 'INV-00209' -> 'INV-00210', '2025-9' -> '2025-10'.
    If no trailing digits, append '-1'.
    """
    if not s:
        return "INV-1"
    m = re.search(r'(.*?)(\d+)$', s)
    if not m:
        return f"{s}-1"
    prefix, digits = m.group(1), m.group(2)
    n = int(digits) + 1
    return f"{prefix}{n:0{len(digits)}d}"

def build_prof_from_payload(enc: dict) -> dict:
    """
    Build a SINGLE flat 'prof' dict containing everything needed by your steps.
    Where the UI expects nested structures (addresses, items, bank_uk/us), we keep them.
    """
    # Your address
    your_addr = _addr_list(enc, "enc_trading_address_line_")
    # Client address
    client_addr = _addr_list(enc, "enc_bill_to_address_line_")

    # Items: indices 1..N
    idxs = sorted({int(m.group(1))
                   for k in enc
                   for m in [re.match(r'enc_item_(\d+)_', k)]
                   if m})
    items = []
    for i in idxs:
        items.append({
            "number":            str(i),
            "basis":             enc.get(f"enc_item_{i}_basis",""),
            "description":       enc.get(f"enc_item_{i}_description",""),
            "qty_display":       enc.get(f"enc_item_{i}_qty_display",""),
            "rate_display":      enc.get(f"enc_item_{i}_rate_display",""),
            "line_total_display":enc.get(f"enc_item_{i}_line_total_display",""),
        })

    # Payments / banks
    bank_country = enc.get("enc_bank_country","")
    bank_uk = {
        "account_name": enc.get("enc_bank_uk_account_name",""),
        "sort_code": enc.get("enc_bank_uk_sort_code",""),
        "account_number": enc.get("enc_bank_uk_account_number",""),
        "iban": enc.get("enc_bank_uk_iban",""),
        "bic": enc.get("enc_bank_uk_bic",""),
    }
    bank_us = {
        "account_name": enc.get("enc_bank_us_account_name",""),
        "routing_number": enc.get("enc_bank_us_routing_number",""),
        "account_number": enc.get("enc_bank_us_account_number",""),
        "ach_wire_notes": enc.get("enc_bank_us_ach_wire_notes",""),
    }

    # Build the flat 'prof' (contains everything used in Steps 1–4)
    prof = {
        # Step 1
        "region":           enc.get("enc_region",""),
        "legal_name":       enc.get("enc_heading_name",""),
        "trading_name":     enc.get("enc_trading_name",""),
        "address_lines":    your_addr,
        "email":            enc.get("enc_email",""),
        "phone":            (enc.get("enc_phone","") or "").replace("\n", " ").replace("\r", " ").strip(),
        "mobile":           (enc.get("enc_mobile","") or "").replace("\n", " ").replace("\r", " ").strip(),
        "company_number":   enc.get("enc_company_number",""),
        "vat_number":       enc.get("enc_vat_number",""),
        "tax_id":           enc.get("enc_tax_id",""),

        # Step 2 – client
        "contact_name":     enc.get("enc_bill_to_contact_name",""),
        "company_name":     enc.get("enc_bill_to_company_name",""),
        "client_address_lines": client_addr,
        "client_email":     enc.get("enc_bill_to_email",""),
        "po_reference":     enc.get("enc_bill_to_po_reference",""),
        "notes":            enc.get("enc_bill_to_notes",""),

        # Step 3 – invoice meta (invoice_number auto-incremented here)
        "invoice_number":   _inc_invoice_number(enc.get("enc_invoice_number","")),
        "invoice_date":     enc.get("enc_invoice_date",""),
        "terms_days":       enc.get("enc_terms_days",""),
        "currency":         enc.get("enc_currency",""),
        "tax_label_mode":   enc.get("enc_tax_label_mode",""),
        "tax_label_custom": enc.get("enc_tax_label_custom",""),
        "tax_rate":         enc.get("enc_tax_rate",""),

        # Items
        "items":            items,

        # Step 4 – payments
        "accept_wise":      _to_bool(enc.get("enc_accept_wise")),
        "wise_text":        enc.get("enc_wise_text",""),
        "accept_stripe":    _to_bool(enc.get("enc_accept_stripe")),
        "stripe_text":      enc.get("enc_stripe_text",""),
        "accept_paypal":    _to_bool(enc.get("enc_accept_paypal")),
        "paypal_text":      enc.get("enc_paypal_text",""),
        "accept_bank":      _to_bool(enc.get("enc_accept_bank")),
        "bank_country":     bank_country,
        "bank_uk":          bank_uk,
        "bank_us":          bank_us,
        "footer_notes":     enc.get("enc_footer_notes",""),
    }
    return prof

# ---- Utilities ----

DEC_QUANT = Decimal("0.01")

def _to_int(x, default=None):
    try:
        return int(str(x).strip()) if x not in (None, "") else default
    except Exception:
        return default

def _to_float(x, default=0.0):
    try:
        return float(str(x).strip()) if x not in (None, "") else default
    except Exception:
        return default

def _parse_invoice_date(s):
    # Your payload uses DD/MM/YYYY (e.g., "09/11/2025")
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y").date() if s else st.session_state.invoice.get("invoice_date")
    except Exception:
        return st.session_state.invoice.get("invoice_date")

def d2(x) -> Decimal:
    return (Decimal(str(x))).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)

def compute_due_date(inv_date: date, terms_days: int) -> date:
    """Return due date as invoice date + (N - 1) days, clamped at 0."""
    days = max(0, int(terms_days) - 1)
    return inv_date + timedelta(days=days)

def tax_label_options():
    return ["None", "VAT", "Sales Tax", "GST", "Custom"]

def ensure_session():
    if "current_step" not in st.session_state:
        st.session_state.current_step = -1
    if "restored" not in st.session_state:
        st.session_state.restored = False
    if "userkey" not in st.session_state:
        st.session_state.userkey = ""

    if "profile" not in st.session_state:
        st.session_state.profile = {
            "legal_name": "",
            "trading_name": "",
            "address_lines": [""],
            "email": "",
            "phone": "",
            "mobile": "",
            "company_number": "",
            "vat_number": "",
            "tax_id": "",
            "region": "",
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
            "terms_days": None,
            "currency": "GBP",
            "tax_label_mode": "None",
            "tax_label_custom": "",
            "tax_rate": 0.0,
        }
    if "line_items" not in st.session_state:
        st.session_state.line_items = []
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
    if "your_addr_count" not in st.session_state:
        st.session_state.your_addr_count = max(1, len(st.session_state.profile.get("address_lines", [""])))
    if "client_addr_count" not in st.session_state:
        st.session_state.client_addr_count = max(1, len(st.session_state.client.get("address_lines", [""])))

def set_step(n: int):
    st.session_state.current_step = n
    save_snapshot()

def currency_symbol(code: str) -> str:
    return {"GBP": "£", "USD": "$", "EUR": "€"}.get(code, "")

def compute_totals(items: List[Dict[str, Any]], tax_rate_percent: float):
    subtotal = Decimal("0.00")
    for it in items:
        qty = d2(it.get("qty", 0))
        rate = d2(it.get("rate", 0))
        line_total = (qty * rate).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
        subtotal += line_total
    subtotal = subtotal.quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
    tax_amount = (subtotal * Decimal(str(tax_rate_percent)) / Decimal("100")).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
    total = (subtotal + tax_amount).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
    return subtotal, tax_amount, total

def format_region_date(d: date, region: str) -> str:
    if region == "US":
        return d.strftime("%m/%d/%Y")
    else:
        return d.strftime("%d/%m/%Y")

def format_quantity_display(basis: str, qty: float) -> str:
    if basis in ("per finished hour", "per hour"):
        total_minutes = int(round(qty * 60))
        hours = total_minutes // 60
        minutes = total_minutes % 60
        parts = []
        if hours:
            parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
        if minutes:
            parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
        return " ".join(parts) if parts else "0 minutes"
    elif basis == "per word":
        return f"{d2(qty).normalize()} words"
    elif basis == "per line":
        return f"{d2(qty).normalize()} lines"
    elif basis == "per session":
        return f"{d2(qty).normalize()} sessions"
    else:
        return f"{d2(qty).normalize()}"

def items_table_preview_html(items, currency):
    sym = currency_symbol(currency)
    rows = []
    for i, it in enumerate(items, start=1):
        qty = float(it.get("qty", 0))
        rate = d2(it.get("rate", 0))
        line_total = d2(qty) * rate
        qty_disp = format_quantity_display(it.get("basis",""), qty)
        rows.append(f"""
        <tr>
            <td style="padding:6px; border:1px solid #ddd;">{i}</td>
            <td style="padding:6px; border:1px solid #ddd;">{html.escape(it.get('basis',''))}</td>
            <td style="padding:6px; border:1px solid #ddd;">{html.escape(it.get('description',''))}</td>
            <td style="padding:6px; border:1px solid #ddd; text-align:right;">{qty_disp}</td>
            <td style="padding:6px; border:1px solid #ddd; text-align:right;">{sym}{rate}</td>
            <td style="padding:6px; border:1px solid #ddd; text-align:right;">{sym}{line_total.quantize(DEC_QUANT)}</td>
        </tr>
        """)
    body = "\n".join(rows) if rows else """
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

# ---- HTML Preview ----

def render_preview_html():
    prof = st.session_state.profile
    cli  = st.session_state.client
    inv  = st.session_state.invoice
    pay  = st.session_state.payments
    items = st.session_state.line_items

    subtotal, tax_amount, total = compute_totals(items, inv.get("tax_rate") or 0.0)
    sym = currency_symbol(inv["currency"])

    tl_mode = inv.get("tax_label_mode", "None")
    if tl_mode == "Custom":
        tax_label = inv.get("tax_label_custom", "").strip() or "Tax"
    elif tl_mode == "None":
        tax_label = None
    else:
        tax_label = tl_mode

    def fmt_addr(lines):
        ls = [l.strip() for l in (lines or []) if (l or "").strip()]
        return "<br/>".join(html.escape(x) for x in ls) if ls else ""

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
            if d.get("bic"):           pay_lines.append(f"BIC / SWIFT: {html.escape(d['bic'])}")
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

    items_html = items_table_preview_html(items, inv["currency"])

    totals_html = ""
    if items:
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

    inv_region = (st.session_state.profile.get("region") or "UK")
    inv_date_str = format_region_date(st.session_state.invoice.get("invoice_date", date.today()), inv_region)
    due_str = ""
    if st.session_state.invoice.get("terms_days") is not None:
        due_date = compute_due_date(st.session_state.invoice["invoice_date"], int(st.session_state.invoice["terms_days"]))
        due_str = format_region_date(due_date, inv_region)

    heading_name = st.session_state.profile.get("legal_name") or st.session_state.profile.get("trading_name") or ""

    header_html = f"""
    <div style="display:flex; justify-content:space-between; gap:20px; align-items:flex-start;">
      <div style="flex:1;">
        <div><strong>{html.escape(heading_name)}</strong></div>
        <div>{html.escape(st.session_state.profile.get('trading_name','')) if st.session_state.profile.get('legal_name') else ''}</div>
        <div>{fmt_addr(st.session_state.profile.get('address_lines'))}</div>
        <div>{html.escape(st.session_state.profile.get('email',''))}</div>
        <div>{html.escape(st.session_state.profile.get('phone',''))}</div>
        <div>{html.escape(st.session_state.profile.get('mobile',''))}</div>
        <div>{('Company Number (if applicable): ' + html.escape(st.session_state.profile.get('company_number',''))) if st.session_state.profile.get('company_number') else ''}</div>
        <div>{('VAT Number (if applicable): ' + html.escape(st.session_state.profile.get('vat_number',''))) if st.session_state.profile.get('vat_number') else ''}</div>
        <div>{('Tax ID: ' + html.escape(st.session_state.profile.get('tax_id',''))) if st.session_state.profile.get('tax_id') else ''}</div>
      </div>
      <div style="text-align:right; min-width:220px;">
        <div><strong>Invoice</strong></div>
        <div>Invoice No: {html.escape(st.session_state.invoice.get('invoice_number',''))}</div>
        <div>Invoice Date: {inv_date_str}</div>
        <div>Terms (days): {html.escape(str(st.session_state.invoice.get('terms_days') or ''))}</div>
        <div>Due Date: {html.escape(due_str)}</div>
        <div>Currency: {html.escape(st.session_state.invoice.get('currency',''))}</div>
      </div>
    </div>
    <hr/>
    <div>
      <div><strong>Bill To</strong></div>
      <div>{html.escape(st.session_state.client.get('contact_name',''))}</div>
      <div>{html.escape(st.session_state.client.get('company_name',''))}</div>
      <div>{fmt_addr(st.session_state.client.get('address_lines'))}</div>
      <div>{html.escape(st.session_state.client.get('email',''))}</div>
      <div>{('PO/Ref: ' + html.escape(st.session_state.client.get('po_reference',''))) if st.session_state.client.get('po_reference') else ''}</div>
      <div>{html.escape(st.session_state.client.get('notes',''))}</div>
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

# ---- PDF builder (ReportLab) w/ payload embedding ----

def build_pdf_bytes():
    prof = st.session_state.profile
    cli  = st.session_state.client
    inv  = st.session_state.invoice
    items = st.session_state.line_items
    pay  = st.session_state.payments

    # Page metadata
    def _on_page(canvas, doc):
        try:
            canvas.setAuthor("Invoice Builder")
            canvas.setTitle(st.session_state.invoice.get("invoice_number") or "Invoice")
            canvas.setSubject("Invoice")
            canvas.setCreator("Invoice Builder")
            canvas.setKeywords(_make_keywords_with_payload())
        except Exception:
            pass

    subtotal, tax_amount, total = compute_totals(items, inv.get("tax_rate") or 0.0)
    sym = currency_symbol(inv["currency"])

    tl_mode = inv.get("tax_label_mode", "None")
    if tl_mode == "Custom":
        tax_label = inv.get("tax_label_custom", "").strip() or "Tax"
    elif tl_mode == "None":
        tax_label = None
    else:
        tax_label = tl_mode

    inv_region = prof.get("region") or "UK"
    inv_date_str = format_region_date(inv.get("invoice_date", date.today()), inv_region)
    due_str = ""
    if inv.get("terms_days") is not None:
        due_str = format_region_date(compute_due_date(inv.get("invoice_date", date.today()), int(inv["terms_days"])), inv_region)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=16*mm, bottomMargin=16*mm)
    styles = getSampleStyleSheet()
    styleN = styles["Normal"]
    styleB = styles["Heading4"]
    styleRight = ParagraphStyle("right", parent=styleN, alignment=TA_RIGHT)
    styleN.leading = 14
    styleB.leading = 16

    story = []

    heading_name = prof.get("legal_name") or prof.get("trading_name") or ""

    header_table_data = [
        [Paragraph(f"<b>{html.escape(heading_name)}</b>", styleN), Paragraph("<b>Invoice</b>", styleRight)],
        [Paragraph(f"{html.escape(prof.get('trading_name','')) if prof.get('legal_name') else ''}", styleN),
         Paragraph(f"Invoice No: {html.escape(inv.get('invoice_number',''))}", styleRight)],
        [Paragraph("<br/>".join([*(html.escape(l) for l in (prof.get('address_lines') or []) if l)]), styleN),
         Paragraph(f"Invoice Date: {inv_date_str}", styleRight)],
        [Paragraph(f"{html.escape(prof.get('email',''))}", styleN),
         Paragraph(f"Terms (days): {html.escape(str(inv.get('terms_days') or ''))}", styleRight)],
        [Paragraph(f"{html.escape(prof.get('phone',''))}" + (f" | {html.escape(prof.get('mobile',''))}" if prof.get('mobile') else ""), styleN),
         Paragraph(f"Due Date: {html.escape(due_str)}", styleRight)],
        [Paragraph(
            " ".join([
                (f"Company Number (if applicable): {html.escape(prof.get('company_number'))}") if prof.get("company_number") else "",
                (f"VAT Number (if applicable): {html.escape(prof.get('vat_number'))}") if prof.get("vat_number") else "",
                (f"Tax ID: {html.escape(prof.get('tax_id'))}") if prof.get("tax_id") else "",
            ]).strip(),
            styleN
         ),
         Paragraph(f"Currency: {html.escape(inv.get('currency',''))}", styleRight)],
    ]
    header_table = Table(header_table_data, colWidths=[110*mm, 60*mm])
    header_table.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP")]))
    story.append(header_table)
    story.append(Spacer(1, 4))

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
    story.append(Spacer(1, 6))

    tbl_header = ["#", "Basis", "Description", "Qty", "Rate", "Line Total"]
    tbl_data = [tbl_header]
    for i, it in enumerate(items, start=1):
        qty = float(it.get("qty", 0))
        rate = d2(it.get("rate", 0))
        total_line = (d2(qty) * rate).quantize(DEC_QUANT, rounding=ROUND_HALF_UP)
        qty_disp = format_quantity_display(it.get("basis",""), qty)
        tbl_data.append([str(i), it.get("basis",""), it.get("description",""), qty_disp, f"{sym}{rate}", f"{sym}{total_line}"])
    if len(tbl_data) == 1:
        tbl_data.append(["", "", "No items", "", "", ""])

    table = Table(tbl_data, repeatRows=1, colWidths=[10*mm, 30*mm, 85*mm, 30*mm, 20*mm, 25*mm])
    table.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("BACKGROUND", (0,0), (-1,0), colors.whitesmoke),
        ("ALIGN", (3,1), (5,-1), "RIGHT"),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
    ]))
    story.append(table)
    story.append(Spacer(1, 6))

    if items:
        subtotal, tax_amount, total = compute_totals(items, inv.get("tax_rate") or 0.0)
        totals_rows = [["Subtotal", f"{sym}{subtotal}"]]
        if tl_mode != "None":
            label = (inv.get("tax_label_custom","Tax") if tl_mode == "Custom" else tl_mode)
            totals_rows.append([f"{label} ({d2(inv.get('tax_rate') or 0.0)}%)", f"{sym}{tax_amount}"])
        totals_rows.append(["Total", f"{sym}{total}"])
        totals_tbl = Table(totals_rows, colWidths=[45*mm, 30*mm])
        totals_tbl.setStyle(TableStyle([("ALIGN", (1,0), (1,-1), "RIGHT"), ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold")]))
        wrap = Table([[totals_tbl]], colWidths=[175*mm])
        wrap.setStyle(TableStyle([("ALIGN", (0,0), (-1,-1), "RIGHT")]))
        story.append(wrap)

    # Payment instructions
    story.append(Spacer(1, 6))
    story.append(Paragraph("<b>Payment Instructions</b>", styleB))
    lines = []

    def add_line(txt):
        if txt:
            lines.append(Paragraph(html.escape(txt), styleN))

    if pay.get("accept_wise"):
        add_line("Wise:"); add_line(pay.get("wise_text",""))
    if pay.get("accept_stripe"):
        add_line("Stripe:"); add_line(pay.get("stripe_text",""))
    if pay.get("accept_paypal"):
        add_line("PayPal:"); add_line(pay.get("paypal_text",""))
    if pay.get("accept_bank"):
        if pay.get("bank_country") == "UK":
            uk = pay.get("bank_uk", {})
            add_line("Bank Transfer (UK)")
            add_line(f"Account name: {uk.get('account_name','')}")
            add_line(f"Sort code: {uk.get('sort_code','')}")
            add_line(f"Account number: {uk.get('account_number','')}")
            if uk.get("iban"): add_line(f"IBAN: {uk.get('iban')}")
            if uk.get("bic"):  add_line(f"BIC / SWIFT: {uk.get('bic')}")
        else:
            us = pay.get("bank_us", {})
            add_line("Bank Transfer (US)")
            add_line(f"Account name: {us.get('account_name','')}")
            add_line(f"Routing number: {us.get('routing_number','')}")
            add_line(f"Account number: {us.get('account_number','')}")
            if us.get("ach_wire_notes"): add_line(f"Notes: {us.get('ach_wire_notes')}")
    if pay.get("footer_notes"):
        add_line(pay.get("footer_notes"))

    if not lines:
        lines = [Paragraph("No payment instructions provided.", styleN)]
    story.extend(lines)
    
    # === BEGIN: machine-readable footer payload (enc_key=value&...) ===
    # Writes ALL fields you listed. Empty/None → "NIL". Booleans as "True"/"False".
    # Address lines and items: emits one entry per element present in the current state.
    # NOTE: Keep this immediately before adding to `story` so the text isn't reflowed later.
    
    def _enc_scalar(v):
        """Convert any Python value to a string for the payload with empty→NIL and & escaped."""
        if v is None:
            s = "NIL"
        elif isinstance(v, bool):
            s = "True" if v else "False"
        else:
            s0 = str(v)
            s = s0 if s0.strip() != "" else "NIL"
        return s.replace("&", "&amp;")
    
    def _enc_pairs_from_state(prof, cli, inv, items, pay, inv_date_str, due_str, sym, tl_mode):
        pairs = []
    
        # ---- ORDERED FIELDS (per your requested UI order) ----
        # Region + your identity
        pairs.append(("enc_region",                  prof.get("region")))
        pairs.append(("enc_heading_name",            prof.get("legal_name")))
        pairs.append(("enc_trading_name",            prof.get("trading_name")))
    
        # Your address lines (emit exactly what's in state; NIL if blank)
        for i, line in enumerate(prof.get("address_lines") or []):
            pairs.append((f"enc_trading_address_line_{i}", line))
    
        pairs.append(("enc_email",                   prof.get("email")))
        pairs.append(("enc_phone",                   prof.get("phone")))
        pairs.append(("enc_mobile",                  prof.get("mobile")))
        pairs.append(("enc_company_number",          prof.get("company_number")))
        pairs.append(("enc_vat_number",              prof.get("vat_number")))
        pairs.append(("enc_tax_id",                  prof.get("tax_id")))
    
        # Client / Bill-To
        pairs.append(("enc_bill_to_contact_name",    cli.get("contact_name")))
        pairs.append(("enc_bill_to_company_name",    cli.get("company_name")))
        for i, line in enumerate(cli.get("address_lines") or []):
            pairs.append((f"enc_bill_to_address_line_{i}", line))
        pairs.append(("enc_bill_to_email",           cli.get("email")))
        pairs.append(("enc_bill_to_po_reference",    cli.get("po_reference")))
        pairs.append(("enc_bill_to_notes",           cli.get("notes")))
    
        # Invoice meta (as stored now; loader will +1 invoice_number on import)
        pairs.append(("enc_invoice_number",          inv.get("invoice_number")))
        pairs.append(("enc_invoice_date",            inv_date_str))
        pairs.append(("enc_terms_days",              inv.get("terms_days")))
        pairs.append(("enc_currency",                inv.get("currency")))
        pairs.append(("enc_tax_label_mode",          inv.get("tax_label_mode")))
        pairs.append(("enc_tax_label_custom",        inv.get("tax_label_custom")))
        pairs.append(("enc_tax_rate",                inv.get("tax_rate")))
        pairs.append(("enc_due_date",                due_str))
    
        # Line items (1..N)
        for i, it in enumerate(items or [], start=1):
            pairs.append((f"enc_item_{i}_number",            i))
            pairs.append((f"enc_item_{i}_basis",             it.get("basis")))
            pairs.append((f"enc_item_{i}_description",       it.get("description")))
            # Store the human-visible displays you currently show in the UI:
            pairs.append((f"enc_item_{i}_qty_display",       it.get("qty_display", it.get("qty"))))
            pairs.append((f"enc_item_{i}_rate_display",      it.get("rate_display", it.get("rate"))))
            pairs.append((f"enc_item_{i}_line_total_display",it.get("line_total_display")))
    
        # Payment methods (Wise / Stripe / PayPal / Bank)
        pairs.append(("enc_accept_wise",             pay.get("accept_wise")))
        pairs.append(("enc_wise_text",               pay.get("wise_text")))
        pairs.append(("enc_accept_stripe",           pay.get("accept_stripe")))
        pairs.append(("enc_stripe_text",             pay.get("stripe_text")))
        pairs.append(("enc_accept_paypal",           pay.get("accept_paypal")))
        pairs.append(("enc_paypal_text",             pay.get("paypal_text")))
        pairs.append(("enc_accept_bank",             pay.get("accept_bank")))
    
        # Bank details (UK vs US)
        pairs.append(("enc_bank_country",            pay.get("bank_country")))
        uk = (pay.get("bank_uk") or {}) if pay.get("bank_country") == "UK" else {}
        us = (pay.get("bank_us") or {}) if pay.get("bank_country") == "US" else {}
    
        # UK branch
        pairs.append(("enc_bank_uk_account_name",    uk.get("account_name")))
        pairs.append(("enc_bank_uk_sort_code",       uk.get("sort_code")))
        pairs.append(("enc_bank_uk_account_number",  uk.get("account_number")))
        pairs.append(("enc_bank_uk_iban",            uk.get("iban")))
        pairs.append(("enc_bank_uk_bic",             uk.get("bic")))
    
        # US branch
        pairs.append(("enc_bank_us_account_name",    us.get("account_name")))
        pairs.append(("enc_bank_us_routing_number",  us.get("routing_number")))
        pairs.append(("enc_bank_us_account_number",  us.get("account_number")))
        pairs.append(("enc_bank_us_ach_wire_notes",  us.get("ach_wire_notes")))
    
        # Footer notes
        pairs.append(("enc_footer_notes",            pay.get("footer_notes")))
        return pairs
    
    # Build the pairs
    enc_pairs = _enc_pairs_from_state(
        prof=prof, cli=cli, inv=inv, items=items, pay=pay,
        inv_date_str=inv_date_str, due_str=due_str, sym=sym, tl_mode=tl_mode
    )
    
    # Assemble with &amp; so ReportLab shows a literal '&'
    payload_line = "&amp;".join(f"{k}={_enc_scalar(v)}" for k, v in enc_pairs)
    
    payload_style = ParagraphStyle(
        "PayloadFooter",
        parent=styleN,
        fontName="Helvetica",
        fontSize=1,
        leading=1.1,
        textColor=colors.white,
    )
    
    story.append(Spacer(1, 1*mm))
    story.append(Paragraph(payload_line, payload_style))
    # === END: machine-readable footer payload ===

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    buf.seek(0)
    return buf.read()

# ---- UI helpers ----

def render_address_inputs_with_external_count(state_dict: dict, count_key: str, label_prefix: str, field: str, max_lines: int = 6):
    st.session_state[count_key] = st.number_input(
        f"{label_prefix} Address Lines (Count)".strip(),
        min_value=1, max_value=max_lines,
        value=int(st.session_state.get(count_key, max(1, len(state_dict.get(field) or [''])))),
        step=1, key=f"{count_key}_num"
    )

def draw_address_inputs_in_form(state_dict: dict, count_key: str, label_prefix: str, field: str):
    count = int(st.session_state.get(count_key, 1))
    lines = state_dict.get(field) or [""]
    if count > len(lines):
        lines += [""] * (count - len(lines))
    elif count < len(lines):
        lines = lines[:count]
    for i in range(count):
        lines[i] = st.text_input(
            f"{(label_prefix + ' Address Line ' + str(i+1)).strip()}",
            value=lines[i],
            key=f"{label_prefix}_addr_{i}_{field}"
        )
    state_dict[field] = lines

# ---- Steps ----

def step_minus_1():
    st.header("Access — Enter a User Key")
    st.caption("Use a unique key to isolate your data. Keep it private.")
    col1, col2 = st.columns([3,1])
    with col1:
        st.session_state.userkey = st.text_input("Enter a User Key", st.session_state.get("userkey",""))
    with col2:
        st.markdown("<div style='height: 1.95rem'></div>", unsafe_allow_html=True)
        if st.button("Generate"):
            st.session_state.userkey = str(uuid.uuid4())
            st.rerun()

    if st.button("Continue"):
        key = st.session_state.get("userkey","").strip()
        if not key:
            st.error("Please enter a user key (or generate one).")
            return
        snap = load_snapshot(key)
        if snap and not st.session_state.restored:
            hydrate_from_snapshot(snap)
            st.session_state.restored = True
        set_step(0)
        st.rerun()

def step0():
    import streamlit as st

    st.header("Step 0 — Upload Last Invoice (Optional)")
    st.caption("Upload a prior PDF created by this app to pre-fill details. If not provided or invalid, continue to Step 1.")

    uploaded = st.file_uploader("Upload your last invoice PDF (from this app)", type=["pdf"], key="prepop_pdf")

    # Show any staged payload preview if present
    if "__prepop_state" in st.session_state:
        st.info("A payload was parsed and is ready to apply. Click Continue to use it.")
        with st.expander("Preview parsed values", expanded=False):
            st.json(st.session_state["__prepop_state"])  # diagnostic preview

    if uploaded is not None:
        try:
            pdf_bytes = uploaded.read()
            # Your existing helper already in the file:
            payload_line = extract_enc_payload_text_from_pdf(pdf_bytes)  # returns the &-joined enc_* line
            if not payload_line:
                st.warning("No embedded payload found in this PDF.")
            else:
                enc = parse_enc_payload_to_dict(payload_line)
                prof = build_prof_from_payload(enc)
                
                # ---- Populate other session sections from prof (so Steps 2–4 prefill) ----

                # CLIENT (Step 2)
                st.session_state.client = {
                    "contact_name":        prof.get("contact_name", ""),
                    "company_name":        prof.get("company_name", ""),
                    "address_lines":       list(prof.get("client_address_lines") or []),
                    "email":               prof.get("client_email", ""),
                    "po_reference":        prof.get("client_po_reference", ""),
                    "notes":               prof.get("client_notes", ""),
                }
                st.session_state.client_addr_count = max(1, len(st.session_state.client.get("address_lines") or []))
                
                # INVOICE META + CURRENCY/TAX (Step 3)
                st.session_state.invoice.update({
                    "invoice_number":   prof.get("invoice_number", ""),
                    "invoice_date":     _parse_invoice_date(prof.get("invoice_date", "")),  # helper below
                    "terms_days":       _to_int(prof.get("terms_days")),
                    "currency":         prof.get("currency", st.session_state.invoice.get("currency", "GBP")),
                    "tax_label_mode":   prof.get("tax_label_mode", st.session_state.invoice.get("tax_label_mode", "None")),
                    "tax_label_custom": prof.get("tax_label_custom", ""),
                    "tax_rate":         _to_float(prof.get("tax_rate")),
                })
                
                # LINE ITEMS (Step 3 table body)
                st.session_state.line_items = [
                    {
                        "basis":       it.get("basis", ""),
                        "description": it.get("description", ""),
                        # Prefer *_display if present; fall back to numeric
                        "qty":         _to_float(it.get("qty_display", it.get("qty", 0))),
                        "rate":        _to_float(it.get("rate_display", it.get("rate", 0))),
                    }
                    for it in (prof.get("items") or [])
                ]
                
                # PAYMENTS (Step 4)
                pay = st.session_state.payments
                pay["accept_wise"]   = bool(prof.get("accept_wise"))
                pay["wise_text"]     = prof.get("wise_text", "")
                pay["accept_stripe"] = bool(prof.get("accept_stripe"))
                pay["stripe_text"]   = prof.get("stripe_text", "")
                pay["accept_paypal"] = bool(prof.get("accept_paypal"))
                pay["paypal_text"]   = prof.get("paypal_text", "")
                pay["accept_bank"]   = bool(prof.get("accept_bank"))
                pay["bank_country"]  = prof.get("bank_country", pay.get("bank_country", "UK"))
                pay["bank_uk"]       = dict(prof.get("bank_uk") or {"account_name": "", "sort_code": "", "account_number": "", "iban": "", "bic": ""})
                pay["bank_us"]       = dict(prof.get("bank_us") or {"account_name": "", "routing_number": "", "account_number": "", "ach_wire_notes": ""})
                pay["footer_notes"]  = prof.get("footer_notes", "")
                # ---- End population ----
                
                # Resize address-line selectors to match parsed payload
                st.session_state.your_addr_count = max(1, len(prof.get("address_lines") or []))
                st.session_state.client_addr_count = max(1, len(prof.get("client_address_lines") or []))
                
                # ---- Diagnostic display of payloads (raw + parsed) ----
                #st.divider()
                #st.subheader("Raw embedded payload text (as found in PDF)")
                #st.text_area(
                #    "Raw payload block",
                #    payload_line,
                #    height=140,
                #    key="raw_payload_preview"
                #)

                #st.subheader("Parsed enc_* key–value pairs")
                #parsed_pairs_preview = "\n".join(f"{k}={v}" for k, v in enc.items())
                #st.text_area(
                #    "Parsed pairs (enc_key=value)",
                #    parsed_pairs_preview,
                #    height=200,
                #    key="parsed_pairs_preview"
                #)

                #with st.expander("Parsed payload as JSON (dict)"):
                #    st.json(enc)

                #with st.expander("Built profile dict to pre-populate Steps 1–4"):
                #    st.json(prof)
                # ---- End diagnostics ----

                # Stage the parsed state; don't navigate yet
                st.session_state.profile = prof
                st.success("Parsed payload. Review (optional) and click Continue to apply.")
        except Exception as e:
            st.error(f"Failed to pre-populate from PDF: {e}")

    # Manual continue: apply staged values (if any), then proceed
    if st.button("Continue"):
        set_step(1)
        st.rerun()

def _tax_mode_changed():
    inv = st.session_state.invoice
    inv["tax_label_mode"] = st.session_state["_tax_mode_widget"]
    if inv["tax_label_mode"] != "Custom":
        inv["tax_label_custom"] = ""
    save_snapshot()
    st.rerun()

def step1():
    st.header("Step 1 — Your Details")

    region = st.selectbox(
        "Region (affects formatting and tax fields)",
        ["Select…", "UK", "US", "EU"],
        index=0 if not st.session_state.profile.get("region") else ["Select…","UK","US","EU"].index(st.session_state.profile.get("region")),
        key="region_select"
    )
    st.session_state.profile["region"] = "" if region == "Select…" else region

    prev_region = st.session_state.get("_prev_region")
    if prev_region and prev_region != region:
        if region == "US":
            st.session_state.profile.pop("vat_number", None)
        elif region in ("UK", "EU"):
            st.session_state.profile.pop("tax_id", None)
    st.session_state["_prev_region"] = region
    save_snapshot()

    if region == "Select…" or not region:
        st.info("Please select your region to continue.")
        return

    prof = st.session_state.profile
    render_address_inputs_with_external_count(prof, "your_addr_count", "Your", "address_lines")

    with st.form("form_step1"):
        prof["legal_name"]    = st.text_input("Legal Name (optional if Trading or Stage Name is provided)", prof.get("legal_name",""))
        prof["trading_name"]  = st.text_input("Trading or Stage Name (optional if Legal Name is provided)", prof.get("trading_name",""))

        draw_address_inputs_in_form(prof, "your_addr_count", "Your", "address_lines")

        prof["email"]          = st.text_input("Email (required)", prof.get("email",""))
        prof["phone"]          = st.text_input("Phone", prof.get("phone",""))
        mobile_label = "Cell Phone" if region == "US" else "Mobile Phone"
        prof["mobile"]         = st.text_input(mobile_label, prof.get("mobile",""))
        prof["company_number"] = st.text_input("Company Number (if applicable)", prof.get("company_number",""))

        if region in ("UK", "EU"):
            prof["vat_number"] = st.text_input("VAT Number (if applicable)", prof.get("vat_number",""))
        elif region == "US":
            prof["tax_id"] = st.text_input("Tax ID / EIN (optional)", prof.get("tax_id",""))

        cont = st.form_submit_button("Continue to Step 2")

    if cont:
        if not prof["email"].strip():
            st.error("Email is required.")
            return
        if not (prof.get("legal_name","").strip() or prof.get("trading_name","").strip()):
            st.error("Enter at least a Legal Name or a Trading/Stage Name.")
            return
        if not any((l or "").strip() for l in prof.get("address_lines", [])):
            st.error("Provide at least one address line.")
            return
        save_snapshot()
        set_step(2)
        st.rerun()

def step2():
    st.header("Step 2 — Client Details")
    cli = st.session_state.client

    render_address_inputs_with_external_count(cli, "client_addr_count", "Client", "address_lines")

    with st.form("form_step2"):
        cli["contact_name"] = st.text_input("Contact Person (optional if Company Name is provided)", cli.get("contact_name",""))
        cli["company_name"] = st.text_input("Company Name (optional if Contact Person is provided)", cli.get("company_name",""))

        draw_address_inputs_in_form(cli, "client_addr_count", "Client", "address_lines")

        cli["email"]        = st.text_input("Email", cli.get("email",""))
        cli["po_reference"] = st.text_input("PO / Reference", cli.get("po_reference",""))
        cli["notes"]        = st.text_area("Notes (shown on invoice)", cli.get("notes",""))

        back = st.form_submit_button("Back to Step 1")
        cont = st.form_submit_button("Continue to Step 3")
    if back:
        set_step(1); st.rerun()
    if cont:
        if not (cli["contact_name"].strip() or cli["company_name"].strip()):
            st.error("Provide at least a Contact Person or a Company Name.")
            return
        if not any((l or "").strip() for l in cli.get("address_lines", [])):
            st.error("Provide at least one Address line.")
            return
        save_snapshot(); set_step(3); st.rerun()

def step3():
    st.header("Step 3 — Items and Invoice Metadata")
    inv = st.session_state.invoice
    prof = st.session_state.profile
    region = prof.get("region") or "UK"

    if not inv.get("invoice_number"):
        inv["invoice_number"] = "INV-00000"
    inv["invoice_number"] = st.text_input("Invoice Number (required)", inv.get("invoice_number",""))

    fmt = "MM/DD/YYYY" if region == "US" else "DD/MM/YYYY"
    inv["invoice_date"] = st.date_input("Invoice Date", inv.get("invoice_date", date.today()), format=fmt)

    terms_str = st.text_input("Payment Terms (days, required)", "" if inv.get("terms_days") is None else str(inv.get("terms_days")))
    inv["currency"] = st.selectbox("Currency (single per invoice)", ["GBP","USD","EUR"],
                                   index=["GBP","USD","EUR"].index(inv.get("currency","GBP")))

    options = tax_label_options()
    current = inv.get("tax_label_mode","None")
    idx = options.index(current) if current in options else 0
    st.selectbox("Tax Label", options, index=idx, key="_tax_mode_widget")
    if st.session_state.get("_tax_mode_widget","None") != inv.get("tax_label_mode"):
        inv["tax_label_mode"] = st.session_state["_tax_mode_widget"]
        if inv["tax_label_mode"] != "Custom":
            inv["tax_label_custom"] = ""
        save_snapshot(); st.rerun()

    if inv.get("tax_label_mode") == "Custom":
        inv["tax_label_custom"] = st.text_input("Custom Tax Label", inv.get("tax_label_custom",""))
    inv["tax_rate"] = st.number_input("Tax Rate (%)", min_value=0.0, max_value=100.0,
                                      value=float(inv.get("tax_rate", 0.0)), step=0.5)

    st.subheader("Line Items")
    basis = st.selectbox("Billing Basis", ["per job", "per line", "per word", "per finished hour", "per session", "per hour"], key="_basis")
    desc  = st.text_input("Description", key="_desc")
    c1, c2, c3 = st.columns([1,1,1])

    qty_value = 0.0
    if basis in ("per finished hour", "per hour"):
        h = c1.number_input("Quantity Hours", min_value=0, value=0, step=1, key="_qty_hours")
        m = c2.number_input("Quantity Minutes", min_value=0, max_value=59, value=0, step=1, key="_qty_minutes")
        qty_value = h + (m / 60.0)
    elif basis == "per word":
        qty_value = c1.number_input("Quantity Words", min_value=0.0, value=0.0, step=50.0, format="%.0f", key="_qty_words")
    elif basis == "per line":
        qty_value = c1.number_input("Quantity Lines", min_value=0.0, value=0.0, step=1.0, format="%.0f", key="_qty_lines")
    elif basis == "per session":
        qty_value = c1.number_input("Quantity Sessions", min_value=0.0, value=0.0, step=1.0, format="%.0f", key="_qty_sessions")
    else:
        qty_value = c1.number_input("Quantity", min_value=0.0, value=1.0, step=1.0, format="%.0f", key="_qty_job")

    rate = c3.number_input("Rate", min_value=0.0, value=0.0, step=1.0,  format="%.2f", key="_rate")

    if st.button("Add Item"):
        st.session_state.line_items.append({
            "basis": basis, "description": desc.strip(),
            "qty": float(qty_value), "rate": float(rate),
        })
        save_snapshot(); st.rerun()

    items = st.session_state.line_items
    for idx_, it in enumerate(items):
        cA, cB, cC, cD, cE = st.columns([1,2,2,2,1])
        cA.write(f"{idx_+1}")
        cB.write(it.get("basis",""))
        cC.write(it.get("description",""))
        cD.write(f"{format_quantity_display(it.get('basis',''), float(it.get('qty',0)))} × {currency_symbol(inv['currency'])}{d2(it.get('rate',0))}")
        if cE.button("Remove", key=f"rm_{idx_}"):
            st.session_state.line_items.pop(idx_); save_snapshot(); st.rerun()

    if items:
        subtotal, tax_amount, total = compute_totals(items, inv.get("tax_rate") or 0.0)
        st.write(f"Subtotal: {currency_symbol(inv['currency'])}{subtotal}")
        if inv.get("tax_label_mode") != "None":
            label = inv.get("tax_label_custom","Tax") if inv.get("tax_label_mode") == "Custom" else inv.get("tax_label_mode")
            st.write(f"{label} ({d2(inv.get('tax_rate') or 0.0)}%): {currency_symbol(inv['currency'])}{tax_amount}")
        st.write(f"Total: {currency_symbol(inv['currency'])}{total}")

    c_back, c_cont = st.columns(2)
    if c_back.button("Back to Step 2"):
        set_step(2); st.rerun()
    if c_cont.button("Continue to Step 4"):
        if not inv["invoice_number"].strip():
            st.error("Invoice number is required."); return
        try:
            terms_days = int((terms_str or "").strip())
            if terms_days < 0: raise ValueError
            inv["terms_days"] = terms_days
        except Exception:
            st.error("Payment terms (days) must be a non-negative integer."); return
        if not st.session_state.line_items:
            st.error("Add at least one line item."); return
        save_snapshot(); set_step(4); st.rerun()

def step4():
    st.header("Step 4 — Payment Options")
    pay = st.session_state.payments

    pay["accept_wise"]   = st.checkbox("Accept Wise", value=pay.get("accept_wise", False))
    if pay["accept_wise"]:
        pay["wise_text"] = st.text_area("Wise Instructions / Link", pay.get("wise_text",""))

    pay["accept_stripe"] = st.checkbox("Accept Stripe", value=pay.get("accept_stripe", False))
    if pay["accept_stripe"]:
        pay["stripe_text"] = st.text_area("Stripe Instructions / Link", pay.get("stripe_text",""))

    pay["accept_paypal"] = st.checkbox("Accept PayPal", value=pay.get("accept_paypal", False))
    if pay["accept_paypal"]:
        pay["paypal_text"] = st.text_area("PayPal Instructions / Link", pay.get("paypal_text",""))

    pay["accept_bank"]   = st.checkbox("Accept Direct Payment (Bank)", value=pay.get("accept_bank", False))
    if pay["accept_bank"]:
        pay["bank_country"] = st.selectbox("Bank Country", ["UK", "US"], index=["UK","US"].index(pay.get("bank_country","UK")))
        if pay["bank_country"] == "UK":
            uk = dict(pay.get("bank_uk", {}))
            uk["account_name"]   = st.text_input("UK Account Name", uk.get("account_name",""))
            uk["sort_code"]      = st.text_input("UK Sort Code", uk.get("sort_code",""))
            uk["account_number"] = st.text_input("UK Account Number", uk.get("account_number",""))
            uk["iban"]           = st.text_input("UK IBAN (optional)", uk.get("iban",""))
            uk["bic"]            = st.text_input("UK BIC / SWIFT (optional)", uk.get("bic",""))
            pay["bank_uk"] = uk
        else:
            us = dict(pay.get("bank_us", {}))
            us["account_name"]    = st.text_input("US Account Name", us.get("account_name",""))
            us["routing_number"]  = st.text_input("US Routing Number", us.get("routing_number",""))
            us["account_number"]  = st.text_input("US Account Number", us.get("account_number",""))
            us["ach_wire_notes"]  = st.text_area("US ACH/Wire Notes (optional)", us.get("ach_wire_notes",""))
            pay["bank_us"] = us

    pay["footer_notes"] = st.text_area("Footer Notes (optional, shown on invoice)", pay.get("footer_notes",""))

    c_back, c_cont = st.columns(2)
    if c_back.button("Back to Step 3"):
        save_snapshot(); set_step(3); st.rerun()
    if c_cont.button("Continue to Step 5"):
        errors = []
        def require_nonempty(name, val):
            if not (val or "").strip():
                errors.append(f"{name} text is required when enabled.")

        has_any = False
        if pay.get("accept_wise"):
            has_any = True; require_nonempty("Wise", pay.get("wise_text",""))
        if pay.get("accept_stripe"):
            has_any = True; require_nonempty("Stripe", pay.get("stripe_text",""))
        if pay.get("accept_paypal"):
            has_any = True; require_nonempty("PayPal", pay.get("paypal_text",""))
        if pay.get("accept_bank"):
            has_any = True
            if pay.get("bank_country") == "UK":
                uk = pay.get("bank_uk", {})
                name_ok = (uk.get("account_name","").strip() != "")
                combo1 = uk.get("sort_code","").strip() != "" and uk.get("account_number","").strip() != ""
                combo2 = uk.get("iban","").strip() != "" and uk.get("bic","").strip() != ""
                if not (name_ok and (combo1 or combo2)):
                    errors.append("UK Bank requires Account Name and either (Sort Code + Account Number) or (IBAN + BIC/SWIFT).")
            else:
                us = pay.get("bank_us", {})
                if not (us.get("account_name","").strip() and us.get("routing_number","").strip() and us.get("account_number","").strip()):
                    errors.append("US Bank requires Account Name, Routing Number, and Account Number.")
        if pay.get("footer_notes","").strip():
            has_any = True
        if not has_any:
            errors.append("Provide at least one payment method or footer note.")
        if errors:
            for e in errors: st.error(e)
            return
        save_snapshot(); set_step(5); st.rerun()

def step5():
    st.header("Step 5 — Preview & Export")
    html_doc = render_preview_html()

    components.html(f"""<!doctype html><html><head><meta charset="utf-8"><title>Invoice</title></head>
    <body>{html_doc}</body></html>""", height=900, scrolling=True)

    st.write("---")
    c1, c2, c3, c4 = st.columns([1,1,1,1])
    if c1.button("Return to Step 1"):
        set_step(1); st.rerun()
    if c2.button("Return to Step 2"):
        set_step(2); st.rerun()
    if c3.button("Return to Step 3"):
        set_step(3); st.rerun()
    if c4.button("Return to Step 4"):
        set_step(4); st.rerun()

    pdf_bytes = build_pdf_bytes()
    filename = (st.session_state.invoice.get("invoice_number") or "Invoice") + ".pdf"
    st.download_button("Download PDF", data=pdf_bytes, file_name=filename, mime="application/pdf")

# ---- Main ----

def main():
    st.set_page_config(page_title="Invoice Builder", layout="centered")
    ensure_session()

    # Render access page in isolation
    if st.session_state.get("current_step", -1) == -1:
        st.title("Invoice Builder")
        step_minus_1()
        st.stop()

    # Normal restore (only after key exists)
    key = st.session_state.get("userkey","").strip()
    if key and not st.session_state.restored:
        snap = load_snapshot(key)
        if snap:
            hydrate_from_snapshot(snap)
            st.session_state.restored = True

    st.title("Invoice Builder")
    step = st.session_state.current_step

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
