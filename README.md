# Nextleaf Outreach

Finds South African businesses, opens their websites to find a published company email, writes a personalised draft for each, and emails them automatically. Open the website any time to watch, change the schedule, or switch autopilot off.

## A normal day (autopilot, on by default)

Monday to Thursday, Johannesburg time. You can change every number below on the **Settings** page.

| Time | What happens |
| --- | --- |
| 06:00 | **Find.** Runs up to 80 searches (Tavily), opens each new website, looks for its published email, and writes a draft with your template. |
| 08:00 | **Send.** Emails up to 100 businesses, about 20 seconds apart, through HMail Plus, then stops for the day. |

Contacts you import (for example a list from ChatGPT) are drafted automatically and sent **before** businesses found by search. Raise "Emails per day" to 200 on Settings when you add a big list.

## The website

* **Leads** – the big **Autopilot ON/OFF** switch, searches and credits today, businesses ready, emails sent today, and every lead in tabs. Select businesses to **Send now** or **Skip** them.
* **Sending** – the queue, recent sends, test email.
* **Opt-outs** – people who asked to stop.
* **Settings** – autopilot switch, days, search time, send time, searches per day, emails per day, pause between emails, and whether found businesses are emailed.
* **Tools** – search a specific trade or town, edit the email template, import a CSV, export, re-check websites that had no email.

## Quick start

1. Install Python 3.10+ and run `python -m pip install -r requirements.txt`.
2. Copy `.env.example` to `.env`; enter your mailbox and Tavily key. Never commit `.env`.
3. Run `python run.py` and open http://127.0.0.1:5050. Autopilot needs the app running all the time, see `HOSTING.md`.
4. Under **Tools** send a test email to yourself first.

## Finding emails

For each business the app opens the homepage, then contact, about, footer and legal pages, plus the exact page the search found. It reads normal text, `mailto:` links, "name [at] site [dot] co [dot] za" spellings, structured data, and addresses hidden by Cloudflare. It accepts the company's own domain and Gmail/mweb-style addresses on its pages, and works over http as well as https. It respects `robots.txt` and only reads the company's own site. Sites it still cannot read appear under **Needs attention**.

## What it searches

The same 11 trades in all 9 provinces: laser cutting and metal fabrication, precision engineering, aluminium windows and glazing, signage and printing, kitchens and joinery, shopfitting, electrical, plumbing, HVAC and refrigeration, packaging, and sheet metal and steel. Each province is searched town by town (about 50 towns) so 80 searches a day keep finding new businesses.

## Never emailed

* **Law firms**, attorneys, advocates, conveyancers and notaries, at every step: search results, website checks, drafts, imports, approval and the final send.
* Anyone on the **Opt-outs** list.
* Businesses you **Skip**, and directory pages such as "Top 10 companies".

## Safety built in

* A business is reserved before it is emailed, so restarts or double clicks cannot send twice.
* An email the mail server did not clearly accept is marked **Check delivery** and is never retried automatically. Three of these in a day stop that day's sending.
* Switching autopilot off stops sending within seconds, even mid-batch.
* Every email carries an unsubscribe line and a `List-Unsubscribe` header. Replies asking to stop must be added to **Opt-outs**; the app does not read your inbox.
* Each automatic send records why it was allowed ("business email published on <page>" or "added by the operator"). It does not claim the recipient gave consent.

## Important

* **Lawful use is your responsibility.** A published business email is not marketing consent. POPIA section 69 (South Africa) and PECR / UK GDPR (UK) restrict unsolicited electronic marketing.
* There is no login. Do not expose the port to the internet; use an SSH tunnel (see `HOSTING.md`).
* Back up `outreach.db`; it holds addresses, drafts and logs.
* A successful send means the mail server accepted it. Bounces and replies are not tracked.
* Tavily credits: 1 per search. At 80 searches a day on 4 days a week you use about 1,370 a month, so searching pauses when a 1,000-credit plan runs out. Lower "Searches per day" on Settings to last the month.
