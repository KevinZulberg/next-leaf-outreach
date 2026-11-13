"""Send drafts with recorded marketing permission through HMail Plus."""
import logging
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Thread
from time import sleep

from app import conn, smtp_config, submit_contact


def send_approved(delay):
    retry_after = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with closing(conn()) as db:
        if db.execute(
            "SELECT 1 FROM send_log WHERE result='uncertain' AND sent_at >= ? LIMIT 1",
            (retry_after,),
        ).fetchone():
            return
        contacts = db.execute("""
            SELECT id FROM contacts WHERE status IN ('draft','approved')
              AND consent_basis IN ('consent','existing_customer')
              AND length(trim(consent_evidence)) >= 8
              AND length(trim(subject)) > 0 AND length(trim(body)) > 0
              AND email NOT IN (SELECT email FROM suppression)
            ORDER BY id
        """).fetchall()
    if not contacts:
        return
    smtp_config()
    for contact in contacts:
        result = submit_contact(contact['id'], scheduled=True)
        if result == 'uncertain':
            logging.warning('Background sending delayed after an uncertain SMTP submission.')
            return
        if result == 'submitted':
            sleep(delay)


def start_sending():
    if os.getenv('AUTO_SEND', '0') != '1':
        return
    delay = max(0, min(float(os.getenv('SEND_DELAY_SECONDS', '3')), 60))

    def poll():
        while True:
            try:
                send_approved(delay)
            except Exception as exc:
                logging.error('Background sending failed (%s)', type(exc).__name__)
            sleep(5)

    Thread(target=poll, name='nextleaf-hmailplus', daemon=True).start()
