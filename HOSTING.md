# Run Nextleaf on an always-on server

Autopilot only works while the app is running, so host it on a small Linux server with Docker Compose, a persistent disk, and outbound access to Tavily (HTTPS) and HMail Plus (SMTP 465 or 587).

1. Put the files on the server and copy `.env.example` to `.env`. Restrict its permissions. Fill in the HMail Plus mailbox, your Tavily key and a random `FLASK_SECRET_KEY`.
2. Run `docker compose up -d --build`.
3. From your computer run `ssh -N -L 5050:127.0.0.1:5050 USER@SERVER` and open http://127.0.0.1:5050. Port 5050 stays private.
4. Open **Tools → Send a test email** to check HMail Plus, and **Leads → Find businesses now** to check Tavily.
5. Autopilot is **on by default**: it searches at 06:00 and sends up to 100 emails at 08:00, Monday to Thursday (Johannesburg time). Use the **Autopilot** switch on the Leads page, or **Settings**, to pause, change times, days or amounts. No restart needed.

To move an existing database, stop both apps and install a consistent backup as `/data/outreach.db` in the Docker volume, owned by UID 10001. Older databases upgrade automatically on start, and websites the old finder missed are checked again once.

Replies, bounces and opt-out requests arrive in the mailbox. Add opt-outs under **Opt-outs**. A submission the mail server did not clearly accept is held for you to check and never retried automatically.
