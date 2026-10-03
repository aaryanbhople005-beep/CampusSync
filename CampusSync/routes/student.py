"""Student area.

SRS capabilities: dashboard, submit/view/track complaints with history and
reopening, hostel block + allocation view, maintenance and outpass requests,
hostel notices, lost & found reports, search and claims, notifications.

The original URLs (/student/dashboard, /student/register-complaint,
/student/track-complaint, /student/hostel-request, /student/lost-found,
/student/notifications) are all preserved so existing flows keep working.
"""

import os
from datetime import datetime, date

from flask import (Blueprint, render_template, request, redirect, url_for, flash,
                   session, current_app)
from werkzeug.utils import secure_filename

from models import (db, User, Complaint, ComplaintHistory, HostelRequest, HostelBlock,
                    HostelNotice, RoomAllocation, LostFoundItem, Claim, Notification,
                    Department, ComplaintCategory, CMPL_OPEN, CMPL_RESOLVED, CMPL_CLOSED,
                    CMPL_REOPENED, COMPLAINT_ACTIVE_STATUSES, HR_PENDING,
                    HOSTEL_REQUEST_TYPES, REQ_ALLOCATION, REQ_MAINTENANCE, REQ_OUTPASS,
                    ITEM_LOST, ITEM_FOUND, LF_OPEN, CLAIM_PENDING, CLAIM_APPROVED,
                    ROLE_STUDENT)
from security import (student_required, current_user, notify, log_action, unread_count,
                      get_setting_bool, get_setting_int)
from services import (generate_complaint_id, generate_request_id, generate_item_id,
                      route_complaint, apply_complaint_sla, change_complaint_status,
                      reopen_allowed, refresh_retention_flags)

student_bp = Blueprint('student', __name__, url_prefix='/student')

UPLOAD_FOLDER = 'uploads'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def active_categories():
    return ComplaintCategory.query.filter_by(is_active=True).order_by(
        ComplaintCategory.name).all()


def active_blocks():
    return HostelBlock.query.filter_by(is_active=True).order_by(HostelBlock.name).all()


def my_allocation():
    return RoomAllocation.query.filter_by(student_id=session['user_id'], is_active=True).first()


def _save_upload(file_field):
    """Store an optional attachment; returns a stored path or None."""
    if not file_field or not file_field.filename:
        return None
    if not get_setting_bool('complaint_allow_file_upload', True):
        flash('File attachment is currently disabled by the administrator.', 'error')
        return None
    filename = secure_filename(file_field.filename)
    if not filename:
        return None
    folder = os.path.join(current_app.instance_path, UPLOAD_FOLDER)
    os.makedirs(folder, exist_ok=True)
    stored = '%s_%s' % (datetime.now().strftime('%Y%m%d%H%M%S'), filename)
    path = os.path.join(folder, stored)
    file_field.save(path)

    limit_mb = get_setting_int('max_attachment_mb', 2)
    if os.path.getsize(path) > limit_mb * 1024 * 1024:
        os.remove(path)
        flash('Attachment exceeds the %d MB limit.' % limit_mb, 'error')
        return None
    return os.path.join(UPLOAD_FOLDER, stored)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@student_bp.route('/dashboard')
@student_required
def dashboard():
    user = current_user()
    uid = user.id

    complaints = Complaint.query.filter_by(student_id=uid).all()
    hostel_requests = HostelRequest.query.filter_by(student_id=uid).all()
    items = LostFoundItem.query.filter_by(student_id=uid).all()
    claims = Claim.query.filter_by(student_id=uid).all()

    open_complaints = [c for c in complaints if c.status in COMPLAINT_ACTIVE_STATUSES]
    pending_hostel = [r for r in hostel_requests if r.status == HR_PENDING]

    allocation = my_allocation()
    block = allocation.block if allocation else None

    # Notices for the block the student lives in.
    notices = []
    if block:
        notices = HostelNotice.query.filter_by(hostel_block_id=block.id).order_by(
            HostelNotice.created_at.desc()).all()

    # Resolved complaints the student is still allowed to reopen.
    reopenable = []
    for c in complaints:
        if c.status in (CMPL_RESOLVED, CMPL_CLOSED):
            ok, _ = reopen_allowed(c)
            if ok:
                reopenable.append(c)

    return render_template(
        'student/dashboard.html',
        user=user,
        complaints_count=len(complaints),
        open_complaints=len(open_complaints),
        hostel_count=len(hostel_requests),
        pending_hostel=len(pending_hostel),
        lost_found_count=len(items),
        claims_count=len(claims),
        unread_notifications=unread_count(uid),
        allocation=allocation,
        block=block,
        notices=notices[:3],
        recent_complaints=sorted(complaints, key=lambda c: c.created_at or datetime.min,
                                 reverse=True)[:5],
        reopenable=reopenable,
    )


# ---------------------------------------------------------------------------
# Complaints
# ---------------------------------------------------------------------------

@student_bp.route('/register-complaint', methods=['GET', 'POST'])
@student_required
def register_complaint():
    categories = active_categories()
    departments = Department.query.filter_by(is_active=True).order_by(Department.name).all()

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        category_id = request.form.get('category_id', '').strip()
        department_id = request.form.get('department_id', '').strip()
        description = request.form.get('description', '').strip()

        # --- TC08 / TC09 / TC10: validation with explicit messages -------
        if not title:
            flash('Complaint title is required.', 'error')
            return render_template('student/register_complaint.html',
                                   categories=categories, departments=departments,
                                   form=request.form)

        if not category_id:
            flash('Please select a complaint category.', 'error')
            return render_template('student/register_complaint.html',
                                   categories=categories, departments=departments,
                                   form=request.form)

        category = ComplaintCategory.query.get(category_id) if category_id.isdigit() else None
        if not category or not category.is_active:
            flash('Please select a valid complaint category.', 'error')
            return render_template('student/register_complaint.html',
                                   categories=categories, departments=departments,
                                   form=request.form)

        if not description:
            flash('Complaint description is required.', 'error')
            return render_template('student/register_complaint.html',
                                   categories=categories, departments=departments,
                                   form=request.form)

        # Department is derived from the category unless the student overrides it.
        if not department_id or not department_id.isdigit():
            department_id = category.department_id
        elif not Department.query.get(department_id):
            department_id = category.department_id

        attachment = None
        if request.files.get('attachment') and request.files['attachment'].filename:
            attachment = _save_upload(request.files['attachment'])
            if attachment is None and not get_setting_bool('complaint_allow_file_upload', True):
                return render_template('student/register_complaint.html',
                                       categories=categories, departments=departments,
                                       form=request.form)

        user = current_user()
        complaint = Complaint(
            complaint_id=generate_complaint_id(),
            student_id=user.id,
            title=title,
            category=category.name,          # keeps the original column meaningful
            description=description,
            status=CMPL_OPEN,
            category_id=category.id,
            attachment_path=attachment,
        )
        db.session.add(complaint)
        db.session.flush()                  # need complaint.id for history/notify

        assignee = route_complaint(complaint)
        apply_complaint_sla(complaint)

        db.session.add(ComplaintHistory(
            complaint_id=complaint.id,
            from_status=None,
            to_status=CMPL_OPEN,
            changed_by_id=user.id,
            note='Complaint submitted by student.',
        ))

        notify(user.id, 'Complaint Submitted',
               'Your complaint %s has been submitted and is now OPEN.' % complaint.complaint_id,
               ntype='complaint', related_module='complaint',
               related_id=complaint.complaint_id)

        if assignee:
            notify(assignee.id, 'New Complaint Assigned',
                   'Complaint %s (%s) has been assigned to you.' % (
                       complaint.complaint_id, complaint.title),
                   ntype='complaint', related_module='complaint',
                   related_id=complaint.complaint_id)

        log_action(user, 'COMPLAINT_CREATED', module='complaint',
                   record_ref=complaint.complaint_id,
                   description='Complaint "%s" created and routed to %s' % (
                       title, assignee.username if assignee else 'no staff'))
        db.session.commit()

        flash('Complaint submitted successfully! Your Complaint ID is: %s'
              % complaint.complaint_id, 'success')
        return redirect(url_for('student.track_complaint'))

    return render_template('student/register_complaint.html',
                           categories=categories, departments=departments, form={})


@student_bp.route('/track-complaint', methods=['GET', 'POST'])
@student_required
def track_complaint():
    user = current_user()
    complaints = Complaint.query.filter_by(student_id=user.id).order_by(
        Complaint.created_at.desc()).all()

    complaint_detail = None
    if request.method == 'POST':
        complaint_id = request.form.get('complaint_id', '').strip()
        if complaint_id:
            # Ownership check: a student can only ever load their own complaint.
            complaint_detail = Complaint.query.filter_by(
                complaint_id=complaint_id, student_id=user.id).first()
            if not complaint_detail:
                # TC12: distinguish "no such id" from "someone else's id".
                exists = Complaint.query.filter_by(complaint_id=complaint_id).first()
                if exists:
                    flash('You are not authorized to access this page.', 'error')
                else:
                    flash('Complaint not found.', 'error')
        else:
            flash('Complaint ID is required.', 'error')

    return render_template('student/track_complaint.html',
                           complaints=complaints,
                           complaint_detail=complaint_detail,
                           open_statuses=COMPLAINT_ACTIVE_STATUSES)


@student_bp.route('/complaint/<int:complaint_pk>')
@student_required
def complaint_detail(complaint_pk):
    """Full detail view: status history + resolution notes (ownership enforced)."""
    user = current_user()
    complaint = Complaint.query.get_or_404(complaint_pk)

    if complaint.student_id != user.id:
        log_action(user, 'ACCESS_DENIED', module='complaint',
                   record_ref=complaint.complaint_id,
                   description='Student tried to open another student\'s complaint')
        flash('You are not authorized to access this page.', 'error')
        return redirect(url_for('student.track_complaint'))

    can_reopen, reason = reopen_allowed(complaint)
    history = ComplaintHistory.query.filter_by(complaint_id=complaint.id).order_by(
        ComplaintHistory.created_at).all()

    return render_template('student/complaint_detail.html',
                           complaint=complaint, history=history,
                           can_reopen=can_reopen, reopen_reason=reason)


@student_bp.route('/complaint/<int:complaint_pk>/reopen', methods=['POST'])
@student_required
def reopen_complaint(complaint_pk):
    user = current_user()
    complaint = Complaint.query.get_or_404(complaint_pk)

    if complaint.student_id != user.id:
        flash('You are not authorized to access this page.', 'error')
        return redirect(url_for('student.track_complaint'))

    allowed, reason = reopen_allowed(complaint)
    if not allowed:
        flash(reason, 'error')
        return redirect(url_for('student.complaint_detail', complaint_pk=complaint.id))

    note = request.form.get('reason', '').strip()
    if not note:
        flash('Please provide a reason for reopening the complaint.', 'error')
        return redirect(url_for('student.complaint_detail', complaint_pk=complaint.id))

    complaint.reopen_count = (complaint.reopen_count or 0) + 1
    change_complaint_status(complaint, CMPL_REOPENED, user, note='Reopened by student: ' + note)

    if complaint.assigned_to_id:
        notify(complaint.assigned_to_id, 'Complaint Reopened',
               'Complaint %s was reopened by the student: %s' % (complaint.complaint_id, note),
               ntype='complaint', related_module='complaint',
               related_id=complaint.complaint_id)

    log_action(user, 'COMPLAINT_REOPENED', module='complaint',
               record_ref=complaint.complaint_id, description=note)
    db.session.commit()

    flash('Complaint %s has been reopened.' % complaint.complaint_id, 'success')
    return redirect(url_for('student.complaint_detail', complaint_pk=complaint.id))


# ---------------------------------------------------------------------------
# Hostel
# ---------------------------------------------------------------------------

@student_bp.route('/hostel')
@student_required
def hostel():
    """My hostel block, room allocation and block notices."""
    allocation = my_allocation()
    block = allocation.block if allocation else None
    notices = []
    if block:
        notices = HostelNotice.query.filter_by(hostel_block_id=block.id).order_by(
            HostelNotice.created_at.desc()).all()
    requests = HostelRequest.query.filter_by(student_id=session['user_id']).order_by(
        HostelRequest.created_at.desc()).all()
    return render_template('student/hostel.html', allocation=allocation,
                           block=block, notices=notices, requests=requests)


@student_bp.route('/hostel-request', methods=['GET', 'POST'])
@student_required
def hostel_request():
    blocks = active_blocks()
    allocation = my_allocation()
    default_block_id = allocation.block.id if allocation else None

    if request.method == 'POST':
        request_type = request.form.get('request_type', REQ_ALLOCATION).strip()
        block_id = request.form.get('hostel_block_id', '').strip()
        reason = request.form.get('reason', '').strip()

        # --- TC15 / TC16: missing / invalid hostel information -----------
        if request_type not in HOSTEL_REQUEST_TYPES:
            flash('Please select a valid request type.', 'error')
            return render_template('student/hostel_request.html', blocks=blocks,
                                   request_types=HOSTEL_REQUEST_TYPES,
                                   default_block_id=default_block_id, form=request.form)

        block = None
        if block_id and block_id.isdigit():
            block = HostelBlock.query.get(block_id)
        if not block or not block.is_active:
            flash('Please select a valid hostel block.', 'error')
            return render_template('student/hostel_request.html', blocks=blocks,
                                   request_types=HOSTEL_REQUEST_TYPES,
                                   default_block_id=default_block_id, form=request.form)

        if not reason:
            flash('Reason is required.', 'error')
            return render_template('student/hostel_request.html', blocks=blocks,
                                   request_types=HOSTEL_REQUEST_TYPES,
                                   default_block_id=default_block_id, form=request.form)

        extra = {}

        if request_type == REQ_MAINTENANCE:
            issue_type = request.form.get('issue_type', '').strip()
            location = request.form.get('location', '').strip()
            if not issue_type:
                flash('Issue type is required.', 'error')
                return render_template('student/hostel_request.html', blocks=blocks,
                                       request_types=HOSTEL_REQUEST_TYPES,
                                       default_block_id=default_block_id, form=request.form)
            if not location:
                flash('Location is required.', 'error')
                return render_template('student/hostel_request.html', blocks=blocks,
                                       request_types=HOSTEL_REQUEST_TYPES,
                                       default_block_id=default_block_id, form=request.form)
            extra = {'issue_type': issue_type, 'location': location}

        elif request_type == REQ_OUTPASS:
            start_raw = request.form.get('start_date', '').strip()
            end_raw = request.form.get('end_date', '').strip()
            if not start_raw or not end_raw:
                flash('Start date and end date are required.', 'error')
                return render_template('student/hostel_request.html', blocks=blocks,
                                       request_types=HOSTEL_REQUEST_TYPES,
                                       default_block_id=default_block_id, form=request.form)
            try:
                start_date = datetime.strptime(start_raw, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_raw, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid date format. Use YYYY-MM-DD.', 'error')
                return render_template('student/hostel_request.html', blocks=blocks,
                                       request_types=HOSTEL_REQUEST_TYPES,
                                   default_block_id=default_block_id, form=request.form)
            if end_date < start_date:
                flash('End date cannot be earlier than start date.', 'error')
                return render_template('student/hostel_request.html', blocks=blocks,
                                       request_types=HOSTEL_REQUEST_TYPES,
                                   default_block_id=default_block_id, form=request.form)
            extra = {'start_date': start_date, 'end_date': end_date}

        user = current_user()
        req = HostelRequest(
            request_id=generate_request_id(),
            student_id=user.id,
            hostel_name=block.name,
            room_preference=request.form.get('room_preference', '').strip() or 'Any',
            reason=reason,
            status=HR_PENDING,
            hostel_block_id=block.id,
            request_type=request_type,
            **extra
        )
        db.session.add(req)
        db.session.flush()

        notify(user.id, 'Hostel Request Submitted',
               'Your %s request %s has been submitted and is PENDING.' % (
                   request_type.lower(), req.request_id),
               ntype='hostel', related_module='hostel_request', related_id=req.request_id)

        # Notify the warden responsible for this block.
        if block.warden_id:
            notify(block.warden_id, 'New Hostel Request',
                   'Request %s (%s) from %s.' % (req.request_id, request_type,
                                                 user.full_name),
                   ntype='hostel', related_module='hostel_request', related_id=req.request_id)

        log_action(user, 'HOSTEL_REQUEST_CREATED', module='hostel',
                   record_ref=req.request_id,
                   description='%s request for %s' % (request_type, block.name))
        db.session.commit()

        flash('Hostel request submitted successfully! Your Request ID is: %s'
              % req.request_id, 'success')
        return redirect(url_for('student.hostel_request'))

    requests = HostelRequest.query.filter_by(student_id=session['user_id']).order_by(
        HostelRequest.created_at.desc()).all()
    return render_template('student/hostel_request.html', blocks=blocks,
                           request_types=HOSTEL_REQUEST_TYPES,
                           default_block_id=default_block_id, requests=requests,
                           form={})


@student_bp.route('/hostel-request/<int:req_pk>')
@student_required
def hostel_request_detail(req_pk):
    user = current_user()
    req = HostelRequest.query.get_or_404(req_pk)
    if req.student_id != user.id:
        flash('You are not authorized to access this page.', 'error')
        return redirect(url_for('student.hostel_request'))
    return render_template('student/hostel_request_detail.html', request=req)


# ---------------------------------------------------------------------------
# Lost & Found
# ---------------------------------------------------------------------------

def _search_items(student_id, keyword, category, type_filter, date_from, date_to,
                  claimable_only=False):
    q = LostFoundItem.query
    if keyword:
        like = '%' + keyword.strip() + '%'
        q = q.filter(db.or_(LostFoundItem.item_name.ilike(like),
                            LostFoundItem.description.ilike(like),
                            LostFoundItem.location.ilike(like),
                            LostFoundItem.item_id.ilike(like)))
    if category:
        q = q.filter(LostFoundItem.item_category == category)
    if type_filter in ('LOST', 'FOUND'):
        q = q.filter(LostFoundItem.item_type == type_filter)
    if date_from:
        q = q.filter(LostFoundItem.date >= date_from)
    if date_to:
        q = q.filter(LostFoundItem.date <= date_to)

    if claimable_only:
        # Only unclaimed FOUND items stay visible for claiming.
        q = q.filter(LostFoundItem.item_type == ITEM_FOUND,
                     LostFoundItem.status == LF_OPEN)
    else:
        # CLAIMED items are preserved in the DB but removed from active results.
        q = q.filter(db.or_(LostFoundItem.status != 'CLAIMED',
                            LostFoundItem.claimed_by == student_id))
    return q.order_by(LostFoundItem.created_at.desc()).all()


@student_bp.route('/lost-found', methods=['GET', 'POST'])
@student_required
def lost_found():
    today = date.today().isoformat()
    user = current_user()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'report':
            item_name = request.form.get('item_name', '').strip()
            item_type = request.form.get('item_type', '').strip().upper()
            description = request.form.get('description', '').strip()
            location = request.form.get('location', '').strip()
            date_raw = request.form.get('date', '').strip()
            item_category = request.form.get('item_category', '').strip()

            # --- TC18 / TC19 validation ----------------------------------
            if not item_name:
                flash('Item name is required.', 'error')
            elif not description:
                flash('Item description is required.', 'error')
            elif item_type not in ('LOST', 'FOUND'):
                flash('Please select Lost or Found.', 'error')
            elif not location:
                flash('Location is required.', 'error')
            elif not date_raw:
                flash('Date is required.', 'error')
            else:
                try:
                    item_date = datetime.strptime(date_raw, '%Y-%m-%d').date()
                except ValueError:
                    flash('Invalid date format. Use YYYY-MM-DD.', 'error')
                    item_date = None

                if item_date:
                    item = LostFoundItem(
                        item_id=generate_item_id(),
                        student_id=user.id,
                        item_name=item_name,
                        item_type=item_type,
                        description=description,
                        location=location,
                        date=item_date,
                        status=LF_OPEN,
                        item_category=item_category or None,
                    )
                    db.session.add(item)
                    db.session.flush()

                    action_name = 'LOST_ITEM_REPORT' if item_type == 'LOST' else 'FOUND_ITEM_REPORT'
                    notify(user.id, 'Lost & Found Report Submitted',
                           'Your %s item report %s has been submitted.' % (
                               item_type.lower(), item.item_id),
                           ntype='lost_found', related_module='lost_found',
                           related_id=item.item_id)

                    log_action(user, action_name, module='lost_found',
                               record_ref=item.item_id,
                               description='%s item "%s" at %s' % (item_type, item_name,
                                                                     location))
                    db.session.commit()

                    flash('%s item reported successfully! Your Item ID is: %s'
                          % (item_type.title(), item.item_id), 'success')
                    return redirect(url_for('student.lost_found'))

        elif action == 'claim':
            item_ref = request.form.get('item_id', '').strip()
            identifying = request.form.get('identifying_info', '').strip()

            # --- TC21: unknown item id ----------------------------------
            item = LostFoundItem.query.filter_by(item_id=item_ref).first()
            if not item:
                flash('Item not found.', 'error')
                return redirect(url_for('student.lost_found'))

            if item.item_type != ITEM_FOUND:
                flash('Only found items can be claimed.', 'error')
                return redirect(url_for('student.lost_found'))

            if item.status != LF_OPEN:
                flash('This item is no longer available for claiming.', 'error')
                return redirect(url_for('student.lost_found'))

            if not identifying:
                flash('Identifying information is required to submit a claim.', 'error')
                return redirect(url_for('student.lost_found'))

            existing = Claim.query.filter_by(item_id=item.id, student_id=user.id,
                                             status=CLAIM_PENDING).first()
            if existing:
                flash('You already have a pending claim for this item.', 'error')
                return redirect(url_for('student.lost_found'))

            claim = Claim(item_id=item.id, student_id=user.id,
                          identifying_info=identifying, status=CLAIM_PENDING)
            db.session.add(claim)
            db.session.flush()

            notify(user.id, 'Claim Submitted',
                   'Your claim for item %s (%s) has been submitted and is awaiting review.'
                   % (item.item_id, item.item_name),
                   ntype='claim', related_module='claim', related_id=str(claim.id))

            log_action(user, 'CLAIM_SUBMITTED', module='lost_found',
                       record_ref=item.item_id,
                       description='Claim #%s submitted for item %s' % (claim.id, item.item_id))
            db.session.commit()

            flash('Claim submitted successfully! Reference: Claim #%d' % claim.id, 'success')
            return redirect(url_for('student.lost_found'))

    # ---- search / filter (GET and the default POST) --------------------
    refresh_retention_flags()

    keyword = request.args.get('keyword', '').strip()
    category = request.args.get('category', '').strip()
    type_filter = request.args.get('type', '').strip().upper()
    date_from = request.args.get('from', '').strip()
    date_to = request.args.get('to', '').strip()

    results = _search_items(user.id, keyword, category, type_filter,
                            date_from, date_to)

    # The claim tab only ever lists unclaimed FOUND items.
    claimable = _search_items(user.id, '', '', 'FOUND', '', '', claimable_only=True)

    my_items = LostFoundItem.query.filter_by(student_id=user.id).order_by(
        LostFoundItem.created_at.desc()).all()
    my_claims = Claim.query.filter_by(student_id=user.id).order_by(
        Claim.created_at.desc()).all()
    categories = [c for c in db.session.query(
        LostFoundItem.item_category).distinct().all() if c[0]]

    return render_template('student/lost_found.html', today=today,
                           items=results, claimable=claimable,
                           user_items=my_items, my_claims=my_claims,
                           categories=categories, search={
                               'keyword': keyword, 'category': category,
                               'type': type_filter, 'from': date_from, 'to': date_to})


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@student_bp.route('/notifications')
@student_required
def notifications():
    user = current_user()
    notifications = Notification.query.filter_by(user_id=user.id).order_by(
        Notification.created_at.desc()).all()

    # Mark as read when the centre is opened.
    changed = False
    for n in notifications:
        if not n.is_read:
            n.is_read = True
            changed = True
    if changed:
        db.session.commit()

    return render_template('student/notifications.html', notifications=notifications)


@student_bp.route('/notifications/<int:notification_pk>/read', methods=['POST'])
@student_required
def read_notification(notification_pk):
    """Mark one notification read and jump to the related record."""
    user = current_user()
    n = Notification.query.get_or_404(notification_pk)
    if n.user_id != user.id:
        flash('You are not authorized to access this page.', 'error')
        return redirect(url_for('student.notifications'))

    n.is_read = True
    db.session.commit()

    target = _related_link(n)
    if target:
        return redirect(target)
    return redirect(url_for('student.notifications'))


def _related_link(n):
    """Resolve the notification's related record into a safe in-app URL."""
    if not n.related_module:
        return None
    if n.related_module == 'complaint':
        c = Complaint.query.filter_by(complaint_id=n.related_id).first()
        if c and c.student_id == session['user_id']:
            return url_for('student.complaint_detail', complaint_pk=c.id)
        return url_for('student.track_complaint')
    if n.related_module == 'hostel_request':
        r = HostelRequest.query.filter_by(request_id=n.related_id).first()
        if r and r.student_id == session['user_id']:
            return url_for('student.hostel_request_detail', req_pk=r.id)
        return url_for('student.hostel_request')
    if n.related_module in ('claim', 'lost_found'):
        return url_for('student.lost_found')
    if n.related_module == 'hostel_notice':
        return url_for('student.hostel')
    return None