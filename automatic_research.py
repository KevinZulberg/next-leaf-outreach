"""Business discovery for Outreach Studio: scheduled daily runs plus on-demand runs.

Every run does the whole job: search -> check each new website for a published
company email -> write drafts. Nothing is approved or sent automatically.
"""
import logging
import os
from calendar import monthrange
from contextlib import closing
from datetime import datetime, timedelta
from threading import Lock, Thread

from app import TIMEZONE, conn, enrich_prospects, prepare_website_drafts, save_prospects
from discovery import find_companies, forget_credit_cache, search_allowance

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
MANUAL_SEARCHES = 3      # searches used by one click of "Find businesses now"
PENDING_PER_RUN = 40     # older, never-checked websites picked up by each run


def search_days_left(now):
    """Scheduled search days (Monday-Thursday) left this month, including today."""
    return sum(
        1 for day in range(now.day, monthrange(now.year, now.month)[1] + 1)
        if now.replace(day=day).weekday() < 4
    )


def daily_search_budget(now, remaining):
    """Searches for one scheduled run: spread the remaining credits over the days left.

    RESEARCH_DAILY_SEARCHES, when set, is a hard ceiling per day.
    """
    days = search_days_left(now)
    budget = (remaining + days - 1) // days if days else 0
    try:
        ceiling = int(os.getenv('RESEARCH_DAILY_SEARCHES', '') or 0)
    except ValueError:
        ceiling = 0
    if ceiling > 0:
        budget = min(budget, ceiling)
    return max(0, min(budget, remaining))


def settings():
    hour, minute = map(int, os.getenv('RESEARCH_TIME', '09:00').split(':'))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError('RESEARCH_TIME must use 24-hour HH:MM format')
    return hour, minute


def is_running():
    return RUN_LOCK.locked()


def _finish(run_id, status):
    with closing(conn()) as db, db:
        db.execute('UPDATE research_runs SET status=? WHERE id=?', (status, run_id))


def run_search(manual=False, custom=None):
    """One research run. `custom` is (industry, location, target) for a specific search."""
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
            region, industry = ('South Africa', 'Multiple industries')
            if custom:
                industry, region = custom[0], custom[1]
            run_id = db.execute(
                """INSERT INTO research_runs(day, manual, started_at, region, industry, status)
                   VALUES(?,?,?,?,?,'running')""",
                (day, int(manual), now.isoformat(), region, industry),
            ).lastrowid

        def count_search():
            with closing(conn()) as db, db:
                db.execute('UPDATE research_runs SET searches=searches+1 WHERE id=?', (run_id,))

        domains = set()
        new_ids = []
        with search_allowance() as remaining:
            if custom:
                leads = find_companies(custom[0], custom[1], custom[2],
                                       max_searches=min(MANUAL_SEARCHES, remaining), on_search=count_search)
                domains.update(lead['domain'] for lead in leads)
                _, new_ids = save_prospects(leads)
            else:
                budget = min(MANUAL_SEARCHES, remaining) if manual else daily_search_budget(now, remaining)
                for offset in range(budget):
                    position = index + offset
                    place = REGIONS[position % len(REGIONS)]
                    trade = INDUSTRIES[position % len(INDUSTRIES)]
                    leads = find_companies(trade, f'{place}, South Africa', 20, max_searches=1,
                                           on_search=count_search)
                    fresh = [lead for lead in leads if lead['domain'] not in domains]
                    domains.update(lead['domain'] for lead in fresh)
                    new_ids.extend(save_prospects(fresh)[1])
        forget_credit_cache()

        # Check the new sites, plus any older ones that were found but never checked.
        with closing(conn()) as db:
            pending = [row['id'] for row in db.execute(
                'SELECT id FROM prospects WHERE researched=0 ORDER BY id LIMIT ?', (PENDING_PER_RUN,))]
        inspected, emails = enrich_prospects(list(dict.fromkeys(new_ids + pending)))
        prepare_website_drafts()
        with closing(conn()) as db, db:
            db.execute(
                """UPDATE research_runs SET status='completed', found=?, added=?, inspected=?, emails=?
                   WHERE id=?""",
                (len(domains), len(new_ids), inspected, emails, run_id),
            )
    except Exception as exc:
        logging.getLogger(__name__).error('Business search failed (%s)', type(exc).__name__)
        if run_id is not None:
            _finish(run_id, 'failed')
    finally:
        RUN_LOCK.release()


def start_search():
    Thread(target=run_search, kwargs={'manual': True}, daemon=True).start()


def start_custom_search(industry, location, target):
    Thread(target=run_search, kwargs={'manual': True, 'custom': (industry, location, target)},
           daemon=True).start()


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
