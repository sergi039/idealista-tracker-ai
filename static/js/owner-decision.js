// The owner's decision, written from a list row (#630).
//
// One delegated listener for the whole list. The endpoint and every sentence
// are on #owner-decision-config once, not on each row. Unlike
// recommendation-vote.js the write is immediate, not delayed: a decision must
// survive a fast click on a listing link. Undo is therefore a second write of
// the previous decision (null when the row was undecided).
(() => {
    const config = document.getElementById('owner-decision-config');
    if (!config) return;

    const STATES = ['interested', 'waiting', 'rejected'];
    const REQUEST_TIMEOUT_MS = 12000;
    const DEFAULT_STATE_EXPR = 'undecided';

    const urlFor = (id) => config.dataset.urlTemplate.replace('/0/', `/${encodeURIComponent(id)}/`);

    function currentState(control) {
        const pressed = control.querySelector('[data-decision][aria-pressed="true"]');
        return pressed ? pressed.dataset.decision : null;
    }

    function labelFor(control, state) {
        const button = control.querySelector(`[data-decision="${state}"]`);
        return button ? button.textContent.trim() : '';
    }

    function announce(control, message) {
        const status = control.querySelector('[data-owner-decision-status]');
        if (status) status.textContent = message;
    }

    function undoButton(control) {
        let undo = control.querySelector('[data-owner-decision-undo]');
        if (!undo) {
            undo = document.createElement('button');
            undo.type = 'button';
            undo.className = 'btn btn-sm btn-link owner-decision-undo';
            undo.setAttribute('data-owner-decision-undo', '');
            undo.hidden = true;
            undo.textContent = config.dataset.undo || '';
            const group = control.querySelector('.btn-group');
            if (group) group.after(undo);
            else control.appendChild(undo);
        }
        return undo;
    }

    // Draws a state on the row. Pure presentation: no counts are touched and
    // nothing reloads; the owner's list stays where it is.
    function draw(control, state) {
        for (const button of control.querySelectorAll('[data-decision]')) {
            const pressed = button.dataset.decision === state;
            button.setAttribute('aria-pressed', String(pressed));
            button.classList.toggle('active', pressed);
        }
        const row = control.closest('.property-row, .listing-anchor');
        if (!row) return;
        row.classList.toggle('owner-decided', state !== null);
        // The server-rendered badge describes the state at load time; once
        // the owner has pressed anything it can only be stale.
        for (const badge of row.querySelectorAll('[data-owner-badge]')) badge.hidden = true;
        for (const note of row.querySelectorAll('[data-owner-undecided]')) note.hidden = state !== null;
    }

    // Under `verdict=undecided` the page is a queue: deciding a row removes it
    // from the result set, so every later row shifts up by the rows just
    // decided. The server-rendered "next" link still points at page N+1 of the
    // old queue and would skip those rows; the page the owner is on is where
    // the unseen rows now begin.
    function repairNextLink() {
        if (new URLSearchParams(window.location.search).get('verdict') !== DEFAULT_STATE_EXPR) return;
        for (const link of document.querySelectorAll('a[rel="next"]')) {
            link.setAttribute('href', window.location.href);
        }
    }

    async function write(control, wanted) {
        const response = await window.csrfFetch(urlFor(control.dataset.propertyId), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({decision: wanted}),
            keepalive: true,
            signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
        if (!response.ok) throw new Error('Decision request failed');
        const result = await response.json();
        if (!result || result.success !== true) throw new Error('Decision was not saved');
        return result;
    }

    async function decide(control, wanted, isUndo) {
        if (control.dataset.busy === '1') return;
        const previous = currentState(control);
        const undo = undoButton(control);
        control.dataset.busy = '1';
        control.setAttribute('aria-busy', 'true');
        draw(control, wanted);
        undo.hidden = true;
        try {
            const result = await write(control, wanted);
            const saved = STATES.includes(result.state) ? result.state : null;
            draw(control, saved);
            announce(control, saved
                ? (config.dataset.saved || '%s').replace('%s', labelFor(control, saved))
                : config.dataset.cleared || '');
            if (!isUndo && result.changed) {
                undo.dataset.restore = previous === null ? '' : previous;
                undo.hidden = false;
            }
            if (result.changed) repairNextLink();
        } catch {
            draw(control, previous);
            announce(control, config.dataset.error || '');
        } finally {
            control.dataset.busy = '0';
            control.removeAttribute('aria-busy');
        }
    }

    document.addEventListener('click', (event) => {
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        const undo = target.closest('[data-owner-decision-undo]');
        if (undo) {
            const control = undo.closest('[data-owner-decision]');
            if (!control) return;
            const restore = undo.dataset.restore || null;
            decide(control, restore, true).then(() => {
                const focus = control.querySelector('[data-decision]');
                if (focus && document.activeElement === document.body) {
                    focus.focus({preventScroll: true});
                }
            });
            return;
        }
        const button = target.closest('[data-owner-decision] [data-decision]');
        if (!button) return;
        const control = button.closest('[data-owner-decision]');
        if (!control) return;
        // Pressing the pressed button clears the decision, like Undo.
        const wanted = currentState(control) === button.dataset.decision ? null : button.dataset.decision;
        decide(control, wanted, false);
    });
})();
