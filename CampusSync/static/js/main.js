document.addEventListener('DOMContentLoaded', function () {
    setupTabs();
    setupFlashAutoHide();
    setupModals();
    setupHostelRequestTypeToggle();
    setupDashboardBackGuard();
});

/* ---------------------------------------------------------------- tabs --- */
function setupTabs() {
    var tabButtons = document.querySelectorAll('.tab-btn[data-tab]');
    var tabContents = document.querySelectorAll('.tab-content');

    tabButtons.forEach(function (btn) {
        btn.addEventListener('click', function () {
            var target = btn.getAttribute('data-tab');
            tabButtons.forEach(function (b) { b.classList.remove('active'); });
            tabContents.forEach(function (c) { c.classList.remove('active'); });
            btn.classList.add('active');
            var panel = document.getElementById(target);
            if (panel) { panel.classList.add('active'); }
        });
    });
}

/* -------------------------------------------------------- flash notice --- */
function setupFlashAutoHide() {
    var flashes = document.querySelectorAll('.flash');
    if (!flashes.length) { return; }
    setTimeout(function () {
        flashes.forEach(function (f) {
            f.style.transition = 'opacity .4s';
            f.style.opacity = '0';
            setTimeout(function () { f.remove(); }, 400);
        });
    }, 6000);
}

/* -------------------------------------------------------------- modals --- */
function setupModals() {
    document.querySelectorAll('[data-modal-open]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var el = document.getElementById(btn.getAttribute('data-modal-open'));
            if (el) { el.classList.add('open'); }
        });
    });

    document.querySelectorAll('[data-modal-close]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var el = document.getElementById(btn.getAttribute('data-modal-close'));
            if (el) { el.classList.remove('open'); }
        });
    });

    document.querySelectorAll('.modal').forEach(function (modal) {
        modal.addEventListener('click', function (e) {
            if (e.target === modal) { modal.classList.remove('open'); }
        });
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal.open').forEach(function (m) {
                m.classList.remove('open');
            });
        }
    });
}

/* ------------------------------------------- hostel request type fields --- */
function setupHostelRequestTypeToggle() {
    var radios = document.querySelectorAll('input[name="request_type"]');
    if (!radios.length) { return; }

    var sections = document.querySelectorAll('[data-section]');

    function apply() {
        var selected = document.querySelector('input[name="request_type"]:checked');
        var requires = selected ? (selected.getAttribute('data-requires') || '') : '';
        sections.forEach(function (s) {
            var name = s.getAttribute('data-section');
            var show = (name === 'allocation' && !requires) || name === requires;
            s.style.display = show ? '' : 'none';
        });
    }

    radios.forEach(function (r) { r.addEventListener('change', apply); });
    apply();
}

/* ------------------------------- admin notification target radio group --- */
function toggleStudentSelect() {
    var specific = document.querySelector('input[name="target"][value="specific"]');
    var group = document.getElementById('student-select-group');
    if (specific && group) {
        group.style.display = specific.checked ? '' : 'none';
    }
}

/* ------------------------------------- back-from-dashboard logs the user out --- */
function setupDashboardBackGuard() {
    var body = document.body;
    if (!body || body.dataset.guardExit !== 'true') { return; }

    var logoutUrl = body.dataset.logoutUrl;
    if (!logoutUrl || !window.history || !history.pushState) { return; }

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