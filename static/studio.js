const queue = document.getElementById('permission-form');

if (queue) {
    const rows = [...queue.querySelectorAll('[data-contact-status]')];
    const search = document.getElementById('queue-search');
    const selectAll = document.getElementById('select-all-drafts');
    const confirm = document.getElementById('confirm-drafts');
    const count = document.getElementById('selection-count');
    const empty = document.getElementById('empty-queue');
    const controls = document.getElementById('permission-controls');
    const filters = [...document.querySelectorAll('[data-filter]')];
    let status = new URLSearchParams(window.location.search).get('view') === 'sent' ? 'sent' : 'draft';

    function visibleChecks() {
        return rows.filter(row => !row.hidden).flatMap(row => [...row.querySelectorAll('input[name="ids"]')]);
    }

    function updateSelection() {
        const checks = visibleChecks();
        const selected = checks.filter(check => check.checked).length;
        count.textContent = `${selected} selected`;
        confirm.disabled = selected === 0;
        controls.hidden = checks.length === 0;
        selectAll.disabled = checks.length === 0;
        selectAll.textContent = selected > 0 && selected === checks.length ? 'Clear selection' : 'Select visible drafts';
    }

    function updateRows() {
        const query = search.value.trim().toLowerCase();
        for (const row of rows) {
            const matchesStatus = status === 'all' || row.dataset.contactStatus === status;
            row.hidden = !matchesStatus || !row.dataset.contactSearch.toLowerCase().includes(query);
            const checkbox = row.querySelector('input[name="ids"]');
            if (row.hidden && checkbox) checkbox.checked = false;
        }
        empty.hidden = rows.some(row => !row.hidden);
        empty.firstElementChild.textContent = !query && status === 'draft'
            ? 'No drafts to review.' : 'No contacts match this view.';
        updateSelection();
    }

    function setFilter(value) {
        status = value;
        for (const filter of filters) filter.setAttribute('aria-pressed', filter.dataset.filter === status);
        updateRows();
    }

    for (const filter of filters) filter.addEventListener('click', () => setFilter(filter.dataset.filter));
    for (const shortcut of document.querySelectorAll('[data-filter-shortcut]')) {
        shortcut.addEventListener('click', () => {
            search.value = '';
            setFilter(shortcut.dataset.filterShortcut);
            document.getElementById('draft-queue').scrollIntoView({block: 'start'});
        });
    }
    search.addEventListener('input', updateRows);
    queue.addEventListener('change', updateSelection);
    selectAll.addEventListener('click', () => {
        const checks = visibleChecks();
        const select = checks.some(check => !check.checked);
        for (const check of checks) check.checked = select;
        updateSelection();
    });
    setFilter(status);
}

const researchSend = document.getElementById('research-send');
if (researchSend) {
    const checks = [...researchSend.querySelectorAll('input[name="ids"]')];
    const size = document.getElementById('batch-size');
    const count = document.getElementById('batch-count');
    const send = document.getElementById('send-batch');
    const pick = document.getElementById('pick-batch');
    function updateBatch() {
        const total = new Set(checks.filter(check => check.checked).map(check => check.value)).size;
        count.textContent = `${total} selected`;
        send.disabled = total === 0;
    }
    pick.disabled = checks.length === 0;
    pick.addEventListener('click', () => {
        if (!size.reportValidity() || !size.valueAsNumber) return;
        const batch = new Set([...new Set(checks.map(check => check.value))].slice(0, size.valueAsNumber));
        for (const check of checks) check.checked = batch.has(check.value);
        updateBatch();
    });
    researchSend.addEventListener('change', event => {
        const selection = event.target;
        if (selection.name === 'ids') {
            for (const check of checks) {
                if (check.value === selection.value) check.checked = selection.checked;
            }
        }
        updateBatch();
    });
    updateBatch();
}
