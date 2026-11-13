# Nextleaf Outreach Studio

A local, human-reviewed outreach dashboard that imports a CSV, creates personalised drafts, tracks approval and send status, prevents duplicates, manages opt-outs, and sends through HMail Plus SMTP. The AI Research page can discover and inspect company websites, then prepare reviewable outreach drafts.

## Quick start

1. Install Python 3.10 or later.
2. Open a terminal in this directory.
3. Run `python -m pip install -r requirements.txt`.
4. Copy `.env.example` to `.env`. Enter your **own** mailbox email and password. Do not paste your password into ChatGPT or upload `.env` to GitHub.
5. Run `python run.py`.
6. Open http://127.0.0.1:5050 in your browser.
7. Download the sample CSV, fill it with contacts where you have valid permission and documented evidence, then import.
8. Generate drafts, review each one, replace `[Your website / contact details]`, and approve individually.
9. Use **Send a test email** to send a single message to yourself; verify reception.
10. Review the batch and manually select recipients. Max 20 messages per action, default 3 seconds apart. Follow provider rate limits.

## HMail Plus settings

HMail Plus documentation says SMTP host `smtp.hmailplus.com`, port `465` with SSL/TLS or `587` with STARTTLS. Username is your full email address. See: https://help.hostafrica.com/article/general-how-to-guide-for-microsoft-outlook-email-apps-windows-mac-mobile-web

## Important

- This is a **local prototype**, not a hosted multi-user production app. Do not expose its port to the Internet. There is no authentication, HTTPS, CSRF protection, or access control.
- Do not store passwords in source code or commit `.env` to GitHub.
- The app's local SQLite database `outreach.db` stores addresses, evidence, drafts and logs. Back it up securely.
- An SMTP success means the provider accepted the message; delivery, bounces and replies are not tracked.
- Manually add opt-outs to suppression immediately. This prototype does not monitor incoming mail automatically.
- Sending unknown or scraped prospects marketing emails without a lawful basis may violate South African POPIA section 69, and UK recipients may be covered by PECR and UK GDPR. Confirm consent or a legally qualifying customer basis and include your identity and an easy opt-out.
- Your provider may prohibit mass mail or impose additional daily limits. Check before using batches.

## AI Research Studio

Open `http://127.0.0.1:5050/research`. The studio can:

1. Use the **Tavily Search API** to discover public company websites by business sector and city.
2. Inspect up to 10 company websites per click, respecting robots.txt, and try to locate a company-domain email. Not all websites expose emails; results aren't guaranteed to be accurate.
3. Use the **OpenAI API** to write individually reviewable drafts for up to 10 contacts per click.
4. Reuse the existing approval, suppression, duplicate-checking and HMail Plus SMTP sending workflow. **Nothing is automatically approved or sent.**

### Limits and safety

- This implementation performs **up to 3 search queries per click**, yielding at most 60 results before de-duplication; results may be fewer and may include irrelevant businesses. Repeat with different industries/cities to approach 200.
- Manual website enrichment and AI drafting run in groups of up to 10. Scheduled research inspects newly discovered sites in the background. Email approval and sending are manual.
- Website checks may be slow; respect website access limitations and API quotas.
- Finding a publicly listed email is **not evidence of marketing consent**. POPIA/PECR requirements and HMail Plus's sending policy must be checked. The app deliberately requires evidence of a lawful basis before approval.
- This is a **local prototype only**, with no login, access control or CSRF protection. Do not expose it publicly or deploy it to a production server without security review.
- Your `.env` contains secrets. Never upload it to GitHub or send screenshots showing credentials to anyone.
- Existing `.env` SMTP settings remain unchanged.

## Automatic South Africa search

To upgrade an existing installation, stop the current server, back up the
project folder, then copy the files from the archive's `nextleaf_outreach` folder
over the matching files in your existing folder. Keep your existing `.env` and
`outreach.db`. Install the updated dependencies and start the app from that folder.

```powershell
python -m pip install -r requirements.txt
python run.py
```

Set `TAVILY_API_KEY` in your existing `.env` if it is missing. Under **AI Research**,
use **Run SA search now** to try discovery. Refresh to see the latest run and its
counts. A separate scheduled run occurs on weekdays at 09:00 South Africa time
by default, or later that day if the server starts after 09:00. The browser can
be closed. The computer and Python server must be running.

The first search targets Gauteng. Each run advances through the nine provinces
and eleven search categories. The app skips existing website domains, inspects
new sites and records the source pages for published company-domain emails
and website observations. A provider error is recorded as a failed run.
Use **Run SA search now** to run another search.

Optional settings in `.env` (restart after changing them):

- `RESEARCH_AUTOMATION=0` pauses the weekday schedule.
- `RESEARCH_TIME=09:00` sets the local search time.
- `RESEARCH_DAILY_LIMIT=20` sets the candidate limit per scheduled run (1–60).
  Live results depend on the search provider and available company sites.

Discovery only populates the research queue. Use the existing draft and review
steps for outreach. Website-domain deduplication uses the local database.
