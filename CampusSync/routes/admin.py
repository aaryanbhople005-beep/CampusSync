"""Administrator area: analytics, configuration, user management, audit log.

The original admin URLs (/admin/dashboard, /admin/complaints,
/admin/complaints/update-status, /admin/hostel-requests,
/admin/hostel-requests/update-status, /admin/lost-found, /admin/students,
/admin/notifications) are preserved so existing flows keep working.
"""

import csv
import io
from datetime import datetime, date, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for, flash,
                   Response, session)

from models import (db, User, Complaint, ComplaintCategory, Department, HostelBlock,
                    HostelRequest, HostelNotice, LostFoundItem, Claim, Notification,
                    AuditLog, RoomAllocation, Setting, ROLE_LABELS, ALL_ROLES,
                    ROLE_ADMIN, ROLE_STUDENT, ROLE_WARDEN, ROLE_STAFF, ROLE_LOSTFUND,
                    COMPLAINT_STATUSES, CMPL_OPEN, CMPL_IN_PROGRESS, CMPL_RESOLVED,
                    CMPL_CLOSED, CMPL_REOPENED, HOSTEL_REQUEST_STATUSES,
                    REQ_ALLOCATION, REQ_MAINTENANCE, REQ_OUTPASS,
                    ITEM_LOST, ITEM_FOUND, LF_OPEN, LF_CLAIMED, CLAIM_APPROVED)
from security import (admin_required, current_user, notify, log_action, unread_count,
                      get_setting, DEFAULT_SETTINGS)
from services import (change_complaint_status, can_transition, set_hostel_request_status,
                      refresh_sla_flags, refresh_retention_flags)

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def apply_filters(query, model, args, date_field=None):
    """Shared date-range / status / department / block filtering."""
    start = _parse_date(args.get('from'))
    end = _parse_date(args.get('to'))
    if date_field is not None and start:
        query = query.filter(date_field >= datetime.combine(start, datetime.min.time()))
    if date_field is not None and end:
        query = query.filter(date_field <= datetime.combine(end, datetime.max.time()))

    status = args.get('status', '').strip().upper()
    if status:
        query = query.filter(model.status == status)

    dept = args.get('department', '').strip()
    if dept.isdigit():
        query = query.filter(model.department_id == int(dept))
    return query


def complaint_analytics(filters=None):
    filters = filters or {}
    q = Complaint.query
    q = apply_filters(q, Complaint, filters, Complaint.created_at)

    cat = filters.get('category', '').strip()
    if cat.isdigit():
        q = q.filter(Complaint.category_id == int(cat))

    complaints = q.all()

    by_status = {s: 0 for s in COMPLAINT_STATUSES}
    for c in complaints:
        by_status[c.status] = by_status.get(c.status, 0) + 1

    by_category = {}
    for c in complaints:
        key = c.category or 'Uncategorised'
        by_category[key] = by_category.get(key, 0) + 1

    by_department = {}
    for c in complaints:
        if c.department:
            by_department[c.department.name] = by_department.get(c.department.name, 0) + 1
        else:
            by_department['Unassigned'] = by_department.get('Unassigned', 0) + 1

    # Average resolution time in hours, over complaints that were resolved.
    durations = []
    for c in complaints:
        if c.resolved_at and c.created_at:
            durations.append((c.resolved_at - c.created_at).total_seconds() / 3600.0)
    avg_resolution = round(sum(durations) / len(durations), 1) if durations else 0

    return {
        'total': len(complaints),
        'open': by_status.get(CMPL_OPEN, 0),
        'reopened': by_status.get(CMPL_REOPENED, 0),
        'in_progress': by_status.get(CMPL_IN_PROGRESS, 0),
        'resolved': by_status.get(CMPL_RESOLVED, 0),
        'closed': by_status.get(CMPL_CLOSED, 0),
        'by_status': by_status,
        'by_category': by_category,
        'by_department': by_department,
        'avg_resolution_hours': avg_resolution,
        'sla_breached': len([c for c in complaints if c.sla_breached]),
    }


def hostel_analytics(filters=None):
    filters = filters or {}
    q = HostelRequest.query
    q = apply_filters(q, HostelRequest, filters, HostelRequest.created_at)

    req_type = filters.get('type', '').strip().upper()
    if req_type in (REQ_ALLOCATION, REQ_MAINTENANCE, REQ_OUTPASS):
        q = q.filter(HostelRequest.request_type == req_type)

    block = filters.get('block', '').strip()
    if block.isdigit():
        q = q.filter(HostelRequest.hostel_block_id == int(block))

    requests = q.all()
    by_status = {s: 0 for s in HOSTEL_REQUEST_STATUSES}
    for r in requests:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    by_type = {}
    for r in requests:
        key = r.request_type or REQ_ALLOCATION
        by_type[key] = by_type.get(key, 0) + 1

    return {
        'total': len(requests),
        'by_status': by_status,
        'by_type': by_type,
        'pending': by_status.get('PENDING', 0),
        'approved': by_status.get('APPROVED', 0),
        'rejected': by_status.get('REJECTED', 0),
        'clarification': by_status.get('CLARIFICATION REQUIRED', 0),
        'completed': by_status.get('COMPLETED', 0),
    }


def lostfound_analytics(filters=None):
    filters = filters or {}
    q = LostFoundItem.query
    q = apply_filters(q, LostFoundItem, filters, LostFoundItem.created_at)

    category = filters.get('category', '').strip()
    if category:
        q = q.filter(LostFoundItem.item_category == category)

    items = q.all()
    lost = [i for i in items if i.item_type == ITEM_LOST]
    found = [i for i in items if i.item_type == ITEM_FOUND]
    active_found = [i for i in found if i.status == LF_OPEN]
    claimed = [i for i in items if i.status == LF_CLAIMED]

    # Claim rate = approved claims / total claims raised.
    c = Claim.query
    total_claims = c.count()
    approved = c.filter_by(status=CLAIM_APPROVED).count()
    claim_rate = round((approved / total_claims) * 100, 1) if total_claims else 0.0

    return {
        'total': len(items),
        'lost': len(lost),
        'found': len(found),
        'active_found': len(active_found),
        'claimed': len(claimed),
        'claim_rate': claim_rate,
        'retention_flagged': len([i for i in active_found if i.retention_flag]),
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    user = current_user()
    refresh_sla_flags()
    refresh_retention_flags()

    filters = {k: request.args.get(k, '') for k in
               ('from', 'to', 'status', 'department', 'category', 'type', 'block')}

    c = complaint_analytics(filters)
    h = hostel_analytics(filters)
    l = lostfound_analytics(filters)

    breached = Complaint.query.filter_by(sla_breached=True).all()
    retention = LostFoundItem.query.filter_by(retention_flag=True, status=LF_OPEN).all()

    return render_template(
        'admin/dashboard.html',
        total_students=User.query.filter_by(role=ROLE_STUDENT, is_active=True).count(),
        total_users=User.query.filter_by(is_active=True).count(),
        c=c, h=h, l=l,
        breached_complaints=breached,
        retention_items=retention,
        departments=Department.query.filter_by(is_active=True).order_by(Department.name).all(),
        categories=ComplaintCategory.query.filter_by(is_active=True).order_by(
            ComplaintCategory.name).all(),
        blocks=HostelBlock.query.filter_by(is_active=True).order_by(HostelBlock.name).all(),
        filters=filters,
        unread_notifications=unread_count(user.id))


# ---------------------------------------------------------------------------
# Complaints
# ---------------------------------------------------------------------------

@admin_bp.route('/complaints')
@admin_required
def complaints():
    user = current_user()
    refresh_sla_flags()

    filters = {k: request.args.get(k, '') for k in
               ('from', 'to', 'status', 'department', 'category', 'q')}
    q = Complaint.query
    q = apply_filters(q, Complaint, filters, Complaint.created_at)
    if filters.get('department', '').isdigit():
        q = q.filter(Complaint.department_id == int(filters['department']))
    if filters.get('category', '').isdigit():
        q = q.filter(Complaint.category_id == int(filters['category']))
    if filters.get('q'):
        like = '%' + filters['q'] + '%'
        q = q.filter(db.or_(Complaint.complaint_id.ilike(like),
                            Complaint.title.ilike(like)))

    return render_template('admin/complaints.html', complaints=q.order_by(
        Complaint.created_at.desc()).all(), statuses=COMPLAINT_STATUSES,
        departments=Department.query.order_by(Department.name).all(),
        categories=ComplaintCategory.query.order_by(ComplaintCategory.name).all(),
        filters=filters, unread_notifications=unread_count(user.id))


@admin_bp.route('/complaints/update-status', methods=['POST'])
@admin_required
def update_complaint_status():
    """Kept for the original admin complaint workflow (TC13)."""
    user = current_user()
    complaint_id = request.form.get('complaint_id', '').strip()
    new_status = request.form.get('status', '').strip().upper()

    if not complaint_id or not new_status:
        flash('Complaint ID and status are required.', 'error')
        return redirect(url_for('admin.complaints'))

    if new_status not in COMPLAINT_STATUSES:
        flash('Invalid complaint status.', 'error')
        return redirect(url_for('admin.complaints'))

    complaint = Complaint.query.filter_by(complaint_id=complaint_id).first()
    if not complaint:
        flash('Complaint not found.', 'error')
        return redirect(url_for('admin.complaints'))

    if not can_transition(complaint.status, new_status):
        flash('Cannot change status from %s to %s.' % (complaint.status, new_status), 'error')
        return redirect(url_for('admin.complaints'))

    change_complaint_status(complaint, new_status, user, note='Updated by administrator')
    db.session.commit()

    flash('Complaint %s status updated to %s.' % (complaint_id, new_status), 'success')
    return redirect(url_for('admin.complaints'))


@admin_bp.route('/complaints/<int:complaint_pk>')
@admin_required
def complaint_detail(complaint_pk):
    complaint = Complaint.query.get_or_404(complaint_pk)
    from models import ComplaintHistory
    history = ComplaintHistory.query.filter_by(complaint_id=complaint.id).order_by(
        ComplaintHistory.created_at).all()
    allowed_next = [s for s in COMPLAINT_STATUSES if can_transition(complaint.status, s)]
    return render_template('admin/complaint_detail.html', complaint=complaint,
                           history=history, allowed_next=allowed_next,
                           statuses=COMPLAINT_STATUSES,
                           staff_members=User.query.filter_by(role=ROLE_STAFF,
                                                              is_active=True).order_by(
                               User.full_name).all(),
                           unread_notifications=unread_count(current_user().id))


@admin_bp.route('/complaints/<int:complaint_pk>/assign', methods=['POST'])
@admin_required
def assign_complaint(complaint_pk):
    user = current_user()
    complaint = Complaint.query.get_or_404(complaint_pk)
    staff_id = request.form.get('assigned_to_id', '').strip()

    if not staff_id or not staff_id.isdigit():
        flash('Please select department staff to assign this complaint to.', 'error')
        return redirect(url_for('admin.complaint_detail', complaint_pk=complaint.id))

    assignee = User.query.get(int(staff_id))
    if not assignee or assignee.role not in (ROLE_STAFF,):
        flash('Please select a valid department staff member.', 'error')
        return redirect(url_for('admin.complaint_detail', complaint_pk=complaint.id))

    previous = complaint.assigned_to_id
    complaint.assigned_to_id = assignee.id
    if assignee.department_id and not complaint.department_id:
        complaint.department_id = assignee.department_id
    db.session.commit()

    notify(assignee.id, 'Complaint Assigned',
           'Complaint %s (%s) has been assigned to you.' % (complaint.complaint_id,
                                                            complaint.title),
           ntype='complaint', related_module='complaint', related_id=complaint.complaint_id)
    if previous and previous != assignee.id:
        notify(previous, 'Complaint Reassigned',
               'Complaint %s has been reassigned to %s.' % (complaint.complaint_id,
                                                             assignee.full_name),
               ntype='complaint', related_module='complaint',
               related_id=complaint.complaint_id)

    log_action(user, 'COMPLAINT_ASSIGNED', module='complaint',
               record_ref=complaint.complaint_id,
               description='Assigned to %s' % assignee.username)
    db.session.commit()
    flash('Complaint %s assigned to %s.' % (complaint.complaint_id, assignee.full_name),
          'success')
    return redirect(url_for('admin.complaint_detail', complaint_pk=complaint.id))


# ---------------------------------------------------------------------------
# Hostel requests
# ---------------------------------------------------------------------------

@admin_bp.route('/hostel-requests')
@admin_required
def hostel_requests():
    user = current_user()
    filters = {k: request.args.get(k, '') for k in ('from', 'to', 'status', 'type', 'block')}
    q = apply_filters(HostelRequest.query, HostelRequest, filters, HostelRequest.created_at)
    if filters.get('block', '').isdigit():
        q = q.filter(HostelRequest.hostel_block_id == int(filters['block']))
    return render_template('admin/hostel_requests.html', requests=q.order_by(
        HostelRequest.created_at.desc()).all(), statuses=HOSTEL_REQUEST_STATUSES,
        blocks=HostelBlock.query.order_by(HostelBlock.name).all(),
        filters=filters, unread_notifications=unread_count(user.id))


@admin_bp.route('/hostel-requests/update-status', methods=['POST'])
@admin_required
def update_hostel_status():
    user = current_user()
    request_id = request.form.get('request_id', '').strip()
    new_status = request.form.get('status', '').strip().upper()

    if not request_id or not new_status:
        flash('Request ID and status are required.', 'error')
        return redirect(url_for('admin.hostel_requests'))

    if new_status not in HOSTEL_REQUEST_STATUSES:
        flash('Invalid hostel request status.', 'error')
        return redirect(url_for('admin.hostel_requests'))

    req = HostelRequest.query.filter_by(request_id=request_id).first()
    if not req:
        flash('Hostel request not found.', 'error')
        return redirect(url_for('admin.hostel_requests'))

    set_hostel_request_status(req, new_status, user)
    db.session.commit()

    flash('Hostel request %s status updated to %s.' % (request_id, new_status), 'success')
    return redirect(url_for('admin.hostel_requests'))


# ---------------------------------------------------------------------------
# Lost & found overview
# ---------------------------------------------------------------------------

@admin_bp.route('/lost-found')
@admin_required
def lost_found():
    user = current_user()
    refresh_retention_flags()
    filters = {k: request.args.get(k, '') for k in ('from', 'to', 'status', 'category')}
    q = apply_filters(LostFoundItem.query, LostFoundItem, filters, LostFoundItem.created_at)
    if filters.get('category'):
        q = q.filter(LostFoundItem.item_category == filters['category'])

    return render_template('admin/lost_found.html', items=q.order_by(
        LostFoundItem.created_at.desc()).all(),
        categories=[c[0] for c in db.session.query(
            LostFoundItem.item_category).distinct().all() if c[0]],
        filters=filters, unread_notifications=unread_count(user.id))


@admin_bp.route('/students')
@admin_required
def students():
    user = current_user()
    role_filter = request.args.get('role', ROLE_STUDENT).strip()
    q = User.query
    if role_filter in ALL_ROLES:
        q = q.filter(User.role == role_filter)
    return render_template('admin/students.html', students=q.order_by(User.full_name).all(),
                           roles=ALL_ROLES, role_labels=ROLE_LABELS,
                           filters={'role': role_filter},
                           departments=Department.query.order_by(Department.name).all(),
                           unread_notifications=unread_count(user.id))


# ---------------------------------------------------------------------------
# Users / roles / departments / categories / blocks
# ---------------------------------------------------------------------------

@admin_bp.route('/users', methods=['GET', 'POST'])
@admin_required
def users():
    user = current_user()

    if request.method == 'POST':
        action = request.form.get('action', 'create')

        if action == 'create':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            full_name = request.form.get('full_name', '').strip()
            email = request.form.get('email', '').strip()
            role = request.form.get('role', '').strip()
            department_id = request.form.get('department_id', '').strip()

            if not username:
                flash('Username is required.', 'error')
            elif not password:
                flash('Password is required.', 'error')
            elif len(password) < 6:
                flash('Password must be at least 6 characters long.', 'error')
            elif not full_name:
                flash('Full name is required.', 'error')
            elif not email:
                flash('Email is required.', 'error')
            elif role not in ALL_ROLES:
                flash('Please select a valid role.', 'error')
            elif User.query.filter_by(username=username).first():
                flash('Username already exists.', 'error')
            elif User.query.filter_by(email=email).first():
                flash('Email already exists.', 'error')
            else:
                new_user = User(username=username, role=role, full_name=full_name,
                                email=email, is_active=True)
                new_user.set_password(password)
                if department_id.isdigit():
                    new_user.department_id = int(department_id)
                db.session.add(new_user)
                db.session.commit()
                log_action(user, 'USER_CREATED', module='user', record_ref=username,
                           description='Created %s account %s' % (ROLE_LABELS[role], username))
                db.session.commit()
                flash('User %s created successfully.' % username, 'success')
            return redirect(url_for('admin.users'))

        if action == 'edit':
            target_id = request.form.get('user_id', '')
            target = User.query.get(int(target_id)) if target_id.isdigit() else None
            if not target:
                flash('User not found.', 'error')
                return redirect(url_for('admin.users'))

            new_role = request.form.get('role', target.role).strip()
            new_dept = request.form.get('department_id', '').strip()

            if new_role not in ALL_ROLES:
                flash('Please select a valid role.', 'error')
            else:
                # Guard: never let the last administrator be demoted or removed.
                if target.role == ROLE_ADMIN and new_role != ROLE_ADMIN:
                    remaining = User.query.filter_by(role=ROLE_ADMIN, is_active=True).count()
                    if remaining <= 1:
                        flash('At least one active administrator must remain.', 'error')
                        return redirect(url_for('admin.users'))

                if target.role != new_role:
                    old_role = target.role
                    target.role = new_role
                    log_action(user, 'ROLE_CHANGE', module='user', record_ref=target.username,
                               description='Role changed from %s to %s' % (old_role, new_role))
                    notify(target.id, 'Role Updated',
                           'Your role has been changed to %s.' % ROLE_LABELS.get(new_role, new_role),
                           ntype='system')
                target.full_name = request.form.get('full_name', target.full_name).strip() \
                    or target.full_name
                target.email = request.form.get('email', target.email).strip() or target.email
                target.department_id = int(new_dept) if new_dept.isdigit() else None

                new_password = request.form.get('password', '').strip()
                if new_password:
                    if len(new_password) < 6:
                        flash('Password must be at least 6 characters long.', 'error')
                        return redirect(url_for('admin.users'))
                    target.set_password(new_password)
                    target.failed_login_attempts = 0
                    target.locked_until = None
                    log_action(user, 'PASSWORD_RESET', module='user',
                               record_ref=target.username,
                               description='Administrator reset the password')

                db.session.commit()
                flash('User %s updated.' % target.username, 'success')
            return redirect(url_for('admin.users'))

        if action == 'toggle_active':
            target_id = request.form.get('user_id', '')
            target = User.query.get(int(target_id)) if target_id.isdigit() else None
            if not target:
                flash('User not found.', 'error')
                return redirect(url_for('admin.users'))
            if target.id == user.id:
                flash('You cannot deactivate your own account.', 'error')
                return redirect(url_for('admin.users'))
            if target.role == ROLE_ADMIN and target.is_active:
                remaining = User.query.filter_by(role=ROLE_ADMIN, is_active=True).count()
                if remaining <= 1:
                    flash('At least one active administrator must remain.', 'error')
                    return redirect(url_for('admin.users'))

            target.is_active = not target.is_active
            db.session.commit()
            log_action(user, 'USER_%s' % ('DEACTIVATED' if not target.is_active else 'ACTIVATED'),
                       module='user', record_ref=target.username,
                       description='Account %s' % ('deactivated' if not target.is_active
                                                   else 'activated'))
            db.session.commit()
            flash('User %s %s.' % (target.username,
                                   'deactivated' if not target.is_active else 'activated'),
                  'success')
            return redirect(url_for('admin.users'))

    return render_template('admin/users.html', users=User.query.order_by(User.role, User.username).all(),
                           roles=ALL_ROLES, role_labels=ROLE_LABELS,
                           departments=Department.query.order_by(Department.name).all(),
                           unread_notifications=unread_count(user.id))


@admin_bp.route('/departments', methods=['GET', 'POST'])
@admin_required
def departments():
    user = current_user()

    if request.method == 'POST':
        action = request.form.get('action')
        name = request.form.get('name', '').strip()
        code = request.form.get('code', '').strip().upper()
        dept_id = request.form.get('department_id', '').strip()

        if action in ('create', 'edit'):
            if not name:
                flash('Department name is required.', 'error')
            else:
                existing = Department.query.filter(
                    db.or_(Department.name == name,
                           Department.code == code if code else False)).first()
                if action == 'create':
                    if existing:
                        flash('A department with that name or code already exists.', 'error')
                    else:
                        db.session.add(Department(name=name, code=code or None))
                        db.session.commit()
                        log_action(user, 'DEPARTMENT_CREATED', module='config',
                                   record_ref=name, description='Created department %s' % name)
                        db.session.commit()
                        flash('Department %s created.' % name, 'success')
                else:
                    dept = Department.query.get(int(dept_id)) if dept_id.isdigit() else None
                    if not dept:
                        flash('Department not found.', 'error')
                    else:
                        dept.name = name
                        dept.code = code or None
                        dept.description = request.form.get('description', '').strip() or None
                        dept.is_active = request.form.get('is_active') == '1'
                        db.session.commit()
                        log_action(user, 'DEPARTMENT_UPDATED', module='config',
                                   record_ref=dept.name, description='Configuration change')
                        db.session.commit()
                        flash('Department %s updated.' % name, 'success')
            return redirect(url_for('admin.departments'))

        if action == 'toggle_active':
            dept = Department.query.get(int(dept_id)) if dept_id.isdigit() else None
            if not dept:
                flash('Department not found.', 'error')
            else:
                dept.is_active = not dept.is_active
                db.session.commit()
                log_action(user, 'DEPARTMENT_TOGGLED', module='config', record_ref=dept.name,
                           description='is_active set to %s' % dept.is_active)
                db.session.commit()
                flash('Department %s %s.' % (dept.name,
                                             'activated' if dept.is_active else 'deactivated'),
                      'success')
            return redirect(url_for('admin.departments'))

    return render_template('admin/departments.html',
                           departments=Department.query.order_by(Department.name).all(),
                           unread_notifications=unread_count(user.id))


@admin_bp.route('/categories', methods=['GET', 'POST'])
@admin_required
def categories():
    user = current_user()

    if request.method == 'POST':
        action = request.form.get('action')
        name = request.form.get('name', '').strip()
        department_id = request.form.get('department_id', '').strip()
        sla_hours = request.form.get('sla_hours', '').strip()
        cat_id = request.form.get('category_id', '').strip()

        if action in ('create', 'edit'):
            if not name:
                flash('Category name is required.', 'error')
            else:
                if action == 'create':
                    if ComplaintCategory.query.filter_by(name=name).first():
                        flash('A category with that name already exists.', 'error')
                    else:
                        db.session.add(ComplaintCategory(
                            name=name,
                            department_id=int(department_id) if department_id.isdigit() else None,
                            sla_hours=int(sla_hours) if sla_hours.isdigit() else None))
                        db.session.commit()
                        log_action(user, 'CATEGORY_CREATED', module='config',
                                   record_ref=name, description='Created category %s' % name)
                        db.session.commit()
                        flash('Category %s created.' % name, 'success')
                else:
                    cat = ComplaintCategory.query.get(int(cat_id)) if cat_id.isdigit() else None
                    if not cat:
                        flash('Category not found.', 'error')
                    else:
                        cat.name = name
                        cat.department_id = int(department_id) if department_id.isdigit() else None
                        cat.sla_hours = int(sla_hours) if sla_hours.isdigit() else None
                        cat.is_active = request.form.get('is_active') == '1'
                        db.session.commit()
                        log_action(user, 'CATEGORY_UPDATED', module='config',
                                   record_ref=cat.name, description='Configuration change')
                        db.session.commit()
                        flash('Category %s updated.' % name, 'success')
            return redirect(url_for('admin.categories'))

        if action == 'toggle_active':
            cat = ComplaintCategory.query.get(int(cat_id)) if cat_id.isdigit() else None
            if not cat:
                flash('Category not found.', 'error')
            else:
                cat.is_active = not cat.is_active
                db.session.commit()
                log_action(user, 'CATEGORY_TOGGLED', module='config', record_ref=cat.name,
                           description='is_active set to %s' % cat.is_active)
                db.session.commit()
                flash('Category %s %s.' % (cat.name,
                                           'activated' if cat.is_active else 'deactivated'),
                      'success')
            return redirect(url_for('admin.categories'))

    return render_template('admin/categories.html',
                           categories=ComplaintCategory.query.order_by(
                               ComplaintCategory.name).all(),
                           departments=Department.query.order_by(Department.name).all(),
                           unread_notifications=unread_count(user.id))


@admin_bp.route('/blocks', methods=['GET', 'POST'])
@admin_required
def blocks():
    user = current_user()

    if request.method == 'POST':
        action = request.form.get('action')
        name = request.form.get('name', '').strip()
        code = request.form.get('code', '').strip().upper()
        warden_id = request.form.get('warden_id', '').strip()
        total_rooms = request.form.get('total_rooms', '').strip()
        block_id = request.form.get('block_id', '').strip()

        warden = User.query.get(int(warden_id)) if warden_id.isdigit() else None
        if warden and warden.role != ROLE_WARDEN:
            flash('The selected user is not a hostel warden.', 'error')
            return redirect(url_for('admin.blocks'))

        if action in ('create', 'edit'):
            if not name:
                flash('Hostel block name is required.', 'error')
            else:
                if action == 'create':
                    clash = HostelBlock.query.filter(
                        db.or_(HostelBlock.name == name,
                               HostelBlock.code == code if code else False)).first()
                    if clash:
                        flash('A hostel block with that name or code already exists.', 'error')
                    else:
                        db.session.add(HostelBlock(
                            name=name, code=code or None,
                            warden_id=warden.id if warden else None,
                            total_rooms=int(total_rooms) if total_rooms.isdigit() else 0))
                        db.session.commit()
                        log_action(user, 'HOSTEL_BLOCK_CREATED', module='config',
                                   record_ref=name,
                                   description='Created block %s (warden: %s)' % (
                                       name, warden.username if warden else 'none'))
                        db.session.commit()
                        flash('Hostel block %s created.' % name, 'success')
                else:
                    block = HostelBlock.query.get(int(block_id)) if block_id.isdigit() else None
                    if not block:
                        flash('Hostel block not found.', 'error')
                    else:
                        old_warden = block.warden_id
                        block.name = name
                        block.code = code or None
                        block.warden_id = warden.id if warden else None
                        block.total_rooms = int(total_rooms) if total_rooms.isdigit() else 0
                        block.description = request.form.get('description', '').strip() or None
                        block.is_active = request.form.get('is_active') == '1'
                        db.session.commit()
                        log_action(user, 'HOSTEL_BLOCK_UPDATED', module='config',
                                   record_ref=block.name,
                                   description='Warden changed from %s to %s' % (
                                       old_warden, block.warden_id))
                        db.session.commit()
                        flash('Hostel block %s updated.' % name, 'success')
            return redirect(url_for('admin.blocks'))

        if action == 'toggle_active':
            block = HostelBlock.query.get(int(block_id)) if block_id.isdigit() else None
            if not block:
                flash('Hostel block not found.', 'error')
            else:
                block.is_active = not block.is_active
                db.session.commit()
                log_action(user, 'HOSTEL_BLOCK_TOGGLED', module='config',
                           record_ref=block.name,
                           description='is_active set to %s' % block.is_active)
                db.session.commit()
                flash('Hostel block %s %s.' % (
                    block.name, 'activated' if block.is_active else 'deactivated'), 'success')
            return redirect(url_for('admin.blocks'))

    return render_template('admin/blocks.html',
                           blocks=HostelBlock.query.order_by(HostelBlock.name).all(),
                           wardens=User.query.filter_by(role=ROLE_WARDEN,
                                                        is_active=True).order_by(
                               User.full_name).all(),
                           unread_notifications=unread_count(user.id))


# ---------------------------------------------------------------------------
# Audit log + settings
# ---------------------------------------------------------------------------

@admin_bp.route('/audit-logs')
@admin_required
def audit_logs():
    user = current_user()
    filters = {k: request.args.get(k, '') for k in ('from', 'to', 'action', 'module', 'q')}

    q = AuditLog.query
    start = _parse_date(filters.get('from'))
    end = _parse_date(filters.get('to'))
    if start:
        q = q.filter(AuditLog.created_at >= datetime.combine(start, datetime.min.time()))
    if end:
        q = q.filter(AuditLog.created_at <= datetime.combine(end, datetime.max.time()))
    if filters.get('action'):
        q = q.filter(AuditLog.action == filters['action'])
    if filters.get('module'):
        q = q.filter(AuditLog.module == filters['module'])
    if filters.get('q'):
        like = '%' + filters['q'] + '%'
        q = q.filter(db.or_(AuditLog.description.ilike(like),
                            AuditLog.record_ref.ilike(like),
                            AuditLog.username.ilike(like)))

    entries = q.order_by(AuditLog.created_at.desc()).limit(500).all()

    return render_template('admin/audit_logs.html', entries=entries, filters=filters,
                           actions=[a[0] for a in db.session.query(
                               AuditLog.action).distinct().all() if a[0]],
                           modules=[m[0] for m in db.session.query(
                               AuditLog.module).distinct().all() if m[0]],
                           unread_notifications=unread_count(user.id))


@admin_bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    user = current_user()

    if request.method == 'POST':
        changed = []
        for key, (default_value, description) in DEFAULT_SETTINGS.items():
            raw = request.form.get(key)
            if raw is None:
                continue
            row = Setting.query.filter_by(key=key).first()
            if not row:
                row = Setting(key=key, value=default_value, description=description)
                db.session.add(row)
            if row.value != raw:
                row.value = raw
                changed.append(key)
        db.session.commit()

        if changed:
            log_action(user, 'SETTINGS_UPDATED', module='config',
                       record_ref=','.join(changed),
                       description='Updated: %s' % ', '.join(changed))
            db.session.commit()
            flash('Configuration updated: %s' % ', '.join(changed), 'success')
        else:
            flash('No configuration changes were made.', 'info')
        return redirect(url_for('admin.settings'))

    rows = {s.key: s for s in Setting.query.all()}
    return render_template('admin/settings.html', rows=rows,
                           defaults=DEFAULT_SETTINGS,
                           unread_notifications=unread_count(user.id))


# ---------------------------------------------------------------------------
# Notifications (original admin broadcast feature, kept)
# ---------------------------------------------------------------------------

@admin_bp.route('/notifications', methods=['GET', 'POST'])
@admin_required
def notifications():
    user = current_user()

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        message = request.form.get('message', '').strip()
        target = request.form.get('target', 'all')
        ntype = request.form.get('ntype', 'system')

        if not title:
            flash('Title is required.', 'error')
            return redirect(url_for('admin.notifications'))
        if not message:
            flash('Message is required.', 'error')
            return redirect(url_for('admin.notifications'))

        if target == 'all':
            students = User.query.filter_by(role=ROLE_STUDENT, is_active=True).all()
            for student in students:
                notify(student.id, title, message, ntype=ntype, related_module='system')
            db.session.commit()
            log_action(user, 'NOTIFICATION_BROADCAST', module='notification',
                       record_ref=title,
                       description='Sent to %d students' % len(students))
            db.session.commit()
            flash('Notification sent to all %d students.' % len(students), 'success')
        else:
            student_id = request.form.get('student_id', '')
            student = User.query.get(int(student_id)) if student_id.isdigit() else None
            if not student:
                flash('Please select a student.', 'error')
            elif student.role != ROLE_STUDENT:
                flash('Notifications can only be sent to students.', 'error')
            else:
                notify(student.id, title, message, ntype=ntype, related_module='system')
                db.session.commit()
                log_action(user, 'NOTIFICATION_SENT', module='notification',
                           record_ref=student.username, description=title)
                db.session.commit()
                flash('Notification sent to %s.' % student.full_name, 'success')
        return redirect(url_for('admin.notifications'))

    students = User.query.filter_by(role=ROLE_STUDENT, is_active=True).order_by(
        User.full_name).all()
    recent = Notification.query.order_by(Notification.created_at.desc()).limit(50).all()
    return render_template('admin/notifications.html', students=students,
                           notifications=recent,
                           unread_notifications=unread_count(user.id))


# ---------------------------------------------------------------------------
# CSV report export
# ---------------------------------------------------------------------------

def _csv_response(filename, header, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=%s' % filename})


@admin_bp.route('/export/<report>')
@admin_required
def export(report):
    user = current_user()
    filters = {k: request.args.get(k, '') for k in
               ('from', 'to', 'status', 'department', 'category', 'type', 'block')}
    stamp = datetime.now().strftime('%Y%m%d')

    if report == 'complaints':
        q = apply_filters(Complaint.query, Complaint, filters, Complaint.created_at)
        rows = [[c.complaint_id, c.created_at.strftime('%Y-%m-%d') if c.created_at else '',
                 c.category, c.department.name if c.department else '',
                 c.student.full_name if c.student else '', c.title, c.status,
                 (c.resolution_notes or '')[:200], 'YES' if c.sla_breached else 'NO']
                for c in q.order_by(Complaint.created_at.desc()).all()]
        return _csv_response('complaints_%s.csv' % stamp,
                             ['Complaint ID', 'Created', 'Category', 'Department',
                              'Student', 'Title', 'Status', 'Resolution Notes',
                              'SLA Breached'], rows)

    if report == 'hostel':
        q = apply_filters(HostelRequest.query, HostelRequest, filters,
                          HostelRequest.created_at)
        rows = [[r.request_id, r.created_at.strftime('%Y-%m-%d') if r.created_at else '',
                 r.request_type or 'HOSTEL ALLOCATION',
                 r.block.name if r.block else r.hostel_name,
                 r.student.full_name if r.student else '', r.status,
                 (r.warden_comment or '')[:200]] for r in
                q.order_by(HostelRequest.created_at.desc()).all()]
        return _csv_response('hostel_requests_%s.csv' % stamp,
                             ['Request ID', 'Created', 'Type', 'Hostel Block', 'Student',
                              'Status', 'Warden Comment'], rows)

    if report == 'lostfound':
        q = apply_filters(LostFoundItem.query, LostFoundItem, filters,
                          LostFoundItem.created_at)
        rows = [[i.item_id, i.created_at.strftime('%Y-%m-%d') if i.created_at else '',
                 i.item_type, i.item_category or '', i.item_name, i.location,
                 i.date.isoformat() if i.date else '', i.status,
                 i.claimant.full_name if i.claimant else ''] for i in
                q.order_by(LostFoundItem.created_at.desc()).all()]
        return _csv_response('lost_found_%s.csv' % stamp,
                             ['Item ID', 'Reported', 'Type', 'Category', 'Item Name',
                              'Location', 'Item Date', 'Status', 'Claimed By'], rows)

    if report == 'audit':
        rows = [[e.created_at.strftime('%Y-%m-%d %H:%M:%S') if e.created_at else '',
                 e.username or '', e.action, e.module or '', e.record_ref or '',
                 (e.description or '')[:250]] for e in
                AuditLog.query.order_by(AuditLog.created_at.desc()).limit(2000).all()]
        return _csv_response('audit_logs_%s.csv' % stamp,
                             ['Timestamp', 'User', 'Action', 'Module', 'Record',
                              'Description'], rows)

    flash('Unknown report type.', 'error')
    return redirect(url_for('admin.dashboard'))