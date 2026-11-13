import csv, imaplib, io, os, re, sqlite3, ssl, smtplib, time
from secrets import compare_digest, token_urlsafe
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from html import escape
from pathlib import Path
from flask import Flask, request, redirect, url_for, flash, Response, abort, session, render_template, send_file
from markupsafe import Markup
from discovery import find_companies, inspect_company_site, search_allowance

BASE = Path(__file__).resolve().parent
DEMO_URL = 'https://nextleaf.co.za/demo'
LEGACY_DEMO_URL = 'https://nextleaf.co.za/#work'
DIRECTORY_TITLE = re.compile(r'\b(?:top\s+\d+|business directory|company directory|list of companies)\b', re.I)
AARON_SIGNATURE = 'Aaron\nNextleaf Digital\n+27 71 886 9647 | nextleaf.co.za'
DB = Path(os.getenv('OUTREACH_DB', BASE / 'outreach.db'))
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'local-only-change-this-before-deploying')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024

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
        c.execute("""CREATE TABLE IF NOT EXISTS research_runs (
            id INTEGER PRIMARY KEY, day TEXT NOT NULL, manual INTEGER NOT NULL,
            started_at TEXT NOT NULL, region TEXT NOT NULL, industry TEXT NOT NULL,
            status TEXT NOT NULL, found INTEGER DEFAULT 0, added INTEGER DEFAULT 0,
            inspected INTEGER DEFAULT 0, emails INTEGER DEFAULT 0
        )""")

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

SUBJECT='A system built around how {{company}} works'
BODY='''Hi {{name}},

I saw {{observation}}. Nice work.

Most workshops run on quotes in one place, clients in another, prices in a spreadsheet and job updates on WhatsApp. It works, but it eats time.

We build custom systems that bring it all together, designed around how your business already works, not the other way round. Your prices and clients are loaded in, so quotes take minutes. Jobs, team and sales sit on one screen, so you can see what's going on without chasing anyone.

Have a click around an example: ''' + DEMO_URL + '''

Yours would be built for {{company}}. Tell me how you currently quote and run jobs, and I'll show you what it could look like, free.

''' + AARON_SIGNATURE + '''

Not relevant? Reply "no" and I won't email again.'''

@app.get('/')
def home():
    with conn() as c:
        contacts=c.execute("""SELECT * FROM contacts
            WHERE status IN ('draft','approved')
               OR id IN (SELECT id FROM contacts ORDER BY id DESC LIMIT 300)
            ORDER BY id DESC""").fetchall()
        counts=dict(c.execute('SELECT status, count(*) n FROM contacts GROUP BY status').fetchall())
        suppressed_emails = {row['email'].lower() for row in c.execute('SELECT email FROM suppression')}
    selectable = {
        r['id'] for r in contacts
        if r['status'] == 'draft' and complete_message(r['subject'], r['body'])
        and not DIRECTORY_TITLE.search(r['company'])
        and r['email'].lower() not in suppressed_emails
    }
    return studio_page(
        'dashboard.html', 'Overview', contacts=contacts, total=sum(counts.values()),
        drafts=counts.get('draft', 0), approved=counts.get('approved', 0), sent=counts.get('sent', 0),
        subject=SUBJECT, template=BODY, auto_send=os.getenv('AUTO_SEND', '0') == '1',
        schedule=os.getenv('RESEARCH_TIME', '09:00'), selectable=selectable, token=permission_token(),
    )


@app.post('/confirm-selected')
def confirm_selected():
    verify_permission_token()
    return queue_outreach()


@app.post('/research/send')
def research_send():
    verify_permission_token()
    return queue_outreach(from_research=True)


def queue_outreach(*, from_research=False):
    destination = url_for('research' if from_research else 'home')
    selected = {int(value) for value in request.form.getlist('ids') if value.isascii() and value.isdigit() and len(value) <= 18}
    if not selected:
        flash('Select at least one draft.')
        return redirect(destination)
    now = datetime.now(timezone.utc).isoformat()
    confirmed = 0
    with conn() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("""SELECT * FROM contacts WHERE status='draft'
            AND NOT EXISTS (SELECT 1 FROM suppression WHERE email=contacts.email)
            AND (? = 0 OR EXISTS (SELECT 1 FROM prospects WHERE prospects.email=contacts.email COLLATE NOCASE))
            ORDER BY id""", (int(from_research),))
        for row in rows.fetchall():
            if (row['id'] in selected and complete_message(row['subject'], row['body'])
                    and not DIRECTORY_TITLE.search(row['company'])):
                basis, evidence = confirmed_permission(row, now)
                db.execute("""UPDATE contacts SET status='approved', consent_basis=?,
                    consent_evidence=? WHERE id=?""", (basis, evidence, row['id']))
                confirmed += 1
    if confirmed and os.getenv('AUTO_SEND', '0') == '1':
        flash(f'{confirmed} emails queued for sending.')
    else:
        flash(f'Permission confirmed for {confirmed} drafts.')
    return redirect(destination)


@app.route('/research', methods=['GET'])
def research():
    other = request.args.get('view') == 'other'
    condition = "p.email = ''" if other else "p.email != ''"
    visible = """FROM prospects p LEFT JOIN contacts c ON c.email=p.email COLLATE NOCASE
        WHERE COALESCE(c.status,'') NOT IN ('sent','suppressed')
        AND NOT EXISTS (SELECT 1 FROM suppression s WHERE s.email=p.email COLLATE NOCASE)"""
    with conn() as c:
        prospects=c.execute(f"""SELECT p.*,c.id AS contact_id,c.status AS contact_status,c.subject,c.body
            {visible} AND {condition}
            ORDER BY CASE WHEN c.status='draft' THEN 0 ELSE 1 END, c.id, p.id DESC LIMIT 300""").fetchall()
        count=c.execute(f'SELECT COUNT(*) {visible}').fetchone()[0]
        ready=c.execute(f"SELECT COUNT(*) {visible} AND p.email != ''").fetchone()[0]
        latest = c.execute('SELECT * FROM research_runs ORDER BY id DESC LIMIT 1').fetchone()
    selectable = {
        row['contact_id'] for row in prospects if row['contact_status'] == 'draft'
        and complete_message(row['subject'], row['body']) and not DIRECTORY_TITLE.search(row['company'])
    }
    return studio_page(
        'research.html', 'Research', prospects=prospects, count=count, ready=ready, latest=latest, other=other,
        selectable=selectable, token=permission_token(), auto_send=os.getenv('AUTO_SEND', '0') == '1',
        enabled=os.getenv('RESEARCH_AUTOMATION', '1') == '1',
        configured=bool(os.getenv('TAVILY_API_KEY', '').strip()),
        schedule=os.getenv('RESEARCH_TIME', '09:00'),
    )


@app.post('/research/automatic')
def research_automatic():
    from automatic_research import start_search

    if os.getenv('TAVILY_API_KEY', '').strip():
        start_search()
        flash('Search requested. Refresh this page to see the run and its results.')
    return redirect('/research')


def save_prospects(leads):
    added_ids = []
    with conn() as c:
        for lead in leads:
            cur = c.execute(
                'INSERT OR IGNORE INTO prospects(domain,company,website,snippet,source_url) VALUES(?,?,?,?,?)',
                (lead['domain'], lead['company'], lead['url'], lead['snippet'], lead['source_url']),
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
            if DIRECTORY_TITLE.search(company):
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
    found = inspected = 0
    condition = "researched=1 AND email='' AND status='discovered'" if retry else 'researched=0'
    for prospect_id in prospect_ids:
        with conn() as c:
            row = c.execute(f'SELECT website FROM prospects WHERE id=? AND {condition}', (prospect_id,)).fetchone()
        if row is None:
            continue
        info = inspect_company_site(row['website'])
        with conn() as c:
            result = c.execute(
                f'''UPDATE prospects SET researched=1,email=?,observation=?,email_source=?,observation_source=?,status=?
                    WHERE id=? AND {condition}''',
                (info['email'], info['observation'], info['source'], info.get('observation_source', ''),
                 info.get('status', 'email_not_found'), prospect_id),
            )
        inspected += result.rowcount
        found += result.rowcount if info['email'] else 0
    return inspected, found

@app.post('/research/find')
def research_find():
    industry=(request.form.get('industry') or '').strip()[:90]
    location=(request.form.get('location') or '').strip()[:90]
    if not industry or not location: abort(400)
    try:target=max(1,min(int(request.form.get('target','40')),60))
    except ValueError:target=40
    try:
        with search_allowance() as remaining:
            found = find_companies(industry, location, target, max_searches=min(3, remaining))
    except Exception as e:flash('Search could not complete: '+str(e)[:200]);return redirect('/research')
    added, _ = save_prospects(found)
    flash(f'Found {len(found)} results; added {added} new company sites. Click Inspect next 10 websites to find public company emails.')
    return redirect('/research')

@app.post('/research/enrich')
def research_enrich():
    retry = request.form.get('retry') == '1'
    condition = "researched=1 AND email='' AND status='discovered'" if retry else 'researched=0'
    with conn() as c:
        order = 'DESC' if retry else 'ASC'
        ids = [row['id'] for row in c.execute(f'SELECT id FROM prospects WHERE {condition} ORDER BY id {order} LIMIT ?', (3 if retry else 10,))]
    inspected, found = enrich_prospects(ids, retry=retry)
    flash(f'Researched {inspected} company websites and found {found} public business-domain email addresses.')
    return redirect(url_for('research', view='other')) if retry else redirect('/research')

@app.post('/research/draft')
def research_draft():
    count = prepare_website_drafts()
    flash(f'Prepared {count} drafts with your outreach template.')
    return redirect('/research')

@app.get('/sample.csv')
def sample():
    out=io.StringIO();w=csv.writer(out);w.writerow(['company','email','contact_name','observation','consent_basis','consent_evidence'])
    w.writerow(['Example Engineering','example@example.com','Sam','you manufacture precision components','consent','Opt-in form dated YYYY-MM-DD'])
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=nextleaf_contacts_sample.csv'})

@app.post('/import')
def upload():
    file=request.files.get('file')
    if not file:abort(400)
    try:reader=csv.DictReader(io.StringIO(file.read().decode('utf-8-sig')))
    except UnicodeDecodeError:abort(400,'CSV must be UTF-8')
    if not reader.fieldnames or not {'company','email'}.issubset(set(reader.fieldnames)):abort(400,'CSV needs company and email')
    ok=0;skipped=0
    with conn() as c:
        for r in reader:
            email=(r.get('email') or '').strip().lower()
            company=(r.get('company') or '').strip()
            if not EMAIL_RE.fullmatch(email) or not company or c.execute('SELECT 1 FROM suppression WHERE email=?',(email,)).fetchone():skipped+=1;continue
            vals=[company,email,(r.get('contact_name') or '').strip(),(r.get('observation') or '').strip(),(r.get('consent_basis') or '').strip(),(r.get('consent_evidence') or '').strip()]
            cur=c.execute('INSERT OR IGNORE INTO contacts(company,email,contact_name,observation,consent_basis,consent_evidence) VALUES(?,?,?,?,?,?)',vals)
            ok+=cur.rowcount;skipped+=not cur.rowcount
    flash(f'Imported {ok} contacts; skipped {skipped} duplicates/invalid/suppressed.')
    return redirect('/')

@app.post('/generate')
def generate():
    subject=(request.form.get('subject') or '').strip();body=(request.form.get('body') or '').strip()
    if not subject or not body:abort(400)
    with conn() as c:
        rows=c.execute("SELECT * FROM contacts WHERE status='draft'").fetchall()
        for r in rows:c.execute('UPDATE contacts SET subject=?,body=? WHERE id=?',(draft_text(subject,r),draft_text(body,r),r['id']))
    flash(f'Created/updated {len(rows)} drafts. Review individually before approving.')
    return redirect('/')

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

def submit_contact(contact_id, *, scheduled=False, confirmed_draft=None):
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
        """, (contact_id, scheduled or confirmed_draft is not None, confirmed_draft is not None)).fetchone()
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
    auto_send = os.getenv('AUTO_SEND', '0') == '1'
    with conn() as c:
        rows=c.execute("SELECT * FROM contacts WHERE status IN ('approved','sending','uncertain') AND email NOT IN (SELECT email FROM suppression) ORDER BY id").fetchall()
    if request.method=='POST':
        requested=[int(x) for x in request.form.getlist('ids') if x.isdigit()][:20]
        if not requested:flash('Select at least one email.');return redirect('/send')
        try:smtp_config()
        except Exception as e:flash(str(e));return redirect('/send')
        sent=failed=0
        for cid in requested:
            result = submit_contact(cid)
            if result == 'submitted':
                sent += 1
                time.sleep(max(0, min(float(os.getenv('SEND_DELAY_SECONDS', '3')), 60)))
            elif result == 'uncertain':
                failed += 1
                break
        flash(f'SMTP accepted {sent} emails; {failed} failed. A successful SMTP submission does not guarantee inbox delivery.')
        return redirect('/')
    return studio_page('sending.html', 'Sending', rows=rows, auto_send=auto_send)

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
