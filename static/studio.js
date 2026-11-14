/* Leads page: tabs, search, selection, action bar, live status */
const form = document.getElementById('lead-form');

if (form) {
    const rows = [...form.querySelectorAll('.lead')];
    const tabs = [...form.querySelectorAll('[data-tab]')];
    const search = document.getElementById('lead-search');
    const picker = document.getElementById('picker');
    const checkAll = document.getElementById('check-all');
    const firstN = document.getElementById('first-n');
    const bar = document.getElementById('action-bar');
    const countLabel = document.getElementById('selected-count');
    const limitNote = document.getElementById('limit-note');
    const permit = document.getElementById('permit');
    const sendBtn = document.getElementById('btn-send');
    const approveBtn = document.getElementById('btn-approve');
    const empty = document.getElementById('lead-empty');
    const limit = Number(form.dataset.limit) || 50;
    const messages = {
        ready: ['Nothing waiting', 'New businesses show up here after the next search.'],
        queued: ['Nothing queued', 'Emails that are approved or being sent appear here.'],
        sent: ['Nothing sent yet', 'Sent emails are listed here.'],
        attention: ['All clear', 'Businesses without a public email or with unfinished drafts appear here.'],
    };
    let stage = 'ready';

    const checks = () => rows.filter(row => !row.hidden).flatMap(row => [...row.querySelectorAll('input[name="ids"]')]);
    const allChecks = () => rows.flatMap(row => [...row.querySelectorAll('input[name="ids"]')]);

    function refresh() {
        const picked = allChecks().filter(check => check.checked);
        const shown = checks();
        for (const row of rows) row.classList.toggle('is-checked', !!row.querySelector('input:checked'));
        countLabel.textContent = `${picked.length} selected`;
        bar.hidden = picked.length === 0;
        document.body.style.paddingBottom = picked.length ? '110px' : '';
        const tooMany = picked.length > limit;
        limitNote.textContent = tooMany ? `Send at most ${limit} at once` : '';
        limitNote.className = tooMany ? 'over-limit' : '';
        const ok = permit.checked && picked.length > 0 && !tooMany;
        sendBtn.disabled = !ok;
        if (approveBtn) approveBtn.disabled = !(permit.checked && picked.length > 0);
        checkAll.checked = shown.length > 0 && shown.every(check => check.checked);
        checkAll.indeterminate = !checkAll.checked && shown.some(check => check.checked);
    }

    function apply() {
        const query = search.value.trim().toLowerCase();
        for (const row of rows) {
            row.hidden = row.dataset.stage !== stage || !row.dataset.search.toLowerCase().includes(query);
            const box = row.querySelector('input[name="ids"]');
            if (row.hidden && box) box.checked = false;
        }
        const any = rows.some(row => !row.hidden);
        empty.hidden = any;
        if (!any) {
            const [title, text] = query ? ['No matches', 'Try a different search.'] : messages[stage];
            document.getElementById('empty-title').textContent = title;
            document.getElementById('empty-text').textContent = text;
        }
        picker.hidden = !(stage === 'ready' && rows.some(row => row.dataset.stage === 'ready' && row.querySelector('input')));
        refresh();
    }

    function setStage(next) {
        stage = next;
        for (const tab of tabs) tab.setAttribute('aria-pressed', tab.dataset.tab === stage);
        apply();
    }

    for (const tab of tabs) tab.addEventListener('click', () => setStage(tab.dataset.tab));
    for (const jump of document.querySelectorAll('[data-jump]')) {
        jump.addEventListener('click', () => setStage(jump.dataset.jump));
    }
    search.addEventListener('input', apply);
    form.addEventListener('change', refresh);
    checkAll.addEventListener('change', () => {
        for (const check of checks()) check.checked = checkAll.checked;
        refresh();
    });
    document.getElementById('pick-first').addEventListener('click', () => {
        const n = Math.max(1, Math.min(Number(firstN.value) || 1, limit));
        checks().forEach((check, index) => { check.checked = index < n; });
        refresh();
    });
    // Clicking anywhere on a ready row toggles it (links and buttons keep working).
    form.querySelector('#lead-rows').addEventListener('click', event => {
        if (event.target.closest('a, button, input')) return;
        const box = event.target.closest('.lead.selectable')?.querySelector('input[name="ids"]');
        if (box) { box.checked = !box.checked; refresh(); }
    });
    // Skipping needs no permission box; keep the box from blocking the form.
    permit.addEventListener('change', refresh);
    setStage(new URLSearchParams(location.search).get('tab') || 'ready');
}

/* Live credits and "search running" auto-refresh */
const steps = document.getElementById('steps');
if (steps) {
    const text = document.getElementById('credits-text');
    const today = document.getElementById('searches-today');
    let wasRunning = !!document.querySelector('[data-running]');

    async function poll() {
        try {
            const response = await fetch(steps.dataset.statusUrl, {headers: {Accept: 'application/json'}});
            const data = await response.json();
            today.textContent = data.searches_today;
            text.textContent = data.credits
                ? `${data.credits.remaining} of ${data.credits.limit} credits left`
                : 'Credits unavailable';
            if (wasRunning && !data.running) { location.reload(); return; }
            wasRunning = data.running;
        } catch (error) {
            text.textContent = 'Credits unavailable';
        }
        if (wasRunning) setTimeout(poll, 6000);
    }
    poll();
}

/* Sending page: refresh while emails are going out */
const refreshMs = Number(document.body.querySelector('[data-autorefresh]')?.dataset.autorefresh);
if (refreshMs) setTimeout(() => location.reload(), refreshMs);

/* Select-all on the Sending page */
const sendForm = document.getElementById('send-form');
if (sendForm && document.getElementById('send-all')) {
    const boxes = [...sendForm.querySelectorAll('input[name="ids"]')];
    const all = document.getElementById('send-all');
    const button = document.getElementById('send-selected');
    const label = document.getElementById('send-count');
    const update = () => {
        const n = boxes.filter(box => box.checked).length;
        label.textContent = `${n} selected`;
        button.disabled = n === 0;
        all.checked = n > 0 && n === boxes.length;
    };
    all.addEventListener('change', () => { boxes.forEach(box => { box.checked = all.checked; }); update(); });
    sendForm.addEventListener('change', update);
    update();
}

/* Settings page: quick buttons and a credit estimate */
const settingsForm = document.getElementById('settings-form');
if (settingsForm) {
    for (const button of settingsForm.querySelectorAll('[data-set]')) {
        button.addEventListener('click', () => { settingsForm.elements[button.dataset.set].value = button.dataset.value; });
    }
    const note = document.getElementById('credit-note');
    const perDay = settingsForm.elements.searches_per_day;
    let credits = null;
    const estimate = () => {
        const days = settingsForm.querySelectorAll('input[name="days"]:checked').length;
        const perMonth = Math.round((Number(perDay.value) || 0) * days * 4.3);
        if (!credits) { note.textContent = 'Each search uses 1 Tavily credit.'; return; }
        note.textContent = perMonth > credits.limit
            ? `About ${perMonth} searches a month, but you have ${credits.limit} credits, so searching pauses when they run out.`
            : `About ${perMonth} searches a month. You have ${credits.remaining} of ${credits.limit} credits left.`;
    };
    fetch(settingsForm.dataset.statusUrl).then(r => r.json()).then(data => { credits = data.credits; estimate(); }).catch(() => {});
    settingsForm.addEventListener('input', estimate);
    settingsForm.addEventListener('change', estimate);
}
