"""Autopilot sending: a daily quota for South Africa and one for the UK, each at its own time.

Everything comes from the Settings page (stored in the database), so changes apply at once.
"""
import logging
from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Thread
from time import sleep

from blocklist import is_law_firm
from app import (DIRECTORY_TITLE, TIMEZONE, conn, day_start_utc, get_settings, hour_minute, market_sql,
                 sent_today_by_market, smtp_config, submit_contact)

UNCERTAIN_PAUSE = timedelta(minutes=15)
UNCERTAIN_DAILY_STOP = 3


def pick_contacts(db, room, send_found=True, market='sa'):
    """Oldest first within each group: approved by hand, then added by you (imports), then found online."""
    rows = db.execute(f"""
        SELECT c.company, c.email, c.observation, c.id, c.status, c.consent_basis, c.consent_evidence,
               (SELECT p.email_source FROM prospects p WHERE p.email = c.email COLLATE NOCASE LIMIT 1) AS source,
               EXISTS (SELECT 1 FROM prospects p WHERE p.email = c.email COLLATE NOCASE) AS found_online
        FROM contacts c
        WHERE c.status IN ('approved','draft')
          AND {market_sql('c.email')} = ?
          AND c.email NOT IN (SELECT email FROM suppression)
          AND length(trim(c.subject)) > 0 AND length(trim(c.body)) > 0
          AND c.body NOT LIKE '%[Your website / contact details]%'
          AND (c.status = 'approved'
               OR (c.consent_basis IN ('consent','existing_customer') AND length(trim(c.consent_evidence)) >= 8)
               OR NOT EXISTS (SELECT 1 FROM prospects p WHERE p.email = c.email COLLATE NOCASE)
               OR ?)
        ORDER BY CASE WHEN c.status = 'approved' THEN 0
                      WHEN NOT EXISTS (SELECT 1 FROM prospects p WHERE p.email = c.email COLLATE NOCASE) THEN 1
                      ELSE 2 END, c.id
        LIMIT ?
    """, (market, int(send_found), room + 300)).fetchall()
    sendable = [row for row in rows if not DIRECTORY_TITLE.search(row['company'])
                and not is_law_firm(row['company'], row['email'], row['observation'])]
    return sendable[:room]


def send_market(now, market, delay):
    """Send one market's batch if it is its send time and it is under its daily limit."""
    cfg = get_settings()
    when, limit = ((cfg['send_time'], cfg['emails_per_day']) if market == 'sa'
                   else (cfg['uk_send_time'], cfg['uk_emails_per_day']))
    if not cfg['autopilot'] or now.weekday() not in cfg['days'] or limit <= 0:
        return 0
    if (now.hour, now.minute) < hour_minute(when):
        return 0
    start = day_start_utc(now)
    with closing(conn()) as db:
        used = sent_today_by_market(db, start)[market]
        trouble = [row['sent_at'] for row in db.execute(
            "SELECT sent_at FROM send_log WHERE result='uncertain' AND sent_at >= ? ORDER BY id DESC", (start,))]
        if len(trouble) >= UNCERTAIN_DAILY_STOP:
            return 0
        if trouble and datetime.now(timezone.utc) - datetime.fromisoformat(trouble[0]) < UNCERTAIN_PAUSE:
            return 0
        if used >= limit:
            return 0
        contacts = pick_contacts(db, limit - used, cfg['send_found'], market)
    if not contacts:
        return 0
    smtp_config()
    sent = 0
    for contact in contacts:
        if datetime.now(TIMEZONE).date() != now.date() or not get_settings()['autopilot']:
            break   # new day, or you switched autopilot off while it was sending
        proven = contact['consent_basis'] in ('consent', 'existing_customer') and len((contact['consent_evidence'] or '').strip()) >= 8
        evidence = None
        if contact['status'] == 'draft' and not proven:
            today = now.date().isoformat()
            if contact['found_online']:
                evidence = (f"Business email published on {contact['source'] or 'its own website'}; "
                            f"sent automatically by Nextleaf on {today}.")
            else:
                evidence = f"Added by the Nextleaf operator (contact import); sent automatically on {today}."
        result = submit_contact(contact['id'], scheduled=True, auto_evidence=evidence,
                                basis='published_business_email' if contact['found_online'] else 'operator_added')
        if result == 'uncertain':
            logging.warning('Sending paused after an uncertain SMTP submission.')
            break
        if result == 'submitted':
            sent += 1
            sleep(delay)
    return sent


def run_nightly_send(now=None, delay=None):
    """South Africa first, then the UK at its own time. Returns the number of emails sent."""
    now = now or datetime.now(TIMEZONE)
    delay = get_settings()['delay_seconds'] if delay is None else delay
    return sum(send_market(now, market, delay) for market in ('sa', 'uk'))


def start_sending():
    """Always running; every 30 seconds it checks the Settings page to see whether it is time."""
    def poll():
        while True:
            try:
                run_nightly_send()
            except Exception as exc:
                logging.error('Autopilot sending failed (%s)', type(exc).__name__)
            sleep(30)

    Thread(target=poll, name='nextleaf-sending', daemon=True).start()
