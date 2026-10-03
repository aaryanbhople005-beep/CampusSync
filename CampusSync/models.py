"""CampusSync data model.

Design notes
------------
* Every table and column from the original working application is preserved so
  existing records survive the upgrade.  New SRS entities are appended and new
  columns are added by ``migrations.py`` (ALTER TABLE ... ADD COLUMN).
* ``users.role`` intentionally stays a plain string column instead of becoming a
  foreign key.  The original rows already store values ('student', 'admin') and
  keeping the column avoids rewriting identity data.  The ``roles`` table acts
  as the reference/configuration list of valid roles and the set of permissions.
"""

from datetime import datetime, date

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# ---------------------------------------------------------------------------
# Role constants (SRS section: Authentication & Role Management)
# ---------------------------------------------------------------------------

ROLE_STUDENT = 'student'
ROLE_WARDEN = 'warden'
ROLE_STAFF = 'staff'
ROLE_LOSTFUND = 'lostfound'
ROLE_ADMIN = 'admin'

ALL_ROLES = [ROLE_STUDENT, ROLE_WARDEN, ROLE_STAFF, ROLE_LOSTFUND, ROLE_ADMIN]

ROLE_LABELS = {
    ROLE_STUDENT: 'Student',
    ROLE_WARDEN: 'Hostel Warden / Staff',
    ROLE_STAFF: 'Department Staff',
    ROLE_LOSTFUND: 'Lost & Found Staff',
    ROLE_ADMIN: 'Administrator',
}

# Home route for each role after login.
ROLE_HOME = {
    ROLE_STUDENT: 'student.dashboard',
    ROLE_WARDEN: 'warden.dashboard',
    ROLE_STAFF: 'staff.dashboard',
    ROLE_LOSTFUND: 'lostfound.dashboard',
    ROLE_ADMIN: 'admin.dashboard',
}

# ---------------------------------------------------------------------------
# Complaint workflow constants (SRS section: Complaint Management)
# ---------------------------------------------------------------------------

CMPL_OPEN = 'OPEN'
CMPL_IN_PROGRESS = 'IN PROGRESS'
CMPL_RESOLVED = 'RESOLVED'
CMPL_CLOSED = 'CLOSED'
CMPL_REOPENED = 'REOPENED'

COMPLAINT_STATUSES = [CMPL_OPEN, CMPL_IN_PROGRESS, CMPL_RESOLVED, CMPL_CLOSED, CMPL_REOPENED]

# Statuses that still require action.
COMPLAINT_ACTIVE_STATUSES = [CMPL_OPEN, CMPL_IN_PROGRESS, CMPL_REOPENED]

# ---------------------------------------------------------------------------
# Hostel workflow constants (SRS section: Hostel Administration)
# ---------------------------------------------------------------------------

HR_PENDING = 'PENDING'
HR_APPROVED = 'APPROVED'
HR_REJECTED = 'REJECTED'
HR_CLARIFICATION = 'CLARIFICATION REQUIRED'
HR_COMPLETED = 'COMPLETED'

HOSTEL_REQUEST_STATUSES = [HR_PENDING, HR_APPROVED, HR_REJECTED, HR_CLARIFICATION, HR_COMPLETED]

REQ_ALLOCATION = 'HOSTEL ALLOCATION'
REQ_MAINTENANCE = 'MAINTENANCE'
REQ_OUTPASS = 'OUTPASS / LEAVE'

HOSTEL_REQUEST_TYPES = [REQ_ALLOCATION, REQ_MAINTENANCE, REQ_OUTPASS]

# ---------------------------------------------------------------------------
# Lost & Found workflow constants (SRS section: Lost & Found)
# ---------------------------------------------------------------------------

ITEM_LOST = 'LOST'
ITEM_FOUND = 'FOUND'

LF_OPEN = 'OPEN'
LF_CLAIMED = 'CLAIMED'

CLAIM_PENDING = 'PENDING'
CLAIM_APPROVED = 'APPROVED'
CLAIM_REJECTED = 'REJECTED'


# ===========================================================================
# Reference / configuration tables
# ===========================================================================

class Role(db.Model):
    __tablename__ = 'roles'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), unique=True, nullable=False)
    label = db.Column(db.String(60), nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'label': self.label,
                'description': self.description, 'is_active': self.is_active}


class Department(db.Model):
    __tablename__ = 'departments'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    code = db.Column(db.String(20), unique=True)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    categories = db.relationship('ComplaintCategory', backref='department', lazy=True)
    staff_members = db.relationship('User', backref='department', lazy=True)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'code': self.code,
                'description': self.description, 'is_active': self.is_active}


class ComplaintCategory(db.Model):
    __tablename__ = 'complaint_categories'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=True)
    sla_hours = db.Column(db.Integer)          # per-category SLA override
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'department_id': self.department_id,
                'sla_hours': self.sla_hours, 'is_active': self.is_active}


class HostelBlock(db.Model):
    __tablename__ = 'hostel_blocks'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    code = db.Column(db.String(20), unique=True)
    warden_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    total_rooms = db.Column(db.Integer, default=0)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    warden = db.relationship('User', foreign_keys=[warden_id], backref='warden_of_blocks')

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'code': self.code,
                'warden_id': self.warden_id, 'total_rooms': self.total_rooms,
                'description': self.description, 'is_active': self.is_active}


class Setting(db.Model):
    """Configurable business rules (SRS: keep values out of hard-coded code)."""

    __tablename__ = 'settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(60), unique=True, nullable=False)
    value = db.Column(db.String(255))
    description = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {'key': self.key, 'value': self.value, 'description': self.description}


# ===========================================================================
# Users  (original columns preserved, new columns appended)
# ===========================================================================

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # student|warden|staff|lostfound|admin
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- added for the SRS upgrade -----------------------------------------
    phone = db.Column(db.String(20))
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    failed_login_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)

    complaints = db.relationship('Complaint', backref='student', lazy=True,
                                foreign_keys='Complaint.student_id')
    hostel_requests = db.relationship('HostelRequest', backref='student', lazy=True,
                                      foreign_keys='HostelRequest.student_id')
    notifications = db.relationship('Notification', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_locked(self):
        if self.locked_until and self.locked_until > datetime.utcnow():
            return True
        return False

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'role_label': self.role_label,
            'full_name': self.full_name,
            'email': self.email,
            'phone': self.phone,
            'department_id': self.department_id,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


# ===========================================================================
# Complaints  (original columns preserved)
# ===========================================================================

class Complaint(db.Model):
    __tablename__ = 'complaints'

    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.String(20), unique=True, nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(50), nullable=False)   # original free-text category
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='Submitted')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # --- added for the SRS upgrade -----------------------------------------
    category_id = db.Column(db.Integer, db.ForeignKey('complaint_categories.id'), nullable=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    attachment_path = db.Column(db.String(255))
    resolution_notes = db.Column(db.Text)
    escalation_note = db.Column(db.Text)
    reopen_count = db.Column(db.Integer, default=0)
    resolved_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)
    sla_due_at = db.Column(db.DateTime)
    sla_breached = db.Column(db.Boolean, default=False)

    history = db.relationship('ComplaintHistory', backref='complaint',
                              lazy=True, cascade='all, delete-orphan',
                              order_by='ComplaintHistory.created_at')
    category_ref = db.relationship('ComplaintCategory', lazy=True)
    department = db.relationship('Department', lazy=True)
    assigned_to = db.relationship('User', foreign_keys=[assigned_to_id], lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'complaint_id': self.complaint_id,
            'student_id': self.student_id,
            'student_name': self.student.full_name if self.student else None,
            'title': self.title,
            'category': self.category,
            'description': self.description,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class ComplaintHistory(db.Model):
    __tablename__ = 'complaint_history'

    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey('complaints.id'), nullable=False)
    from_status = db.Column(db.String(20))
    to_status = db.Column(db.String(20), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    changed_by = db.relationship('User', lazy=True)


# ===========================================================================
# Hostel  (original HostelRequest columns preserved)
# ===========================================================================

class HostelRequest(db.Model):
    __tablename__ = 'hostel_requests'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.String(20), unique=True, nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    hostel_name = db.Column(db.String(100), nullable=False)
    room_preference = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='Pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # --- added for the SRS upgrade -----------------------------------------
    hostel_block_id = db.Column(db.Integer, db.ForeignKey('hostel_blocks.id'), nullable=True)
    request_type = db.Column(db.String(40), default=REQ_ALLOCATION)
    issue_type = db.Column(db.String(60))       # maintenance issue type
    location = db.Column(db.String(120))        # maintenance location
    start_date = db.Column(db.Date)             # outpass
    end_date = db.Column(db.Date)               # outpass
    warden_comment = db.Column(db.Text)
    clarification_note = db.Column(db.Text)
    decided_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    decided_at = db.Column(db.DateTime)

    block = db.relationship('HostelBlock', lazy=True)
    decided_by = db.relationship('User', foreign_keys=[decided_by_id], lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'request_id': self.request_id,
            'student_id': self.student_id,
            'student_name': self.student.full_name if self.student else None,
            'hostel_name': self.hostel_name,
            'room_preference': self.room_preference,
            'reason': self.reason,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class HostelNotice(db.Model):
    __tablename__ = 'hostel_notices'

    id = db.Column(db.Integer, primary_key=True)
    hostel_block_id = db.Column(db.Integer, db.ForeignKey('hostel_blocks.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    block = db.relationship('HostelBlock', lazy=True)
    created_by = db.relationship('User', lazy=True)


class RoomAllocation(db.Model):
    __tablename__ = 'room_allocations'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    hostel_block_id = db.Column(db.Integer, db.ForeignKey('hostel_blocks.id'), nullable=False)
    room_number = db.Column(db.String(20), nullable=False)
    bed_number = db.Column(db.String(10))
    allocated_from = db.Column(db.Date, default=date.today)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('User', lazy=True)
    block = db.relationship('HostelBlock', lazy=True)


# ===========================================================================
# Lost & Found  (original `lost_found` table, renamed to lost_found_items)
# ===========================================================================

class LostFoundItem(db.Model):
    __tablename__ = 'lost_found_items'

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.String(20), unique=True, nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    item_name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(10), nullable=False)  # 'Lost' or 'Found'
    description = db.Column(db.Text, nullable=False)
    location = db.Column(db.String(200), nullable=False)
    date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default='Open')
    claimed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # --- added for the SRS upgrade -----------------------------------------
    item_category = db.Column(db.String(60))
    photo_path = db.Column(db.String(255))
    retention_flag = db.Column(db.Boolean, default=False)

    student = db.relationship('User', foreign_keys=[student_id], backref='lost_found_items')
    claimant = db.relationship('User', foreign_keys=[claimed_by], backref='claimed_items')
    claims = db.relationship('Claim', backref='item', lazy=True,
                             cascade='all, delete-orphan')

    @property
    def type_normalised(self):
        return (self.item_type or '').upper()

    @property
    def status_normalised(self):
        return (self.status or '').upper()

    def to_dict(self):
        return {
            'id': self.id,
            'item_id': self.item_id,
            'student_id': self.student_id,
            'student_name': self.student.full_name if self.student else None,
            'item_name': self.item_name,
            'item_type': self.item_type,
            'description': self.description,
            'location': self.location,
            'date': self.date.isoformat() if self.date else None,
            'status': self.status,
            'claimed_by': self.claimed_by,
            'claimant_name': self.claimant.full_name if self.claimant else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class Claim(db.Model):
    __tablename__ = 'claims'

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('lost_found_items.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    identifying_info = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default=CLAIM_PENDING)
    review_note = db.Column(db.Text)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('User', foreign_keys=[student_id], lazy=True)
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_id], lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'item_id': self.item_id,
            'student_id': self.student_id,
            'identifying_info': self.identifying_info,
            'status': self.status,
            'review_note': self.review_note,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None
        }


# ===========================================================================
# Notifications  (original columns preserved)
# ===========================================================================

class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- added for the SRS upgrade -----------------------------------------
    ntype = db.Column(db.String(40))            # complaint|hostel|claim|notice|system
    related_module = db.Column(db.String(40))
    related_id = db.Column(db.String(40))

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'title': self.title,
            'message': self.message,
            'is_read': self.is_read,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


# ===========================================================================
# Audit + security
# ===========================================================================

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    username = db.Column(db.String(80))
    action = db.Column(db.String(60), nullable=False)
    module = db.Column(db.String(40))
    record_ref = db.Column(db.String(60))
    description = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', lazy=True)


class LoginAttempt(db.Model):
    __tablename__ = 'login_attempts'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80))
    success = db.Column(db.Boolean, default=False)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)