"""Hostel Warden / Staff area.

Scope rule: a warden only ever sees requests, students and notices belonging to
a hostel block they are assigned to.  The block scope is re-checked server-side
on every request.
"""

from datetime import datetime, date

from flask import Blueprint, render_template, request, redirect, url_for, flash, session

from models import (db, User, HostelRequest, HostelBlock, HostelNotice, Notification,
                    HR_PENDING, HR_APPROVED, HR_REJECTED, HR_CLARIFICATION, HR_COMPLETED,
                    HOSTEL_REQUEST_STATUSES, REQ_ALLOCATION, REQ_MAINTENANCE, REQ_OUTPASS)
from security import (warden_required, current_user, notify, log_action, unread_count)
from services import set_hostel_request_status

warden_bp = Blueprint('warden', __name__, url_prefix='/warden')


def my_blocks(user):
    """Hostel blocks this warden is responsible for."""
    if user.role == 'admin':
        return HostelBlock.query.filter_by(is_active=True).order_by(HostelBlock.name).all()
    return HostelBlock.query.filter_by(warden_id=user.id, is_active=True).order_by(
        HostelBlock.name).all()


def block_ids(user):
    return [b.id for b in my_blocks(user)]


def scoped_requests(user, status_filter=None, type_filter=None, keyword=None):
    ids = block_ids(user)
    if not ids:
        return []
    q = HostelRequest.query.filter(HostelRequest.hostel_block_id.in_(ids))
    if status_filter and status_filter in HOSTEL_REQUEST_STATUSES:
        q = q.filter(HostelRequest.status == status_filter)
    if type_filter and type_filter in (REQ_ALLOCATION, REQ_MAINTENANCE, REQ_OUTPASS):
        q = q.filter(HostelRequest.request_type == type_filter)
    if keyword:
        like = '%' + keyword + '%'
        q = q.filter(db.or_(HostelRequest.request_id.ilike(like),
                            HostelRequest.reason.ilike(like),
                            HostelRequest.student.has(
                                User.full_name.ilike(like))))
    return q.order_by(HostelRequest.created_at.desc()).all()


def get_scoped_request(user, req_pk):
    """Fetch a request only if it belongs to one of the warden's blocks."""
    req = HostelRequest.query.get_or_404(req_pk)
    if req.hostel_block_id not in block_ids(user):
        log_action(user, 'ACCESS_DENIED', module='hostel',
                   record_ref=req.request_id,
                   description='Warden tried to open a request outside their block')
        flash('You are not authorized to access this page.', 'error')
        return None
    return req


# ---------------------------------------------------------------------------

@warden_bp.route('/dashboard')
@warden_required
def dashboard():
    user = current_user()
    blocks = my_blocks(user)
    ids = [b.id for b in blocks]
    requests = scoped_requests(user) if ids else []
    pending = [r for r in requests if r.status == HR_PENDING]
    notices = HostelNotice.query.filter(
        HostelNotice.hostel_block_id.in_(ids)).order_by(
        HostelNotice.created_at.desc()).all() if ids else []

    return render_template('warden/dashboard.html', user=user, blocks=blocks,
                           requests=requests[:8], pending=len(pending),
                           total=len(requests), notices=notices[:5],
                           unread_notifications=unread_count(user.id))


@warden_bp.route('/requests')
@warden_required
def requests():
    user = current_user()
    status_filter = request.args.get('status', '').strip().upper()
    type_filter = request.args.get('type', '').strip().upper()
    keyword = request.args.get('q', '').strip()

    return render_template('warden/requests.html', user=user,
                           requests=scoped_requests(user, status_filter, type_filter, keyword),
                           blocks=my_blocks(user),
                           statuses=HOSTEL_REQUEST_STATUSES,
                           filters={'status': status_filter, 'type': type_filter,
                                    'q': keyword},
                           unread_notifications=unread_count(user.id))


@warden_bp.route('/requests/<int:req_pk>')
@warden_required
def request_detail(req_pk):
    user = current_user()
    req = get_scoped_request(user, req_pk)
    if req is None:
        return redirect(url_for('warden.requests'))

    if request.method == 'POST':
        action = request.form.get('action')
        comment = request.form.get('comment', '').strip()
        clarification = request.form.get('clarification', '').strip()

        if action == 'status':
            new_status = request.form.get('status', '').strip().upper()
            if new_status not in HOSTEL_REQUEST_STATUSES:
                flash('Invalid hostel request status.', 'error')
                return redirect(url_for('warden.request_detail', req_pk=req.id))

            if new_status == HR_CLARIFICATION and not clarification:
                flash('Please provide the clarification you need from the student.', 'error')
                return redirect(url_for('warden.request_detail', req_pk=req.id))

            set_hostel_request_status(req, new_status, user,
                                      comment=comment or None,
                                      clarification=clarification or None)

            action_name = 'HOSTEL_REQUEST_%s' % new_status.replace(' ', '_')
            log_action(user, action_name, module='hostel', record_ref=req.request_id,
                       description='Status set to %s' % new_status)
            db.session.commit()

            flash('Request %s status updated to %s.' % (req.request_id, new_status), 'success')
            return redirect(url_for('warden.request_detail', req_pk=req.id))

        if action == 'comment':
            if not comment:
                flash('Comment cannot be empty.', 'error')
            else:
                req.warden_comment = comment
                db.session.commit()
                notify(req.student_id, 'Hostel Request Comment',
                       'Warden comment on request %s: %s' % (req.request_id, comment),
                       ntype='hostel', related_module='hostel_request',
                       related_id=req.request_id)
                log_action(user, 'HOSTEL_REQUEST_COMMENT', module='hostel',
                           record_ref=req.request_id, description=comment)
                db.session.commit()
                flash('Comment added.', 'success')
            return redirect(url_for('warden.request_detail', req_pk=req.id))

    return render_template('warden/request_detail.html', request=req,
                           statuses=HOSTEL_REQUEST_STATUSES,
                           unread_notifications=unread_count(user.id))


@warden_bp.route('/notices', methods=['GET', 'POST'])
@warden_required
def notices():
    user = current_user()
    blocks = my_blocks(user)
    ids = [b.id for b in blocks]

    if request.method == 'POST':
        block_id = request.form.get('hostel_block_id', '').strip()
        title = request.form.get('title', '').strip()
        message = request.form.get('message', '').strip()

        block = HostelBlock.query.get(block_id) if block_id.isdigit() else None
        if not block or block.id not in ids:
            flash('Please select a hostel block you are assigned to.', 'error')
        elif not title:
            flash('Notice title is required.', 'error')
        elif not message:
            flash('Notice message is required.', 'error')
        else:
            notice = HostelNotice(hostel_block_id=block.id, title=title,
                                  message=message, created_by_id=user.id)
            db.session.add(notice)
            db.session.flush()

            # Notify every student allocated to that block.
            from models import RoomAllocation
            student_ids = [a.student_id for a in RoomAllocation.query.filter_by(
                hostel_block_id=block.id, is_active=True).all()]
            for sid in student_ids:
                notify(sid, 'Hostel Notice: %s' % title, message,
                       ntype='notice', related_module='hostel_notice',
                       related_id=str(notice.id))

            log_action(user, 'HOSTEL_NOTICE_CREATED', module='hostel',
                       record_ref=str(notice.id),
                       description='Notice "%s" for %s (%d recipients)' % (
                           title, block.name, len(student_ids)))
            db.session.commit()
            flash('Notice published to %d student(s) in %s.' % (len(student_ids), block.name),
                  'success')
            return redirect(url_for('warden.notices'))

    all_notices = HostelNotice.query.filter(
        HostelNotice.hostel_block_id.in_(ids)).order_by(
        HostelNotice.created_at.desc()).all() if ids else []

    return render_template('warden/notices.html', notices=all_notices, blocks=blocks,
                           unread_notifications=unread_count(user.id))