document.addEventListener('DOMContentLoaded', function () {
    var tabButtons = document.querySelectorAll('.tab-btn');
    var tabContents = document.querySelectorAll('.tab-content');

    tabButtons.forEach(function (btn) {
        btn.addEventListener('click', function () {
            var target = btn.getAttribute('data-tab');

            tabButtons.forEach(function (b) { b.classList.remove('active'); });
            tabContents.forEach(function (c) { c.classList.remove('active'); });

            btn.classList.add('active');
            var panel = document.getElementById(target);
            if (panel) {
                panel.classList.add('active');
            }
        });
    });

    var flashes = document.querySelectorAll('.flash');
    if (flashes.length) {
        setTimeout(function () {
            flashes.forEach(function (f) {
                f.style.transition = 'opacity .4s';
                f.style.opacity = '0';
                setTimeout(function () { f.remove(); }, 400);
            });
        }, 5000);
    }

    setupDashboardBackGuard();
});

function setupDashboardBackGuard() {
    var body = document.body;
    if (!body || body.dataset.guardExit !== 'true') {
        return;
    }

    var logoutUrl = body.dataset.logoutUrl;
    if (!logoutUrl || !window.history || !history.pushState) {
        return;
    }

    history.replaceState({ campusDashboard: true }, '', location.href);
    history.pushState({ campusGuard: true }, '', location.href);

    window.addEventListener('popstate', function (e) {
        if (e.state && e.state.campusDashboard) {
            location.replace(logoutUrl);
            return;
        }
        if (e.state && e.state.campusGuard) {
            history.pushState({ campusGuard: true }, '', location.href);
        }
    });
}
