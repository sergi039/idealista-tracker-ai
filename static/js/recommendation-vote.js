// Delay the write itself so Undo requires no compensating request.
(() => {
    const undoDelayMs = 5000;
    const pending = new Map();
    const failures = new Set();
    let refresh = false;

    function finish() {
        if (refresh && !pending.size && !failures.size) {
            window.location.reload();
        }
    }

    for (const control of document.querySelectorAll('[data-recommendation-vote]')) {
        const buttons = [...control.querySelectorAll('[data-vote]')];
        const undo = control.querySelector('[data-vote-undo]');
        const status = control.querySelector('[data-vote-status]');
        const initialStatus = status.textContent.trim();
        const setBusy = (busy) => {
            for (const button of buttons) {
                button.disabled = busy;
            }
        };

        async function save(answer, source) {
            pending.get(control).timer = undefined;
            undo.hidden = true;
            status.textContent = control.dataset.saving;
            try {
                const response = await window.csrfFetch(control.dataset.url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({answer, revision: control.dataset.revision}),
                    signal: AbortSignal.timeout(12000),
                });
                if (!response.ok) {
                    throw new Error('Vote request failed');
                }
                const result = await response.json();
                control.dataset.answer = result.answer || '';
                control.dataset.revision = result.revision;
                for (const button of buttons) {
                    if (button.hasAttribute('aria-pressed')) {
                        const pressed = button.dataset.vote === result.answer;
                        button.setAttribute('aria-pressed', String(pressed));
                        button.classList.toggle('active', pressed);
                    }
                }
                status.textContent = control.dataset.saved;
                failures.delete(control);
                if (control.dataset.refresh === 'yes') {
                    refresh = true;
                    if (result.answer === 'no') {
                        control.closest('.property-row, [data-recommendation-card]').hidden = true;
                    }
                } else {
                    source.hidden = true;
                }
            } catch {
                status.textContent = control.dataset.error;
                failures.add(control);
            } finally {
                pending.delete(control);
                setBusy(false);
                if (!source.hidden && document.activeElement === document.body &&
                    !control.closest('[hidden]')) {
                    source.focus({preventScroll: true});
                }
                finish();
            }
        }

        function cancelPending() {
            const operation = pending.get(control);
            if (!operation || operation.timer === undefined) return;
            window.clearTimeout(operation.timer);
            pending.delete(control);
            undo.hidden = true;
            status.textContent = initialStatus;
            setBusy(false);
            return operation.source;
        }

        undo.addEventListener('click', () => {
            cancelPending()?.focus();
            finish();
        });
        window.addEventListener('pagehide', cancelPending);

        for (const button of buttons) {
            button.addEventListener('click', () => {
                if (pending.has(control)) return;
                const selected = button.dataset.vote;
                const answer = selected && selected !== control.dataset.answer ? selected : null;
                setBusy(true);
                const operation = {source: button};
                pending.set(control, operation);
                if (answer === 'no') {
                    undo.hidden = false;
                    status.textContent = control.dataset.pending;
                    undo.focus({preventScroll: true});
                    operation.timer = window.setTimeout(() => save(answer, button), undoDelayMs);
                } else {
                    save(answer, button);
                }
            });
        }
        control.hidden = false;
    }
    window.addEventListener('pageshow', (event) => {
        if (event.persisted) finish();
    });
})();
