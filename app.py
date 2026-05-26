import os
from datetime import datetime, date
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, send_file
from dotenv import load_dotenv
import sqlite3
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'sb_secret_key_2024_#xP9mQ')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'shoppybrand.db')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'shoppybrand')
INVOICES_DIR = os.path.join(BASE_DIR, 'data', 'invoices')


# ── DATABASE ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_settings():
    conn = get_db()
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}


# ── PDF INVOICE ───────────────────────────────────────────────────────────────

def generate_invoice(order_data):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
    from reportlab.lib.utils import ImageReader

    os.makedirs(INVOICES_DIR, exist_ok=True)
    bestelnummer = order_data['bestelnummer']
    pdf_path = os.path.join(INVOICES_DIR, f'factuur_{bestelnummer}.pdf')

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
        pdf_path, pagesize=A4,
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
        # <a href> inside Paragraph creates a real clickable PDF hyperlink
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
    return pdf_path


# ── EMAIL ─────────────────────────────────────────────────────────────────────

def send_invoice_email(order_data, pdf_path):
    load_dotenv(override=True)
    gmail_user = os.getenv('GMAIL_USER', '').strip()
    gmail_pwd  = os.getenv('GMAIL_APP_PASSWORD', '').replace(' ', '').strip()
    if not gmail_user or not gmail_pwd:
        raise ValueError('E-mail credentials niet ingesteld in .env')

    customer_email = order_data.get('email', '').strip()
    if not customer_email:
        raise ValueError('Geen e-mailadres voor klant')

    bestelnummer   = order_data['bestelnummer']
    naam           = order_data.get('naam') or 'Klant'
    totaal         = f"{float(order_data['totaalbedrag']):.2f}".replace('.', ',')
    verzendmethode = order_data.get('verzendmethode') or '–'

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

    msg = MIMEMultipart('mixed')
    msg['From']    = f'ShoppyBrand <{gmail_user}>'
    msg['To']      = customer_email
    msg['Subject'] = f'Bevestiging bestelling #{bestelnummer} – ShoppyBrand'
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    with open(pdf_path, 'rb') as f:
        att = MIMEApplication(f.read(), _subtype='pdf')
        att.add_header('Content-Disposition', 'attachment', filename=f'factuur_{bestelnummer}.pdf')
        msg.attach(att)

    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_user, gmail_pwd)
        server.sendmail(gmail_user, customer_email, msg.as_bytes())


def send_owner_notification(order_data):
    load_dotenv(override=True)
    gmail_user = os.getenv('GMAIL_USER', '').strip()
    gmail_pwd  = os.getenv('GMAIL_APP_PASSWORD', '').replace(' ', '').strip()
    if not gmail_user or not gmail_pwd:
        raise ValueError('E-mail credentials niet ingesteld in .env')

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
        f'Ga naar het admin panel: http://localhost:5000/admin'
    )

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From']    = f'ShoppyBrand <{gmail_user}>'
    msg['To']      = gmail_user
    msg['Subject'] = f'\U0001f6d2 Nieuwe bestelling #{bestelnummer} – ShoppyBrand'

    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_user, gmail_pwd)
        server.sendmail(gmail_user, gmail_user, msg.as_bytes())


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript('''
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
        );

        CREATE TABLE IF NOT EXISTS on_demand_products (
            sku_key TEXT PRIMARY KEY,
            item_id TEXT,
            naam TEXT,
            type TEXT,
            maat TEXT,
            demand_prijs REAL,
            verkoop_prijs REAL,
            levertijd TEXT DEFAULT '10-12 werkdagen',
            created_at TEXT
        );

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
        );

        CREATE TABLE IF NOT EXISTS orders (
            bestelnummer INTEGER PRIMARY KEY AUTOINCREMENT,
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
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bestelnummer INTEGER,
            sku_key TEXT,
            product_naam TEXT,
            maat TEXT,
            aantal INTEGER,
            prijs_per_stuk REAL,
            is_on_demand INTEGER DEFAULT 0
        );

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
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS discount_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            klant_id TEXT NOT NULL,
            used_at TEXT,
            UNIQUE(code, klant_id)
        );
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
    c.executemany(
        'INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)',
        defaults
    )
    conn.commit()
    # Migration: add levertijd column to existing databases
    try:
        c.execute("ALTER TABLE on_demand_products ADD COLUMN levertijd TEXT DEFAULT '10-12 werkdagen'")
        conn.commit()
    except Exception:
        pass
    # Migration: add foto column to on_demand_products
    try:
        c.execute("ALTER TABLE on_demand_products ADD COLUMN foto TEXT DEFAULT NULL")
        conn.commit()
    except Exception:
        pass
    # Migration: fix eenmalig codes wrongly deactivated by old logic
    try:
        conn.execute("UPDATE discount_codes SET actief = 1 WHERE eenmalig = 1 AND actief = 0")
        conn.commit()
    except Exception:
        pass
    # Migration: renumber klant_id from KL-instagram to KL0001 format
    try:
        old_count = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE klant_id LIKE 'KL-%'"
        ).fetchone()[0]
        if old_count > 0:
            customers_to_migrate = conn.execute(
                "SELECT klant_id FROM customers ORDER BY created_at"
            ).fetchall()
            for i, row in enumerate(customers_to_migrate, 1):
                old_id = row[0]  # plain tuple — no row_factory in init_db
                new_id = f'KL{str(i).zfill(4)}'
                if old_id != new_id:
                    conn.execute('UPDATE customers SET klant_id = ? WHERE klant_id = ?', (new_id, old_id))
                    conn.execute('UPDATE orders SET klant_id = ? WHERE klant_id = ?', (new_id, old_id))
            conn.commit()
    except Exception:
        pass
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
    rows = conn.execute(
        'SELECT sku_key, item_id, naam, type, maat, voorraad, verkoop_prijs, foto '
        'FROM products ORDER BY naam, CAST(maat AS REAL), maat'
    ).fetchall()
    conn.close()

    products = {}
    for r in rows:
        iid = r['item_id']
        if iid not in products:
            products[iid] = {
                'item_id': iid,
                'naam': r['naam'],
                'type': r['type'],
                'verkoop_prijs': r['verkoop_prijs'],
                'foto': r['foto'],
                'sizes': [],
            }
        products[iid]['sizes'].append({
            'sku_key': r['sku_key'],
            'maat': r['maat'],
            'voorraad': r['voorraad'],
        })
    return jsonify(list(products.values()))


@app.route('/api/on-demand')
def api_on_demand():
    conn = get_db()
    rows = conn.execute(
        'SELECT sku_key, item_id, naam, type, maat, demand_prijs, verkoop_prijs, levertijd, foto '
        'FROM on_demand_products ORDER BY naam, maat'
    ).fetchall()
    conn.close()
    groups = {}
    for r in rows:
        iid = r['item_id'] or r['sku_key']
        if iid not in groups:
            groups[iid] = {
                'item_id':      iid,
                'naam':         r['naam'],
                'type':         r['type'],
                'demand_prijs': r['demand_prijs'],
                'verkoop_prijs':r['verkoop_prijs'],
                'levertijd':    r['levertijd'],
                'foto':         r['foto'],
                'sizes':        [],
            }
        elif not groups[iid]['foto'] and r['foto']:
            groups[iid]['foto'] = r['foto']
        groups[iid]['sizes'].append({'sku_key': r['sku_key'], 'maat': r['maat']})
    return jsonify(list(groups.values()))


@app.route('/api/customer')
def api_customer():
    instagram = request.args.get('instagram', '').strip()
    if not instagram:
        return jsonify(None)
    if not instagram.startswith('@'):
        instagram = '@' + instagram
    conn = get_db()
    row = conn.execute('SELECT * FROM customers WHERE instagram = ?', (instagram,)).fetchone()
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
    d = conn.execute('SELECT * FROM discount_codes WHERE code = ?', (code,)).fetchone()

    if not d:
        conn.close()
        return jsonify({'valid': False, 'bericht': 'Ongeldige kortingscode'})
    if not d['actief']:
        conn.close()
        return jsonify({'valid': False, 'bericht': 'Deze kortingscode is niet meer geldig'})
    if d['eind_datum']:
        try:
            if datetime.fromisoformat(d['eind_datum']).date() < date.today():
                conn.close()
                return jsonify({'valid': False, 'bericht': 'Deze kortingscode is verlopen'})
        except ValueError:
            pass
    if d['minimum_bestelbedrag'] and subtotal < float(d['minimum_bestelbedrag']):
        conn.close()
        return jsonify({'valid': False, 'bericht': f'Minimum bestelbedrag {float(d["minimum_bestelbedrag"]):.2f} vereist'})

    # Per-klant eenmalig check
    if d['eenmalig'] and instagram:
        ig = instagram if instagram.startswith('@') else '@' + instagram
        klant = conn.execute(
            'SELECT klant_id FROM customers WHERE instagram = ?', (ig,)
        ).fetchone()
        if klant:
            used = conn.execute(
                'SELECT 1 FROM discount_usage WHERE code = ? AND klant_id = ?',
                (code, klant['klant_id'])
            ).fetchone()
            if used:
                conn.close()
                return jsonify({'valid': False, 'bericht': 'Je hebt deze code al gebruikt'})

    conn.close()

    if d['type'] == 'PERCENTAGE':
        kortingsbedrag = round(subtotal * float(d['waarde']) / 100, 2)
        bericht = f'{float(d["waarde"]):.0f}% korting – je bespaart € {kortingsbedrag:.2f}!'
    elif d['type'] == 'VAST_BEDRAG':
        kortingsbedrag = min(float(d['waarde']), subtotal)
        bericht = f'€ {kortingsbedrag:.2f} korting toegepast!'
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
def submit_order():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Geen data ontvangen'}), 400

    for field in ['naam', 'instagram', 'email', 'adres', 'postcode', 'stad', 'land', 'verzendmethode', 'items']:
        if not data.get(field):
            return jsonify({'success': False, 'error': f'Veld {field} is verplicht'}), 400
    if not data['items']:
        return jsonify({'success': False, 'error': 'Geen producten geselecteerd'}), 400

    conn = get_db()
    c = conn.cursor()
    now = datetime.now().isoformat()

    try:
        instagram = data['instagram'].strip()
        if not instagram.startswith('@'):
            instagram = '@' + instagram

        existing = c.execute('SELECT klant_id FROM customers WHERE instagram = ?', (instagram,)).fetchone()
        if existing:
            klant_id = existing['klant_id']
        else:
            max_row = c.execute(
                "SELECT MAX(CAST(SUBSTR(klant_id, 3) AS INTEGER)) FROM customers WHERE klant_id LIKE 'KL%'"
            ).fetchone()
            next_num = (max_row[0] or 0) + 1
            klant_id = f'KL{str(next_num).zfill(4)}'
            c.execute('''
                INSERT INTO customers
                (klant_id, naam, instagram, email, telefoon, adres, postcode, stad, land,
                 totaal_uitgegeven, aantal_bestellingen, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            ''', (klant_id, data['naam'], instagram, data.get('email', ''),
                  data.get('telefoon', ''), data['adres'], data['postcode'],
                  data['stad'], data['land'], now))

        settings = {r['key']: r['value'] for r in c.execute('SELECT key, value FROM settings').fetchall()}

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
            discount = c.execute(
                'SELECT * FROM discount_codes WHERE code = ? AND actief = 1', (actiecode,)
            ).fetchone()
            if discount:
                if discount['minimum_bestelbedrag'] and subtotaal < float(discount['minimum_bestelbedrag']):
                    conn.close()
                    return jsonify({'success': False, 'error': 'Minimum bestelbedrag niet bereikt'}), 400
                if discount['eind_datum']:
                    try:
                        if datetime.fromisoformat(discount['eind_datum']).date() < date.today():
                            conn.close()
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

        c.execute('''
            INSERT INTO orders
            (klant_id, besteldatum, verzendmethode, verzendkosten, actiecode, kortingsbedrag,
             subtotaal, totaalbedrag, bestellingstatus, betaalstatus, opmerking, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Ontvangen', 'Onbetaald', ?, ?, ?)
        ''', (klant_id, now, verzendmethode, verzendkosten, actiecode,
              kortingsbedrag, subtotaal, totaalbedrag, data.get('opmerking') or None, now, now))
        bestelnummer = c.lastrowid

        for item in data['items']:
            is_od = int(item.get('is_on_demand', 0))
            c.execute('''
                INSERT INTO order_items
                (bestelnummer, sku_key, product_naam, maat, aantal, prijs_per_stuk, is_on_demand)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (bestelnummer, item['sku_key'], item['naam'], item['maat'],
                  int(item['aantal']), float(item['prijs']), is_od))
            if not is_od:
                c.execute(
                    'UPDATE products SET voorraad = MAX(0, voorraad - ?), updated_at = ? WHERE sku_key = ?',
                    (int(item['aantal']), now, item['sku_key'])
                )

        c.execute('''
            UPDATE customers SET
                naam = ?, email = ?, telefoon = ?, adres = ?, postcode = ?, stad = ?, land = ?,
                totaal_uitgegeven = totaal_uitgegeven + ?,
                aantal_bestellingen = aantal_bestellingen + 1,
                laatste_bestelling = ?
            WHERE klant_id = ?
        ''', (data['naam'], data.get('email', ''), data.get('telefoon', ''),
              data['adres'], data['postcode'], data['stad'], data['land'],
              totaalbedrag, now, klant_id))

        if discount:
            c.execute(
                'UPDATE discount_codes SET huidige_gebruik = huidige_gebruik + 1 WHERE code = ?',
                (actiecode,)
            )
            if discount['eenmalig']:
                # Track per-klant usage; code stays active for other customers
                try:
                    c.execute(
                        'INSERT OR IGNORE INTO discount_usage (code, klant_id, used_at) VALUES (?, ?, ?)',
                        (actiecode, klant_id, now)
                    )
                except Exception:
                    pass

        conn.commit()
        conn.close()

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
        try:
            pdf_path = generate_invoice(order_data)
            send_invoice_email(order_data, pdf_path)
        except Exception as mail_err:
            logging.error('Invoice/email error for order %s: %s', bestelnummer, mail_err)
        try:
            send_owner_notification(order_data)
        except Exception as notif_err:
            logging.error('Owner notification error for order %s: %s', bestelnummer, notif_err)

        return jsonify({'success': True, 'bestelnummer': bestelnummer})

    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/success/<int:bestelnummer>')
def success(bestelnummer):
    conn = get_db()
    order_row = conn.execute(
        'SELECT * FROM orders WHERE bestelnummer = ?', (bestelnummer,)
    ).fetchone()
    conn.close()
    if not order_row:
        return redirect('/')
    return render_template('success.html', bestelnummer=bestelnummer, order=dict(order_row))


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

    stats = {
        'today_revenue': conn.execute(
            "SELECT COALESCE(SUM(totaalbedrag),0) FROM orders "
            "WHERE DATE(besteldatum)=? AND betaalstatus='Betaald'", (today,)
        ).fetchone()[0],
        'week_revenue': conn.execute(
            "SELECT COALESCE(SUM(totaalbedrag),0) FROM orders "
            "WHERE strftime('%Y-%W',besteldatum)=strftime('%Y-%W','now') AND betaalstatus='Betaald'"
        ).fetchone()[0],
        'month_revenue': conn.execute(
            "SELECT COALESCE(SUM(totaalbedrag),0) FROM orders "
            "WHERE strftime('%Y-%m',besteldatum)=strftime('%Y-%m','now') AND betaalstatus='Betaald'"
        ).fetchone()[0],
        'pending_payment': conn.execute(
            "SELECT COALESCE(SUM(totaalbedrag),0) FROM orders WHERE betaalstatus='Onbetaald'"
        ).fetchone()[0],
        'low_stock_count': conn.execute(
            "SELECT COUNT(*) FROM products WHERE voorraad > 0 AND voorraad <= 1"
        ).fetchone()[0],
        'total_orders': conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
    }

    last_orders = conn.execute('''
        SELECT o.bestelnummer, c.naam as klant_naam, c.instagram,
               o.besteldatum, o.totaalbedrag, o.bestellingstatus, o.betaalstatus
        FROM orders o LEFT JOIN customers c ON o.klant_id=c.klant_id
        ORDER BY o.bestelnummer DESC LIMIT 5
    ''').fetchall()

    conn.close()
    return jsonify({
        **{k: round(float(v), 2) for k, v in stats.items()},
        'last_orders': [dict(r) for r in last_orders],
    })


@app.route('/admin/api/orders')
@admin_required
def admin_orders_api():
    conn = get_db()
    orders = conn.execute('''
        SELECT o.*, c.naam as klant_naam, c.instagram, c.adres, c.postcode, c.stad, c.land
        FROM orders o LEFT JOIN customers c ON o.klant_id=c.klant_id
        ORDER BY o.bestelnummer DESC
    ''').fetchall()

    result = []
    for o in orders:
        o_dict = dict(o)
        items = conn.execute(
            'SELECT * FROM order_items WHERE bestelnummer = ?', (o['bestelnummer'],)
        ).fetchall()
        o_dict['items'] = [dict(i) for i in items]
        result.append(o_dict)

    conn.close()
    return jsonify(result)


@app.route('/admin/api/products')
@admin_required
def admin_products_api():
    conn = get_db()
    products = conn.execute(
        'SELECT * FROM products ORDER BY naam, CAST(maat AS REAL), maat'
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
    customers = conn.execute(
        'SELECT * FROM customers ORDER BY aantal_bestellingen DESC, totaal_uitgegeven DESC'
    ).fetchall()
    result = []
    for cust in customers:
        c_dict = dict(cust)
        orders = conn.execute(
            'SELECT bestelnummer, besteldatum, totaalbedrag, bestellingstatus, betaalstatus '
            'FROM orders WHERE klant_id=? ORDER BY bestelnummer DESC',
            (cust['klant_id'],)
        ).fetchall()
        c_dict['orders'] = [dict(o) for o in orders]
        result.append(c_dict)
    conn.close()
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
    data = request.get_json()
    field = data.get('field')
    if field not in ('bestellingstatus', 'betaalstatus'):
        return jsonify({'success': False, 'error': 'Ongeldig veld'}), 400
    conn = get_db()
    conn.execute(
        f'UPDATE orders SET {field}=?, updated_at=? WHERE bestelnummer=?',
        (data['value'], datetime.now().isoformat(), data['bestelnummer'])
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
        'UPDATE products SET voorraad=?, updated_at=? WHERE sku_key=?',
        (int(data['voorraad']), datetime.now().isoformat(), data['sku_key'])
    )
    conn.commit()
    conn.close()
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (data['sku_key'], data['item_id'], data['naam'], data['type'], data['maat'],
              int(data.get('voorraad', 0)), float(data.get('aankoop_prijs', 0)),
              float(data.get('verkoop_prijs', 0)), now, now))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
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
            VALUES (?, ?, ?, ?, ?, ?, 1)
        ''', (data['code'].upper(), data['type'], float(data.get('waarde', 0)),
              float(data.get('minimum_bestelbedrag', 0)),
              data.get('eind_datum') or None,
              1 if data.get('eenmalig') else 0))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': 'Code bestaat al'}), 400


@app.route('/admin/api/toggle-discount', methods=['POST'])
@admin_required
def admin_toggle_discount():
    data = request.get_json()
    conn = get_db()
    conn.execute(
        'UPDATE discount_codes SET actief=CASE WHEN actief=1 THEN 0 ELSE 1 END WHERE code=?',
        (data['code'],)
    )
    conn.commit()
    row = conn.execute('SELECT actief FROM discount_codes WHERE code=?', (data['code'],)).fetchone()
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
            'INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)',
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
            WHERE o.bestelnummer = ?
        ''', (bestelnummer,)).fetchone()
        if not order:
            conn.close()
            return jsonify({'error': 'Bestelling niet gevonden'}), 404
        items = conn.execute(
            'SELECT * FROM order_items WHERE bestelnummer = ?', (bestelnummer,)
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
    order = conn.execute('''
        SELECT o.*, c.naam, c.instagram, c.email, c.telefoon, c.adres, c.postcode, c.stad, c.land
        FROM orders o LEFT JOIN customers c ON o.klant_id = c.klant_id
        WHERE o.bestelnummer = ?
    ''', (bestelnummer,)).fetchone()
    if not order:
        conn.close()
        return jsonify({'success': False, 'error': 'Bestelling niet gevonden'}), 404
    items = conn.execute(
        'SELECT * FROM order_items WHERE bestelnummer = ?', (bestelnummer,)
    ).fetchall()
    conn.close()
    order_data = dict(order)
    order_data['items'] = [dict(i) for i in items]
    try:
        pdf_path = generate_invoice(order_data)
        send_invoice_email(order_data, pdf_path)
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
        'UPDATE products SET naam=?, voorraad=?, aankoop_prijs=?, verkoop_prijs=?, updated_at=? WHERE sku_key=?',
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
    conn.execute('DELETE FROM products WHERE sku_key=?', (sku_key,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/api/add-on-demand', methods=['POST'])
@admin_required
def admin_add_on_demand():
    import re as _re
    data = request.get_json()
    naam = (data.get('naam') or '').strip()
    maat = (data.get('maat') or '').strip()
    if not naam:
        return jsonify({'success': False, 'error': 'Naam is verplicht'}), 400
    slug = _re.sub(r'[^a-z0-9]+', '-', naam.lower()).strip('-')
    custom_sku = (data.get('sku_key') or '').strip()
    sku_key = custom_sku if custom_sku else (f'OD-{slug}-{maat}' if maat else f'OD-{slug}')
    now = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO on_demand_products
            (sku_key, item_id, naam, type, maat, demand_prijs, verkoop_prijs, levertijd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (sku_key, slug, naam, data.get('type', ''), maat,
              float(data.get('demand_prijs', 0)), float(data.get('verkoop_prijs', 0)),
              data.get('levertijd') or '10-12 werkdagen', now))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'sku_key': sku_key})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': f'SKU "{sku_key}" bestaat al — pas naam of maat aan'}), 400
    except Exception as e:
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
                'SELECT foto FROM on_demand_products WHERE sku_key=?', (old_sku,)
            ).fetchone()
            foto = existing['foto'] if existing else None
            conn.execute('DELETE FROM on_demand_products WHERE sku_key=?', (old_sku,))
            conn.execute('''
                INSERT INTO on_demand_products (sku_key, naam, type, maat, demand_prijs, verkoop_prijs, levertijd, foto)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (new_sku, naam, type_, maat, demand, verkoop, levertijd, foto))
            conn.commit()
            conn.close()
            return jsonify({'success': True})
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'success': False, 'error': f'SKU "{new_sku}" bestaat al'}), 400
    else:
        conn.execute('''
            UPDATE on_demand_products
            SET naam=?, type=?, maat=?, demand_prijs=?, verkoop_prijs=?, levertijd=?
            WHERE sku_key=?
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
    conn.execute('DELETE FROM on_demand_products WHERE sku_key=?', (sku_key,))
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
    groups = {}
    for r in rows:
        iid = r['item_id'] or r['sku_key']
        if iid not in groups:
            groups[iid] = {
                'item_id':       iid,
                'naam':          r['naam'],
                'type':          r['type'],
                'demand_prijs':  r['demand_prijs'],
                'verkoop_prijs': r['verkoop_prijs'],
                'levertijd':     r['levertijd'],
                'foto':          r['foto'],
                'sizes':         [],
            }
        elif not groups[iid]['foto'] and r['foto']:
            groups[iid]['foto'] = r['foto']
        groups[iid]['sizes'].append({'sku_key': r['sku_key'], 'maat': r['maat']})

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
        'UPDATE on_demand_products SET naam=?, type=?, demand_prijs=?, verkoop_prijs=?, levertijd=? '
        'WHERE item_id=?',
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
        'SELECT naam, type, demand_prijs, verkoop_prijs, levertijd FROM on_demand_products WHERE item_id=? LIMIT 1',
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
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (sku_key, item_id, existing['naam'], existing['type'], maat,
             existing['demand_prijs'], existing['verkoop_prijs'], existing['levertijd'], now)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'sku_key': sku_key})
    except sqlite3.IntegrityError:
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
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (sku_key, item_id, naam, type_, maat, demand, verkoop, levertijd, now)
            )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError as e:
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
    conn.execute('DELETE FROM on_demand_products WHERE item_id=?', (item_id,))
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
    img_dir = os.path.join(BASE_DIR, 'static', 'product_images')
    os.makedirs(img_dir, exist_ok=True)
    if item_id:
        safe_name = item_id.replace('/', '_').replace('\\', '_')
        filename  = f'{safe_name}.jpg'
        file.save(os.path.join(img_dir, filename))
        foto_path = f'static/product_images/{filename}'
        conn = get_db()
        conn.execute('UPDATE products SET foto = ? WHERE item_id = ?', (foto_path, item_id))
        conn.commit()
        conn.close()
    else:
        safe_sku  = sku_key.replace('/', '_').replace('\\', '_')
        filename  = f'{safe_sku}.jpg'
        file.save(os.path.join(img_dir, filename))
        foto_path = f'static/product_images/{filename}'
        conn = get_db()
        conn.execute('UPDATE products SET foto = ? WHERE sku_key = ?', (foto_path, sku_key))
        conn.commit()
        conn.close()
    return jsonify({'success': True, 'foto': foto_path})


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
    img_dir = os.path.join(BASE_DIR, 'static', 'product_images')
    os.makedirs(img_dir, exist_ok=True)
    if item_id:
        safe_name = item_id.replace('/', '_').replace('\\', '_')
        filename  = f'od_{safe_name}.jpg'
        file.save(os.path.join(img_dir, filename))
        foto_path = f'static/product_images/{filename}'
        conn = get_db()
        conn.execute('UPDATE on_demand_products SET foto = ? WHERE item_id = ?', (foto_path, item_id))
        conn.commit()
        conn.close()
    else:
        safe_sku  = sku_key.replace('/', '_').replace('\\', '_')
        filename  = f'od_{safe_sku}.jpg'
        file.save(os.path.join(img_dir, filename))
        foto_path = f'static/product_images/{filename}'
        conn = get_db()
        conn.execute('UPDATE on_demand_products SET foto = ? WHERE sku_key = ?', (foto_path, sku_key))
        conn.commit()
        conn.close()
    return jsonify({'success': True, 'foto': foto_path})


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
        conn.execute('UPDATE products SET sku_key = ? WHERE sku_key = ?', (new_sku, old_sku))
        conn.execute('UPDATE order_items SET sku_key = ? WHERE sku_key = ?', (new_sku, old_sku))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': f'SKU "{new_sku}" bestaat al'}), 400


_CATEGORY_ORDER = ['Sneakers', 'Tracksuits', 'Horloges', 'Accessoires']


@app.route('/admin/api/products-grouped')
@admin_required
def admin_products_grouped():
    conn = get_db()
    rows = conn.execute(
        'SELECT sku_key, item_id, naam, type, maat, voorraad, aankoop_prijs, verkoop_prijs, foto '
        'FROM products ORDER BY item_id, maat'
    ).fetchall()
    conn.close()
    groups = {}
    for r in rows:
        iid = r['item_id'] or r['sku_key']
        if iid not in groups:
            groups[iid] = {
                'item_id': iid,
                'naam': r['naam'],
                'type': r['type'],
                'aankoop_prijs': r['aankoop_prijs'],
                'verkoop_prijs': r['verkoop_prijs'],
                'foto': r['foto'],
                'sizes': [],
            }
        groups[iid]['sizes'].append({
            'sku_key': r['sku_key'],
            'maat': r['maat'],
            'voorraad': r['voorraad'],
        })

    def _sort_key(g):
        cat = g['type'] or ''
        idx = _CATEGORY_ORDER.index(cat) if cat in _CATEGORY_ORDER else len(_CATEGORY_ORDER)
        return (idx, (g['naam'] or '').lower())

    sorted_groups = sorted(groups.values(), key=_sort_key)
    return jsonify({'success': True, 'groups': sorted_groups})


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
            'SELECT sku_key, maat FROM products WHERE item_id=?', (old_id,)
        ).fetchall()
        for r in rows:
            new_sku = f"{new_id}-{r['maat']}" if r['maat'] else new_id
            conn.execute('UPDATE products SET sku_key=?, item_id=? WHERE sku_key=?',
                         (new_sku, new_id, r['sku_key']))
            conn.execute('UPDATE order_items SET sku_key=? WHERE sku_key=?',
                         (new_sku, r['sku_key']))
        # Rename photo file if it exists
        old_foto = os.path.join(BASE_DIR, 'static', 'product_images', f'{old_id}.jpg')
        new_foto = os.path.join(BASE_DIR, 'static', 'product_images', f'{new_id}.jpg')
        if os.path.exists(old_foto) and not os.path.exists(new_foto):
            os.rename(old_foto, new_foto)
            new_foto_path = f'static/product_images/{new_id}.jpg'
            conn.execute('UPDATE products SET foto=? WHERE item_id=?', (new_foto_path, new_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError as e:
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
        'UPDATE products SET naam=?, type=?, aankoop_prijs=?, verkoop_prijs=? WHERE item_id=?',
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
    conn.execute('UPDATE products SET voorraad=? WHERE sku_key=?', (int(voorraad), sku_key))
    conn.commit()
    conn.close()
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
        'SELECT naam, type, aankoop_prijs, verkoop_prijs FROM products WHERE item_id=? LIMIT 1',
        (item_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({'success': False, 'error': 'item_id niet gevonden'}), 404
    sku_key = f'{item_id}-{maat}'
    try:
        conn.execute(
            'INSERT INTO products (sku_key, item_id, naam, type, maat, voorraad, aankoop_prijs, verkoop_prijs) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (sku_key, item_id, existing['naam'], existing['type'], maat, voorraad,
             existing['aankoop_prijs'], existing['verkoop_prijs'])
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'sku_key': sku_key})
    except sqlite3.IntegrityError:
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
    conn.execute('DELETE FROM products WHERE sku_key=?', (sku_key,))
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
                'VALUES (?,?,?,?,?,?,?,?)',
                (sku_key, item_id, naam, type_, maat, voorraad, aankoop, verkoop)
            )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError as e:
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/admin/api/test-email')
@admin_required
def admin_test_email():
    gmail_user = os.getenv('GMAIL_USER', '').strip()
    gmail_pwd  = os.getenv('GMAIL_APP_PASSWORD', '').replace(' ', '').strip().strip()
    if not gmail_user or not gmail_pwd:
        return jsonify({'success': False, 'error': 'GMAIL_USER of GMAIL_APP_PASSWORD niet ingesteld in .env'})
    try:
        msg = MIMEText('Test e-mail van ShoppyBrand admin panel.', 'plain', 'utf-8')
        msg['From']    = f'ShoppyBrand <{gmail_user}>'
        msg['To']      = gmail_user
        msg['Subject'] = 'ShoppyBrand – Test e-mail'
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.ehlo()
            server.starttls()
            server.login(gmail_user, gmail_pwd)
            server.sendmail(gmail_user, gmail_user, msg.as_bytes())
        return jsonify({'success': True, 'message': f'Test e-mail verstuurd naar {gmail_user}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/api/delete-discount', methods=['POST'])
@admin_required
def admin_delete_discount():
    data = request.get_json()
    code = data.get('code')
    if not code:
        return jsonify({'success': False, 'error': 'Code is verplicht'}), 400
    conn = get_db()
    conn.execute('DELETE FROM discount_codes WHERE code = ?', (code,))
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
            'SELECT sku_key, aantal, is_on_demand FROM order_items WHERE bestelnummer = ?',
            (bestelnummer,)
        ).fetchall()
        for item in items:
            if not item['is_on_demand']:
                conn.execute(
                    'UPDATE products SET voorraad = voorraad + ?, updated_at = ? WHERE sku_key = ?',
                    (item['aantal'], now, item['sku_key'])
                )
        order = conn.execute(
            'SELECT klant_id, totaalbedrag FROM orders WHERE bestelnummer = ?',
            (bestelnummer,)
        ).fetchone()
        conn.execute('DELETE FROM order_items WHERE bestelnummer = ?', (bestelnummer,))
        conn.execute('DELETE FROM orders WHERE bestelnummer = ?', (bestelnummer,))
        if order:
            conn.execute('''
                UPDATE customers SET
                    totaal_uitgegeven = MAX(0, totaal_uitgegeven - ?),
                    aantal_bestellingen = MAX(0, aantal_bestellingen - 1)
                WHERE klant_id = ?
            ''', (order['totaalbedrag'], order['klant_id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    init_db()
    app.run(debug=True)
