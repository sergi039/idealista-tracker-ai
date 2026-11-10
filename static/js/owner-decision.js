// The owner's decision, written from a list row (#630).
//
// One delegated listener for the whole list. The endpoint and every sentence
// are on #owner-decision-config once, not on each row. Unlike
// recommendation-vote.js the write is immediate, not delayed: a decision must
// survive a fast click on a listing link. Undo is therefore a second write of
// the previous decision (null when the row was undecided).
(() => {
    const STATES = ['interested', 'waiting', 'rejected'];
    const REQUEST_TIMEOUT_MS = 12000;
    const QUEUE_FILTER = 'undecided';

    // `undecided` on the wire and null in the DOM are the same fact.
    const normalize = (state) => (STATES.includes(state) ? state : null);

    // --- pure decisions (no DOM; exercised under node by
    // tests/test_owner_decision_logic.py) ---------------------------------

    // What a row shows after a write.
    //   outcome 'ok'          the POST answered 2xx; serverState is its state
    //   outcome 'http_error'  a definite 4xx/5xx: the write did not happen
    //   outcome 'uncertain'   timeout / network error / unreadable answer: the
    //                         write may or may not have committed, so the
    //                         caller read the server (serverState) or could not
    //                         (serverState === undefined)
    // kind: 'saved' | 'failed' | 'unconfirmed'. `unconfirmed`
    // carries no state: it is NOT undecided and must not be drawn as such.
    function settleAfterWrite(outcome, previous, serverState, wanted) {
        if (outcome === 'http_error') {
            return {kind: 'failed', state: normalize(previous)};
        }
        if (outcome === 'ok') {
            return {kind: 'saved', state: normalize(serverState)};
        }
        if (serverState === undefined) {
            return {kind: 'unconfirmed', state: undefined};
        }
        // The server showing the wanted decision proves the write landed.
        // The server showing anything else proves nothing: the write may
        // still be waiting on the row lock and commit a moment after this
        // read. So that case is unconfirmed too, never "not saved".
        const state = normalize(serverState);
        if (state === normalize(wanted)) {
            return {kind: 'saved', state};
        }
        return {kind: 'unconfirmed', state: undefined};
    }

    // Where "next" should point. Under verdict=undecided the page is a queue:
    // a row that went from undecided to decided leaves it, so every later row
    // shifts up and the server-rendered "next" (page N+1 of the OLD queue)
    // would skip rows; the unseen rows now begin on the current page. Only
    // that move counts. A row already decided at load that is changed leaves
    // the queue unchanged, and one that is CLEARED joins the queue at its
    // sorted place, which can only push later rows down by one: "next" then
    // repeats a row at worst, it never skips one, so it is left alone. With no
    // such row (all undone) the original link is exactly right again.
    function nextLinkTarget(originalHref, currentHref, movedOutCount) {
        return movedOutCount > 0 ? currentHref : originalHref;
    }

    // Did this row's membership of the undecided queue change since load?
    function leftTheQueue(initialState, state) {
        return normalize(initialState) === null && normalize(state) !== null;
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {settleAfterWrite, nextLinkTarget, leftTheQueue};
    }
    if (typeof document === 'undefined') return;

    // --- DOM -------------------------------------------------------------

    const config = document.getElementById('owner-decision-config');
    if (!config) return;

    const initialStates = new Map(); // control -> state at load
    const movedOut = new Set(); // controls that left the undecided queue

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

    function warning(control) {
        let note = control.querySelector('[data-owner-decision-unconfirmed]');
        if (!note) {
            note = document.createElement('span');
            note.className = 'owner-decision-unconfirmed';
            note.setAttribute('data-owner-decision-unconfirmed', '');
            note.hidden = true;
            note.textContent = config.dataset.unconfirmed || '';
            control.appendChild(note);
        }
        return note;
    }

    // While a write is in flight or unconfirmed nothing else may be sent for
    // the row: two writes would race.
    function setLocked(control, locked) {
        for (const element of control.querySelectorAll('button')) {
            element.disabled = locked;
            if (locked) element.setAttribute('aria-disabled', 'true');
            else element.removeAttribute('aria-disabled');
        }
        control.dataset.busy = locked ? '1' : '0';
        if (locked) control.setAttribute('aria-busy', 'true');
        else control.removeAttribute('aria-busy');
    }

    // Draws a state on the row. Pure presentation: no counts are touched and
    // nothing reloads. `undefined` is "unknown": nothing pressed, not decided.
    function draw(control, state) {
        const unknown = state === undefined;
        for (const button of control.querySelectorAll('[data-decision]')) {
            const pressed = !unknown && button.dataset.decision === state;
            button.setAttribute('aria-pressed', String(pressed));
            button.classList.toggle('active', pressed);
        }
        warning(control).hidden = !unknown;
        const row = control.closest('.property-row, .listing-anchor');
        if (!row) return;
        row.classList.toggle('owner-decided', !unknown && state !== null);
        // The server-rendered badge describes the state at load time; once
        // the owner has pressed anything it can only be stale.
        for (const badge of row.querySelectorAll('[data-owner-badge]')) badge.hidden = true;
        for (const note of row.querySelectorAll('[data-owner-undecided]')) note.hidden = unknown || state !== null;
    }

    function syncNextLink() {
        if (new URLSearchParams(window.location.search).get('verdict') !== QUEUE_FILTER) return;
        for (const link of document.querySelectorAll('a[rel="next"]')) {
            // Remembered once, from the server-rendered link.
            if (!link.dataset.ownerOriginalHref) link.dataset.ownerOriginalHref = link.getAttribute('href') || '';
            link.setAttribute('href', nextLinkTarget(link.dataset.ownerOriginalHref, window.location.href, movedOut.size));
        }
    }

    function recordMembership(control, state) {
        if (leftTheQueue(initialStates.get(control), state)) movedOut.add(control);
        else movedOut.delete(control);
        syncNextLink();
    }

    // Resolves {outcome, state}; never rejects. A response that arrived with an
    // error status is definite; no response at all is not.
    async function post(control, wanted) {
        let response;
        try {
            response = await window.csrfFetch(urlFor(control.dataset.ownerDecisionId), {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({decision: wanted}),
                keepalive: true,
                signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
            });
        } catch {
            return {outcome: 'uncertain'};
        }
        if (!response.ok) return {outcome: 'http_error'};
        try {
            const result = await response.json();
            if (result && result.success === true) {
                return {outcome: 'ok', state: result.state, changed: result.changed === true};
            }
        } catch {
            // fall through: a 2xx we could not read says nothing reliable.
        }
        return {outcome: 'uncertain'};
    }

    // One read of the stored decision; undefined when it cannot be had.
    async function readStored(control) {
        try {
            const response = await window.csrfFetch(urlFor(control.dataset.ownerDecisionId), {
                method: 'GET',
                signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
            });
            if (!response.ok) return undefined;
            const result = await response.json();
            return result && result.success === true ? normalize(result.state) : undefined;
        } catch {
            return undefined;
        }
    }

    async function decide(control, wanted, isUndo) {
        if (control.dataset.busy === '1') return;
        if (!initialStates.has(control)) initialStates.set(control, currentState(control));
        const previous = currentState(control);
        const undo = undoButton(control);
        undo.hidden = true;
        draw(control, wanted);
        setLocked(control, true);
        let attempt = await post(control, wanted);
        let stored = attempt.state;
        if (attempt.outcome === 'uncertain') stored = await readStored(control);
        const settled = settleAfterWrite(attempt.outcome, previous, stored, wanted);
        draw(control, settled.state);
        if (settled.kind === 'unconfirmed') {
            // Stay locked: whatever the row would say next could be wrong.
            announce(control, config.dataset.unconfirmed || '');
            return;
        }
        recordMembership(control, settled.state);
        if (settled.kind === 'saved') {
            announce(control, settled.state
                ? (config.dataset.saved || '%s').replace('%s', labelFor(control, settled.state))
                : config.dataset.cleared || '');
            if (!isUndo && settled.state !== previous) {
                undo.dataset.restore = previous === null ? '' : previous;
                undo.hidden = false;
            }
        } else {
            announce(control, config.dataset.error || '');
        }
        setLocked(control, false);
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
