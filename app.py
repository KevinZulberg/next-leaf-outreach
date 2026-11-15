import csv, imaplib, io, os, re, sqlite3, ssl, smtplib, time
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from zoneinfo import ZoneInfo
from secrets import compare_digest, token_urlsafe
from datetime import datetime, time as clock, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from html import escape
from pathlib import Path
from flask import Flask, request, redirect, url_for, flash, Response, abort, session, render_template, send_file
from markupsafe import Markup
from blocklist import is_law_firm
from discovery import FREE_MAIL, credit_status, inspect_company_site

BASE = Path(__file__).resolve().parent
TIMEZONE = ZoneInfo('Africa/Johannesburg')
DEMO_URL = 'https://nextleaf.co.za/demo'
LEGACY_DEMO_URL = 'https://nextleaf.co.za/#work'
DIRECTORY_TITLE = re.compile(r'\b(?:top\s+\d+|business directory|company directory|list of companies)\b', re.I)
AARON_SIGNATURE = 'Aaron\nNextleaf Digital\n+27 71 886 9647 | nextleaf.co.za'
DB = Path(os.getenv('OUTREACH_DB', BASE / 'outreach.db'))
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'local-only-change-this-before-deploying')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024

FINDER_VERSION = '2'


def conn():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    return c

def init_db():
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY, company TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE, contact_name TEXT DEFAULT '', observation TEXT DEFAULT '', consent_basis TEXT DEFAULT '', consent_evidence TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'draft', subject TEXT DEFAULT '', body TEXT DEFAULT '', sent_at TEXT DEFAULT '', error TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS suppression (email TEXT PRIMARY KEY COLLATE NOCASE, reason TEXT DEFAULT '', created_at TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS send_log (id INTEGER PRIMARY KEY, email TEXT NOT NULL, sent_at TEXT NOT NULL, result TEXT NOT NULL, detail TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS prospects (id INTEGER PRIMARY KEY, domain TEXT NOT NULL UNIQUE COLLATE NOCASE, company TEXT NOT NULL, website TEXT NOT NULL, snippet TEXT DEFAULT '', source_url TEXT DEFAULT '', email TEXT DEFAULT '', observation TEXT DEFAULT '', email_source TEXT DEFAULT '', researched INTEGER DEFAULT 0, drafted INTEGER DEFAULT 0, status TEXT DEFAULT 'discovered')''')

        c.execute('CREATE TABLE IF NOT EXISTS scheduled_sends (day TEXT PRIMARY KEY)')
        columns = {row['name'] for row in c.execute('PRAGMA table_info(prospects)')}
        if 'observation_source' not in columns:
            c.execute("ALTER TABLE prospects ADD COLUMN observation_source TEXT DEFAULT ''")
        if 'found_at' not in columns:
            c.execute("ALTER TABLE prospects ADD COLUMN found_at TEXT DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS research_runs (
            id INTEGER PRIMARY KEY, day TEXT NOT NULL, manual INTEGER NOT NULL,
            started_at TEXT NOT NULL, region TEXT NOT NULL, industry TEXT NOT NULL,
            status TEXT NOT NULL, found INTEGER DEFAULT 0, added INTEGER DEFAULT 0,
            inspected INTEGER DEFAULT 0, emails INTEGER DEFAULT 0
        )""")
        if 'searches' not in {row['name'] for row in c.execute('PRAGMA table_info(research_runs)')}:
            c.execute('ALTER TABLE research_runs ADD COLUMN searches INTEGER DEFAULT 0')
        if 'market' not in {row['name'] for row in c.execute('PRAGMA table_info(contacts)')}:
            c.execute("ALTER TABLE contacts ADD COLUMN market TEXT DEFAULT ''")
        if 'market' not in {row['name'] for row in c.execute('PRAGMA table_info(prospects)')}:
            c.execute("ALTER TABLE prospects ADD COLUMN market TEXT DEFAULT ''")
        c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        for row in c.execute("SELECT id, company, email, observation FROM contacts WHERE status IN ('draft','approved')").fetchall():
            if is_law_firm(row['company'], row['email'], row['observation']):
                c.execute("UPDATE contacts SET status='skipped' WHERE id=?", (row['id'],))
        if not c.execute("SELECT 1 FROM settings WHERE key='template_version' AND value='3'").fetchone():
            for row in c.execute("SELECT id, company, contact_name, observation FROM contacts WHERE status IN ('draft','approved') AND (body LIKE ? OR body LIKE ?)", tuple('%' + m + '%' for m in OLD_TEMPLATE_MARKERS)).fetchall():
                subject, body = draft_contact(dict(row))
                c.execute('UPDATE contacts SET subject=?, body=? WHERE id=?', (subject, body, row['id']))
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('template_version','3')")
        seen = c.execute("SELECT value FROM settings WHERE key='finder_version'").fetchone()
        if not seen or seen['value'] != FINDER_VERSION:
            # The email finder was rebuilt: give sites it used to miss another look.
            c.execute("UPDATE prospects SET researched=0 WHERE email='' AND status IN ('email_not_found','contact_form','discovered')")
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('finder_version',?)", (FINDER_VERSION,))

EMAIL_RE=re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

def studio_page(template_name, title, **context):
    return render_template('layout.html', page_title=title, body=render_template(template_name, **context))


@app.get('/studio-logo.png')
def studio_logo():
    return send_file(BASE / 'Nextleaf-Digital-email-logo.png', max_age=86400)

def permission_token():
    if 'permission_token' not in session:
        session['permission_token'] = token_urlsafe(32)
    return session['permission_token']


def verify_permission_token():
    expected = session.get('permission_token')
    supplied = request.form.get('permission_token', '')
    if not expected or not compare_digest(expected.encode(), supplied.encode()):
        abort(400, 'Refresh the page and try again.')


def complete_message(subject, body):
    return bool(subject and subject.strip() and '\r' not in subject and '\n' not in subject
                and body and body.strip() and '[Your website / contact details]' not in body)


def confirmed_permission(row, now):
    if row['consent_basis'] in ('consent', 'existing_customer') and len((row['consent_evidence'] or '').strip()) >= 8:
        return row['consent_basis'], row['consent_evidence']
    return 'consent', f'Nextleaf operator confirmed recipient consent on {now}.'


def fields(row):
    company = row['company'] or ''
    name = (row['contact_name'] or '').split()
    observation, separator, _ = (row['observation'] or '').partition(' · https://')
    if separator:
        observation = f'that you offer {observation}'
    return {'company': company, 'name': name[0] if name else f'{company} team',
            'observation': observation or 'your business website'}

def draft_text(template, row):
    values = fields(row)
    return re.sub(r'\{\{(company|name|observation)\}\}', lambda match: values[match[1]], template)

SUBJECT='Quick question about quotes at {{company}}'
BODY='''Hi {{name}},

A customer asks for an update. The quote is in someone's inbox. The job details are on WhatsApp. Nobody's sure who was meant to follow up.

Nobody did anything wrong. The work just lives in five places, so even a good team ends up chasing updates.

I'm Aaron, founder of Nextleaf Digital. I build custom workflow software for small businesses, from the first enquiry through to payment.

That might mean sending quotes faster from your own prices, and seeing who owns the next step on every job.

Here's a live demo you can click around in: ''' + DEMO_URL + '''

Roughly how many hours a week go on quotes and chasing updates? Reply with a rough number, and I'll tell you where I'd save you time.

If you're interested in seeing how it works, just message me on WhatsApp: 071 886 9647.

''' + AARON_SIGNATURE + '''

Not relevant? Reply "no" and I won't email again.'''
OLD_TEMPLATE_MARKERS = ('Most workshops run on quotes in one place', "What's the one step you're always chasing?")

def email_domain(email):
    return (email or '').rsplit('@', 1)[-1].strip().lower()


def business_taken(db, email, statuses=None):
    """True if another address at the same business is already on the list (or already sent to).

    Gmail-style addresses are ignored, since unrelated businesses share those.
    """
    domain = email_domain(email)
    if not domain or domain in FREE_MAIL:
        return False
    query = "SELECT 1 FROM contacts WHERE lower(substr(email, instr(email, '@') + 1)) = ? AND lower(email) != lower(?)"
    params = [domain, email]
    if statuses:
        query += f" AND status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    return db.execute(query + ' LIMIT 1', params).fetchone() is not None


def draft_contact(row):
    """Subject and body from the standard template; drops the 'I saw ...' line when there is nothing to say."""
    has_note = bool((row.get('observation') or '').strip())
    template = BODY if has_note else BODY.replace('I saw {{observation}}. Nice work.\n\n', '')
    return draft_text(SUBJECT, row), draft_text(template, row)


DEFAULT_SETTINGS = {
    'autopilot': '1', 'research_time': '06:00', 'send_time': '08:00', 'days': '0,1,2,3',
    'searches_per_day': '80', 'emails_per_day': '80', 'delay_seconds': '20', 'send_found': '1',
    'uk_emails_per_day': '20', 'uk_send_time': '11:00', 'uk_searches_per_day': '10',
}
DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
TIME_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')


def get_settings():
    """Autopilot settings, edited on the Settings page and stored in the database."""
    try:
        with conn() as c:
            stored = dict(c.execute('SELECT key, value FROM settings').fetchall())
    except sqlite3.Error:
        stored = {}
    raw = {**DEFAULT_SETTINGS, **stored}

    def number(key, low, high):
        try:
            return max(low, min(int(float(raw[key])), high))
        except (TypeError, ValueError):
            return int(DEFAULT_SETTINGS[key])

    def clock_text(key):
        return raw[key] if TIME_RE.match(raw[key] or '') else DEFAULT_SETTINGS[key]

    days = sorted({int(d) for d in raw['days'].split(',') if d.strip().isdigit() and 0 <= int(d) <= 6})
    return {
        'autopilot': raw['autopilot'] == '1', 'research_time': clock_text('research_time'),
        'send_time': clock_text('send_time'), 'days': days or [0, 1, 2, 3],
        'searches_per_day': number('searches_per_day', 0, 500), 'emails_per_day': number('emails_per_day', 0, 1000),
        'delay_seconds': number('delay_seconds', 0, 300), 'send_found': raw['send_found'] == '1',
        'uk_emails_per_day': number('uk_emails_per_day', 0, 1000), 'uk_send_time': clock_text('uk_send_time'),
        'uk_searches_per_day': number('uk_searches_per_day', 0, 500),
    }


def save_settings(values):
    with conn() as c:
        for key, value in values.items():
            c.execute('INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)', (key, str(value)))


def hour_minute(text):
    hour, minute = text.split(':')
    return int(hour), int(minute)


def days_text(days):
    days = sorted(days)
    if len(days) == 7:
        return 'every day'
    if len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
        return f'{DAY_NAMES[days[0]]} to {DAY_NAMES[days[-1]]}'
    return ', '.join(DAY_NAMES[d][:3] for d in days)


def market_sql(column):
    """SQL for 'uk' or 'sa': a business found by the UK search, or any .uk address, counts as UK."""
    return (f"(CASE WHEN EXISTS (SELECT 1 FROM prospects mp WHERE mp.email = {column} COLLATE NOCASE AND mp.market = 'uk') "
            f"OR EXISTS (SELECT 1 FROM contacts mc WHERE mc.email = {column} COLLATE NOCASE AND mc.market = 'uk') "
            f"OR lower({column}) LIKE '%.uk' THEN 'uk' ELSE 'sa' END)")


def sent_today_by_market(db, start):
    """Emails handled today, split into South Africa and UK (uncertain ones count, to stay safe)."""
    counts = {'sa': 0, 'uk': 0}
    for row in db.execute(f"SELECT {market_sql('s.email')} AS market, count(*) AS n FROM send_log s WHERE s.sent_at >= ? GROUP BY market", (start,)):
        counts[row['market']] = row['n']
    return counts


def next_slot(now, text, days, done_today=False):
    """Human label for the next scheduled run, e.g. 'tomorrow 08:00'."""
    from datetime import timedelta
    hour, minute = hour_minute(text)
    for offset in range(8):
        day = now + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        slot = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if offset == 0 and (slot <= now or done_today):
            continue
        name = 'today' if offset == 0 else ('tomorrow' if offset == 1 else slot.strftime('%A'))
        return f'{name} {text}'
    return ''


SEND_BATCH_LIMIT = max(1, int(os.getenv('SEND_BATCH_LIMIT', '50') or 50))


def send_delay():
    return get_settings()['delay_seconds']


def send_batch(contact_ids):
    """Send approved contacts one by one in the background. Stops at the first uncertain result."""
    delay = send_delay()
    for contact_id in contact_ids:
        try:
            result = submit_contact(contact_id)
        except Exception:
            break
        if result == 'uncertain':
            break
        if result == 'submitted':
            time.sleep(delay)


def day_start_utc(now):
    return datetime.combine(now.date(), clock.min, TIMEZONE).astimezone(timezone.utc).isoformat()


def build_leads(c, now):
    """Every business the user may need to act on, as plain dicts for the Leads page."""
    today = now.date().isoformat()
    suppressed = {row['email'].lower() for row in c.execute('SELECT email FROM suppression')}
    sites = {row['email'].lower(): row for row in c.execute("SELECT * FROM prospects WHERE email != ''")}
    leads = []
    for r in c.execute("""SELECT * FROM contacts
            WHERE status IN ('draft','approved','sending','uncertain','skipped')
               OR id IN (SELECT id FROM contacts WHERE status='sent' ORDER BY id DESC LIMIT 200)
            ORDER BY id DESC"""):
        site = sites.get(r['email'].lower())
        lead = {
            'id': r['id'], 'company': r['company'], 'email': r['email'], 'status': r['status'],
            'service': (r['observation'] or '').split(' · ')[0] if site else (r['observation'] or '')[:60],
            'website': site['website'] if site else '', 'domain': site['domain'] if site else '',
            'email_source': site['email_source'] if site else '',
            'evidence': site['observation_source'] if site else '',
            'origin': 'found' if site else 'added', 'is_new': bool(site and (site['found_at'] or '')[:10] == today),
            'sent_at': r['sent_at'], 'note': '', 'selectable': False,
        }
        if r['status'] == 'draft':
            if is_law_firm(r['company'], r['email'], r['observation']):
                lead['note'] = 'Law firm, never emailed'
            elif DIRECTORY_TITLE.search(r['company']):
                lead['note'] = 'Looks like a directory page, not a business'
            elif not complete_message(r['subject'], r['body']):
                lead['note'] = 'Draft needs finishing'
            elif r['email'].lower() in suppressed:
                lead['note'] = 'On the opt-out list'
            else:
                lead['selectable'] = True
            lead['stage'] = 'ready' if lead['selectable'] else 'attention'
        elif r['status'] == 'skipped':
            lead['stage'] = 'attention'
            lead['note'] = r['error'] or 'Skipped, will not be emailed'
        elif r['status'] == 'sent':
            lead['stage'] = 'sent'
        else:
            lead['stage'] = 'queued'
        leads.append(lead)
    for p in c.execute("SELECT * FROM prospects WHERE email='' AND status != 'filtered' ORDER BY id DESC LIMIT 200"):
        if p['status'] == 'law_firm':
            note = 'Law firm, never emailed'
        elif p['status'] == 'contact_form':
            note = 'Contact form only, no public email'
        elif p['researched']:
            note = 'No public email found'
        else:
            note = 'Waiting to be checked'
        leads.append({
            'id': None, 'origin': 'found', 'company': p['company'], 'email': '', 'status': 'no_email', 'stage': 'attention',
            'service': '', 'website': p['website'], 'domain': p['domain'],
            'email_source': p['email_source'] if p['status'] == 'contact_form' else '', 'evidence': '',
            'is_new': (p['found_at'] or '')[:10] == today, 'sent_at': '', 'note': note, 'selectable': False,
        })
    order = {'ready': 0, 'queued': 1, 'sent': 2, 'attention': 3}
    leads.sort(key=lambda lead: order[lead['stage']])
    return leads


@app.get('/')
def home():
    from automatic_research import is_running

    now = datetime.now(TIMEZONE)
    today = now.date().isoformat()
    with conn() as c:
        leads = build_leads(c, now)
        sent_today = c.execute("SELECT count(*) FROM contacts WHERE status='sent' AND sent_at >= ?",
                               (day_start_utc(now),)).fetchone()[0]
        sent_total = c.execute("SELECT count(*) FROM contacts WHERE status='sent'").fetchone()[0]
        found = c.execute("""SELECT count(*), coalesce(sum(email != ''), 0) FROM prospects
                             WHERE substr(found_at,1,10)=?""", (today,)).fetchone()
        searches_today = c.execute('SELECT coalesce(sum(searches),0) FROM research_runs WHERE day=?',
                                   (today,)).fetchone()[0]
        latest = c.execute('SELECT * FROM research_runs ORDER BY id DESC LIMIT 1').fetchone()
    counts = {stage: sum(1 for lead in leads if lead['stage'] == stage)
              for stage in ('ready', 'queued', 'sent', 'attention')}
    counts['sent'] = sent_total
    cfg = get_settings()
    with conn() as c:
        by_market = sent_today_by_market(c, day_start_utc(now))
        scheduled_today = c.execute("SELECT 1 FROM research_runs WHERE day=? AND manual=0 AND region != 'United Kingdom'", (today,)).fetchone()
        stuck = c.execute("SELECT count(*) FROM contacts WHERE status='uncertain'").fetchone()[0]
    used_today = by_market['sa'] + by_market['uk']
    limit_total = cfg['emails_per_day'] + cfg['uk_emails_per_day']
    done_all = by_market['sa'] >= cfg['emails_per_day'] and by_market['uk'] >= cfg['uk_emails_per_day']
    next_sa = next_slot(now, cfg['send_time'], cfg['days'], done_today=by_market['sa'] >= cfg['emails_per_day'])
    next_uk = next_slot(now, cfg['uk_send_time'], cfg['days'], done_today=by_market['uk'] >= cfg['uk_emails_per_day'])
    autopilot = {
        'on': cfg['autopilot'], 'limit': limit_total, 'used_today': used_today,
        'limit_sa': cfg['emails_per_day'], 'limit_uk': cfg['uk_emails_per_day'],
        'used_sa': by_market['sa'], 'used_uk': by_market['uk'],
        'found_too': cfg['send_found'], 'searches_per_day': cfg['searches_per_day'] + cfg['uk_searches_per_day'],
        'next_send': next_sa if cfg['emails_per_day'] or not cfg['uk_emails_per_day'] else next_uk,
        'send_time': cfg['send_time'], 'uk_send_time': cfg['uk_send_time'], 'research_time': cfg['research_time'],
        'days': days_text(cfg['days']),
        'next_search': next_slot(now, cfg['research_time'], cfg['days'], done_today=bool(scheduled_today)),
        'stuck': stuck, 'done_all': done_all,
    }
    return studio_page(
        'dashboard.html', 'Leads', leads=leads, counts=counts, sent_today=sent_today,
        found_today=found[0], found_today_emails=found[1], searches_today=searches_today, latest=latest,
        running=is_running(), token=permission_token(), limit=SEND_BATCH_LIMIT,
        autopilot=autopilot, configured=bool(os.getenv('TAVILY_API_KEY', '').strip()),
    )


@app.get('/status.json')
def status_json():
    """Small live-status feed for the Leads page: credits come from Tavily (cached)."""
    from automatic_research import is_running

    now = datetime.now(TIMEZONE)
    with conn() as c:
        used_today = c.execute('SELECT coalesce(sum(searches),0) FROM research_runs WHERE day=?',
                               (now.date().isoformat(),)).fetchone()[0]
    cfg = get_settings()
    return {'running': is_running(), 'searches_today': used_today, 'daily_budget': cfg['searches_per_day'] + cfg['uk_searches_per_day'],
            'credits': credit_status()}


@app.post('/confirm-selected')
def confirm_selected():
    verify_permission_token()
    return queue_outreach()


@app.post('/research/send')
def research_send():
    verify_permission_token()
    return queue_outreach()


def queue_outreach():
    """Give permission for the selected drafts and, when asked, start sending them."""
    selected = {int(value) for value in request.form.getlist('ids') if value.isascii() and value.isdigit() and len(value) <= 18}
    send_now = request.form.get('send_now') == '1'
    cfg = get_settings()
    auto_send = cfg['autopilot']
    if not selected:
        flash('Select at least one business first.')
        return redirect(url_for('home'))
    if request.form.get('confirm_permission') != '1':
        flash('Tick the permission box to confirm these businesses can be emailed.')
        return redirect(url_for('home'))
    if send_now:
        if len(selected) > SEND_BATCH_LIMIT:
            flash(f'Select at most {SEND_BATCH_LIMIT} businesses to send at once.')
            return redirect(url_for('home'))
        try:
            smtp_config()
        except RuntimeError as exc:
            flash(str(exc))
            return redirect(url_for('home'))
    now = datetime.now(timezone.utc).isoformat()
    approved = []
    with conn() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("""SELECT * FROM contacts WHERE status='draft'
            AND NOT EXISTS (SELECT 1 FROM suppression WHERE email=contacts.email)
            ORDER BY id""")
        for row in rows.fetchall():
            if (row['id'] in selected and complete_message(row['subject'], row['body'])
                    and not DIRECTORY_TITLE.search(row['company'])
                    and not is_law_firm(row['company'], row['email'], row['observation'])):
                basis, evidence = confirmed_permission(row, now)
                db.execute("""UPDATE contacts SET status='approved', consent_basis=?,
                    consent_evidence=? WHERE id=?""", (basis, evidence, row['id']))
                approved.append(row['id'])
    if not approved:
        flash('Nothing was approved. Those drafts may already be handled or need finishing.')
        return redirect(url_for('home'))
    skipped = len(selected) - len(approved)
    extra = f' {skipped} skipped (already handled or incomplete).' if skipped else ''
    if send_now:
        Thread(target=send_batch, args=(approved,), daemon=True).start()
        flash(f'{len(approved)} approved. Sending now, about {send_delay():g}s apart.{extra}')
        return redirect(url_for('send'))
    if auto_send:
        flash(f'{len(approved)} approved. They go out at the next {cfg["send_time"]} send, up to {cfg["emails_per_day"] + cfg["uk_emails_per_day"]} a day.{extra}')
        return redirect(url_for('home'))
    flash(f'{len(approved)} approved and waiting on the Sending page.{extra}')
    return redirect(url_for('home'))


@app.post('/skip')
def skip_selected():
    """Take drafts out of the queue so autopilot never sends them."""
    verify_permission_token()
    selected = [int(v) for v in request.form.getlist('ids') if v.isascii() and v.isdigit() and len(v) <= 18]
    if not selected:
        flash('Select at least one business first.')
        return redirect(url_for('home'))
    with conn() as db:
        marks = ','.join('?' * len(selected))
        n = db.execute(f"UPDATE contacts SET status='skipped' WHERE status IN ('draft','approved') AND id IN ({marks})",
                       selected).rowcount
    flash(f'Skipped {n}. They will not be emailed.')
    return redirect(url_for('home'))


@app.route('/research', methods=['GET'])
def research():
    return redirect(url_for('home'))


@app.post('/research/automatic')
def research_automatic():
    from automatic_research import is_running, start_search

    if not os.getenv('TAVILY_API_KEY', '').strip():
        flash('Add your Tavily API key to .env to search for businesses.')
    elif is_running():
        flash('A search is already running.')
    else:
        start_search()
        time.sleep(0.3)
        flash('Searching now. New businesses appear here as soon as their websites are checked.')
    return redirect(url_for('home'))


def save_prospects(leads, market=''):
    added_ids = []
    found_at = datetime.now(TIMEZONE).isoformat(timespec='seconds')
    with conn() as c:
        for lead in leads:
            cur = c.execute(
                'INSERT OR IGNORE INTO prospects(domain,company,website,snippet,source_url,found_at,market) VALUES(?,?,?,?,?,?,?)',
                (lead['domain'], lead['company'], lead['url'], lead['snippet'], lead['source_url'], found_at, market),
            )
            if cur.rowcount:
                added_ids.append(cur.lastrowid)
    return len(added_ids), added_ids


def prepare_website_drafts():
    services = (
        'laser cutting', 'metal fabrication', 'precision machining', 'glazing',
        'signage', 'commercial printing', 'joinery', 'shopfitting', 'refrigeration',
        'air conditioning', 'packaging', 'plumbing', 'electrical contracting',
    )
    count = 0
    with conn() as db:
        rows = db.execute("""
            SELECT * FROM prospects
            WHERE email != '' AND drafted=0 AND observation_source != ''
        """).fetchall()
        for row in rows:
            observation = row['observation'].lower()
            service = next((service for service in services
                            if re.search(r'\b' + re.escape(service) + r'\b', observation)), None)
            company = ' '.join(row['company'].split())
            if business_taken(db, row['email']):
                db.execute('UPDATE prospects SET drafted=1 WHERE id=?', (row['id'],))
                continue
            if DIRECTORY_TITLE.search(company) or is_law_firm(company, row['email'], row['observation'], row['domain']):
                db.execute('UPDATE prospects SET drafted=1 WHERE id=?', (row['id'],))
                continue
            observation = f'{service} · {row["observation_source"]}' if service else ''
            contact = {'company': company, 'contact_name': '', 'observation': observation}
            template = BODY if observation else BODY.replace('I saw {{observation}}. Nice work.\n\n', '')
            subject, body = draft_text(SUBJECT, contact), draft_text(template, contact)
            result = db.execute("""
                INSERT OR IGNORE INTO contacts(company,email,observation,subject,body)
                SELECT ?,?,?,?,?
                WHERE NOT EXISTS (SELECT 1 FROM suppression WHERE email=? COLLATE NOCASE)
            """, (company, row['email'], observation,
                  subject, body, row['email']))
            db.execute('UPDATE prospects SET drafted=1 WHERE id=?', (row['id'],))
            count += result.rowcount
    return count


def enrich_prospects(prospect_ids, *, retry=False):
    """Check company websites for a published email. Eight sites are checked at a time."""
    condition = "researched=1 AND email='' AND status='discovered'" if retry else 'researched=0'

    def check(prospect_id):
        with conn() as c:
            row = c.execute(f'SELECT website, source_url, snippet, market FROM prospects WHERE id=? AND {condition}', (prospect_id,)).fetchone()
        if row is None:
            return 0, 0
        try:
            info = inspect_company_site(row['website'], extra_urls=[row['source_url']], snippet=row['snippet'] or '',
                                        uk_only=row['market'] == 'uk')
        except Exception:
            info = {'email': '', 'observation': '', 'source': '', 'observation_source': '', 'status': 'email_not_found'}
        with conn() as c:
            result = c.execute(
                f'''UPDATE prospects SET researched=1,email=?,observation=?,email_source=?,observation_source=?,status=?
                    WHERE id=? AND {condition}''',
                (info['email'], info['observation'], info['source'], info.get('observation_source', ''),
                 info.get('status', 'email_not_found'), prospect_id),
            )
        return result.rowcount, result.rowcount if info['email'] else 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check, prospect_ids))
    return sum(r[0] for r in results), sum(r[1] for r in results)

@app.post('/research/find')
def research_find():
    from automatic_research import is_running, start_custom_search

    industry=(request.form.get('industry') or '').strip()[:90]
    location=(request.form.get('location') or '').strip()[:90]
    if not industry or not location: abort(400)
    try:target=max(1,min(int(request.form.get('target','40')),60))
    except ValueError:target=40
    if not os.getenv('TAVILY_API_KEY', '').strip():
        flash('Add your Tavily API key to .env to search for businesses.')
    elif is_running():
        flash('A search is already running.')
    else:
        start_custom_search(industry, location, target)
        time.sleep(0.3)
        flash(f'Searching for {industry} in {location}. New businesses appear on Leads as their websites are checked.')
    return redirect(url_for('home'))

@app.post('/research/enrich')
def research_enrich():
    retry = request.form.get('retry') == '1'
    condition = "researched=1 AND email='' AND status='discovered'" if retry else 'researched=0'
    with conn() as c:
        order = 'DESC' if retry else 'ASC'
        ids = [row['id'] for row in c.execute(f'SELECT id FROM prospects WHERE {condition} ORDER BY id {order} LIMIT ?', (3 if retry else 10,))]
    inspected, found = enrich_prospects(ids, retry=retry)
    flash(f'Checked {inspected} company websites and found {found} public business-domain email addresses.')
    return redirect(url_for('tools'))

@app.post('/research/draft')
def research_draft():
    count = prepare_website_drafts()
    flash(f'Prepared {count} drafts with your outreach template.')
    return redirect(url_for('home'))

@app.post('/research/uk')
def research_uk():
    from automatic_research import is_running, start_uk_search

    if not os.getenv('TAVILY_API_KEY', '').strip():
        flash('Add your Tavily API key to .env to search for businesses.')
    elif is_running():
        flash('A search is already running. Try again when it finishes.')
    else:
        try:
            target = max(10, min(int(request.form.get('target', '200')), 500))
        except ValueError:
            target = 200
        start_uk_search(target)
        time.sleep(0.3)
        flash(f'Looking for {target} UK businesses with a published company email. They appear on Leads as they are found and join the send queue.')
    return redirect(url_for('home'))


@app.post('/research/recheck')
def research_recheck():
    from automatic_research import is_running, start_recheck

    if is_running():
        flash('A search is already running. Try again when it finishes.')
    else:
        with conn() as c:
            n = c.execute("UPDATE prospects SET researched=0 WHERE email='' AND status IN ('email_not_found','contact_form','discovered')").rowcount
        start_recheck()
        time.sleep(0.3)
        flash(f'Re-checking {n} websites for a published email. New drafts appear on Leads as they are found.')
    return redirect(url_for('home'))


@app.post('/autopilot')
def autopilot_switch():
    verify_permission_token()
    on = request.form.get('state') == 'on'
    save_settings({'autopilot': '1' if on else '0'})
    flash('Autopilot is on. It will search and send on your schedule.' if on else 'Autopilot is off. Nothing will be searched or sent automatically.')
    return redirect(request.form.get('next') if request.form.get('next', '').startswith('/') else url_for('home'))


@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        verify_permission_token()
        form = request.form
        problems = []
        for key, label in (('research_time', 'Search time'), ('send_time', 'Send time'), ('uk_send_time', 'UK send time')):
            if not TIME_RE.match(form.get(key, '')):
                problems.append(f'{label} must look like 08:00.')
        days = sorted({int(d) for d in form.getlist('days') if d.isdigit() and 0 <= int(d) <= 6})
        if not days:
            problems.append('Pick at least one day.')
        numbers = {}
        for key, label, high in (('searches_per_day', 'South Africa searches per day', 500),
                                 ('emails_per_day', 'South Africa emails per day', 1000),
                                 ('uk_searches_per_day', 'UK searches per day', 500), ('uk_emails_per_day', 'UK emails per day', 1000),
                                 ('delay_seconds', 'Pause between emails', 300)):
            try:
                value = int(form.get(key, ''))
                if not 0 <= value <= high:
                    raise ValueError
                numbers[key] = value
            except ValueError:
                problems.append(f'{label} must be a number from 0 to {high}.')
        if problems:
            flash(' '.join(problems))
            return redirect(url_for('settings_page'))
        save_settings({
            'autopilot': '1' if form.get('autopilot') == '1' else '0',
            'research_time': form['research_time'], 'send_time': form['send_time'], 'uk_send_time': form['uk_send_time'],
            'days': ','.join(map(str, days)), 'send_found': '1' if form.get('send_found') == '1' else '0', **numbers,
        })
        flash('Settings saved. They apply straight away.')
        return redirect(url_for('settings_page'))
    from automatic_research import INDUSTRIES, REGIONS

    return studio_page('settings.html', 'Settings', cfg=get_settings(), token=permission_token(), day_names=DAY_NAMES,
                       industries=INDUSTRIES, regions=REGIONS, configured=bool(os.getenv('TAVILY_API_KEY', '').strip()))


@app.get('/tools')
def tools():
    with conn() as c:
        runs = c.execute('SELECT * FROM research_runs ORDER BY id DESC LIMIT 12').fetchall()
        no_email = c.execute("SELECT count(*) FROM prospects WHERE email=''").fetchone()[0]
    return studio_page(
        'tools.html', 'Tools', runs=runs, subject=SUBJECT, template=BODY, no_email=no_email,
        configured=bool(os.getenv('TAVILY_API_KEY', '').strip()),
    )

@app.get('/sample.csv')
def sample():
    out=io.StringIO();w=csv.writer(out);w.writerow(['company','email','contact_name','observation','market'])
    w.writerow(['Example Engineering','example@example.com','Sam','you manufacture precision components','uk'])
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=nextleaf_contacts_sample.csv'})

@app.post('/import')
def upload():
    file=request.files.get('file')
    if not file:abort(400)
    try:reader=csv.DictReader(io.StringIO(file.read().decode('utf-8-sig')))
    except UnicodeDecodeError:abort(400,'CSV must be UTF-8')
    if not reader.fieldnames or not {'company','email'}.issubset(set(reader.fieldnames)):abort(400,'CSV needs company and email')
    ok=0;skipped=0;law_firms=0;same_business=0;uk_added=0
    with conn() as c:
        for r in reader:
            email=(r.get('email') or '').strip().lower()
            company=(r.get('company') or '').strip()
            if is_law_firm(company, email, r.get('observation') or ''):law_firms+=1;continue
            if EMAIL_RE.fullmatch(email) and business_taken(c, email):same_business+=1;continue
            if not EMAIL_RE.fullmatch(email) or not company or c.execute('SELECT 1 FROM suppression WHERE email=?',(email,)).fetchone():skipped+=1;continue
            vals=[company,email,(r.get('contact_name') or '').strip(),(r.get('observation') or '').strip(),(r.get('consent_basis') or '').strip(),(r.get('consent_evidence') or '').strip(),
                  'uk' if (r.get('market') or '').strip().lower() == 'uk' else '']
            cur=c.execute('INSERT OR IGNORE INTO contacts(company,email,contact_name,observation,consent_basis,consent_evidence,market) VALUES(?,?,?,?,?,?,?)',vals)
            if cur.rowcount:
                subject, body = draft_contact({'company': company, 'contact_name': vals[2], 'observation': vals[3]})
                c.execute('UPDATE contacts SET subject=?, body=? WHERE id=?', (subject, body, cur.lastrowid))
            ok+=cur.rowcount;skipped+=not cur.rowcount
            uk_added+=cur.rowcount and vals[6] == 'uk'
    cfg = get_settings()
    message = f'Imported {ok} contacts with drafts ready; skipped {skipped} duplicates/invalid/suppressed.'
    if law_firms:
        message += f' Left out {law_firms} law firms, which are never emailed.'
    if same_business:
        message += f' Left out {same_business} more addresses at businesses already on the list (one email per business).'
    if ok and cfg['autopilot']:
        now = datetime.now(TIMEZONE)
        sa_added = ok - uk_added
        parts = []
        if sa_added:
            days = -(-sa_added // max(1, cfg['emails_per_day'])) if cfg['emails_per_day'] else 0
            parts.append(f'{sa_added} South African from {next_slot(now, cfg["send_time"], cfg["days"])}, up to {cfg["emails_per_day"]} a day'
                         + (f' (about {days} sending days)' if days > 1 else ''))
        if uk_added:
            days = -(-uk_added // max(1, cfg['uk_emails_per_day'])) if cfg['uk_emails_per_day'] else 0
            parts.append(f'{uk_added} UK from {next_slot(now, cfg["uk_send_time"], cfg["days"])}, up to {cfg["uk_emails_per_day"]} a day'
                         + (f' (about {days} sending days)' if days > 1 else ''))
        message += ' Autopilot emails them: ' + '; '.join(parts) + '. Change the daily limits in Settings to go faster or slower.'
    elif ok:
        message += ' Autopilot is off, so send them from Leads.'
    flash(message)
    return redirect('/')

@app.post('/generate')
def generate():
    subject=(request.form.get('subject') or '').strip();body=(request.form.get('body') or '').strip()
    if not subject or not body:abort(400)
    with conn() as c:
        rows=c.execute("SELECT * FROM contacts WHERE status='draft'").fetchall()
        for r in rows:c.execute('UPDATE contacts SET subject=?,body=? WHERE id=?',(draft_text(subject,r),draft_text(body,r),r['id']))
    flash(f'Updated {len(rows)} drafts with the new template.')
    return redirect(url_for('tools'))

@app.route('/contact/<int:cid>',methods=['GET','POST'])
def contact(cid):
    if request.method == 'POST' and request.form.get('action') == 'send-now':
        verify_permission_token()
        subject = (request.form.get('subject') or '').strip()
        body = (request.form.get('body') or '').strip()
        if not complete_message(subject, body):
            flash('Complete the subject, body and sender details before sending.')
            return redirect(url_for('contact', cid=cid))
        try:
            smtp_config()
        except RuntimeError as exc:
            flash(str(exc))
            return redirect(url_for('contact', cid=cid))
        result = submit_contact(cid, confirmed_draft=(subject, body))
        flash({
            'submitted': 'Email accepted by HMail Plus.',
            'uncertain': 'HMail Plus did not confirm the submission. Check the mailbox before retrying.',
            'skipped': 'This draft is no longer available to send.',
        }[result])
        return redirect(url_for('contact', cid=cid))
    with conn() as c:
        if request.method == 'POST':
            c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM contacts WHERE id=?',(cid,)).fetchone()
        if not r:abort(404)
        if request.method=='POST':
            action=request.form.get('action')
            if action=='suppress':
                c.execute('INSERT OR REPLACE INTO suppression(email,reason,created_at) VALUES(?,?,?)',(r['email'],'manual unsubscribe',datetime.now(timezone.utc).isoformat()))
                c.execute("UPDATE contacts SET status='suppressed' WHERE id=?",(cid,));flash('Email suppressed permanently.');return redirect('/')
            if r['status'] in ('sent','suppressed','sending','uncertain'):abort(400,'This contact cannot be edited while sending or after an uncertain submission.')
            subject=(request.form.get('subject') or '').strip();body=(request.form.get('body') or '').strip()
            basis=(request.form.get('consent_basis') or '').strip();evidence=(request.form.get('consent_evidence') or '').strip()
            if action=='approve':
                if basis not in ('consent','existing_customer') or len(evidence)<8:abort(400,'Record valid permission and supporting evidence first')
                if not subject or not body or '[Your website / contact details]' in body:abort(400,'Complete the subject, body and your sender contact details before approving')
                status='approved'
            else:status='draft'
            c.execute('UPDATE contacts SET subject=?,body=?,consent_basis=?,consent_evidence=?,status=? WHERE id=?',(subject,body,basis,evidence,status,cid))
            flash('Saved '+status);return redirect('/')
    preview, _ = render_email_body(r['body'] or '', url_for('studio_logo'))
    return studio_page('contact.html', 'Review draft', r=r, token=permission_token(),
                       email_preview=Markup(preview))


def smtp_config():
    user=os.getenv('SMTP_USER','').strip();password=os.getenv('SMTP_PASSWORD','')
    host=os.getenv('SMTP_HOST','smtp.hmailplus.com');port=int(os.getenv('SMTP_PORT','465'))
    if not user or not password:raise RuntimeError('Set SMTP_USER and SMTP_PASSWORD in .env')
    if port not in (465,587):raise RuntimeError('Port must be 465 or 587')
    return user,password,host,port

def render_email_body(body, logo_src):
    paragraphs = []
    has_logo = False
    for paragraph in body.replace('\r\n', '\n').split('\n\n'):
        if '\n'.join(line.strip() for line in paragraph.strip().splitlines()) == AARON_SIGNATURE:
            has_logo = True
            paragraphs.append(
                '<p>Aaron<br><a href="https://nextleaf.co.za/">'
                f'<img src="{escape(logo_src)}" width="170" '
                'alt="Nextleaf Digital" style="display:block;height:auto;margin:6px 0;"></a>'
                '+27 71 886 9647 | <a href="https://nextleaf.co.za/">nextleaf.co.za</a></p>'
            )
            continue
        content = escape(paragraph).replace('\n', '<br>')
        content = content.replace('info@nextleaf.co.za', '<a href="mailto:info@nextleaf.co.za">info@nextleaf.co.za</a>')
        content = re.sub(
            rf'{re.escape(DEMO_URL)}|{re.escape(LEGACY_DEMO_URL)}|https://nextleaf\.co\.za/(?![\w/#?])',
            lambda link: f'<a href="{link[0]}">'
            f'{"Nextleaf workflow demo" if link[0] in (DEMO_URL, LEGACY_DEMO_URL) else "nextleaf.co.za"}</a>',
            content,
        )
        paragraphs.append(f'<p>{content}</p>')
    return ''.join(paragraphs), has_logo


def build_message(user, to, subject, body):
    msg=EmailMessage();msg['From']=user;msg['To']=to;msg['Subject']=subject
    msg['Date'] = format_datetime(datetime.now(timezone.utc))
    msg['Message-ID'] = make_msgid(domain=user.rsplit('@', 1)[-1])
    msg['Reply-To']=user;msg['List-Unsubscribe']=f'<mailto:{user}?subject=unsubscribe>'
    msg.set_content(body)
    logo_cid = make_msgid(domain='nextleaf.co.za')
    html, has_logo = render_email_body(body, f'cid:{logo_cid[1:-1]}')
    msg.add_alternative('<html><body>' + html + '</body></html>', subtype='html')
    if has_logo:
        msg.get_body(preferencelist=('html',)).add_related(
            (BASE / 'Nextleaf-Digital-email-logo.png').read_bytes(),
            maintype='image', subtype='png', cid=logo_cid,
        )
    return msg


def save_sent_copy(user, password, message, context):
    with imaplib.IMAP4_SSL('imap.hmailplus.com', 993, ssl_context=context, timeout=10) as mailbox:
        mailbox.login(user, password)
        status, _ = mailbox.append('Sent', r'(\Seen)', None, message)
        if status != 'OK':
            raise imaplib.IMAP4.error('Sent folder rejected the message copy')


def send_message(to,subject,body):
    user,password,host,port=smtp_config()
    msg = build_message(user, to, subject, body)
    message = msg.as_bytes(policy=msg.policy.clone(linesep='\r\n'))
    context = ssl.create_default_context()
    smtp = (smtplib.SMTP_SSL(host, port, context=context, timeout=20) if port == 465
            else smtplib.SMTP(host, port, timeout=20))
    try:
        if port == 587:
            smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.sendmail(user, [to], message)
    finally:
        try:
            smtp.quit()
        except (OSError, smtplib.SMTPException):
            smtp.close()

    try:
        save_sent_copy(user, password, message, context)
    except (OSError, imaplib.IMAP4.error) as exc:
        return f'HMail Plus accepted the email, but saving a copy to Sent could not be confirmed ({type(exc).__name__}).'
    return None

def submit_contact(contact_id, *, scheduled=False, confirmed_draft=None, auto_evidence=None,
                   basis='published_business_email'):
    """Reserve a recipient before SMTP so simultaneous or interrupted runs cannot resend."""
    now = datetime.now(timezone.utc).isoformat()
    with conn() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("""
            SELECT * FROM contacts
            WHERE id=? AND (status='approved' OR (? AND status='draft'))
              AND (? OR (consent_basis IN ('consent','existing_customer')
                   AND length(trim(consent_evidence)) >= 8
                   AND length(trim(subject)) > 0 AND length(trim(body)) > 0))
              AND NOT EXISTS (SELECT 1 FROM suppression WHERE email=contacts.email)
        """, (contact_id, scheduled or confirmed_draft is not None,
              confirmed_draft is not None or auto_evidence is not None)).fetchone()
        if row is not None and is_law_firm(row['company'], row['email'], row['observation']):
            db.execute("UPDATE contacts SET status='skipped' WHERE id=?", (contact_id,))
            return 'skipped'
        if row is not None and business_taken(db, row['email'], statuses=('sent', 'sending', 'uncertain')):
            db.execute("UPDATE contacts SET status='skipped', error='Already emailed another address at this business' WHERE id=?", (contact_id,))
            return 'skipped'
        if row is None or (scheduled and DIRECTORY_TITLE.search(row['company'])):
            return 'skipped'
        if confirmed_draft is None and not complete_message(row['subject'], row['body']):
            return 'skipped'
        if confirmed_draft is not None:
            subject, body = confirmed_draft
            basis, evidence = confirmed_permission(row, now)
            db.execute("""UPDATE contacts SET status='sending', subject=?, body=?,
                consent_basis=?, consent_evidence=? WHERE id=?""",
                (subject, body, basis, evidence, contact_id))
        elif auto_evidence is not None:
            # Autopilot: recorded honestly as a published business email, not as given consent.
            subject, body = row['subject'], row['body']
            db.execute("UPDATE contacts SET status='sending', consent_basis=?, consent_evidence=? WHERE id=?",
                       (basis, auto_evidence, contact_id))
        else:
            subject, body = row['subject'], row['body']
            db.execute("UPDATE contacts SET status='sending' WHERE id=?", (contact_id,))
        attempt = db.execute("""
            INSERT INTO send_log(email,sent_at,result,detail) VALUES(?,?,'sending','Reserved before SMTP')
        """, (row['email'], now)).lastrowid

    try:
        archive_warning = send_message(row['email'], subject, body)
    except Exception as exc:
        result, status = 'uncertain', 'uncertain'
        detail = f'{type(exc).__name__}. Reconcile with the mailbox before attempting again.'
    else:
        result, status = 'submitted', 'sent'
        detail = archive_warning or 'SMTP accepted and copy saved to Sent'

    with conn() as db:
        db.execute("UPDATE send_log SET result=?, detail=? WHERE id=?", (result, detail, attempt))
        db.execute("""
            UPDATE contacts SET status=?, sent_at=?, error=? WHERE id=? AND status='sending'
        """, (status, now if status == 'sent' else '',
              detail if status == 'uncertain' else (archive_warning or ''), contact_id))
    return result


@app.route('/test',methods=['GET','POST'])
def test():
    if request.method=='POST':
        to=(request.form.get('email') or '').strip()
        if not EMAIL_RE.fullmatch(to):abort(400)
        try:archive_warning = send_message(to,'Nextleaf SMTP test','Your HMail Plus SMTP connection is working. No marketing emails were sent.')
        except Exception as e:flash('Test failed: '+str(e))
        else:flash(archive_warning or 'Test email accepted and copied to Sent. Check your inbox and spam folder.')
        return redirect('/test')
    return studio_page('test-email.html', 'Test email')

@app.route('/send',methods=['GET','POST'])
def send():
    auto_send = get_settings()['autopilot']
    if request.method=='POST':
        verify_permission_token()
        requested=[int(x) for x in request.form.getlist('ids') if x.isascii() and x.isdigit() and len(x) <= 18]
        if not requested:flash('Select at least one email.');return redirect(url_for('send'))
        if len(requested) > SEND_BATCH_LIMIT:
            flash(f'Select at most {SEND_BATCH_LIMIT} emails to send at once.');return redirect(url_for('send'))
        try:smtp_config()
        except Exception as e:flash(str(e));return redirect(url_for('send'))
        Thread(target=send_batch, args=(requested,), daemon=True).start()
        flash(f'Sending {len(requested)} emails, about {send_delay():g}s apart. This page updates by itself.')
        return redirect(url_for('send'))
    with conn() as c:
        rows=c.execute("SELECT * FROM contacts WHERE status IN ('approved','sending','uncertain') AND email NOT IN (SELECT email FROM suppression) ORDER BY id").fetchall()
        recent=c.execute("SELECT * FROM contacts WHERE status='sent' ORDER BY sent_at DESC LIMIT 15").fetchall()
    return studio_page('sending.html', 'Sending', rows=rows, recent=recent, auto_send=auto_send,
                       token=permission_token(), limit=SEND_BATCH_LIMIT,
                       busy=any(r['status']=='sending' for r in rows))

@app.route('/suppression',methods=['GET','POST'])
def suppression():
    if request.method=='POST':
        email=(request.form.get('email') or '').strip().lower()
        if not EMAIL_RE.fullmatch(email):abort(400)
        with conn() as c:
            c.execute('INSERT OR REPLACE INTO suppression(email,reason,created_at) VALUES(?,?,?)',(email,(request.form.get('reason') or 'opt-out').strip(),datetime.now(timezone.utc).isoformat()))
            c.execute("UPDATE contacts SET status='suppressed' WHERE email=? AND status!='sent'",(email,))
        flash('Address added to suppression list.');return redirect('/suppression')
    with conn() as c:rows=c.execute('SELECT * FROM suppression ORDER BY created_at DESC').fetchall()
    return studio_page('opt-outs.html', 'Opt-outs', rows=rows)

@app.get('/export')
def export():
    with conn() as c:rows=c.execute('SELECT company,email,contact_name,observation,consent_basis,consent_evidence,status,subject,body,sent_at,error FROM contacts ORDER BY id').fetchall()
    out=io.StringIO();w=csv.writer(out);w.writerow(['company','email','contact_name','observation','consent_basis','consent_evidence','status','subject','body','sent_at','error'])
    for r in rows:w.writerow(list(r))
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=nextleaf_outreach_export.csv'})

if __name__=='__main__':
    init_db();app.run(host='127.0.0.1',port=5050,debug=False)
else:init_db()
