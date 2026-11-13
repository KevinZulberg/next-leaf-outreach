# Run Nextleaf on an always-on server

Use a Linux server with Docker Compose, a persistent disk, and outbound access
to Tavily over HTTPS and HMailPlus over SMTP 465 or 587.

1. Place the files on the server. Keep the existing local Outreach Studio and
   its database backed up.
2. Copy .env.example to .env on the server. Restrict file permissions and enter
   the existing HMailPlus SMTP settings and your Tavily API key. Leave
   AUTO_SEND=0 for the first check. Set a random FLASK_SECRET_KEY.
3. Run `docker compose up -d --build`.
4. From your computer run `ssh -N -L 5050:127.0.0.1:5050 USER@SERVER`.
   Open http://127.0.0.1:5050. The server only exposes the dashboard through its
   loopback interface. Keep port 5050 private.
5. Use AI Research → Run SA search now to verify Tavily and Send a test email
   to verify HMailPlus with your own address.
6. Reconcile the local SQLite database and HMailPlus Sent and opt-outs before
   activating the scheduled send queue. Existing databases can be migrated by
   stopping both apps and installing a consistent backup as
   /data/outreach.db in the Docker volume, owned by UID 10001.
7. Set AUTO_SEND=1 and run `docker compose up -d`. The default is weekdays at
   10:00 Johannesburg time and at most 10 approved recipients per scheduled
   run. Set AUTO_SEND=0 and recreate the service to pause.

Tavily discovery defaults to weekdays at 09:00. It does not consume ChatGPT task
credits. Tavily usage may be billed separately. The server runs searches and
processes the approved send queue even when the browser and your PC are off.

Discovery does not establish marketing eligibility. Review the contact, record
the applicable permission evidence and approve the completed message to queue it.
Record opt-outs in the suppression list. Replies and bounces are handled in the
mailbox. When a submission is uncertain or interrupted, it is held for
reconciliation instead of being automatically retried.
