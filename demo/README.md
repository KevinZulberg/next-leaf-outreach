# Personal demo link

Each email's demo link carries the business name, for example
`https://nextleaf.co.za/demo?c=Albion+Sections`. The outreach app adds it when the email is sent.

Two small changes on the demo site (done in ChatGPT, where the demo lives) make it show:

1. **Add `personalise.js`** to every demo page, just before `</body>`:
   `<script src="static/js/personalise.js" defer></script>`
   Put the file at `static/js/personalise.js`. If the sample workshop name changes, edit `SAMPLE_NAMES` at the top.
2. **Make `nextleaf.co.za/demo` keep the name when it forwards.** Replace its redirect with:
   `<script>location.replace('https://demo.nextleaf.co.za/overview' + location.search)</script>`
   (keep the existing `<noscript>` or meta refresh as the fallback).

Until both are done nothing breaks: the demo simply opens as before.

Privacy: only the business name is added to the link. No person's name, no email address, and nothing is tracked.
