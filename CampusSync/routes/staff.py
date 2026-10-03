"""Department Staff area.

Scope rule: a department staff member sees complaints routed to their own
department (plus anything explicitly assigned to them).  The department scope is
derived from the database on every request, never from the session.
"""

from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash

from models import (db, Complaint, ComplaintHistory, User, Department,
                    CMPL_OPEN, CMPL_IN_PROGRESS, CMPL_RESOLVED, CMPL_CLOSED,
                    CMPL_REOPENED, COMPLAINT_STATUSES, COMPLAINT_ACTIVE_STATUSES,
                    ROLE_STAFF)
from security import staff_required, current_user, notify, log_action, unread_count
from services import (change_complaint_status, can_transition, refresh_sla_flags,
                      complaint_sla_hours)

staff_bp = Blueprint('staff', __name__, url_prefix='/staff')


def my_department_id(user):
    return user.department_id


def scoped_complaints(user, status_filter=None, keyword=None, sla_only=False):
    """Complaints for my department, or assigned directly to me."""
    q = Complaint.query.filter(
        db.or_(Complaint.department_id == my_department_id(user),
               Complaint.assigned_to_id == user.id))
    if status_filter and status_filter in COMPLAINT_STATUSES:
        q = q.filter(Complaint.status == status_filter)
    if sla_only == '1':
        q = q.filter(Complaint.sla_breached.is_(True))
    if keyword:
        like = '%' + keyword + '%'
        q = q.filter(db.or_(Complaint.complaint_id.ilike(like),
                            Complaint.title.ilike(like),
                            Complaint.description.ilike(like)))
    return q.order_by(Complaint.created_at.desc()).all()


def get_scoped_complaint(user, complaint_pk):
    complaint = Complaint.query.get_or_404(complaint_pk)
    allowed = (complaint.department_id == my_department_id(user)
               or complaint.assigned_to_id == user.id)
    if not allowed:
        log_action(user, 'ACCESS_DENIED', module='complaint',
                   record_ref=complaint.complaint_id,
                   description='Staff tried to open a complaint outside their department')
        flash('You are not authorized to access this page.', 'error')
        return None
    return complaint


# ---------------------------------------------------------------------------

@staff_bp.route('/dashboard')
@staff_required
def dashboard():
    user = current_user()
    refresh_sla_flags()

    complaints = scoped_complaints(user)
    dept = user.department
    open_c = [c for c in complaints if c.status in (CMPL_OPEN, CMPL_REOPENED)]
    progress = [c for c in complaints if c.status == CMPL_IN_PROGRESS]
    resolved = [c for c in complaints if c.status == CMPL_RESOLVED]
    breached = [c for c in complaints if c.sla_breached]

    by_status = {s: 0 for s in COMPLAINT_STATUSES}
    for c in complaints:
        by_status[c.status] = by_status.get(c.status, 0) + 1

    return render_template('staff/dashboard.html', user=user, department=dept,
                           complaints=complaints[:8], total=len(complaints),
                           open_count=len(open_c), progress_count=len(progress),
                           resolved_count=len(resolved), breached=breached,
                           by_status=by_status,
                           unread_notifications=unread_count(user.id))


@staff_bp.route('/complaints')
@staff_required
def complaints():
    user = current_user()
    refresh_sla_flags()

    status_filter = request.args.get('status', '').strip().upper()
    keyword = request.args.get('q', '').strip()
    sla_only = request.args.get('sla', '').strip()

    return render_template('staff/complaints.html',
                           complaints=scoped_complaints(user, status_filter, keyword, sla_only),
                           statuses=COMPLAINT_STATUSES,
                           filters={'status': status_filter, 'q': keyword, 'sla': sla_only},
                           unread_notifications=unread_count(user.id))


@staff_bp.route('/complaints/<int:complaint_pk>', methods=['GET', 'POST'])
@staff_required
def complaint_detail(complaint_pk):
    user = current_user()
    complaint = get_scoped_complaint(user, complaint_pk)
    if complaint is None:
        return redirect(url_for('staff.complaints'))

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'status':
            new_status = request.form.get('status', '').strip().upper()
            note = request.form.get('note', '').strip()
            resolution = request.form.get('resolution_notes', '').strip()

            if new_status not in COMPLAINT_STATUSES:
                flash('Invalid complaint status.', 'error')
                return redirect(url_for('staff.complaint_detail', complaint_pk=complaint.id))

            if not can_transition(complaint.status, new_status):
                flash('Cannot change status from %s to %s.' % (complaint.status, new_status),
                      'error')
                return redirect(url_for('staff.complaint_detail', complaint_pk=complaint.id))

            if new_status == CMPL_RESOLVED and not resolution:
                flash('Resolution notes are required to resolve a complaint.', 'error')
                return redirect(url_for('staff.complaint_detail', complaint_pk=complaint.id))

            change_complaint_status(complaint, new_status, user,
                                    note=note or None,
                                    resolution_notes=resolution or None)

            if new_status == CMPL_RESOLVED:
                log_action(user, 'COMPLAINT_RESOLVED', module='complaint',
                           record_ref=complaint.complaint_id,
                           description=resolution[:200])
            db.session.commit()

            flash('Complaint %s status updated to %s.' % (complaint.complaint_id, new_status),
                  'success')
            return redirect(url_for('staff.complaint_detail', complaint_pk=complaint.id))

        if action == 'escalate':
            note = request.form.get('escalation_note', '').strip()
            if not note:
                flash('Please provide an escalation note.', 'error')
            else:
                complaint.escalation_note = note
                db.session.commit()
                admins = User.query.filter_by(role='admin', is_active=True).all()
                for a in admins:
                    notify(a.id, 'Complaint Escalated',
                           'Complaint %s was escalated by %s: %s' % (
                               complaint.complaint_id, user.full_name, note),
                           ntype='complaint', related_module='complaint',
                           related_id=complaint.complaint_id)
                log_action(user, 'COMPLAINT_ESCALATED', module='complaint',
                           record_ref=complaint.complaint_id, description=note)
                db.session.commit()
                flash('Complaint escalated to the administrator.', 'success')
            return redirect(url_for('staff.complaint_detail', complaint_pk=complaint.id))

    history = ComplaintHistory.query.filter_by(complaint_id=complaint.id).order_by(
        ComplaintHistory.created_at).all()
    allowed_next = [s for s in COMPLAINT_STATUSES if can_transition(complaint.status, s)]

    return render_template('staff/complaint_detail.html', complaint=complaint,
                           history=history, allowed_next=allowed_next,
                           unread_notifications=unread_count(user.id))