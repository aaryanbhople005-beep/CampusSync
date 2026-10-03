"""Non-destructive schema upgrade for CampusSync.

The original application already has live data, so the upgrade never drops or
recreates a populated table.  Instead it:

1. renames ``lost_found`` -> ``lost_found_items`` (SQLite rename keeps rows),
2. creates any brand-new SRS table that does not exist yet,
3. adds missing columns to existing tables with ALTER TABLE ... ADD COLUMN,
4. normalises legacy status strings to the SRS vocabulary,
5. back-fills sensible defaults for pre-existing rows.

Safe to run repeatedly - every step is idempotent.
"""

from sqlalchemy import text, inspect

from models import db

# New columns required by the SRS, keyed by existing table name.
# (column_name, sqlite_type_ddl)
COLUMN_UPGRADES = {
    'users': [
        ('phone', 'VARCHAR(20)'),
        ('department_id', 'INTEGER'),
        ('is_active', 'BOOLEAN DEFAULT 1'),
        ('failed_login_attempts', 'INTEGER DEFAULT 0'),
        ('locked_until', 'DATETIME'),
        ('last_login_at', 'DATETIME'),
    ],
    'complaints': [
        ('category_id', 'INTEGER'),
        ('department_id', 'INTEGER'),
        ('assigned_to_id', 'INTEGER'),
        ('attachment_path', 'VARCHAR(255)'),
        ('resolution_notes', 'TEXT'),
        ('escalation_note', 'TEXT'),
        ('reopen_count', 'INTEGER DEFAULT 0'),
        ('resolved_at', 'DATETIME'),
        ('closed_at', 'DATETIME'),
        ('sla_due_at', 'DATETIME'),
        ('sla_breached', 'BOOLEAN DEFAULT 0'),
    ],
    'hostel_requests': [
        ('hostel_block_id', 'INTEGER'),
        ('request_type', "VARCHAR(40) DEFAULT 'HOSTEL ALLOCATION'"),
        ('issue_type', 'VARCHAR(60)'),
        ('location', 'VARCHAR(120)'),
        ('start_date', 'DATE'),
        ('end_date', 'DATE'),
        ('warden_comment', 'TEXT'),
        ('clarification_note', 'TEXT'),
        ('decided_by_id', 'INTEGER'),
        ('decided_at', 'DATETIME'),
    ],
    'lost_found_items': [
        ('item_category', 'VARCHAR(60)'),
        ('photo_path', 'VARCHAR(255)'),
        ('retention_flag', 'BOOLEAN DEFAULT 0'),
    ],
    'notifications': [
        ('ntype', 'VARCHAR(40)'),
        ('related_module', 'VARCHAR(40)'),
        ('related_id', 'VARCHAR(40)'),
    ],
}

# Legacy value -> SRS value
COMPLAINT_STATUS_MAP = {
    'Submitted': 'OPEN',
    'Open': 'OPEN',
    'Under Review': 'IN PROGRESS',
    'In Progress': 'IN PROGRESS',
    'Resolved': 'RESOLVED',
    'Closed': 'CLOSED',
}

HOSTEL_STATUS_MAP = {
    'Pending': 'PENDING',
    'Approved': 'APPROVED',
    'Rejected': 'REJECTED',
    'Clarification Required': 'CLARIFICATION REQUIRED',
    'Completed': 'COMPLETED',
}

ITEM_TYPE_MAP = {'Lost': 'LOST', 'Found': 'FOUND'}
ITEM_STATUS_MAP = {'Open': 'OPEN', 'Claimed': 'CLAIMED'}


def _existing_columns(table):
    return {c['name'] for c in inspect(db.engine).get_columns(table)}


def _table_exists(table):
    return table in inspect(db.engine).get_table_names()


def run_migrations():
    """Apply every upgrade step.  Returns a list of human-readable log lines."""
    log = []

    # ---- 1. rename lost_found -> lost_found_items ------------------------
    if _table_exists('lost_found') and not _table_exists('lost_found_items'):
        db.session.execute(text('ALTER TABLE lost_found RENAME TO lost_found_items'))
        db.session.commit()
        log.append('Renamed table lost_found -> lost_found_items (rows preserved)')

    # ---- 2. create tables that do not exist yet ---------------------------
    # db.create_all() only creates missing tables; it never modifies or drops
    # an existing one, so this is safe for the populated original database.
    db.create_all()
    log.append('Created any missing tables')

    # ---- 3. add missing columns to pre-existing tables -------------------
    for table, columns in COLUMN_UPGRADES.items():
        if not _table_exists(table):
            continue
        present = _existing_columns(table)
        added = []
        for name, ddl in columns:
            if name not in present:
                db.session.execute(text('ALTER TABLE %s ADD COLUMN %s %s' % (table, name, ddl)))
                added.append(name)
        if added:
            db.session.commit()
            log.append('Added columns to %s: %s' % (table, ', '.join(added)))

    # ---- 4. normalise legacy status vocabulary ---------------------------
    if _table_exists('complaints'):
        for old, new in COMPLAINT_STATUS_MAP.items():
            db.session.execute(
                text('UPDATE complaints SET status = :new WHERE status = :old'),
                {'old': old, 'new': new})
        db.session.commit()
        log.append('Normalised legacy complaint statuses to the SRS vocabulary')

    if _table_exists('hostel_requests'):
        for old, new in HOSTEL_STATUS_MAP.items():
            db.session.execute(
                text('UPDATE hostel_requests SET status = :new WHERE status = :old'),
                {'old': old, 'new': new})
        db.session.execute(text(
            "UPDATE hostel_requests SET status = 'PENDING' "
            "WHERE status IS NULL OR status = ''"))
        db.session.commit()
        log.append('Normalised legacy hostel statuses to the SRS vocabulary')

    if _table_exists('lost_found_items'):
        for old, new in ITEM_TYPE_MAP.items():
            db.session.execute(
                text('UPDATE lost_found_items SET item_type = :new WHERE item_type = :old'),
                {'old': old, 'new': new})
        for old, new in ITEM_STATUS_MAP.items():
            db.session.execute(
                text('UPDATE lost_found_items SET status = :new WHERE status = :old'),
                {'old': old, 'new': new})
        db.session.commit()
        log.append('Normalised legacy lost & found values to the SRS vocabulary')

    # ---- 5. back-fill defaults on legacy rows ----------------------------
    if _table_exists('users'):
        db.session.execute(text('UPDATE users SET is_active = 1 WHERE is_active IS NULL'))
        db.session.execute(text('UPDATE users SET failed_login_attempts = 0 '
                                'WHERE failed_login_attempts IS NULL'))
        db.session.commit()
        log.append('Back-filled default values for existing users')

    if _table_exists('notifications'):
        db.session.execute(text("UPDATE notifications SET ntype = 'system' "
                                "WHERE ntype IS NULL OR ntype = ''"))
        db.session.commit()
        log.append('Back-filled notification type for legacy rows')

    return log