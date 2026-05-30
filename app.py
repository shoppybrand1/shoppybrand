import os
import io
import re
import time
import atexit
from datetime import datetime, date, timedelta
from functools import wraps
from html import escape

from flask import Flask, render_template, request, jsonify, session, redirect, send_file
from dotenv import load_dotenv
from whitenoise import WhiteNoise
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as _pg_pool
import psycopg2.errors
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import cloudinary
import cloudinary.uploader
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

_log_level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)

# ── EMAIL CREDENTIALS (read once at startup) ──────────────────────────────────
# Set GMAIL_USER and GMAIL_PASSWORD as environment variables on your platform.
# On Render: Dashboard → your service → Environment → Add environment variable.
GMAIL_USER     = os.environ.get('GMAIL_USER', '').strip()
GMAIL_PASSWORD = os.environ.get('GMAIL_PASSWORD', '').replace(' ', '').strip()

if not GMAIL_USER:
    logging.warning('GMAIL_USER environment variable is not set — emails will not send')
if not GMAIL_PASSWORD:
    logging.warning('GMAIL_PASSWORD environment variable is not set — emails will not send')

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']       # must be set — no fallback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL   = os.getenv('DATABASE_URL')
ADMIN_PASSWORD = os.environ['ADMIN_PASSWORD']    # must be set — no fallback
INVOICES_DIR   = os.path.join(BASE_DIR, 'data', 'invoices')
BASE_URL       = os.getenv('BASE_URL', 'https://shoppybrand.onrender.com')

# CQ-07: moved here from mid-file so it is defined before first use
_CATEGORY_ORDER = ['Sneakers', 'Tracksuits', 'Horloges', 'Accessoires']

# PERF-05: incremented whenever product stock changes; clients use it as ETag seed
_products_version: int = 0

cloudinary.config(cloudinary_url=os.getenv('CLOUDINARY_URL', ''))

# Serve static files in production (gunicorn has no built-in static handler)
app.wsgi_app = WhiteNoise(app.wsgi_app, root=os.path.join(BASE_DIR, 'static'), prefix='static')

# ── CONNECTION POOL (STB-01) ──────────────────────────────────────────────────
_pool = None
if DATABASE_URL:
    try:
        _pool = _pg_pool.ThreadedConnectionPool(
            minconn=2, maxconn=10,
            dsn=DATABASE_URL,
            cursor_factory=RealDictCursor,
        )
        logging.info('DB connection pool ready (min=2, max=10)')
    except Exception:
        logging.exception('Failed to initialise DB connection pool')


# ── RATE LIMITING ─────────────────────────────────────────────────────────────

def _instagram_rate_key():
    """Use the normalised Instagram handle as the rate-limit bucket key."""
    data = request.get_json(silent=True) or {}
    ig = (data.get('instagram') or '').strip().lower().lstrip('@')
    return ig or get_remote_address()

limiter = Limiter(
    key_func=get_remote_address,   # global default (not applied to any route by default)
    app=app,
    storage_uri='memory://',       # in-memory; fine for workers=1
    default_limits=[],             # no global limit — only explicit per-route limits
)

@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({
        'success': False,
        'error': 'Je hebt te veel bestellingen geplaatst. Probeer het over een uur opnieuw.',
    }), 429


# ── DATABASE ──────────────────────────────────────────────────────────────────

class _DbConn:
    """Thin wrapper that borrows a connection from the pool."""

    def __init__(self):
        if _pool is None:
            raise RuntimeError('DB pool not initialised — check DATABASE_URL env var')
        self._conn = _pool.getconn()

    def execute(self, query, params=None):
        cur = self._conn.cursor()
        cur.execute(query, params or ())
        return cur

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        # Ensure clean state before returning the connection to the pool.
        # rollback() is a no-op when nothing is pending (post-commit).
        try:
            self._conn.rollback()
        except Exception:
            pass
        _pool.putconn(self._conn)


def get_db():
    return _DbConn()


_settings_cache: dict = {}
_settings_ts: float = 0.0


def _invalidate_products_cache():
    """Increment the ETag seed so the next /api/products fetch bypasses the browser cache."""
    global _products_version
    _products_version += 1

def get_settings():
    global _settings_cache, _settings_ts
    if time.time() - _settings_ts < 60:          # serve from cache if < 60s old
        return _settings_cache
    conn = get_db()
    try:
        rows = conn.execute('SELECT key, value FROM settings').fetchall()
    finally:
        conn.close()
    _settings_cache = {r['key']: r['value'] for r in rows}
    _settings_ts    = time.time()
    return _settings_cache


# ── PRODUCT GROUPING HELPER (CQ-03) ──────────────────────────────────────────

def _group_products(rows, group_keys, size_keys, update_foto=False):
    """
    Collapse flat product rows into {item_id → group_dict} ordered by first seen.

    group_keys:  field names kept at group level (read from first row per group).
    size_keys:   field names kept per size variant.
    update_foto: if True, promote a non-None foto from later rows to the group.
    """
    groups: dict = {}
    order: list  = []
    for r in rows:
        iid = r.get('item_id') or r.get('sku_key')
        if iid not in groups:
            order.append(iid)
            groups[iid] = {k: r.get(k) for k in group_keys}
            groups[iid]['item_id'] = iid
            groups[iid]['sizes']   = []
        elif update_foto and not groups[iid].get('foto') and r.get('foto'):
            groups[iid]['foto'] = r['foto']
        groups[iid]['sizes'].append({k: r.get(k) for k in size_keys})
    return {iid: groups[iid] for iid in order}


# ── EMAIL HELPERS (CQ-04) ─────────────────────────────────────────────────────

def _resolve_pdf_bytes(pdf_source) -> bytes:
    """Accept bytes, BytesIO, or a file path and return raw PDF bytes."""
    if isinstance(pdf_source, (bytes, bytearray)):
        return bytes(pdf_source)
    if hasattr(pdf_source, 'read'):
        return pdf_source.read()
    with open(pdf_source, 'rb') as fh:
        return fh.read()


def _send_email_with_attachment(to_addr, subject, html_body, pdf_bytes, bestelnummer):
    """Send one HTML+PDF email via Gmail SMTP. Raises on any failure."""
    if not GMAIL_USER or not GMAIL_PASSWORD:
        raise ValueError(
            'E-mail credentials ontbreken. '
            'Stel GMAIL_USER en GMAIL_PASSWORD in als omgevingsvariabelen.'
        )
    msg = MIMEMultipart('mixed')
    msg['From']    = f'ShoppyBrand <{GMAIL_USER}>'
    msg['To']      = to_addr
    msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    att = MIMEApplication(pdf_bytes, _subtype='pdf')
    att.add_header('Content-Disposition', 'attachment',
                   filename=f'factuur_{bestelnummer}.pdf')
    msg.attach(att)

    with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, to_addr, msg.as_bytes())


# ── PDF INVOICE ───────────────────────────────────────────────────────────────

def generate_invoice(order_data, output=None):
    """
    Build the invoice PDF.

    output=None   → write to INVOICES_DIR/<bestelnummer>.pdf and return the path.
    output=BytesIO → write into that buffer and return the buffer (seek(0) already called).
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
    from reportlab.lib.utils import ImageReader

    bestelnummer = order_data['bestelnummer']

    if output is None:
        os.makedirs(INVOICES_DIR, exist_ok=True)
        dest = os.path.join(INVOICES_DIR, f'factuur_{bestelnummer}.pdf')
    else:
        dest = output

    BLACK      = colors.HexColor('#000000')
    DARK_GREY  = colors.HexColor('#444444')
    LIGHT_GREY = colors.HexColor('#f5f5f5')
    GOLD       = colors.HexColor('#c9a84c')
    ALT_ROW    = colors.HexColor('#fafafa')
    RED        = colors.HexColor('#CC0000')
    BLUE       = colors.HexColor('#0000CC')
    BASE       = 11

    PAGE_W, _ = A4
    margin    = 40
    cw        = PAGE_W - 2 * margin

    doc = SimpleDocTemplate(
        dest, pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin,
    )

    def p(text, font='Helvetica', size=BASE, color=DARK_GREY, align=TA_LEFT):
        return Paragraph(text, ParagraphStyle(
            'x', fontName=font, fontSize=size, textColor=color,
            alignment=align, leading=size * 1.45,
        ))

    def fmt_eur(n):
        return '€ ' + f'{float(n):.2f}'.replace('.', ',')

    elements = []

    # 1. HEADER
    logo_path = os.path.join(BASE_DIR, 'static', 'logo.jpg')
    if os.path.exists(logo_path):
        try:
            ir = ImageReader(logo_path)
            iw, ih = ir.getSize()
            lh = 60
            lw = min(int(lh * iw / ih), 150)
            logo_cell = Image(logo_path, width=lw, height=lh)
        except Exception:
            logo_cell = p('<b>ShoppyBrand</b>', font='Helvetica-Bold', size=14, color=BLACK)
    else:
        logo_cell = p('<b>ShoppyBrand</b>', font='Helvetica-Bold', size=14, color=BLACK)

    brand_block = p(
        'ShoppyBrand<br/>Instagram: @Shoppybrand_<br/>Facebook: Shoppybrand<br/>E-mail: Shoppy.brand1@gmail.com',
        size=BASE, color=DARK_GREY, align=TA_LEFT,
    )
    hdr = Table([[logo_cell, brand_block]], colWidths=[cw * 0.5, cw * 0.5])
    hdr.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN',  (1, 0), (1, 0),   'RIGHT'),
    ]))
    elements += [hdr, Spacer(1, 10)]

    # 2. Gold divider
    elements += [HRFlowable(width='100%', thickness=2, color=GOLD), Spacer(1, 14)]

    # 3. INVOICE INFO — heading full width, then two columns
    try:
        datum = datetime.fromisoformat(order_data['besteldatum']).strftime('%d-%m-%Y')
    except Exception:
        datum = datetime.now().strftime('%d-%m-%Y')

    naam      = order_data.get('naam') or '–'
    klant_id  = order_data.get('klant_id') or '–'
    email     = order_data.get('email') or '–'
    telefoon  = order_data.get('telefoon') or '–'
    adres     = order_data.get('adres') or '–'
    postcode  = order_data.get('postcode') or ''
    stad      = order_data.get('stad') or ''
    land      = order_data.get('land') or ''

    elements += [
        p('FACTUUR', font='Helvetica-Bold', size=22, color=BLACK),
        Spacer(1, 6),
        p(f'Bestelnummer: <b>#{bestelnummer}</b>', size=BASE, color=DARK_GREY),
        p(f'Datum: {datum}', size=BASE, color=DARK_GREY),
        Spacer(1, 12),
    ]

    left_col = [
        p('<b>KLANTGEGEVENS</b>', font='Helvetica-Bold', size=BASE, color=BLACK),
        Spacer(1, 6),
        p(f'Naam: {naam}', size=BASE),
        p(f'Klant-ID: {klant_id}', size=BASE),
        p(f'Telefoon: {telefoon}', size=BASE),
        p(f'E-mail: {email}', size=BASE),
    ]
    right_col = [
        p('<b>AFLEVERADRES</b>', font='Helvetica-Bold', size=BASE, color=BLACK),
        Spacer(1, 6),
        p(adres, size=BASE),
        p(f'{postcode} {stad}'.strip(), size=BASE),
        p(land, size=BASE),
    ]
    info_tbl = Table([[left_col, right_col]], colWidths=[cw * 0.5, cw * 0.5])
    info_tbl.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    elements += [info_tbl, Spacer(1, 16)]

    # 4. PRODUCTS TABLE
    items  = order_data.get('items', [])
    t_head = [['Product', 'Maat', 'Aantal', 'Stukprijs', 'Totaal']]
    t_rows = []
    for item in items:
        pnaam = item.get('product_naam') or item.get('naam') or '–'
        if item.get('is_on_demand'):
            pnaam += ' (op aanvraag)'
        qty   = int(item.get('aantal', 1))
        price = float(item.get('prijs_per_stuk') or item.get('prijs') or 0)
        t_rows.append([pnaam, str(item.get('maat') or '–'), str(qty), fmt_eur(price), fmt_eur(price * qty)])

    col_w = [cw * r for r in [0.38, 0.12, 0.10, 0.20, 0.20]]
    pt = Table(t_head + t_rows, colWidths=col_w)
    ts = [
        ('BACKGROUND',   (0, 0), (-1, 0),  LIGHT_GREY),
        ('TEXTCOLOR',    (0, 0), (-1, 0),  BLACK),
        ('FONTNAME',     (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTNAME',     (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE',     (0, 0), (-1, -1), BASE),
        ('TEXTCOLOR',    (0, 1), (-1, -1), DARK_GREY),
        ('GRID',         (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
        ('TOPPADDING',   (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 6),
        ('LEFTPADDING',  (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('ALIGN',        (2, 0), (-1, -1), 'RIGHT'),
        ('LINEBELOW',    (0, -1), (-1, -1), 1, DARK_GREY),
    ]
    for i in range(1, len(t_rows) + 1):
        if i % 2 == 0:
            ts.append(('BACKGROUND', (0, i), (-1, i), ALT_ROW))
    pt.setStyle(TableStyle(ts))
    elements += [pt, Spacer(1, 14)]

    # 5. COST SUMMARY
    subtotaal      = float(order_data.get('subtotaal', 0))
    verzendkosten  = float(order_data.get('verzendkosten', 0))
    kortingsbedrag = float(order_data.get('kortingsbedrag', 0))
    totaalbedrag   = float(order_data.get('totaalbedrag', 0))
    verzendmethode = order_data.get('verzendmethode') or 'Verzending'

    sum_rows = [
        ['', p('Subtotaal:', align=TA_RIGHT, size=BASE),
             p(fmt_eur(subtotaal), align=TA_RIGHT, size=BASE)],
        ['', p(f'Verzendkosten ({verzendmethode}):', align=TA_RIGHT, size=BASE),
             p(fmt_eur(verzendkosten), align=TA_RIGHT, size=BASE)],
    ]
    if kortingsbedrag > 0:
        actiecode = order_data.get('actiecode') or 'Korting'
        sum_rows.append(['', p(f'Korting ({actiecode}):', align=TA_RIGHT, size=BASE),
                             p(f'–{fmt_eur(kortingsbedrag)}', align=TA_RIGHT, size=BASE)])
    sum_rows.append(['', p('─────────────────', align=TA_RIGHT, color=colors.HexColor('#cccccc'), size=BASE), ''])
    sum_rows.append([
        '',
        p('<b>TOTAAL:</b>', font='Helvetica-Bold', size=13, color=BLACK, align=TA_RIGHT),
        p(f'<b>{fmt_eur(totaalbedrag)}</b>', font='Helvetica-Bold', size=13, color=BLACK, align=TA_RIGHT),
    ])
    st = Table(sum_rows, colWidths=[cw * 0.40, cw * 0.40, cw * 0.20])
    st.setStyle(TableStyle([
        ('ALIGN',         (0, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME',      (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE',      (0, 0), (-1, -1), BASE),
        ('TEXTCOLOR',     (0, 0), (-1, -1), DARK_GREY),
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements += [st, Spacer(1, 18)]

    # 6. PAYMENT SECTION
    settings    = get_settings()
    paypal_user = settings.get('paypal_username') or 'shoppybrand'
    ing_link    = settings.get('ing_payment_link') or ''
    totaal_str  = f'{totaalbedrag:.2f}'.replace(',', '.')
    paypal_link = f'paypal.me/{paypal_user}/{totaal_str}'

    elements.append(p('<b>Betaling</b>', font='Helvetica-Bold', size=12, color=BLACK))
    elements += [HRFlowable(width='100%', thickness=1.5, color=GOLD), Spacer(1, 4)]
    elements.append(p('<b>Betaal binnen 24 uur!</b>', font='Helvetica-Bold', size=BASE, color=BLACK))
    def plink(display, href):
        return Paragraph(
            f'<a href="{href}" color="#0000CC"><u>{display}</u></a>',
            ParagraphStyle('lnk', fontName='Helvetica', fontSize=BASE,
                           leading=BASE * 1.45, textColor=BLUE),
        )

    elements += [Spacer(1, 10)]
    elements += [
        p('<b>PayPal:</b>', font='Helvetica-Bold', size=BASE, color=BLACK),
        p('Betaal eenvoudig via de link', size=BASE, color=RED),
        plink(paypal_link, f'https://{paypal_link}'),
        Spacer(1, 8),
    ]
    if ing_link:
        ing_href = ing_link if ing_link.startswith('http') else f'https://{ing_link}'
        elements += [
            p('<b>iDEAL (ING):</b>', font='Helvetica-Bold', size=BASE, color=BLACK),
            p('Betaal eenvoudig via de link', size=BASE, color=RED),
            plink(ing_link, ing_href),
            p(f'Bedrag: {fmt_eur(totaalbedrag)}', size=BASE),
        ]

    # 7. FOOTER
    elements += [
        Spacer(1, 22),
        HRFlowable(width='100%', thickness=0.5, color=LIGHT_GREY),
        Spacer(1, 8),
        p('Bedankt voor je bestelling bij ShoppyBrand!', color=DARK_GREY, align=TA_CENTER),
    ]

    doc.build(elements)

    if output is None:
        return dest          # caller gets the file path
    output.seek(0)
    return output            # caller gets the BytesIO (already seeked to start)


# ── EMAIL ─────────────────────────────────────────────────────────────────────

def send_invoice_email(order_data, pdf_source):
    """Send the order-confirmation email with PDF invoice to the customer."""
    customer_email = order_data.get('email', '').strip()
    if not customer_email:
        raise ValueError('Geen e-mailadres voor klant')

    bestelnummer   = order_data['bestelnummer']
    naam           = escape(order_data.get('naam') or 'Klant')
    totaal         = f"{float(order_data['totaalbedrag']):.2f}".replace('.', ',')
    verzendmethode = escape(order_data.get('verzendmethode') or '–')

    html_body = f'''<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; color: #1a1a1a; max-width: 500px; margin: 0 auto;">
  <h2 style="color: #c9a84c;">Bedankt voor je bestelling!</h2>
  <p>Hallo {naam},</p>
  <p>Je bestelling bij ShoppyBrand is ontvangen. In de bijlage vind je je factuur met de betaalgegevens.</p>
  <table style="width:100%; border-collapse:collapse; margin: 20px 0;">
    <tr><td style="padding:8px; color:#666;">Bestelnummer</td><td style="padding:8px;"><strong>#{bestelnummer}</strong></td></tr>
    <tr style="background:#f9f9f9;"><td style="padding:8px; color:#666;">Totaalbedrag</td><td style="padding:8px;"><strong>€{totaal}</strong></td></tr>
    <tr><td style="padding:8px; color:#666;">Verzending</td><td style="padding:8px;">{verzendmethode}</td></tr>
  </table>
  <p>Betaal via de links in de bijlage. Je bestelling wordt verwerkt zodra de betaling is ontvangen.</p>
  <p>Vragen? Stuur ons een DM op Instagram: <strong>@shoppybrand</strong></p>
  <br>
  <p>Groetjes,<br><strong>ShoppyBrand</strong></p>
</body>
</html>'''

    _send_email_with_attachment(
        to_addr=customer_email,
        subject=f'Bevestiging bestelling #{bestelnummer} – ShoppyBrand',
        html_body=html_body,
        pdf_bytes=_resolve_pdf_bytes(pdf_source),
        bestelnummer=bestelnummer,
    )


def send_owner_notification(order_data):
    gmail_user = GMAIL_USER
    gmail_pwd  = GMAIL_PASSWORD
    if not gmail_user or not gmail_pwd:
        raise ValueError(
            'E-mail credentials ontbreken. '
            'Stel GMAIL_USER en GMAIL_PASSWORD in als omgevingsvariabelen.'
        )

    bestelnummer   = order_data['bestelnummer']
    naam           = order_data.get('naam') or '–'
    instagram      = order_data.get('instagram') or '–'
    totaal         = f"{float(order_data['totaalbedrag']):.2f}".replace('.', ',')
    verzendmethode = order_data.get('verzendmethode') or '–'
    aantal_items   = len(order_data.get('items', []))

    body = (
        f'Nieuwe bestelling ontvangen!\n\n'
        f'Bestelnummer: #{bestelnummer}\n'
        f'Klant: {naam} ({instagram})\n'
        f'Totaal: €{totaal}\n'
        f'Verzending: {verzendmethode}\n'
        f'Producten: {aantal_items} artikel(en)\n\n'
        f'Ga naar het admin panel: {BASE_URL}/admin'
    )

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From']    = f'ShoppyBrand <{gmail_user}>'
    msg['To']      = gmail_user
    msg['Subject'] = f'\U0001f6d2 Nieuwe bestelling #{bestelnummer} – ShoppyBrand'

    with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_user, gmail_pwd)
        server.sendmail(gmail_user, gmail_user, msg.as_bytes())


def send_reminder_email(order_data, pdf_source):
    """Send a payment-reminder email to the customer with the invoice attached."""
    customer_email = order_data.get('email', '').strip()
    if not customer_email:
        raise ValueError('Geen e-mailadres voor klant')

    bestelnummer = order_data['bestelnummer']
    naam         = escape(order_data.get('naam') or 'Klant')
    totaal       = f"{float(order_data['totaalbedrag']):.2f}".replace('.', ',')

    html_body = f'''<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; color: #1a1a1a; max-width: 500px; margin: 0 auto;">
  <h2 style="color: #c9a84c;">Herinnering: Je bestelling bij ShoppyBrand</h2>
  <p>Hallo {naam},</p>
  <p>Je hebt recentelijk een bestelling geplaatst bij ShoppyBrand,
     maar we hebben je betaling nog niet ontvangen.</p>
  <p>In de bijlage vind je de factuur nogmaals met alle betaalgegevens.</p>
  <table style="width:100%; border-collapse:collapse; margin: 20px 0;">
    <tr>
      <td style="padding:8px; color:#666;">Bestelnummer</td>
      <td style="padding:8px;"><strong>#{bestelnummer}</strong></td>
    </tr>
    <tr style="background:#f9f9f9;">
      <td style="padding:8px; color:#666;">Openstaand bedrag</td>
      <td style="padding:8px;"><strong>€{totaal}</strong></td>
    </tr>
  </table>
  <p>Betaal graag zo snel mogelijk via de links in de bijlage zodat we je bestelling
     kunnen verwerken.</p>
  <p><em>Heb je al betaald? Dan kun je deze herinnering negeren.</em></p>
  <p>Vragen? Stuur ons een DM op Instagram: <strong>@Shoppybrand_</strong></p>
  <br>
  <p>Met vriendelijke groet,<br><strong>ShoppyBrand</strong></p>
</body>
</html>'''

    _send_email_with_attachment(
        to_addr=customer_email,
        subject=f'Herinnering: Je bestelling #{bestelnummer} bij ShoppyBrand',
        html_body=html_body,
        pdf_bytes=_resolve_pdf_bytes(pdf_source),
        bestelnummer=bestelnummer,
    )


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS products (
            sku_key TEXT PRIMARY KEY,
            item_id TEXT,
            naam TEXT,
            type TEXT,
            maat TEXT,
            voorraad INTEGER,
            aankoop_prijs REAL,
            verkoop_prijs REAL,
            foto TEXT DEFAULT NULL,
            created_at TEXT,
            updated_at TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS on_demand_products (
            sku_key TEXT PRIMARY KEY,
            item_id TEXT,
            naam TEXT,
            type TEXT,
            maat TEXT,
            demand_prijs REAL,
            verkoop_prijs REAL,
            levertijd TEXT DEFAULT '10-12 werkdagen',
            created_at TEXT,
            foto TEXT DEFAULT NULL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS customers (
            klant_id TEXT PRIMARY KEY,
            naam TEXT,
            instagram TEXT UNIQUE NOT NULL,
            email TEXT,
            telefoon TEXT,
            adres TEXT,
            postcode TEXT,
            stad TEXT,
            land TEXT,
            totaal_uitgegeven REAL DEFAULT 0,
            aantal_bestellingen INTEGER DEFAULT 0,
            laatste_bestelling TEXT,
            created_at TEXT
        )
    ''')

    # Sequence for bestelnummer — never reuses numbers after deletion
    c.execute('CREATE SEQUENCE IF NOT EXISTS bestelnummer_seq START 10001')

    c.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            bestelnummer INTEGER PRIMARY KEY DEFAULT nextval('bestelnummer_seq'),
            klant_id TEXT,
            besteldatum TEXT,
            verzendmethode TEXT,
            verzendkosten REAL,
            actiecode TEXT,
            kortingsbedrag REAL DEFAULT 0,
            subtotaal REAL,
            totaalbedrag REAL,
            bestellingstatus TEXT DEFAULT 'Ontvangen',
            betaalstatus TEXT DEFAULT 'Onbetaald',
            opmerking TEXT,
            created_at TEXT,
            updated_at TEXT,
            reminder_sent BOOLEAN DEFAULT FALSE,
            order_token UUID DEFAULT gen_random_uuid(),
            verkoopplatform VARCHAR(50),
            betaaldatum TIMESTAMP,
            betaalmethode VARCHAR(50),
            track_trace VARCHAR(100)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            bestelnummer INTEGER,
            sku_key TEXT,
            product_naam TEXT,
            maat TEXT,
            aantal INTEGER,
            prijs_per_stuk REAL,
            is_on_demand INTEGER DEFAULT 0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS discount_codes (
            code TEXT PRIMARY KEY,
            type TEXT,
            waarde REAL,
            minimum_bestelbedrag REAL DEFAULT 0,
            start_datum TEXT,
            eind_datum TEXT,
            eenmalig INTEGER DEFAULT 0,
            actief INTEGER DEFAULT 1,
            max_gebruik INTEGER DEFAULT 0,
            huidige_gebruik INTEGER DEFAULT 0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS discount_usage (
            id SERIAL PRIMARY KEY,
            code TEXT NOT NULL,
            klant_id TEXT NOT NULL,
            used_at TEXT,
            UNIQUE(code, klant_id)
        )
    ''')

    now = datetime.now().isoformat()
    defaults = [
        ('gratis_verzending_minimum', '200', now),
        ('dhl_kosten', '4.50', now),
        ('postnl_kosten', '5.50', now),
        ('paypal_username', 'shoppybrand', now),
        ('ing_payment_link', '', now),
        ('low_stock_alert', '1', now),
    ]
    for key, value, ts in defaults:
        c.execute(
            'INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, %s) '
            'ON CONFLICT (key) DO NOTHING',
            (key, value, ts)
        )

    conn.commit()
    conn.close()


def migrate_db():
    """Apply idempotent schema additions on every startup."""
    conn = get_db()
    try:
        conn.execute(
            'ALTER TABLE orders ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE'
        )
        # SEC-07: opaque token used in the success URL instead of the sequential integer PK
        conn.execute(
            'ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_token UUID DEFAULT gen_random_uuid()'
        )
        # Bestelnummer sequence — ensures numbers are never reused after deletion
        conn.execute('CREATE SEQUENCE IF NOT EXISTS bestelnummer_seq START 10001')
        # Always advance the sequence to MAX(bestelnummer)+1 so it continues
        # from the highest existing order and never produces a number already used.
        # GREATEST(..., 10000) ensures the floor is 10001 on an empty table.
        # is_called=false means the next nextval() returns exactly this value.
        conn.execute(
            "SELECT setval('bestelnummer_seq', "
            "GREATEST((SELECT MAX(bestelnummer) FROM orders WHERE bestelnummer < 20000), 10000) + 1, false)"
        )
        # Sequence used for collision-free klant_id generation (STB-04)
        conn.execute('CREATE SEQUENCE IF NOT EXISTS klant_id_seq START 1')
        # New order fields
        conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS verkoopplatform VARCHAR(50)')
        conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS betaaldatum TIMESTAMP')
        conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS betaalmethode VARCHAR(50)')
        conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS track_trace VARCHAR(100)')
        # Indexes for frequently-queried columns (PERF-04)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_orders_klant_id       ON orders(klant_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_orders_betaalstatus   ON orders(betaalstatus)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_orders_besteldatum    ON orders(besteldatum)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_order_items_bestelnummer ON order_items(bestelnummer)')
        conn.commit()
        logging.info('migrate_db: schema up to date')
    except Exception:
        conn.rollback()
        logging.exception('migrate_db failed')
    finally:
        conn.close()


# ── ADMIN AUTH ────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            if request.path.startswith('/admin/api/'):
                return jsonify({'error': 'Niet ingelogd'}), 401
            return redirect('/admin')
        return f(*args, **kwargs)
    return decorated


# ── CUSTOMER ROUTES ───────────────────────────────────────────────────────────

@app.route('/')
def order():
    settings = get_settings()
    return render_template('order.html', settings=settings)


@app.route('/api/products')
def api_products():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT sku_key, item_id, naam, type, maat, voorraad, verkoop_prijs, foto "
            "FROM products ORDER BY naam, "
            "CASE WHEN maat ~ '^[0-9]+([.][0-9]+)?$' THEN maat::float ELSE 0::float END, maat"
        ).fetchall()
    finally:
        conn.close()

    groups = _group_products(
        rows,
        group_keys=['naam', 'type', 'verkoop_prijs', 'foto'],
        size_keys=['sku_key', 'maat', 'voorraad'],
    )
    resp = jsonify(list(groups.values()))
    resp.headers['Cache-Control'] = 'public, max-age=30'
    resp.headers['ETag'] = f'"{_products_version}"'
    return resp


@app.route('/api/on-demand')
def api_on_demand():
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT sku_key, item_id, naam, type, maat, demand_prijs, verkoop_prijs, levertijd, foto '
            'FROM on_demand_products ORDER BY naam, maat'
        ).fetchall()
    finally:
        conn.close()
    groups = _group_products(
        rows,
        group_keys=['naam', 'type', 'demand_prijs', 'verkoop_prijs', 'levertijd', 'foto'],
        size_keys=['sku_key', 'maat'],
        update_foto=True,
    )
    resp = jsonify(list(groups.values()))
    resp.headers['Cache-Control'] = 'public, max-age=30'
    resp.headers['ETag'] = f'"{_products_version}"'
    return resp


@app.route('/api/customer')
def api_customer():
    instagram = request.args.get('instagram', '').strip()
    if not instagram:
        return jsonify(None)
    if not instagram.startswith('@'):
        instagram = '@' + instagram
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT naam, telefoon, adres, postcode, stad, land '
            'FROM customers WHERE instagram = %s',
            (instagram,)
        ).fetchone()
    finally:
        conn.close()
    return jsonify(dict(row) if row else None)


@app.route('/api/discount')
def api_discount():
    code      = request.args.get('code', '').strip().upper()
    instagram = request.args.get('instagram', '').strip()
    try:
        subtotal = float(request.args.get('subtotal', 0))
    except (ValueError, TypeError):
        subtotal = 0.0

    if not code:
        return jsonify({'valid': False, 'bericht': 'Voer een code in'})

    conn = get_db()
    try:
        d = conn.execute('SELECT * FROM discount_codes WHERE code = %s', (code,)).fetchone()

        if not d:
            return jsonify({'valid': False, 'bericht': 'Ongeldige kortingscode'})
        if not d['actief']:
            return jsonify({'valid': False, 'bericht': 'Deze kortingscode is niet meer geldig'})
        if d['eind_datum']:
            try:
                if datetime.fromisoformat(d['eind_datum']).date() < date.today():
                    return jsonify({'valid': False, 'bericht': 'Deze kortingscode is verlopen'})
            except ValueError:
                pass
        if d['minimum_bestelbedrag'] and subtotal < float(d['minimum_bestelbedrag']):
            return jsonify({'valid': False, 'bericht': f'Minimum bestelbedrag {float(d["minimum_bestelbedrag"]):.2f} vereist'})

        # Per-klant eenmalig check
        if d['eenmalig'] and instagram:
            ig = instagram if instagram.startswith('@') else '@' + instagram
            klant = conn.execute(
                'SELECT klant_id FROM customers WHERE instagram = %s', (ig,)
            ).fetchone()
            if klant:
                used = conn.execute(
                    'SELECT 1 FROM discount_usage WHERE code = %s AND klant_id = %s',
                    (code, klant['klant_id'])
                ).fetchone()
                if used:
                    return jsonify({'valid': False, 'bericht': 'Je hebt deze code al gebruikt'})
    finally:
        conn.close()

    if d['type'] == 'PERCENTAGE':
        kortingsbedrag = round(subtotal * float(d['waarde']) / 100, 2)
        bericht = f'{float(d["waarde"]):.0f}% korting – je bespaart € {kortingsbedrag:.2f}!'
    elif d['type'] == 'VAST_BEDRAG':
        kortingsbedrag = min(float(d['waarde']), subtotal)
        bericht = f'€ {kortingsbedrag:.2f} korting toegepast!'
    elif d['type'] == 'GRATIS_VERZENDING':
        kortingsbedrag = 0
        bericht = 'Gratis verzending toegepast!'
    else:
        kortingsbedrag = 0
        bericht = 'Korting toegepast'

    return jsonify({
        'valid': True,
        'type': d['type'],
        'waarde': float(d['waarde']),
        'kortingsbedrag': kortingsbedrag,
        'bericht': bericht,
    })


@app.route('/submit-order', methods=['POST'])
@limiter.limit('5 per hour', key_func=_instagram_rate_key)
def submit_order():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Geen data ontvangen'}), 400

    for field in ['naam', 'instagram', 'email', 'adres', 'postcode', 'stad', 'land', 'verzendmethode', 'items']:
        if not data.get(field):
            return jsonify({'success': False, 'error': f'Veld {field} is verplicht'}), 400
    if not data['items']:
        return jsonify({'success': False, 'error': 'Geen producten geselecteerd'}), 400

    # SEC-08: e-mail format validation
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', data['email'].strip()):
        return jsonify({'success': False, 'error': 'Ongeldig e-mailadres'}), 400

    # SEC-09: field length limits
    _FIELD_LIMITS = {'naam': 120, 'adres': 200, 'opmerking': 1000, 'postcode': 20, 'stad': 80}
    for field, max_len in _FIELD_LIMITS.items():
        if data.get(field) and len(str(data[field])) > max_len:
            return jsonify({'success': False,
                            'error': f'{field.capitalize()} is te lang (maximaal {max_len} tekens)'}), 400

    conn = get_db()
    now = datetime.now().isoformat()

    try:
        instagram = data['instagram'].strip()
        if not instagram.startswith('@'):
            instagram = '@' + instagram

        existing = conn.execute(
            'SELECT klant_id FROM customers WHERE instagram = %s', (instagram,)
        ).fetchone()
        if existing:
            klant_id = existing['klant_id']
        else:
            # Sequence guarantees uniqueness under concurrent orders (STB-04)
            next_num = conn.execute("SELECT nextval('klant_id_seq')").fetchone()[0]
            klant_id = f'KL{str(next_num).zfill(4)}'
            conn.execute('''
                INSERT INTO customers
                (klant_id, naam, instagram, email, telefoon, adres, postcode, stad, land,
                 totaal_uitgegeven, aantal_bestellingen, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, %s)
            ''', (klant_id, data['naam'], instagram, data.get('email', ''),
                  data.get('telefoon', ''), data['adres'], data['postcode'],
                  data['stad'], data['land'], now))

        settings_rows = conn.execute('SELECT key, value FROM settings').fetchall()
        settings = {r['key']: r['value'] for r in settings_rows}

        subtotaal = round(sum(float(i['prijs']) * int(i['aantal']) for i in data['items']), 2)

        gratis_min = float(settings.get('gratis_verzending_minimum', 200))
        if subtotaal >= gratis_min:
            verzendmethode = 'Gratis'
            verzendkosten = 0.0
        else:
            verzendmethode = data['verzendmethode']
            verzendkosten = float(settings.get(
                'dhl_kosten' if verzendmethode == 'DHL' else 'postnl_kosten', 4.50
            ))

        kortingsbedrag = 0.0
        actiecode = (data.get('actiecode') or '').strip().upper() or None
        discount = None

        if actiecode:
            discount = conn.execute(
                'SELECT * FROM discount_codes WHERE code = %s AND actief = 1', (actiecode,)
            ).fetchone()
            if discount:
                if discount['minimum_bestelbedrag'] and subtotaal < float(discount['minimum_bestelbedrag']):
                    return jsonify({'success': False, 'error': 'Minimum bestelbedrag niet bereikt'}), 400
                if discount['eind_datum']:
                    try:
                        if datetime.fromisoformat(discount['eind_datum']).date() < date.today():
                            return jsonify({'success': False, 'error': 'Kortingscode is verlopen'}), 400
                    except ValueError:
                        pass
                if discount['type'] == 'PERCENTAGE':
                    kortingsbedrag = round(subtotaal * float(discount['waarde']) / 100, 2)
                elif discount['type'] == 'VAST_BEDRAG':
                    kortingsbedrag = min(float(discount['waarde']), subtotaal)
                elif discount['type'] == 'GRATIS_VERZENDING':
                    kortingsbedrag = verzendkosten

        totaalbedrag = round(subtotaal + verzendkosten - kortingsbedrag, 2)

        # ── Stock check with row-level lock (UXF-01) ──────────────────────────
        for item in data['items']:
            if int(item.get('is_on_demand', 0)):
                continue
            stock_row = conn.execute(
                'SELECT voorraad FROM products WHERE sku_key = %s FOR UPDATE',
                (item['sku_key'],)
            ).fetchone()
            if not stock_row or stock_row['voorraad'] < int(item['aantal']):
                return jsonify({
                    'success': False,
                    'error': f"Sorry, {item['naam']} maat {item.get('maat', '')} is niet meer op voorraad."
                }), 400

        cur = conn.execute('''
            INSERT INTO orders
            (bestelnummer, klant_id, besteldatum, verzendmethode, verzendkosten, actiecode,
             kortingsbedrag, subtotaal, totaalbedrag, bestellingstatus, betaalstatus,
             opmerking, created_at, updated_at)
            VALUES (nextval('bestelnummer_seq'), %s, %s, %s, %s, %s, %s, %s, %s,
                    'Ontvangen', 'Onbetaald', %s, %s, %s)
            RETURNING bestelnummer, order_token
        ''', (klant_id, now, verzendmethode, verzendkosten, actiecode,
              kortingsbedrag, subtotaal, totaalbedrag, data.get('opmerking') or None, now, now))
        row = cur.fetchone()
        bestelnummer = row['bestelnummer']
        order_token  = str(row['order_token'])

        for item in data['items']:
            is_od = int(item.get('is_on_demand', 0))
            conn.execute('''
                INSERT INTO order_items
                (bestelnummer, sku_key, product_naam, maat, aantal, prijs_per_stuk, is_on_demand)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (bestelnummer, item['sku_key'], item['naam'], item['maat'],
                  int(item['aantal']), float(item['prijs']), is_od))
            if not is_od:
                conn.execute(
                    'UPDATE products SET voorraad = GREATEST(0, voorraad - %s), updated_at = %s WHERE sku_key = %s',
                    (int(item['aantal']), now, item['sku_key'])
                )

        conn.execute('''
            UPDATE customers SET
                naam = %s, email = %s, telefoon = %s, adres = %s, postcode = %s, stad = %s, land = %s,
                totaal_uitgegeven = totaal_uitgegeven + %s,
                aantal_bestellingen = aantal_bestellingen + 1,
                laatste_bestelling = %s
            WHERE klant_id = %s
        ''', (data['naam'], data.get('email', ''), data.get('telefoon', ''),
              data['adres'], data['postcode'], data['stad'], data['land'],
              totaalbedrag, now, klant_id))

        if discount:
            conn.execute(
                'UPDATE discount_codes SET huidige_gebruik = huidige_gebruik + 1 WHERE code = %s',
                (actiecode,)
            )
            if discount['eenmalig']:
                try:
                    conn.execute(
                        'INSERT INTO discount_usage (code, klant_id, used_at) VALUES (%s, %s, %s) '
                        'ON CONFLICT (code, klant_id) DO NOTHING',
                        (actiecode, klant_id, now)
                    )
                except Exception:
                    pass

        conn.commit()

        order_data = {
            'bestelnummer':  bestelnummer,
            'besteldatum':   now,
            'klant_id':      klant_id,
            'naam':          data['naam'],
            'instagram':     instagram,
            'email':         data.get('email', ''),
            'telefoon':      data.get('telefoon', ''),
            'adres':         data['adres'],
            'postcode':      data['postcode'],
            'stad':          data['stad'],
            'land':          data['land'],
            'verzendmethode':verzendmethode,
            'verzendkosten': verzendkosten,
            'subtotaal':     subtotaal,
            'kortingsbedrag':kortingsbedrag,
            'actiecode':     actiecode,
            'totaalbedrag':  totaalbedrag,
            'items': [
                {
                    'product_naam':  item['naam'],
                    'maat':          item.get('maat', ''),
                    'aantal':        int(item['aantal']),
                    'prijs_per_stuk':float(item['prijs']),
                    'is_on_demand':  int(item.get('is_on_demand', 0)),
                    'sku_key':       item['sku_key'],
                }
                for item in data['items']
            ],
        }
        # ── Generate PDF in memory (no filesystem dependency) ────────────
        pdf_buf = None
        try:
            pdf_buf = generate_invoice(order_data, output=io.BytesIO())
            logging.info('PDF generated in memory for order %s', bestelnummer)
        except Exception:
            logging.exception('PDF generation failed for order %s', bestelnummer)

        # ── Send invoice email to customer ────────────────────────────────
        email_sent = False
        if pdf_buf is not None:
            try:
                send_invoice_email(order_data, pdf_buf)
                email_sent = True
                logging.info('Invoice email sent to %s for order %s',
                             order_data.get('email'), bestelnummer)
            except Exception:
                logging.exception('Invoice email failed for order %s', bestelnummer)
        else:
            logging.error('Skipping invoice email for order %s: PDF not generated', bestelnummer)
        session['email_sent']  = email_sent
        session['order_token'] = order_token   # used by the success redirect

        # ── Cache PDF to disk for admin panel (best-effort) ───────────────
        try:
            os.makedirs(INVOICES_DIR, exist_ok=True)
            disk_path = os.path.join(INVOICES_DIR, f'factuur_{bestelnummer}.pdf')
            if pdf_buf is not None:
                pdf_buf.seek(0)
                with open(disk_path, 'wb') as fh:
                    fh.write(pdf_buf.read())
            else:
                generate_invoice(order_data)   # fallback: try writing to disk directly
        except Exception:
            logging.warning('Could not cache invoice PDF to disk for order %s', bestelnummer)

        # ── Owner notification ────────────────────────────────────────────
        try:
            send_owner_notification(order_data)
        except Exception:
            logging.exception('Owner notification failed for order %s', bestelnummer)

        return jsonify({'success': True, 'bestelnummer': bestelnummer, 'order_token': order_token})

    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/success/<order_token>')
def success(order_token):
    email_sent = session.pop('email_sent', True)  # default True avoids false warnings on direct nav
    conn = get_db()
    try:
        order_row = conn.execute(
            'SELECT * FROM orders WHERE order_token = %s', (order_token,)
        ).fetchone()
    finally:
        conn.close()
    if not order_row:
        return redirect('/')
    bestelnummer = order_row['bestelnummer']
    return render_template('success.html', bestelnummer=bestelnummer,
                           order=dict(order_row), email_sent=email_sent)


# ── ADMIN ROUTES ──────────────────────────────────────────────────────────────

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect('/admin')
        return render_template('admin.html', show_login=True, login_error=True)
    if not session.get('admin'):
        return render_template('admin.html', show_login=True, login_error=False)
    return render_template('admin.html', show_login=False, login_error=False)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect('/admin')


@app.route('/admin/api/overview')
@admin_required
def admin_overview():
    conn = get_db()
    today = date.today().isoformat()
    try:
        stats = {
            'today_revenue': conn.execute(
                "SELECT COALESCE(SUM(totaalbedrag), 0) AS val FROM orders "
                "WHERE besteldatum::date = %s AND betaalstatus = 'Betaald'", (today,)
            ).fetchone()['val'],
            'week_revenue': conn.execute(
                "SELECT COALESCE(SUM(totaalbedrag), 0) AS val FROM orders "
                "WHERE DATE_TRUNC('week', besteldatum::timestamp) = DATE_TRUNC('week', NOW()) "
                "AND betaalstatus = 'Betaald'"
            ).fetchone()['val'],
            'month_revenue': conn.execute(
                "SELECT COALESCE(SUM(totaalbedrag), 0) AS val FROM orders "
                "WHERE DATE_TRUNC('month', besteldatum::timestamp) = DATE_TRUNC('month', NOW()) "
                "AND betaalstatus = 'Betaald'"
            ).fetchone()['val'],
            'pending_payment': conn.execute(
                "SELECT COALESCE(SUM(totaalbedrag), 0) AS val FROM orders WHERE betaalstatus = 'Onbetaald'"
            ).fetchone()['val'],
            'low_stock_count': conn.execute(
                "SELECT COUNT(*) AS cnt FROM products WHERE voorraad > 0 AND voorraad <= 1"
            ).fetchone()['cnt'],
            'total_orders': conn.execute(
                "SELECT COUNT(*) AS cnt FROM orders"
            ).fetchone()['cnt'],
        }
        last_orders = conn.execute('''
            SELECT o.bestelnummer, c.naam as klant_naam, c.instagram,
                   o.besteldatum, o.totaalbedrag, o.bestellingstatus, o.betaalstatus
            FROM orders o LEFT JOIN customers c ON o.klant_id=c.klant_id
            ORDER BY o.bestelnummer DESC LIMIT 5
        ''').fetchall()
    finally:
        conn.close()
    return jsonify({
        **{k: round(float(v), 2) for k, v in stats.items()},
        'last_orders': [dict(r) for r in last_orders],
    })


@app.route('/admin/api/orders')
@admin_required
def admin_orders_api():
    conn = get_db()
    try:
        rows = conn.execute('''
            SELECT
                o.bestelnummer, o.klant_id, o.besteldatum, o.verzendmethode,
                o.verzendkosten, o.actiecode, o.kortingsbedrag, o.subtotaal,
                o.totaalbedrag, o.bestellingstatus, o.betaalstatus, o.opmerking,
                o.created_at, o.updated_at, o.reminder_sent,
                o.verkoopplatform, o.betaalmethode, o.track_trace,
                to_char(o.betaaldatum, 'YYYY-MM-DD"T"HH24:MI:SS') AS betaaldatum,
                c.naam  AS klant_naam, c.instagram, c.adres, c.postcode, c.stad, c.land,
                oi.id   AS oi_id, oi.sku_key, oi.product_naam, oi.maat,
                oi.aantal, oi.prijs_per_stuk, oi.is_on_demand
            FROM orders o
            LEFT JOIN customers   c  ON c.klant_id   = o.klant_id
            LEFT JOIN order_items oi ON oi.bestelnummer = o.bestelnummer
            ORDER BY o.bestelnummer DESC, oi.id
        ''').fetchall()
    finally:
        conn.close()

    # Reassemble orders with their items in Python (single query, no N+1)
    orders_map: dict = {}
    order_keys: list = []
    item_fields = {'oi_id', 'sku_key', 'product_naam', 'maat', 'aantal', 'prijs_per_stuk', 'is_on_demand'}
    for r in rows:
        bn = r['bestelnummer']
        if bn not in orders_map:
            order_keys.append(bn)
            orders_map[bn] = {k: v for k, v in r.items() if k not in item_fields}
            orders_map[bn]['items'] = []
        if r['oi_id'] is not None:
            orders_map[bn]['items'].append({
                'id':            r['oi_id'],
                'sku_key':       r['sku_key'],
                'product_naam':  r['product_naam'],
                'maat':          r['maat'],
                'aantal':        r['aantal'],
                'prijs_per_stuk':r['prijs_per_stuk'],
                'is_on_demand':  r['is_on_demand'],
            })
    return jsonify([orders_map[bn] for bn in order_keys])


@app.route('/admin/api/products')
@admin_required
def admin_products_api():
    conn = get_db()
    products = conn.execute(
        "SELECT * FROM products ORDER BY naam, "
        "CASE WHEN maat ~ '^[0-9]+([.][0-9]+)?$' THEN maat::float ELSE 0::float END, maat"
    ).fetchall()
    on_demand = conn.execute(
        'SELECT * FROM on_demand_products ORDER BY naam'
    ).fetchall()
    conn.close()
    return jsonify({
        'products': [dict(p) for p in products],
        'on_demand': [dict(p) for p in on_demand],
    })


@app.route('/admin/api/customers')
@admin_required
def admin_customers_api():
    conn = get_db()
    try:
        customers = conn.execute(
            'SELECT * FROM customers ORDER BY aantal_bestellingen DESC, totaal_uitgegeven DESC'
        ).fetchall()
        # Single query for all orders — reassemble in Python (no N+1)
        all_orders = conn.execute(
            'SELECT klant_id, bestelnummer, besteldatum, totaalbedrag, '
            '       bestellingstatus, betaalstatus '
            'FROM orders ORDER BY bestelnummer DESC'
        ).fetchall()
    finally:
        conn.close()

    from collections import defaultdict
    orders_by_klant: dict = defaultdict(list)
    for o in all_orders:
        orders_by_klant[o['klant_id']].append(dict(o))

    result = []
    for cust in customers:
        c_dict = dict(cust)
        c_dict['orders'] = orders_by_klant.get(cust['klant_id'], [])
        result.append(c_dict)
    return jsonify(result)


@app.route('/admin/api/discounts')
@admin_required
def admin_discounts_api():
    conn = get_db()
    codes = conn.execute('SELECT * FROM discount_codes ORDER BY code').fetchall()
    conn.close()
    return jsonify([dict(c) for c in codes])


@app.route('/admin/api/update-status', methods=['POST'])
@admin_required
def admin_update_status():
    data        = request.get_json()
    field       = data.get('field')
    value       = data.get('value', '')
    bestelnummer = data.get('bestelnummer')
    allowed = {'bestellingstatus', 'betaalstatus', 'verkoopplatform', 'betaalmethode'}
    if field not in allowed:
        return jsonify({'success': False, 'error': 'Ongeldig veld'}), 400
    now  = datetime.now().isoformat()
    conn = get_db()
    if field == 'betaalstatus':
        # Automatically set/clear betaaldatum when payment status changes
        if value == 'Betaald':
            conn.execute(
                'UPDATE orders SET betaalstatus=%s, betaaldatum=NOW(), updated_at=%s '
                'WHERE bestelnummer=%s',
                (value, now, bestelnummer)
            )
        else:
            conn.execute(
                'UPDATE orders SET betaalstatus=%s, betaaldatum=NULL, updated_at=%s '
                'WHERE bestelnummer=%s',
                (value, now, bestelnummer)
            )
    else:
        conn.execute(
            f'UPDATE orders SET {field}=%s, updated_at=%s WHERE bestelnummer=%s',
            (value, now, bestelnummer)
        )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/update-track-trace', methods=['POST'])
@admin_required
def admin_update_track_trace():
    data         = request.get_json()
    bestelnummer = data.get('bestelnummer')
    track_trace  = (data.get('track_trace') or '').strip()[:100]
    if not bestelnummer:
        return jsonify({'success': False, 'error': 'Bestelnummer vereist'}), 400
    conn = get_db()
    conn.execute(
        'UPDATE orders SET track_trace=%s, updated_at=%s WHERE bestelnummer=%s',
        (track_trace or None, datetime.now().isoformat(), bestelnummer)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/update-stock', methods=['POST'])
@admin_required
def admin_update_stock():
    data = request.get_json()
    conn = get_db()
    conn.execute(
        'UPDATE products SET voorraad=%s, updated_at=%s WHERE sku_key=%s',
        (int(data['voorraad']), datetime.now().isoformat(), data['sku_key'])
    )
    conn.commit()
    conn.close()
    _invalidate_products_cache()
    return jsonify({'success': True})


@app.route('/admin/api/add-product', methods=['POST'])
@admin_required
def admin_add_product():
    data = request.get_json()
    for f in ['sku_key', 'item_id', 'naam', 'type', 'maat']:
        if not data.get(f):
            return jsonify({'success': False, 'error': f'Veld {f} is verplicht'}), 400
    now = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO products
            (sku_key, item_id, naam, type, maat, voorraad, aankoop_prijs, verkoop_prijs, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (data['sku_key'], data['item_id'], data['naam'], data['type'], data['maat'],
              int(data.get('voorraad', 0)), float(data.get('aankoop_prijs', 0)),
              float(data.get('verkoop_prijs', 0)), now, now))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': 'SKU bestaat al'}), 400


@app.route('/admin/api/add-discount', methods=['POST'])
@admin_required
def admin_add_discount():
    data = request.get_json()
    if not data.get('code') or not data.get('type'):
        return jsonify({'success': False, 'error': 'Code en type zijn verplicht'}), 400
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO discount_codes
            (code, type, waarde, minimum_bestelbedrag, eind_datum, eenmalig, actief)
            VALUES (%s, %s, %s, %s, %s, %s, 1)
        ''', (data['code'].upper(), data['type'], float(data.get('waarde', 0)),
              float(data.get('minimum_bestelbedrag', 0)),
              data.get('eind_datum') or None,
              1 if data.get('eenmalig') else 0))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': 'Code bestaat al'}), 400


@app.route('/admin/api/toggle-discount', methods=['POST'])
@admin_required
def admin_toggle_discount():
    data = request.get_json()
    conn = get_db()
    conn.execute(
        'UPDATE discount_codes SET actief=CASE WHEN actief=1 THEN 0 ELSE 1 END WHERE code=%s',
        (data['code'],)
    )
    conn.commit()
    row = conn.execute('SELECT actief FROM discount_codes WHERE code=%s', (data['code'],)).fetchone()
    conn.close()
    return jsonify({'success': True, 'actief': row['actief'] if row else 0})


@app.route('/admin/api/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute('SELECT key, value FROM settings').fetchall()
        conn.close()
        return jsonify({r['key']: r['value'] for r in rows})
    data = request.get_json()
    now = datetime.now().isoformat()
    conn = get_db()
    for key, value in data.items():
        conn.execute(
            'INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, %s) '
            'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at',
            (key, str(value), now)
        )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/invoice/<int:bestelnummer>')
@admin_required
def admin_download_invoice(bestelnummer):
    pdf_path = os.path.join(INVOICES_DIR, f'factuur_{bestelnummer}.pdf')
    if not os.path.exists(pdf_path):
        conn = get_db()
        order = conn.execute('''
            SELECT o.*, c.naam, c.instagram, c.email, c.telefoon, c.adres, c.postcode, c.stad, c.land
            FROM orders o LEFT JOIN customers c ON o.klant_id = c.klant_id
            WHERE o.bestelnummer = %s
        ''', (bestelnummer,)).fetchone()
        if not order:
            conn.close()
            return jsonify({'error': 'Bestelling niet gevonden'}), 404
        items = conn.execute(
            'SELECT * FROM order_items WHERE bestelnummer = %s', (bestelnummer,)
        ).fetchall()
        conn.close()
        order_data = dict(order)
        order_data['items'] = [dict(i) for i in items]
        try:
            pdf_path = generate_invoice(order_data)
        except Exception as e:
            return f'Fout bij genereren PDF: {e}', 500
    return send_file(pdf_path, as_attachment=True, download_name=f'factuur_{bestelnummer}.pdf')


@app.route('/admin/api/resend-invoice', methods=['POST'])
@admin_required
def admin_resend_invoice():
    data = request.get_json()
    bestelnummer = data.get('bestelnummer')
    if not bestelnummer:
        return jsonify({'success': False, 'error': 'Bestelnummer vereist'}), 400
    conn = get_db()
    try:
        order = conn.execute('''
            SELECT o.*, c.naam, c.instagram, c.email, c.telefoon, c.adres, c.postcode, c.stad, c.land
            FROM orders o LEFT JOIN customers c ON o.klant_id = c.klant_id
            WHERE o.bestelnummer = %s
        ''', (bestelnummer,)).fetchone()
        if not order:
            return jsonify({'success': False, 'error': 'Bestelling niet gevonden'}), 404
        items = conn.execute(
            'SELECT * FROM order_items WHERE bestelnummer = %s', (bestelnummer,)
        ).fetchall()
    finally:
        conn.close()
    order_data = dict(order)
    order_data['items'] = [dict(i) for i in items]
    try:
        pdf_buf = generate_invoice(order_data, output=io.BytesIO())
        send_invoice_email(order_data, pdf_buf)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/api/update-product', methods=['POST'])
@admin_required
def admin_update_product():
    data = request.get_json()
    sku_key = data.get('sku_key')
    if not sku_key:
        return jsonify({'success': False, 'error': 'SKU is verplicht'}), 400
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        'UPDATE products SET naam=%s, voorraad=%s, aankoop_prijs=%s, verkoop_prijs=%s, updated_at=%s WHERE sku_key=%s',
        (data['naam'], int(data['voorraad']), float(data['aankoop_prijs']),
         float(data['verkoop_prijs']), now, sku_key)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/delete-product', methods=['POST'])
@admin_required
def admin_delete_product():
    data = request.get_json()
    sku_key = data.get('sku_key')
    if not sku_key:
        return jsonify({'success': False, 'error': 'SKU is verplicht'}), 400
    conn = get_db()
    conn.execute('DELETE FROM products WHERE sku_key=%s', (sku_key,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/add-on-demand', methods=['POST'])
@admin_required
def admin_add_on_demand():
    data = request.get_json()
    naam = (data.get('naam') or '').strip()
    maat = (data.get('maat') or '').strip()
    if not naam:
        return jsonify({'success': False, 'error': 'Naam is verplicht'}), 400
    slug = re.sub(r'[^a-z0-9]+', '-', naam.lower()).strip('-')
    custom_sku = (data.get('sku_key') or '').strip()
    sku_key = custom_sku if custom_sku else (f'OD-{slug}-{maat}' if maat else f'OD-{slug}')
    now = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO on_demand_products
            (sku_key, item_id, naam, type, maat, demand_prijs, verkoop_prijs, levertijd, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (sku_key, slug, naam, data.get('type', ''), maat,
              float(data.get('demand_prijs', 0)), float(data.get('verkoop_prijs', 0)),
              data.get('levertijd') or '10-12 werkdagen', now))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'sku_key': sku_key})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': f'SKU "{sku_key}" bestaat al — pas naam of maat aan'}), 400
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/api/update-on-demand', methods=['POST'])
@admin_required
def admin_update_on_demand():
    data     = request.get_json()
    old_sku  = (data.get('old_sku') or data.get('sku_key') or '').strip()
    new_sku  = (data.get('new_sku') or data.get('sku_key') or '').strip()
    naam     = (data.get('naam')    or '').strip()
    type_    = (data.get('type')    or '').strip()
    maat     = (data.get('maat')    or '').strip()
    demand   = float(data.get('demand_prijs')  or 0)
    verkoop  = float(data.get('verkoop_prijs') or 0)
    levertijd = (data.get('levertijd') or '10-12 werkdagen').strip()
    if not old_sku or not new_sku:
        return jsonify({'success': False, 'error': 'SKU is verplicht'}), 400
    conn = get_db()
    if old_sku != new_sku:
        try:
            existing = conn.execute(
                'SELECT foto FROM on_demand_products WHERE sku_key=%s', (old_sku,)
            ).fetchone()
            foto = existing['foto'] if existing else None
            conn.execute('DELETE FROM on_demand_products WHERE sku_key=%s', (old_sku,))
            conn.execute('''
                INSERT INTO on_demand_products (sku_key, naam, type, maat, demand_prijs, verkoop_prijs, levertijd, foto)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ''', (new_sku, naam, type_, maat, demand, verkoop, levertijd, foto))
            conn.commit()
            conn.close()
            return jsonify({'success': True})
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            conn.close()
            return jsonify({'success': False, 'error': f'SKU "{new_sku}" bestaat al'}), 400
    else:
        conn.execute('''
            UPDATE on_demand_products
            SET naam=%s, type=%s, maat=%s, demand_prijs=%s, verkoop_prijs=%s, levertijd=%s
            WHERE sku_key=%s
        ''', (naam, type_, maat, demand, verkoop, levertijd, old_sku))
        conn.commit()
        conn.close()
        return jsonify({'success': True})


@app.route('/admin/api/delete-on-demand', methods=['POST'])
@admin_required
def admin_delete_on_demand():
    data = request.get_json()
    sku_key = data.get('sku_key')
    if not sku_key:
        return jsonify({'success': False, 'error': 'SKU is verplicht'}), 400
    conn = get_db()
    conn.execute('DELETE FROM on_demand_products WHERE sku_key=%s', (sku_key,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/on-demand-grouped')
@admin_required
def admin_on_demand_grouped():
    conn = get_db()
    rows = conn.execute(
        'SELECT sku_key, item_id, naam, type, maat, demand_prijs, verkoop_prijs, levertijd, foto '
        'FROM on_demand_products ORDER BY naam, maat'
    ).fetchall()
    conn.close()
    groups = _group_products(
        rows,
        group_keys=['naam', 'type', 'demand_prijs', 'verkoop_prijs', 'levertijd', 'foto'],
        size_keys=['sku_key', 'maat'],
        update_foto=True,
    )

    def _od_sort(g):
        cat = g['type'] or ''
        idx = _CATEGORY_ORDER.index(cat) if cat in _CATEGORY_ORDER else len(_CATEGORY_ORDER)
        return (idx, (g['naam'] or '').lower())

    return jsonify({'success': True, 'groups': sorted(groups.values(), key=_od_sort)})


@app.route('/admin/api/update-od-group', methods=['POST'])
@admin_required
def admin_update_od_group():
    data      = request.get_json()
    item_id   = (data.get('item_id')   or '').strip()
    naam      = (data.get('naam')      or '').strip()
    type_     = (data.get('type')      or '').strip()
    demand    = data.get('demand_prijs')
    verkoop   = data.get('verkoop_prijs')
    levertijd = (data.get('levertijd') or '10-12 werkdagen').strip()
    if not item_id or not naam:
        return jsonify({'success': False, 'error': 'item_id en naam zijn verplicht'}), 400
    conn = get_db()
    conn.execute(
        'UPDATE on_demand_products SET naam=%s, type=%s, demand_prijs=%s, verkoop_prijs=%s, levertijd=%s '
        'WHERE item_id=%s',
        (naam, type_, demand, verkoop, levertijd, item_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/add-od-size', methods=['POST'])
@admin_required
def admin_add_od_size():
    data    = request.get_json()
    item_id = (data.get('item_id') or '').strip()
    maat    = (data.get('maat')    or '').strip()
    if not item_id or not maat:
        return jsonify({'success': False, 'error': 'item_id en maat zijn verplicht'}), 400
    conn = get_db()
    existing = conn.execute(
        'SELECT naam, type, demand_prijs, verkoop_prijs, levertijd FROM on_demand_products WHERE item_id=%s LIMIT 1',
        (item_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({'success': False, 'error': 'item_id niet gevonden'}), 404
    sku_key = f'{item_id}-{maat}'
    now = datetime.now().isoformat()
    try:
        conn.execute(
            'INSERT INTO on_demand_products (sku_key, item_id, naam, type, maat, demand_prijs, verkoop_prijs, levertijd, created_at) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (sku_key, item_id, existing['naam'], existing['type'], maat,
             existing['demand_prijs'], existing['verkoop_prijs'], existing['levertijd'], now)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'sku_key': sku_key})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': f'Maat "{maat}" bestaat al voor dit product'}), 400


@app.route('/admin/api/add-on-demand-group', methods=['POST'])
@admin_required
def admin_add_on_demand_group():
    data      = request.get_json()
    item_id   = (data.get('item_id')   or '').strip()
    naam      = (data.get('naam')      or '').strip()
    type_     = (data.get('type')      or '').strip()
    demand    = float(data.get('demand_prijs')  or 0)
    verkoop   = float(data.get('verkoop_prijs') or 0)
    levertijd = (data.get('levertijd') or '10-12 werkdagen').strip()
    sizes     = data.get('sizes', [])
    if not item_id or not naam or not sizes:
        return jsonify({'success': False, 'error': 'item_id, naam en minstens 1 maat zijn verplicht'}), 400
    now = datetime.now().isoformat()
    conn = get_db()
    try:
        for s in sizes:
            maat = (s.get('maat') or '').strip()
            if not maat:
                continue
            sku_key = f'{item_id}-{maat}'
            conn.execute(
                'INSERT INTO on_demand_products (sku_key, item_id, naam, type, maat, demand_prijs, verkoop_prijs, levertijd, created_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
                (sku_key, item_id, naam, type_, maat, demand, verkoop, levertijd, now)
            )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except psycopg2.errors.UniqueViolation as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/admin/api/delete-od-group', methods=['POST'])
@admin_required
def admin_delete_od_group():
    data    = request.get_json()
    item_id = (data.get('item_id') or '').strip()
    if not item_id:
        return jsonify({'success': False, 'error': 'item_id is verplicht'}), 400
    conn = get_db()
    conn.execute('DELETE FROM on_demand_products WHERE item_id=%s', (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/upload-photo', methods=['POST'])
@admin_required
def admin_upload_photo():
    item_id = request.form.get('item_id', '').strip()
    sku_key = request.form.get('sku_key', '').strip()
    file    = request.files.get('file')
    if (not item_id and not sku_key) or not file:
        return jsonify({'success': False, 'error': 'item_id of sku_key en bestand zijn verplicht'}), 400
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in {'jpg', 'jpeg', 'png', 'webp'}:
        return jsonify({'success': False, 'error': 'Alleen jpg/png/webp toegestaan'}), 400
    file.seek(0, 2)
    if file.tell() > 2 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'Max bestandsgrootte is 2MB'}), 400
    file.seek(0)
    try:
        if item_id:
            safe_name = item_id.replace('/', '_').replace('\\', '_')
            result = cloudinary.uploader.upload(
                file, public_id=f'products/{safe_name}', overwrite=True, resource_type='image'
            )
            foto_url = result['secure_url']
            conn = get_db()
            conn.execute('UPDATE products SET foto = %s WHERE item_id = %s', (foto_url, item_id))
            conn.commit()
            conn.close()
        else:
            safe_sku = sku_key.replace('/', '_').replace('\\', '_')
            result = cloudinary.uploader.upload(
                file, public_id=f'products/{safe_sku}', overwrite=True, resource_type='image'
            )
            foto_url = result['secure_url']
            conn = get_db()
            conn.execute('UPDATE products SET foto = %s WHERE sku_key = %s', (foto_url, sku_key))
            conn.commit()
            conn.close()
        return jsonify({'success': True, 'foto': foto_url})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/api/upload-on-demand-photo', methods=['POST'])
@admin_required
def admin_upload_on_demand_photo():
    item_id = request.form.get('item_id', '').strip()
    sku_key = request.form.get('sku_key', '').strip()
    file    = request.files.get('file')
    if (not item_id and not sku_key) or not file:
        return jsonify({'success': False, 'error': 'item_id of sku_key en bestand zijn verplicht'}), 400
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in {'jpg', 'jpeg', 'png', 'webp'}:
        return jsonify({'success': False, 'error': 'Alleen jpg/png/webp toegestaan'}), 400
    file.seek(0, 2)
    if file.tell() > 2 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'Max bestandsgrootte is 2MB'}), 400
    file.seek(0)
    try:
        if item_id:
            safe_name = item_id.replace('/', '_').replace('\\', '_')
            result = cloudinary.uploader.upload(
                file, public_id=f'od_products/{safe_name}', overwrite=True, resource_type='image'
            )
            foto_url = result['secure_url']
            conn = get_db()
            conn.execute('UPDATE on_demand_products SET foto = %s WHERE item_id = %s', (foto_url, item_id))
            conn.commit()
            conn.close()
        else:
            safe_sku = sku_key.replace('/', '_').replace('\\', '_')
            result = cloudinary.uploader.upload(
                file, public_id=f'od_products/{safe_sku}', overwrite=True, resource_type='image'
            )
            foto_url = result['secure_url']
            conn = get_db()
            conn.execute('UPDATE on_demand_products SET foto = %s WHERE sku_key = %s', (foto_url, sku_key))
            conn.commit()
            conn.close()
        return jsonify({'success': True, 'foto': foto_url})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/api/update-sku', methods=['POST'])
@admin_required
def admin_update_sku():
    data = request.get_json()
    old_sku = (data.get('old_sku') or '').strip()
    new_sku = (data.get('new_sku') or '').strip()
    if not old_sku or not new_sku:
        return jsonify({'success': False, 'error': 'Beide SKU-waarden zijn verplicht'}), 400
    if old_sku == new_sku:
        return jsonify({'success': True})
    conn = get_db()
    try:
        conn.execute('UPDATE products SET sku_key = %s WHERE sku_key = %s', (new_sku, old_sku))
        conn.execute('UPDATE order_items SET sku_key = %s WHERE sku_key = %s', (new_sku, old_sku))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': f'SKU "{new_sku}" bestaat al'}), 400


@app.route('/admin/api/products-grouped')
@admin_required
def admin_products_grouped():
    conn = get_db()
    rows = conn.execute(
        'SELECT sku_key, item_id, naam, type, maat, voorraad, aankoop_prijs, verkoop_prijs, foto '
        'FROM products ORDER BY item_id, maat'
    ).fetchall()
    conn.close()
    groups = _group_products(
        rows,
        group_keys=['naam', 'type', 'aankoop_prijs', 'verkoop_prijs', 'foto'],
        size_keys=['sku_key', 'maat', 'voorraad'],
    )

    def _sort_key(g):
        cat = g['type'] or ''
        idx = _CATEGORY_ORDER.index(cat) if cat in _CATEGORY_ORDER else len(_CATEGORY_ORDER)
        return (idx, (g['naam'] or '').lower())

    return jsonify({'success': True, 'groups': sorted(groups.values(), key=_sort_key)})


@app.route('/admin/api/update-item-id', methods=['POST'])
@admin_required
def admin_update_item_id():
    data   = request.get_json()
    old_id = (data.get('old_item_id') or '').strip()
    new_id = (data.get('new_item_id') or '').strip()
    if not old_id or not new_id:
        return jsonify({'success': False, 'error': 'Beide item_id waarden zijn verplicht'}), 400
    if old_id == new_id:
        return jsonify({'success': True})
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT sku_key, maat FROM products WHERE item_id=%s', (old_id,)
        ).fetchall()
        for r in rows:
            new_sku = f"{new_id}-{r['maat']}" if r['maat'] else new_id
            conn.execute('UPDATE products SET sku_key=%s, item_id=%s WHERE sku_key=%s',
                         (new_sku, new_id, r['sku_key']))
            conn.execute('UPDATE order_items SET sku_key=%s WHERE sku_key=%s',
                         (new_sku, r['sku_key']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except psycopg2.errors.UniqueViolation as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/admin/api/update-product-group', methods=['POST'])
@admin_required
def admin_update_product_group():
    data = request.get_json()
    item_id = (data.get('item_id') or '').strip()
    naam    = (data.get('naam')    or '').strip()
    type_   = (data.get('type')   or '').strip()
    aankoop = data.get('aankoop_prijs')
    verkoop = data.get('verkoop_prijs')
    if not item_id or not naam:
        return jsonify({'success': False, 'error': 'item_id en naam zijn verplicht'}), 400
    conn = get_db()
    conn.execute(
        'UPDATE products SET naam=%s, type=%s, aankoop_prijs=%s, verkoop_prijs=%s WHERE item_id=%s',
        (naam, type_, aankoop, verkoop, item_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/update-size-stock', methods=['POST'])
@admin_required
def admin_update_size_stock():
    data     = request.get_json()
    sku_key  = (data.get('sku_key') or '').strip()
    voorraad = data.get('voorraad')
    if not sku_key or voorraad is None:
        return jsonify({'success': False, 'error': 'sku_key en voorraad zijn verplicht'}), 400
    conn = get_db()
    conn.execute('UPDATE products SET voorraad=%s WHERE sku_key=%s', (int(voorraad), sku_key))
    conn.commit()
    conn.close()
    _invalidate_products_cache()
    return jsonify({'success': True})


@app.route('/admin/api/add-size', methods=['POST'])
@admin_required
def admin_add_size():
    data     = request.get_json()
    item_id  = (data.get('item_id') or '').strip()
    maat     = (data.get('maat')    or '').strip()
    voorraad = int(data.get('voorraad') or 0)
    if not item_id or not maat:
        return jsonify({'success': False, 'error': 'item_id en maat zijn verplicht'}), 400
    conn = get_db()
    existing = conn.execute(
        'SELECT naam, type, aankoop_prijs, verkoop_prijs FROM products WHERE item_id=%s LIMIT 1',
        (item_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({'success': False, 'error': 'item_id niet gevonden'}), 404
    sku_key = f'{item_id}-{maat}'
    try:
        conn.execute(
            'INSERT INTO products (sku_key, item_id, naam, type, maat, voorraad, aankoop_prijs, verkoop_prijs) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
            (sku_key, item_id, existing['naam'], existing['type'], maat, voorraad,
             existing['aankoop_prijs'], existing['verkoop_prijs'])
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'sku_key': sku_key})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': f'Maat "{maat}" bestaat al voor dit product'}), 400


@app.route('/admin/api/delete-size', methods=['POST'])
@admin_required
def admin_delete_size():
    data    = request.get_json()
    sku_key = (data.get('sku_key') or '').strip()
    if not sku_key:
        return jsonify({'success': False, 'error': 'sku_key is verplicht'}), 400
    conn = get_db()
    conn.execute('DELETE FROM products WHERE sku_key=%s', (sku_key,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/add-product-group', methods=['POST'])
@admin_required
def admin_add_product_group():
    data    = request.get_json()
    item_id = (data.get('item_id') or '').strip()
    naam    = (data.get('naam')    or '').strip()
    type_   = (data.get('type')   or '').strip()
    aankoop = data.get('aankoop_prijs')
    verkoop = data.get('verkoop_prijs')
    sizes   = data.get('sizes', [])
    if not item_id or not naam or not sizes:
        return jsonify({'success': False, 'error': 'item_id, naam en minstens 1 maat zijn verplicht'}), 400
    conn = get_db()
    try:
        for s in sizes:
            maat     = (s.get('maat') or '').strip()
            voorraad = int(s.get('voorraad') or 0)
            if not maat:
                continue
            sku_key = f'{item_id}-{maat}'
            conn.execute(
                'INSERT INTO products (sku_key, item_id, naam, type, maat, voorraad, aankoop_prijs, verkoop_prijs) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (sku_key, item_id, naam, type_, maat, voorraad, aankoop, verkoop)
            )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except psycopg2.errors.UniqueViolation as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 400


def _do_test_email():
    """Shared logic for both test-email routes."""
    diag = {
        'GMAIL_USER_set':     bool(GMAIL_USER),
        'GMAIL_PASSWORD_set': bool(GMAIL_PASSWORD),
        'GMAIL_USER_value':   GMAIL_USER or '(not set)',
        'expected_vars':      ['GMAIL_USER', 'GMAIL_PASSWORD'],
    }
    if not GMAIL_USER or not GMAIL_PASSWORD:
        missing = [v for v, ok in [('GMAIL_USER', GMAIL_USER), ('GMAIL_PASSWORD', GMAIL_PASSWORD)] if not ok]
        return jsonify({
            'success': False,
            'error':   f'Omgevingsvariabelen ontbreken: {", ".join(missing)}',
            'diagnostics': diag,
        })
    try:
        msg = MIMEText('Test e-mail van ShoppyBrand.', 'plain', 'utf-8')
        msg['From']    = f'ShoppyBrand <{GMAIL_USER}>'
        msg['To']      = GMAIL_USER
        msg['Subject'] = 'ShoppyBrand – Test e-mail'
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, GMAIL_USER, msg.as_bytes())
        return jsonify({
            'success': True,
            'message': f'Test e-mail verstuurd naar {GMAIL_USER}',
            'diagnostics': diag,
        })
    except Exception as e:
        logging.exception('Test email failed')
        return jsonify({'success': False, 'error': str(e), 'diagnostics': diag})


@app.route('/admin/api/test-email')
@admin_required
def admin_test_email():
    return _do_test_email()


@app.route('/admin/api/delete-discount', methods=['POST'])
@admin_required
def admin_delete_discount():
    data = request.get_json()
    code = data.get('code')
    if not code:
        return jsonify({'success': False, 'error': 'Code is verplicht'}), 400
    conn = get_db()
    conn.execute('DELETE FROM discount_codes WHERE code = %s', (code,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/delete-order', methods=['POST'])
@admin_required
def admin_delete_order():
    data = request.get_json()
    bestelnummer = data.get('bestelnummer')
    if not bestelnummer:
        return jsonify({'success': False, 'error': 'Bestelnummer vereist'}), 400
    conn = get_db()
    now = datetime.now().isoformat()
    try:
        items = conn.execute(
            'SELECT sku_key, aantal, is_on_demand FROM order_items WHERE bestelnummer = %s',
            (bestelnummer,)
        ).fetchall()
        for item in items:
            if not item['is_on_demand']:
                conn.execute(
                    'UPDATE products SET voorraad = voorraad + %s, updated_at = %s WHERE sku_key = %s',
                    (item['aantal'], now, item['sku_key'])
                )
        order = conn.execute(
            'SELECT klant_id, totaalbedrag FROM orders WHERE bestelnummer = %s',
            (bestelnummer,)
        ).fetchone()
        conn.execute('DELETE FROM order_items WHERE bestelnummer = %s', (bestelnummer,))
        conn.execute('DELETE FROM orders WHERE bestelnummer = %s', (bestelnummer,))
        if order:
            conn.execute('''
                UPDATE customers SET
                    totaal_uitgegeven = GREATEST(0, totaal_uitgegeven - %s),
                    aantal_bestellingen = GREATEST(0, aantal_bestellingen - 1)
                WHERE klant_id = %s
            ''', (order['totaalbedrag'], order['klant_id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500


# ── UNPAID ORDER REMINDERS ────────────────────────────────────────────────────

def check_unpaid_orders():
    """
    Scheduled job: send one reminder email per unpaid order older than 24 h.
    Phase 1: fetch all data and immediately close the DB connection.
    Phase 2: send emails (no DB connection held during slow SMTP calls).
    Phase 3: open a short-lived connection per order to mark reminder_sent.
    """
    logging.info('check_unpaid_orders: starting run')
    try:
        # ── Phase 1: fetch, then release connection ────────────────────────
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        conn = get_db()
        try:
            order_rows = conn.execute('''
                SELECT o.*, c.naam, c.email, c.instagram
                FROM orders o
                LEFT JOIN customers c ON o.klant_id = c.klant_id
                WHERE o.betaalstatus = 'Onbetaald'
                  AND o.reminder_sent = FALSE
                  AND o.besteldatum < %s
            ''', (cutoff,)).fetchall()
            orders_data = []
            for row in order_rows:
                od = dict(row)
                items = conn.execute(
                    'SELECT * FROM order_items WHERE bestelnummer = %s',
                    (od['bestelnummer'],)
                ).fetchall()
                od['items'] = [dict(i) for i in items]
                orders_data.append(od)
        finally:
            conn.close()   # release before any slow SMTP calls

        logging.info('check_unpaid_orders: %d order(s) need a reminder', len(orders_data))

        # ── Phase 2 + 3: send email, then update DB per order ─────────────
        for order_data in orders_data:
            bestelnummer = order_data['bestelnummer']
            try:
                pdf_buf = generate_invoice(order_data, output=io.BytesIO())
                send_reminder_email(order_data, pdf_buf)

                upd = get_db()
                try:
                    upd.execute(
                        'UPDATE orders SET reminder_sent = TRUE, updated_at = %s '
                        'WHERE bestelnummer = %s',
                        (datetime.now().isoformat(), bestelnummer)
                    )
                    upd.commit()
                finally:
                    upd.close()

                logging.info('check_unpaid_orders: reminder sent for order %s', bestelnummer)
            except Exception:
                logging.exception('check_unpaid_orders: failed for order %s', bestelnummer)

    except Exception:
        logging.exception('check_unpaid_orders: job crashed')


# ── STARTUP (runs when gunicorn imports the module) ───────────────────────────

try:
    init_db()     # CREATE TABLE IF NOT EXISTS — safe to run every boot (CQ-06)
except Exception:
    logging.exception('init_db failed at startup')

try:
    migrate_db()  # ALTER TABLE / CREATE INDEX / CREATE SEQUENCE
except Exception:
    logging.exception('migrate_db failed at startup — app continues; some features may be degraded')

_scheduler = BackgroundScheduler(daemon=True)
_scheduler.add_job(
    check_unpaid_orders,
    trigger='interval',
    hours=24,
    id='unpaid_reminders',
)
# Only start the scheduler in the actual worker process, not in Flask's reloader parent.
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    _scheduler.start()
    atexit.register(lambda: _scheduler.shutdown(wait=False))


if __name__ == '__main__':
    init_db()
    app.run(debug=True)
