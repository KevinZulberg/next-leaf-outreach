"""Nightly autopilot sending: up to AUTO_SEND_LIMIT emails a day, Monday-Thursday."""
import logging
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Thread
from time import sleep

from app import TIMEZONE, auto_send_settings, conn, day_start_utc, send_delay, smtp_config, submit_contact

UNCERTAIN_PAUSE = timedelta(minutes=15)
UNCERTAIN_DAILY_STOP = 3


def sent_today(db, start):
    """Emails already handled today (manual and automatic), uncertain ones included to stay safe."""
    return db.execute("SELECT count(*) FROM send_log WHERE sent_at >= ?", (start,)).fetchone()[0]


def pick_contacts(db, room):
    """Oldest first: manually approved, then recorded-permission drafts, then website-found drafts."""
    auto = os.getenv('AUTO_APPROVE', '1') == '1'
    return db.execute("""
        SELECT c.id, c.status, c.consent_basis, c.consent_evidence,
               (SELECT p.email_source FROM prospects p WHERE p.email = c.email COLLATE NOCASE LIMIT 1) AS source,
               EXISTS (SELECT 1 FROM prospects p WHERE p.email = c.email COLLATE NOCASE) AS found_online
        FROM contacts c
        WHERE c.status IN ('approved','draft')
          AND c.email NOT IN (SELECT email FROM suppression)
          AND length(trim(c.subject)) > 0 AND length(trim(c.body)) > 0
          AND (c.status = 'approved'
               OR (c.consent_basis IN ('consent','existing_customer') AND length(trim(c.consent_evidence)) >= 8)
               OR (? AND EXISTS (SELECT 1 FROM prospects p WHERE p.email = c.email COLLATE NOCASE)))
        ORDER BY CASE c.status WHEN 'approved' THEN 0 ELSE 1 END, c.id
        LIMIT ?
    """, (int(auto), room)).fetchall()


def run_nightly_send(now=None, delay=None):
    """Send tonight's batch if it is a send day, past send time and under the daily limit."""
    now = now or datetime.now(TIMEZONE)
    hour, minute, limit = auto_send_settings()
    if now.weekday() >= 4 or (now.hour, now.minute) < (hour, minute):
        return 0
    start = day_start_utc(now)
    with closing(conn()) as db:
        used = sent_today(db, start)
        trouble = [row['sent_at'] for row in db.execute(
            "SELECT sent_at FROM send_log WHERE result='uncertain' AND sent_at >= ? ORDER BY id DESC", (start,))]
        if len(trouble) >= UNCERTAIN_DAILY_STOP:
            return 0
        if trouble and datetime.now(timezone.utc) - datetime.fromisoformat(trouble[0]) < UNCERTAIN_PAUSE:
            return 0
        if used >= limit:
            return 0
        contacts = pick_contacts(db, limit - used)
    if not contacts:
        return 0
    smtp_config()
    delay = send_delay() if delay is None else delay
    sent = 0
    for contact in contacts:
        if datetime.now(TIMEZONE).date() != now.date():
            break
        proven = contact['consent_basis'] in ('consent', 'existing_customer') and len((contact['consent_evidence'] or '').strip()) >= 8
        evidence = None
        if contact['status'] == 'draft' and not proven:
            evidence = (f"Business email published on {contact['source'] or 'its own website'}; "
                        f"sent automatically by Nextleaf on {now.date().isoformat()}.")
        result = submit_contact(contact['id'], scheduled=True, auto_evidence=evidence)
        if result == 'uncertain':
            logging.warning('Nightly sending paused after an uncertain SMTP submission.')
            break
        if result == 'submitted':
            sent += 1
            sleep(delay)
    return sent


def start_sending():
    if os.getenv('AUTO_SEND', '0') != '1':
        return
    auto_send_settings()

    def poll():
        while True:
            try:
                run_nightly_send()
            except Exception as exc:
                logging.error('Nightly sending failed (%s)', type(exc).__name__)
            sleep(30)

    Thread(target=poll, name='nextleaf-sending', daemon=True).start()
