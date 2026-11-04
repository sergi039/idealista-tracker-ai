/* Presentation preferences only. Listing state belongs to the URL/session. */
document.addEventListener('DOMContentLoaded', function () {
    const sidebar = document.getElementById('navbarNav');
    const menu = document.getElementById('workspace-menu');
    const collapse = document.getElementById('workspace-collapse');
    if (sidebar && menu && collapse) {
        const desktop = window.matchMedia('(min-width: 1200px)');
        const setCollapsed = (value) => {
            document.documentElement.dataset.navCollapsed = String(value);
            try { localStorage.setItem('workspace-nav-collapsed', String(value)); } catch (_) { /* Presentation can remain transient. */ }
        };
        collapse.addEventListener('click', () => { setCollapsed(true); menu.focus(); });
        menu.addEventListener('click', () => {
            if (desktop.matches) {
                setCollapsed(false);
                collapse.focus();
            } else {
                bootstrap.Offcanvas.getOrCreateInstance(sidebar).show(menu);
            }
        });
        // The desktop sidebar stays fixed, so Bootstrap's position-based
        // responsive cleanup does not apply. Let its instance release the
        // backdrop, focus trap and body scroll lock before using the rail.
        window.addEventListener('resize', () => {
            if (desktop.matches) bootstrap.Offcanvas.getInstance(sidebar)?.hide();
        }, { passive: true });
        sidebar.addEventListener('hidden.bs.offcanvas', () => {
            const target = desktop.matches && document.documentElement.dataset.navCollapsed !== 'true' ? collapse : menu;
            target.focus();
        });
    }

    document.querySelectorAll('a.scope-switch[role="button"]').forEach(link => {
        link.addEventListener('keydown', event => {
            if (event.key === ' ') { event.preventDefault(); link.click(); }
        });
    });

    const search = document.getElementById('subscription-search');
    if (search) {
        search.addEventListener('input', () => {
            const query = search.value.toLocaleLowerCase().trim();
            document.querySelectorAll('#profile-select-menu .form-check').forEach(row => {
                row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
            });
        });
    }
});
