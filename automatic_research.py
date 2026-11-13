"""Weekday business discovery for the local Outreach Studio."""
import logging
import os
from calendar import monthrange
from contextlib import closing
from datetime import datetime, timedelta
from threading import Lock, Thread
from zoneinfo import ZoneInfo

from app import conn, enrich_prospects, prepare_website_drafts, save_prospects
from discovery import find_companies, search_allowance

TIMEZONE = ZoneInfo('Africa/Johannesburg')
REGIONS = (
    'Johannesburg and Gauteng', 'Cape Town and Western Cape',
    'Durban and KwaZulu-Natal', 'Gqeberha and Eastern Cape',
    'Bloemfontein and Free State', 'Mbombela and Mpumalanga',
    'Polokwane and Limpopo', 'Rustenburg and North West',
    'Kimberley and Northern Cape',
)
INDUSTRIES = (
    'laser cutting and metal fabrication', 'precision engineering workshops',
    'aluminium windows and glazing installers', 'signage and commercial printing',
    'custom kitchens and joinery', 'shopfitting contractors',
    'electrical contractors', 'plumbing contractors',
    'HVAC and refrigeration contractors', 'packaging manufacturers and suppliers',
    'sheet metal and structural steel fabrication',
)
RUN_LOCK = Lock()


def daily_search_budget(now, remaining):
    search_days = sum(
        1 for day in range(now.day, monthrange(now.year, now.month)[1] + 1)
        if now.replace(day=day).weekday() < 4
    )
    return (remaining + search_days - 1) // search_days if search_days else 0


def settings():
    hour, minute = map(int, os.getenv('RESEARCH_TIME', '09:00').split(':'))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError('RESEARCH_TIME must use 24-hour HH:MM format')
    return hour, minute


def run_search(manual=False):
    if not os.getenv('TAVILY_API_KEY', '').strip() or not RUN_LOCK.acquire(blocking=False):
        return
    run_id = None
    try:
        now = datetime.now(TIMEZONE)
        hour, minute = settings()
        day = now.date().isoformat()
        with closing(conn()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT * FROM research_runs ORDER BY id DESC LIMIT 1').fetchone()
            if latest and latest['status'] == 'running':
                started = datetime.fromisoformat(latest['started_at'])
                if now - started < timedelta(minutes=45):
                    return
                db.execute("UPDATE research_runs SET status='interrupted' WHERE id=?", (latest['id'],))
            if not manual:
                if now.weekday() >= 4 or (now.hour, now.minute) < (hour, minute):
                    return
                if db.execute('SELECT 1 FROM research_runs WHERE day=? AND manual=0', (day,)).fetchone():
                    return
            index = (latest['id'] if latest else 0) * 97
        domains = set()
        prospect_ids = []
        with search_allowance() as remaining:
            with closing(conn()) as db, db:
                run_id = db.execute(
                    """INSERT INTO research_runs(day, manual, started_at, region, industry, status)
                       VALUES(?,?,?,?,?,'running')""",
                    (day, int(manual), now.isoformat(), 'South Africa', 'Multiple industries'),
                ).lastrowid
            search_budget = min(3, remaining) if manual else daily_search_budget(now, remaining)
            for offset in range(search_budget):
                position = index + offset
                region = REGIONS[position % len(REGIONS)]
                industry = INDUSTRIES[position % len(INDUSTRIES)]
                leads = find_companies(industry, f'{region}, South Africa', 20, max_searches=1)
                new_leads = [lead for lead in leads if lead['domain'] not in domains]
                domains.update(lead['domain'] for lead in new_leads)
                _, new_ids = save_prospects(new_leads)
                prospect_ids.extend(new_ids)
        added = len(prospect_ids)
        inspected, emails = enrich_prospects(prospect_ids)
        prepare_website_drafts()
        with closing(conn()) as db, db:
            db.execute(
                """UPDATE research_runs SET status='completed', found=?, added=?, inspected=?, emails=?
                   WHERE id=?""",
                (len(domains), added, inspected, emails, run_id),
            )
    except Exception as exc:
        logging.getLogger(__name__).error('Business search failed (%s)', type(exc).__name__)
        if run_id is not None:
            with closing(conn()) as db, db:
                db.execute("UPDATE research_runs SET status='failed' WHERE id=?", (run_id,))
    finally:
        RUN_LOCK.release()


def start_search():
    Thread(target=run_search, kwargs={'manual': True}, daemon=True).start()


def start_scheduler():
    from time import sleep

    if os.getenv('RESEARCH_AUTOMATION', '1') != '1':
        return
    settings()

    def poll():
        while True:
            run_search()
            sleep(60)

    Thread(target=poll, name='nextleaf-research', daemon=True).start()
