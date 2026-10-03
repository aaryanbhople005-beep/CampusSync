"""Lost & Found Staff area.

Responsibilities: view found items on the desk, review claim requests,
approve/reject claims (which moves the item to CLAIMED), and review old
unclaimed found items flagged by the retention rule.
"""

from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash

from models import (db, LostFoundItem, Claim, User, ITEM_FOUND, ITEM_LOST,
                    LF_OPEN, LF_CLAIMED, CLAIM_PENDING, CLAIM_APPROVED, CLAIM_REJECTED)
from security import (lostfound_required, current_user, notify, log_action,
                      unread_count, get_setting_int)
from services import approve_claim, reject_claim, refresh_retention_flags

lostfound_bp = Blueprint('lostfound', __name__, url_prefix='/lostfound')


@lostfound_bp.route('/dashboard')
@lostfound_required
def dashboard():
    user = current_user()
    refresh_retention_flags()

    pending = Claim.query.filter_by(status=CLAIM_PENDING).order_by(Claim.created_at).all()
    found_open = LostFoundItem.query.filter_by(item_type=ITEM_FOUND,
                                               status=LF_OPEN).order_by(
        LostFoundItem.created_at.desc()).all()
    claimed = LostFoundItem.query.filter_by(status=LF_CLAIMED).all()
    retention = LostFoundItem.query.filter_by(item_type=ITEM_FOUND, status=LF_OPEN,
                                              retention_flag=True).all()

    return render_template('lostfound/dashboard.html', user=user,
                           pending_claims=pending[:8], pending_count=len(pending),
                           found_open=len(found_open), claimed_count=len(claimed),
                           total=len(found_open) + len(claimed),
                           retention_items=retention,
                           retention_days=get_setting_int('found_item_retention_days', 30),
                           unread_notifications=unread_count(user.id))


@lostfound_bp.route('/items')
@lostfound_required
def items():
    type_filter = request.args.get('type', '').strip().upper()
    status_filter = request.args.get('status', '').strip().upper()
    keyword = request.args.get('q', '').strip()

    q = LostFoundItem.query
    if type_filter in (ITEM_LOST, ITEM_FOUND):
        q = q.filter(LostFoundItem.item_type == type_filter)
    if status_filter in (LF_OPEN, LF_CLAIMED):
        q = q.filter(LostFoundItem.status == status_filter)
    if keyword:
        like = '%' + keyword + '%'
        q = q.filter(db.or_(LostFoundItem.item_name.ilike(like),
                            LostFoundItem.item_id.ilike(like),
                            LostFoundItem.description.ilike(like),
                            LostFoundItem.location.ilike(like)))

    return render_template('lostfound/items.html',
                           items=q.order_by(LostFoundItem.created_at.desc()).all(),
                           filters={'type': type_filter, 'status': status_filter,
                                    'q': keyword},
                           unread_notifications=unread_count(user.id))


@lostfound_bp.route('/claims')
@lostfound_required
def claims():
    status_filter = request.args.get('status', CLAIM_PENDING).strip().upper()
    q = Claim.query
    if status_filter in (CLAIM_PENDING, CLAIM_APPROVED, CLAIM_REJECTED):
        q = q.filter(Claim.status == status_filter)
    return render_template('lostfound/claims.html',
                           claims=q.order_by(Claim.created_at.desc()).all(),
                           filters={'status': status_filter},
                           statuses=[CLAIM_PENDING, CLAIM_APPROVED, CLAIM_REJECTED],
                           unread_notifications=unread_count(user.id))


@lostfound_bp.route('/claims/<int:claim_pk>', methods=['POST'])
@lostfound_required
def review_claim(claim_pk):
    user = current_user()
    claim = Claim.query.get_or_404(claim_pk)
    action = request.form.get('action', '').strip()
    note = request.form.get('review_note', '').strip()

    if claim.status != CLAIM_PENDING:
        flash('This claim has already been reviewed.', 'error')
        return redirect(url_for('lostfound.claims'))

    if action == 'approve':
        approve_claim(claim, user, note=note or None)
        db.session.commit()
        flash('Claim approved. Item %s is now CLAIMED.' % claim.item.item_id, 'success')

    elif action == 'reject':
        if not note:
            flash('A rejection reason is required.', 'error')
            return redirect(url_for('lostfound.claims', status=claim.status))
        reject_claim(claim, user, note=note)
        db.session.commit()
        flash('Claim rejected.', 'success')

    else:
        flash('Invalid action.', 'error')

    return redirect(url_for('lostfound.claims'))


@lostfound_bp.route('/items/<int:item_pk>/mark-claimed', methods=['POST'])
@lostfound_required
def mark_claimed(item_pk):
    """Manually mark an item CLAIMED (e.g. handed over at the desk)."""
    user = current_user()
    item = LostFoundItem.query.get_or_404(item_pk)

    if item.status == LF_CLAIMED:
        flash('This item is already marked as CLAIMED.', 'error')
        return redirect(url_for('lostfound.items'))

    note = request.form.get('note', '').strip()
    item.status = LF_CLAIMED
    item.updated_at = datetime.utcnow()

    notify(item.student_id, 'Item Marked as Claimed',
           'Your found item %s (%s) has been marked as CLAIMED.%s' % (
               item.item_id, item.item_name, (' ' + note) if note else ''),
           ntype='claim', related_module='lost_found', related_id=item.item_id)

    log_action(user, 'ITEM_MARKED_CLAIMED', module='lost_found',
               record_ref=item.item_id, description=note or 'Marked claimed by staff')
    db.session.commit()

    flash('Item %s marked as CLAIMED.' % item.item_id, 'success')
    return redirect(url_for('lostfound.items'))