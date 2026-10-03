"""Authorisation, audit logging and configurable settings helpers.

All authorisation decisions are made server-side from the *session user id*;
the role stored in the browser session is only a cached copy that is re-verified
against the database on every request by :func:`current_user`.
"""

from functools import wraps

from flask import session, redirect, url_for, flash, request

from models import (db, User, AuditLog, LoginAttempt, Setting, Notification,
                    ROLE_ADMIN, ROLE_WARDEN, ROLE_STAFF, ROLE_LOSTFUND,
                    ROLE_STUDENT, ROLE_HOME)

# Default values for every configurable business rule (SRS marks the concrete
# university numbers as TBD, so development defaults are used and documented).
DEFAULT_SETTINGS = {
    'complaint_sla_hours': ('72', 'Hours an OPEN complaint may stay unresolved before it is flagged as an SLA breach.'),
    'found_item_retention_days': ('30', 'Days a FOUND item may remain unclaimed before it is flagged for administrator review.'),
    'complaint_reopen_allowed': ('true', 'Whether a student may reopen a resolved complaint.'),
    'complaint_reopen_window_days': ('7', 'Days after RESOLVED within which a student may reopen the complaint.'),
    'max_failed_logins': ('5', 'Failed login attempts before the account is temporarily locked.'),
    'lockout_minutes': ('15', 'Minutes an account stays locked after too many failed logins.'),
    'complaint_allow_file_upload': ('true', 'Whether students may attach a file to a complaint.'),
    'max_attachment_mb': ('2', 'Maximum attachment size in megabytes.'),
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    row = Setting.query.filter_by(key=key).first()
    if row and row.value is not None:
        return row.value
    if key in DEFAULT_SETTINGS:
        return DEFAULT_SETTINGS[key][0]
    return default


def get_setting_int(key, default=0):
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def get_setting_bool(key, default=False):
    value = get_setting(key)
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def seed_default_settings():
    for key, (value, description) in DEFAULT_SETTINGS.items():
        if not Setting.query.filter_by(key=key).first():
            db.session.add(Setting(key=key, value=value, description=description))
    db.session.commit()


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------

def current_user():
    """Load the user from the database on every request (never trust the session role)."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    return db.session.get(User, user_id)


def _client_ip():
    return request.remote_addr


# ---------------------------------------------------------------------------
# Authorisation decorators
# ---------------------------------------------------------------------------

def _deny():
    """SRS wording for a blocked cross-role attempt."""
    flash('You are not authorized to access this page.', 'error')
    return redirect(url_for('auth.login'))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('auth.login'))
        if not user.is_active:
            session.clear()
            flash('Your account has been deactivated. Contact the administrator.', 'error')
            return redirect(url_for('auth.login'))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    """Allow only the listed roles.  Anything else gets the SRS error message."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash('Please log in to access this page.', 'error')
                return redirect(url_for('auth.login'))
            if not user.is_active:
                session.clear()
                flash('Your account has been deactivated. Contact the administrator.', 'error')
                return redirect(url_for('auth.login'))
            if user.role not in roles:
                log_action(user, 'ACCESS_DENIED',
                           module=roles[0],
                           description='Role %s tried to open %s' % (user.role, request.path))
                return _deny()
            return view(*args, **kwargs)
        return wrapped
    return decorator


def student_required(view):
    return role_required(ROLE_STUDENT)(view)


def warden_required(view):
    return role_required(ROLE_WARDEN)(view)


def staff_required(view):
    return role_required(ROLE_STAFF)(view)


def lostfound_required(view):
    return role_required(ROLE_LOSTFUND)(view)


def admin_required(view):
    return role_required(ROLE_ADMIN)(view)


def redirect_for_role(user):
    endpoint = ROLE_HOME.get(user.role)
    if endpoint:
        return redirect(url_for(endpoint))
    return redirect(url_for('auth.login'))


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def log_action(user, action, module=None, record_ref=None, description=None, commit=True):
    """Write one audit_logs row.  Never raises into the request path."""
    try:
        username = user.username if user else None
        entry = AuditLog(
            user_id=user.id if user else None,
            username=username,
            action=action,
            module=module,
            record_ref=record_ref,
            description=description,
            ip_address=_client_ip(),
        )
        db.session.add(entry)
        if commit:
            db.session.commit()
        return entry
    except Exception:  # auditing must never break a business action
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def record_login_attempt(username, success):
    try:
        db.session.add(LoginAttempt(username=username, success=success,
                                     ip_address=_client_ip()))
        db.session.commit()
    except Exception:
        db.session.rollback()


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------

def notify(user_id, title, message, ntype='system', related_module=None, related_id=None):
    """Create a notification for a single recipient."""
    if not user_id:
        return None
    n = Notification(user_id=user_id, title=title, message=message, ntype=ntype,
                     related_module=related_module,
                     related_id=str(related_id) if related_id else None)
    db.session.add(n)
    return n


def notify_many(user_ids, title, message, ntype='system', related_module=None, related_id=None):
    for uid in user_ids:
        notify(uid, title, message, ntype, related_module, related_id)


def unread_count(user_id):
    return Notification.query.filter_by(user_id=user_id, is_read=False).count()