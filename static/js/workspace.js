/* Presentation preferences only. Listing state belongs to the URL/session. */
document.addEventListener('DOMContentLoaded', function () {
    const sidebar = document.getElementById('navbarNav');
    const menu = document.getElementById('workspace-menu');
    const collapse = document.getElementById('workspace-collapse');
    if (sidebar && menu && collapse) {
        const setCollapsed = (value) => {
            document.documentElement.dataset.navCollapsed = String(value);
            try { localStorage.setItem('workspace-nav-collapsed', String(value)); } catch (_) { /* Presentation can remain transient. */ }
        };
        collapse.addEventListener('click', () => { setCollapsed(true); menu.focus(); });
        menu.addEventListener('click', () => {
            if (window.matchMedia('(min-width: 1200px)').matches) {
                setCollapsed(false);
                collapse.focus();
            } else {
                bootstrap.Offcanvas.getOrCreateInstance(sidebar).show(menu);
            }
        });
        sidebar.addEventListener('hidden.bs.offcanvas', () => menu.focus());
    }

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
