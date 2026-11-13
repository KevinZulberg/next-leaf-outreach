# Nextleaf Outreach

Finds South African businesses, checks their websites for a published company email, writes a personalised draft for each, and sends the emails automatically. Open the app any time to watch what it is doing.

## How a normal day works (autopilot)

Monday to Thursday, Johannesburg time:

| Time | What happens |
| --- | --- |
| 09:00 | **Find.** Searches for businesses (Tavily), opens each new website, looks for a published company email, and writes a draft. Searches per day are worked out from the credits you have left this month. |
| 21:00 | **Send.** Sends up to 100 emails (oldest first), about 20 seconds apart, through HMail Plus. Stops at 100 for the day. |

Nothing needs approving. Businesses with no public email are never emailed; they appear under **Needs attention**. Anyone on the **Opt-outs** list is never emailed.

## The app

* **Leads** – the whole workflow on one page: searches and credits today, businesses ready to email, how many were sent today, and every lead in tabs (Ready, Queued, Sent, Needs attention). Select businesses to **Send now** (after ticking the permission box) or **Skip** them so autopilot never emails them.
* **Sending** – the queue, recent sends, and a test-email button.
* **Opt-outs** – add anyone who asks to stop.
* **Tools** – search a specific trade/city, edit the email template, import a CSV, export everything, recent search history.

## Quick start

1. Install Python 3.10+ and run `python -m pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and fill in your HMail Plus mailbox and Tavily key. Never commit `.env`.
3. Run `python run.py` and open http://127.0.0.1:5050 (or see `HOSTING.md` to run it 24/7 on a server, which autopilot needs).
4. On **Tools → Send a test email**, send one to yourself first.

## Settings (`.env`, restart after changing)

| Setting | Default | Meaning |
| --- | --- | --- |
| `TAVILY_API_KEY` | – | Needed to find businesses. |
| `TAVILY_MONTHLY_CREDITS` | `1000` | Credits the app may spend per month (1 per search). |
| `RESEARCH_AUTOMATION` / `RESEARCH_TIME` | `1` / `09:00` | Daily search on or off, and when. |
| `RESEARCH_DAILY_SEARCHES` | blank | Optional ceiling on searches per day. |
| `AUTO_SEND` | `0` | `1` turns on autopilot sending. |
| `AUTO_APPROVE` | `1` | `1` also sends drafts for businesses found online. `0` sends only emails you approved by hand. |
| `AUTO_SEND_TIME` / `AUTO_SEND_LIMIT` | `21:00` / `100` | When to send and the maximum per day (manual sends count too). |
| `SEND_DELAY_SECONDS` | `3` | Pause between emails. 20 is gentler on mail providers. |
| `SEND_BATCH_LIMIT` | `50` | Most emails you can send by hand in one click. |

## Safety built in

* Duplicate protection: a business is reserved before it is emailed, so restarts or double clicks cannot send twice.
* An email the mail server did not clearly accept is marked **Check delivery** and is never retried automatically. Three of these in a day stop that night's sending.
* Every email carries an unsubscribe line and a `List-Unsubscribe` header. Replies asking to stop must be added to **Opt-outs** by you; the app does not read your inbox.
* Each automatic send records *why* it was allowed: "Business email published on <page>; sent automatically on <date>". It does not claim the recipient gave consent.

## Important

* **Lawful use is your responsibility.** A published business email is not marketing consent. South Africa (POPIA section 69) and the UK (PECR, UK GDPR) restrict unsolicited electronic marketing. Set `AUTO_APPROVE=0` if you want a human to approve every email.
* There is no login. Do not expose the port to the internet; reach it through an SSH tunnel (see `HOSTING.md`).
* Back up `outreach.db`; it holds addresses, drafts and logs.
* SMTP acceptance is not inbox delivery. Bounces and replies are not tracked.
* Website checks respect `robots.txt` and only read the company's own site.
