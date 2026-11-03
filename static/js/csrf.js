/* The same session-bound token protects form, fetch and HTMX writes. */
(function () {
    function tokenFor(url, method) {
        if (/^(GET|HEAD|OPTIONS|TRACE)$/i.test(method)) return null;
        if (new URL(url, window.location.href).origin !== window.location.origin) return null;
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : null;
    }

    // Explicit helper: preserve native fetch for reads and outside services.
    window.csrfFetch = function (input, init = {}) {
        const request = input instanceof Request ? input : null;
        const token = tokenFor(request ? request.url : input, init.method || (request && request.method) || 'GET');
        if (!token) return fetch(input, init);
        const headers = new Headers(init.headers || (request && request.headers) || {});
        headers.set('X-CSRFToken', token);
        return fetch(input, { ...init, headers });
    };

    document.addEventListener('htmx:configRequest', function (event) {
        const token = tokenFor(event.detail.path, event.detail.verb);
        if (token) event.detail.headers['X-CSRFToken'] = token;
    });
})();
