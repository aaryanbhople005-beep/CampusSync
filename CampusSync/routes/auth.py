"""Authentication for all five SRS roles.

Roles: student, warden, staff, lostfound, admin.
Every attempt (success or failure) is written to ``login_attempts`` and to
``audit_logs``; repeated failures lock the account for a configurable period.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session

from models import db, User
from security import (log_action, record_login_attempt, redirect_for_role,
                      get_setting_int, current_user)
from datetime import datetime, timedelta

auth_bp = Blueprint('auth', __name__)


def _locked_message(minutes):
    return ('Too many failed login attempts. This account is locked for %d minutes.'
            % minutes)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    # Already signed in?  Go straight to the correct home page.
    existing = current_user()
    if existing:
        return redirect_for_role(existing)

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        # --- TC04 / TC05: empty and malformed input --------------------
        if not username and not password:
            flash('Username is required.', 'error')
            flash('Password is required.', 'error')
            return render_template('login.html', username='')

        if not username:
            flash('Username is required.', 'error')
            return render_template('login.html', username='')

        if not password:
            flash('Password is required.', 'error')
            return render_template('login.html', username=username)

        # Very small sanity check on format - no spaces, printable characters only.
        if ' ' in username or len(username) > 80:
            flash('Username is invalid.', 'error')
            return render_template('login.html', username=username)

        user = User.query.filter_by(username=username).first()
        max_attempts = get_setting_int('max_failed_logins', 5)
        lockout_minutes = get_setting_int('lockout_minutes', 15)

        # --- TC02 / TC03: wrong password / unknown username -------------
        if not user or not user.check_password(password):
            record_login_attempt(username, False)
            if user:
                user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
                if user.failed_login_attempts >= max_attempts:
                    user.locked_until = datetime.utcnow() + timedelta(minutes=lockout_minutes)
                    user.failed_login_attempts = 0
                    db.session.commit()
                    log_action(user, 'ACCOUNT_LOCKED', module='auth',
                               description='Locked after %d failed logins' % max_attempts)
                    flash(_locked_message(lockout_minutes), 'error')
                    return render_template('login.html', username=username)
                db.session.commit()
                remaining = max_attempts - user.failed_login_attempts
                flash('Invalid username or password.', 'error')
                flash('%d login attempt(s) remaining before the account is locked.'
                      % remaining, 'error')
                log_action(user, 'LOGIN_FAILED', module='auth',
                           description='Invalid password (%d/%d)' % (
                               max_attempts - remaining, max_attempts))
            else:
                log_action(None, 'LOGIN_FAILED', module='auth',
                           description='Unknown username %r' % username)
                flash('Invalid username or password.', 'error')
            return render_template('login.html', username=username)

        # --- locked account ---------------------------------------------
        if user.is_locked:
            flash(_locked_message(lockout_minutes), 'error')
            log_action(user, 'LOGIN_BLOCKED_LOCKED', module='auth',
                       description='Login attempt on a locked account')
            return render_template('login.html', username=username)

        # --- deactivated account ----------------------------------------
        if not user.is_active:
            flash('Your account has been deactivated. Contact the administrator.', 'error')
            log_action(user, 'LOGIN_BLOCKED_INACTIVE', module='auth',
                       description='Login attempt on a deactivated account')
            return render_template('login.html', username=username)

        # --- TC01 / TC23: success ---------------------------------------
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = datetime.utcnow()
        session.clear()
        session['user_id'] = user.id
        session['username'] = user.username
        session['role'] = user.role
        session['full_name'] = user.full_name
        db.session.commit()

        record_login_attempt(username, True)
        log_action(user, 'LOGIN', module='auth',
                   description='%s logged in' % user.username)
        flash('Welcome, %s!' % user.full_name, 'success')

        return redirect_for_role(user)

    return render_template('login.html', username='')


@auth_bp.route('/logout')
def logout():
    user = current_user()
    if user:
        log_action(user, 'LOGOUT', module='auth',
                   description='%s logged out' % user.username)
    session.clear()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('auth.login'))