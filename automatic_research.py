"""Business discovery: scheduled daily runs plus on-demand runs.

Every run does the whole job: search -> open each new website and look for a published
company email -> write a draft. Nothing is sent from here. Schedule, days and the number
of searches per day come from the Settings page (the database), so changes apply at once.
"""
import logging
import os
from contextlib import closing
from datetime import datetime
from math import gcd
from threading import Lock, Thread

import requests

from app import (TIMEZONE, conn, enrich_prospects, get_settings, hour_minute, prepare_website_drafts,
                 save_prospects, save_settings)
from discovery import find_companies, forget_credit_cache, search_allowance

# The same trades and provinces as always; each province is now searched town by town so that
# a full day of searches keeps finding new businesses instead of repeating itself.
INDUSTRIES = (
    'laser cutting and metal fabrication', 'precision engineering workshops',
    'aluminium windows and glazing installers', 'signage and commercial printing',
    'custom kitchens and joinery', 'shopfitting contractors',
    'electrical contractors', 'plumbing contractors',
    'HVAC and refrigeration contractors', 'packaging manufacturers and suppliers',
    'sheet metal and structural steel fabrication',
)
PLACES = {
    'Gauteng': ('Johannesburg', 'Pretoria', 'Sandton', 'Midrand', 'Centurion', 'Germiston', 'Boksburg', 'Benoni',
                'Roodepoort', 'Vereeniging', 'Randburg', 'Kempton Park', 'Alberton', 'Springs'),
    'Western Cape': ('Cape Town', 'Stellenbosch', 'Paarl', 'George', 'Bellville', 'Somerset West', 'Worcester'),
    'KwaZulu-Natal': ('Durban', 'Pietermaritzburg', 'Richards Bay', 'Pinetown', 'Umhlanga', 'Newcastle'),
    'Eastern Cape': ('Gqeberha', 'East London', 'Mthatha', 'Kariega'),
    'Free State': ('Bloemfontein', 'Welkom', 'Kroonstad'),
    'Mpumalanga': ('Mbombela', 'eMalahleni', 'Secunda', 'Middelburg'),
    'Limpopo': ('Polokwane', 'Tzaneen', 'Mokopane', 'Makhado'),
    'North West': ('Rustenburg', 'Potchefstroom', 'Klerksdorp', 'Mahikeng'),
    'Northern Cape': ('Kimberley', 'Upington'),
}
REGIONS = tuple(PLACES)
LOCATIONS = tuple(f'{city}, {province}, South Africa' for province, cities in PLACES.items() for city in cities)
VARIANTS = 5                      # different wordings of the same search
COMBOS = len(INDUSTRIES) * len(LOCATIONS)
STRIDE = next(n for n in range(37, 400) if gcd(n, COMBOS) == 1)   # visits every trade/town pair before repeating
RUN_LOCK = Lock()
MANUAL_SEARCHES = 10      # searches used by one click of "Find businesses now"
PENDING_PER_RUN = 150     # older, never-checked websites picked up by each scheduled run
CHUNK = 40                # websites checked between draft-writing passes


def query_for(position):
    """Trade, town and wording for the n-th search of the endless rotation."""
    combo = (position * STRIDE) % COMBOS
    wording = (position // COMBOS) % VARIANTS
    return INDUSTRIES[combo % len(INDUSTRIES)], LOCATIONS[(combo // len(INDUSTRIES)) % len(LOCATIONS)], wording


def is_running():
    return RUN_LOCK.locked()


def _finish(run_id, status):
    with closing(conn()) as db, db:
        db.execute('UPDATE research_runs SET status=? WHERE id=?', (status, run_id))


def run_search(manual=False, custom=None, recheck=False):
    """One research run.

    manual: started by a click. custom: (industry, location, target). recheck: no searching, only
    look again at websites that have no email yet.
    """
    if not recheck and not os.getenv('TAVILY_API_KEY', '').strip():
        return
    if not RUN_LOCK.acquire(blocking=False):
        return
    run_id = None
    try:
        now = datetime.now(TIMEZONE)
        cfg = get_settings()
        day = now.date().isoformat()
        with closing(conn()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT * FROM research_runs ORDER BY id DESC LIMIT 1').fetchone()
            if latest and latest['status'] == 'running':
                # We hold the lock, so no run is really going: it was cut off by a restart.
                db.execute("UPDATE research_runs SET status='interrupted' WHERE id=?", (latest['id'],))
            used_today = db.execute('SELECT coalesce(sum(searches),0) FROM research_runs WHERE day=?', (day,)).fetchone()[0]
            if not manual and not recheck:
                if not cfg['autopilot'] or now.weekday() not in cfg['days']:
                    return
                if (now.hour, now.minute) < hour_minute(cfg['research_time']):
                    return
                if db.execute('SELECT 1 FROM research_runs WHERE day=? AND manual=0', (day,)).fetchone():
                    return
            region, industry = 'South Africa', 'Multiple industries'
            if custom:
                industry, region = custom[0], custom[1]
            elif recheck:
                industry = 'Websites without an email'
            run_id = db.execute(
                """INSERT INTO research_runs(day, manual, started_at, region, industry, status)
                   VALUES(?,?,?,?,?,'running')""",
                (day, int(manual or recheck), now.isoformat(), region, industry),
            ).lastrowid

        def count_search():
            with closing(conn()) as db, db:
                db.execute('UPDATE research_runs SET searches=searches+1 WHERE id=?', (run_id,))

        domains, new_ids = set(), []
        searched = failures = 0
        wanted = 0
        if not recheck:
            with search_allowance() as remaining:
                if custom:
                    wanted = min(MANUAL_SEARCHES, remaining)
                elif manual:
                    wanted = min(MANUAL_SEARCHES, remaining)
                else:
                    wanted = min(max(0, cfg['searches_per_day'] - used_today), remaining)
                if custom:
                    try:
                        leads = find_companies(custom[0], custom[1], custom[2], max_searches=min(3, wanted),
                                               on_search=count_search)
                        searched = min(3, wanted)
                        domains.update(lead['domain'] for lead in leads)
                        new_ids.extend(save_prospects(leads)[1])
                    except requests.RequestException:
                        failures += 1
                else:
                    cursor = int(_stored('search_cursor', 0))
                    for offset in range(wanted):
                        trade, place, wording = query_for(cursor + offset)
                        try:
                            leads = find_companies(trade, place, 20, max_searches=1, on_search=count_search,
                                                   phrase_index=wording)
                        except requests.RequestException as exc:
                            logging.getLogger(__name__).error('Search failed (%s)', type(exc).__name__)
                            failures += 1
                            if failures >= 3:
                                break
                            continue
                        searched += 1
                        fresh = [lead for lead in leads if lead['domain'] not in domains]
                        domains.update(lead['domain'] for lead in fresh)
                        new_ids.extend(save_prospects(fresh)[1])
                        save_settings({'search_cursor': cursor + offset + 1})
            forget_credit_cache()

        # Check the new sites, plus older ones that were found but never checked; write drafts as we go.
        limit = 2000 if recheck else PENDING_PER_RUN
        with closing(conn()) as db:
            pending = [row['id'] for row in db.execute(
                'SELECT id FROM prospects WHERE researched=0 ORDER BY id LIMIT ?', (limit,))]
        queue = list(dict.fromkeys(new_ids + pending))
        inspected = emails = 0
        for start in range(0, len(queue), CHUNK):
            done, found = enrich_prospects(queue[start:start + CHUNK])
            inspected += done
            emails += found
            prepare_website_drafts()
        prepare_website_drafts()
        failed = bool(wanted and searched == 0 and failures)
        with closing(conn()) as db, db:
            db.execute(
                """UPDATE research_runs SET status=?, found=?, added=?, inspected=?, emails=? WHERE id=?""",
                ('failed' if failed else 'completed', len(domains), len(new_ids), inspected, emails, run_id),
            )
    except Exception as exc:
        logging.getLogger(__name__).error('Business search failed (%s)', type(exc).__name__)
        if run_id is not None:
            _finish(run_id, 'failed')
    finally:
        RUN_LOCK.release()


def _stored(key, default):
    with closing(conn()) as db:
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def start_search():
    Thread(target=run_search, kwargs={'manual': True}, daemon=True).start()


def start_custom_search(industry, location, target):
    Thread(target=run_search, kwargs={'manual': True, 'custom': (industry, location, target)},
           daemon=True).start()


def start_recheck():
    Thread(target=run_search, kwargs={'recheck': True}, daemon=True).start()


def start_scheduler():
    """Always running; each minute it checks the Settings page to see whether it is time."""
    from time import sleep

    def poll():
        while True:
            try:
                run_search()
            except Exception as exc:
                logging.getLogger(__name__).error('Scheduler error (%s)', type(exc).__name__)
            sleep(60)

    Thread(target=poll, name='nextleaf-research', daemon=True).start()
