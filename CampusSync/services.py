"""Shared business logic used by more than one blueprint.

Keeping status transitions, ID generation and SLA evaluation here means the
complaint rules are enforced identically no matter which role performs them.
"""

from datetime import datetime, timedelta

from models import (db, Complaint, ComplaintHistory, HostelRequest, LostFoundItem,
                    Claim, User, HOSTEL_REQUEST_STATUSES, COMPLAINT_STATUSES,
                    CMPL_OPEN, CMPL_IN_PROGRESS, CMPL_RESOLVED, CMPL_CLOSED,
                    CMPL_REOPENED, HR_PENDING, CLAIM_APPROVED, CLAIM_REJECTED,
                    CLAIM_PENDING, LF_OPEN, LF_CLAIMED, ITEM_FOUND, ROLE_WARDEN,
                    ROLE_STAFF, ROLE_LOSTFUND)
from security import (notify, log_action, get_setting_int, get_setting_bool,
                      get_setting)


# ---------------------------------------------------------------------------
# Human readable tracking IDs
# ---------------------------------------------------------------------------

def _next_id(prefix, model, column, year=None):
    year = year or datetime.now().year
    count = model.query.count()
    while True:
        candidate = '%s-%d-%03d' % (prefix, year, count + 1)
        if not model.query.filter(column == candidate).first():
            return candidate
        count += 1


def generate_complaint_id():
    return _next_id('CMP', Complaint, Complaint.complaint_id)


def generate_request_id():
    return _next_id('HST', HostelRequest, HostelRequest.request_id)


def generate_item_id():
    return _next_id('LF', LostFoundItem, LostFoundItem.item_id)


# ---------------------------------------------------------------------------
# SLA handling
# ---------------------------------------------------------------------------

def complaint_sla_hours(complaint):
    """Per-category SLA if configured, otherwise the global default."""
    if complaint.category_ref and complaint.category_ref.sla_hours:
        return int(complaint.category_ref.sla_hours)
    return get_setting_int('complaint_sla_hours', 72)


def apply_complaint_sla(complaint):
    """Set the SLA due date when a complaint becomes active; clear it when done."""
    if complaint.status in (CMPL_OPEN, CMPL_REOPENED):
        if not complaint.sla_due_at:
            complaint.sla_due_at = complaint.created_at + timedelta(
                hours=complaint_sla_hours(complaint))
    return complaint.sla_due_at


def refresh_sla_flags():
    """Flag every OPEN complaint that has blown its SLA.  Returns flagged ids."""
    flagged = []
    for c in Complaint.query.filter(Complaint.status.in_([CMPL_OPEN, CMPL_REOPENED])).all():
        if c.sla_due_at and c.sla_due_at < datetime.utcnow():
            if not c.sla_breached:
                c.sla_breached = True
                flagged.append(c.complaint_id)
    if flagged:
        db.session.commit()
    return flagged


# ---------------------------------------------------------------------------
# Complaints
# ---------------------------------------------------------------------------

VALID_COMPLAINT_TRANSITIONS = {
    CMPL_OPEN: [CMPL_IN_PROGRESS, CMPL_RESOLVED, CMPL_CLOSED],
    CMPL_REOPENED: [CMPL_IN_PROGRESS, CMPL_RESOLVED, CMPL_CLOSED],
    CMPL_IN_PROGRESS: [CMPL_RESOLVED, CMPL_CLOSED],
    CMPL_RESOLVED: [CMPL_CLOSED, CMPL_REOPENED],
    CMPL_CLOSED: [CMPL_REOPENED],
}


def can_transition(current, new):
    if new not in COMPLAINT_STATUSES:
        return False
    return new in VALID_COMPLAINT_TRANSITIONS.get(current, [])


def change_complaint_status(complaint, new_status, actor, note=None, resolution_notes=None):
    """Apply a status change: update, write history, notify student, audit."""
    old_status = complaint.status
    complaint.status = new_status
    complaint.updated_at = datetime.utcnow()

    if resolution_notes:
        complaint.resolution_notes = resolution_notes
    if new_status == CMPL_RESOLVED:
        complaint.resolved_at = datetime.utcnow()
        complaint.sla_breached = False
    if new_status == CMPL_CLOSED:
        complaint.closed_at = datetime.utcnow()
    if new_status in (CMPL_OPEN, CMPL_REOPENED):
        complaint.sla_due_at = complaint.created_at + timedelta(
            hours=complaint_sla_hours(complaint))

    db.session.add(ComplaintHistory(
        complaint_id=complaint.id,
        from_status=old_status,
        to_status=new_status,
        changed_by_id=actor.id if actor else None,
        note=note,
    ))

    notify(complaint.student_id,
           'Complaint Status Updated',
           'Your complaint %s status changed from %s to %s.' % (
               complaint.complaint_id, old_status, new_status),
           ntype='complaint', related_module='complaint',
           related_id=complaint.complaint_id)

    log_action(actor, 'COMPLAINT_STATUS_CHANGE', module='complaint',
               record_ref=complaint.complaint_id,
               description='Status changed from %s to %s%s' % (
                   old_status, new_status, ('. Note: ' + note) if note else ''))
    return complaint


def reopen_allowed(complaint):
    """Configurable reopening rule (RULE 9: default dev values, documented)."""
    if not get_setting_bool('complaint_reopen_allowed', True):
        return False, 'Complaint reopening is currently disabled by the administrator.'
    if complaint.status not in (CMPL_RESOLVED, CMPL_CLOSED):
        return False, 'Only a resolved or closed complaint can be reopened.'
    window = get_setting_int('complaint_reopen_window_days', 7)
    reference = complaint.resolved_at or complaint.updated_at or complaint.created_at
    if reference and window > 0:
        if reference + timedelta(days=window) < datetime.utcnow():
            return False, ('The %d day reopening window for this complaint has expired.'
                           % window)
    return True, ''


# ---------------------------------------------------------------------------
# Hostel requests
# ---------------------------------------------------------------------------

def set_hostel_request_status(req, new_status, warden, comment=None, clarification=None):
    """Apply a hostel request decision and notify the student."""
    old_status = req.status
    req.status = new_status
    req.updated_at = datetime.utcnow()
    req.decided_by_id = warden.id if warden else None
    req.decided_at = datetime.utcnow()

    if comment:
        req.warden_comment = comment
    if clarification:
        req.clarification_note = clarification

    db.session.add(_hostel_history(req, old_status, new_status, warden, comment))

    notify(req.student_id,
           'Hostel Request Updated',
           'Your hostel request %s (%s) status changed from %s to %s.' % (
               req.request_id, req.request_type or 'HOSTEL REQUEST', old_status, new_status),
           ntype='hostel', related_module='hostel_request', related_id=req.request_id)

    log_action(warden, 'HOSTEL_REQUEST_STATUS_CHANGE', module='hostel',
               record_ref=req.request_id,
               description='Status changed from %s to %s%s' % (
                   old_status, new_status, ('. Comment: ' + comment) if comment else ''))
    return req


def _hostel_history(req, old_status, new_status, warden, comment):
    """Hostel requests reuse the complaint-history table shape for a single audit trail."""
    from models import AuditLog
    return AuditLog(user_id=warden.id if warden else None,
                    username=warden.username if warden else None,
                    action='HOSTEL_REQUEST_STATUS_CHANGE',
                    module='hostel',
                    record_ref=req.request_id,
                    description='%s -> %s%s' % (old_status, new_status,
                                               ('. ' + comment) if comment else ''))


# ---------------------------------------------------------------------------
# Lost & Found
# ---------------------------------------------------------------------------

def approve_claim(claim, reviewer, note=None):
    """Approve a claim: item becomes CLAIMED, student + reporter notified."""
    claim.status = CLAIM_APPROVED
    claim.reviewed_by_id = reviewer.id
    claim.reviewed_at = datetime.utcnow()
    claim.review_note = note

    item = claim.item
    item.status = LF_CLAIMED
    item.claimed_by = claim.student_id
    item.updated_at = datetime.utcnow()

    # Any other pending claims on the same item can no longer succeed.
    for other in Claim.query.filter_by(item_id=item.id).all():
        if other.id != claim.id and other.status == CLAIM_PENDING:
            other.status = CLAIM_REJECTED
            other.reviewed_by_id = reviewer.id
            other.reviewed_at = datetime.utcnow()
            other.review_note = 'Item was already claimed by another student.'
            notify(other.student_id,
                   'Claim Rejected',
                   'Your claim for item %s was rejected because the item was already claimed.'
                   % item.item_id,
                   ntype='claim', related_module='claim', related_id=str(claim.id))

    notify(claim.student_id,
           'Claim Approved',
           'Your claim for item %s (%s) has been approved. The item is now marked as CLAIMED.'
           % (item.item_id, item.item_name),
           ntype='claim', related_module='claim', related_id=str(claim.id))

    if item.student_id and item.student_id != claim.student_id:
        notify(item.student_id,
               'Item Claimed',
               'Your found item %s (%s) has been claimed.' % (item.item_id, item.item_name),
               ntype='claim', related_module='lost_found', related_id=item.item_id)

    log_action(reviewer, 'CLAIM_APPROVED', module='lost_found',
               record_ref=item.item_id,
               description='Claim #%s approved for item %s' % (claim.id, item.item_id))
    return claim


def reject_claim(claim, reviewer, note=None):
    item = claim.item
    claim.status = CLAIM_REJECTED
    claim.reviewed_by_id = reviewer.id
    claim.reviewed_at = datetime.utcnow()
    claim.review_note = note

    notify(claim.student_id,
           'Claim Rejected',
           'Your claim for item %s (%s) has been rejected.%s' % (
               item.item_id, item.item_name,
               (' Reason: ' + note) if note else ''),
           ntype='claim', related_module='claim', related_id=str(claim.id))

    log_action(reviewer, 'CLAIM_REJECTED', module='lost_found',
               record_ref=item.item_id,
               description='Claim #%s rejected for item %s' % (claim.id, item.item_id))
    return claim


def refresh_retention_flags():
    """Flag FOUND items unclaimed beyond the retention period for admin review."""
    days = get_setting_int('found_item_retention_days', 30)
    flagged = []
    cutoff = datetime.utcnow() - timedelta(days=days)
    for item in LostFoundItem.query.filter_by(item_type=ITEM_FOUND, status=LF_OPEN).all():
        reference = item.created_at or datetime.utcnow()
        if reference < cutoff:
            if not item.retention_flag:
                item.retention_flag = True
                flagged.append(item.item_id)
    if flagged:
        db.session.commit()
    return flagged


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

def route_complaint(complaint):
    """Assign a complaint to the department staff / warden that owns its category."""
    department_id = None
    if complaint.category_ref:
        department_id = complaint.category_ref.department_id
    if department_id is None:
        department_id = complaint.department_id
    complaint.department_id = department_id

    assignee = None
    if department_id:
        assignee = User.query.filter_by(role=ROLE_STAFF, department_id=department_id,
                                        is_active=True).first()
    if assignee is None:
        # Fall back to any active department staff so nothing is orphaned.
        assignee = User.query.filter_by(role=ROLE_STAFF, is_active=True).first()
    complaint.assigned_to_id = assignee.id if assignee else None
    return assignee


def block_warden(user):
    return user.warden_of_blocks if user else []


def is_valid_status(value, allowed):
    return value in allowed