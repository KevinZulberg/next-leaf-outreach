# Run Nextleaf on an always-on server

Autopilot only works while the app is running, so host it on a small Linux server with Docker Compose, a persistent disk, and outbound access to Tavily over HTTPS and HMail Plus over SMTP 465 or 587.

1. Put the files on the server and copy `.env.example` to `.env`. Restrict its permissions. Fill in the HMail Plus settings, your Tavily key and a random `FLASK_SECRET_KEY`.
2. Start with `AUTO_SEND=0`. Run `docker compose up -d --build`.
3. From your computer run `ssh -N -L 5050:127.0.0.1:5050 USER@SERVER` and open http://127.0.0.1:5050. Port 5050 stays private.
4. Use **Leads → Find businesses now** to check Tavily, and **Tools → Send a test email** to check HMail Plus.
5. When happy, set `AUTO_SEND=1` in `.env` and run `docker compose up -d`. From then on it searches at 09:00 and sends up to 100 emails at 21:00, Monday to Thursday (Johannesburg time). Set `AUTO_SEND=0` and recreate the service to pause.

To move an existing database, stop both apps and install a consistent backup as `/data/outreach.db` in the Docker volume, owned by UID 10001. Old databases upgrade automatically on start.

Replies, bounces and opt-out requests arrive in the mailbox. Add opt-outs under **Opt-outs**. If a submission is uncertain it is held for you to check and never retried automatically.
