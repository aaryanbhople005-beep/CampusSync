"""Database initialisation and demo data.

Run with:  python init_db.py

Every step is idempotent - running it twice will not duplicate rows and will not
delete anything the application created later.  Demo records use a fixed set of
identifiers so they can be detected reliably.
"""

from datetime import datetime, date, timedelta

from app import create_app
from models import (db, User, Role, Department, ComplaintCategory, HostelBlock,
                    Complaint, ComplaintHistory, HostelRequest, HostelNotice,
                    RoomAllocation, LostFoundItem, Claim, Notification, AuditLog,
                    Setting, ROLE_STUDENT, ROLE_WARDEN, ROLE_STAFF, ROLE_LOSTFUND,
                    ROLE_ADMIN, ROLE_LABELS, CMPL_OPEN, CMPL_IN_PROGRESS, CMPL_RESOLVED,
                    HR_PENDING, HR_APPROVED, ITEM_LOST, ITEM_FOUND, LF_OPEN, LF_CLAIMED,
                    CLAIM_PENDING, CLAIM_APPROVED, REQ_ALLOCATION, REQ_MAINTENANCE,
                    REQ_OUTPASS)
from migrations import run_migrations
from security import seed_default_settings
from services import apply_complaint_sla, route_complaint, change_complaint_status

app = create_app()

DEMO_COMPLAINTS = ('CMP-2024-001', 'CMP-2024-002')
DEMO_HOSTEL_REQUESTS = ('HST-2024-001', 'HST-2024-002')
DEMO_ITEMS = ('LF-2024-001', 'LF-2024-002')


def ensure_user(username, password, role, full_name, email, department=None,
                phone=None, is_active=True):
    user = User.query.filter_by(username=username).first()
    if user:
        # Keep the demo credentials usable after any role experiments.
        if not user.check_password(password):
            user.set_password(password)
        user.role = role
        user.is_active = is_active
        if department is not None:
            user.department_id = department.id if department else None
        db.session.commit()
        return user
    user = User(username=username, role=role, full_name=full_name, email=email,
                department_id=department.id if department else None,
                phone=phone, is_active=is_active, failed_login_attempts=0)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def main():
    with app.app_context():
        for line in run_migrations():
            print('  migration:', line)
        seed_default_settings()

        # ---------------- roles (reference data) -------------------------
        role_descriptions = {
            ROLE_STUDENT: 'Submits and tracks complaints, hostel requests and lost & found reports.',
            ROLE_WARDEN: 'Reviews hostel requests for their assigned hostel block and publishes notices.',
            ROLE_STAFF: 'Reviews and resolves complaints routed to their department.',
            ROLE_LOSTFUND: 'Reviews lost & found claim requests and manages found items.',
            ROLE_ADMIN: 'Manages users, configuration, analytics, reports and the audit log.',
        }
        for key in [ROLE_STUDENT, ROLE_WARDEN, ROLE_STAFF, ROLE_LOSTFUND, ROLE_ADMIN]:
            if not Role.query.filter_by(name=key).first():
                db.session.add(Role(name=key, label=ROLE_LABELS[key],
                                    description=role_descriptions[key]))
        db.session.commit()
        print('  roles ensured')

        # ---------------- departments ------------------------------------
        dept_specs = [
            ('Academic Affairs', 'ACA', 'Course scheduling, examinations and academic support.'),
            ('Infrastructure', 'INF', 'Campus buildings, power, water and civil works.'),
            ('Maintenance', 'MNT', 'Repairs, housekeeping and equipment maintenance.'),
            ('Security', 'SEC', 'Campus security, access control and safety.'),
            ('Hostel Office', 'HST', 'Hostel allocation, outpass and resident welfare.'),
            ('Food Services', 'FDS', 'Cafeteria and mess services.'),
        ]
        departments = {}
        for name, code, desc in dept_specs:
            dept = Department.query.filter_by(name=name).first()
            if not dept:
                dept = Department(name=name, code=code, description=desc)
                db.session.add(dept)
            dept.code = code
            dept.description = desc
            dept.is_active = True
            departments[name] = dept
        db.session.commit()
        print('  departments ensured')

        # ---------------- hostel blocks -----------------------------------
        block_specs = [
            ('North Campus Hostel', 'NCH', 60, 'Gents hostel - North campus'),
            ('South Campus Hostel', 'SCH', 60, 'Gents hostel - South campus'),
            ('East Campus Hostel', 'ECH', 48, 'Ladies hostel - East campus'),
            ('West Campus Hostel', 'WCH', 48, 'Ladies hostel - West campus'),
        ]
        blocks = {}
        for name, code, rooms, desc in block_specs:
            block = HostelBlock.query.filter_by(name=name).first()
            if not block:
                block = HostelBlock(name=name, code=code, total_rooms=rooms, description=desc)
                db.session.add(block)
            block.code = code
            block.total_rooms = rooms
            block.description = desc
            block.is_active = True
            blocks[name] = block
        db.session.commit()
        print('  hostel blocks ensured')

        # ---------------- demo accounts -----------------------------------
        admin = ensure_user('admin', 'admin123', ROLE_ADMIN, 'Prof. Anita Rao',
                            'admin@university.edu')
        student = ensure_user('student', 'student123', ROLE_STUDENT, 'John Doe',
                              'student@university.edu',
                              department=departments['Academic Affairs'], phone='9876543210')
        warden = ensure_user('warden', 'warden123', ROLE_WARDEN, 'Mr. Ramesh Iyer',
                             'warden@university.edu', phone='9876543211')
        staff = ensure_user('staff', 'staff123', ROLE_STAFF, 'Dr. Priya Nair',
                            'staff@university.edu', department=departments['Infrastructure'])
        lostfound = ensure_user('lostfound', 'lostfound123', ROLE_LOSTFUND, 'Ms. Farah Khan',
                                'lostfound@university.edu', phone='9876543212')

        # A second student is useful for ownership / scope testing.
        student2 = ensure_user('student2', 'student2123', ROLE_STUDENT, 'Sara Khan',
                               'sara@university.edu', department=departments['Hostel Office'])
        print('  demo accounts ensured')

        # Warden is responsible for North + South blocks.
        blocks['North Campus Hostel'].warden_id = warden.id
        blocks['South Campus Hostel'].warden_id = warden.id
        db.session.commit()

        # ---------------- complaint categories -----------------------------
        category_specs = [
            ('Academic', 'Academic Affairs', 48),
            ('Infrastructure', 'Infrastructure', 96),
            ('Maintenance', 'Maintenance', 72),
            ('Security', 'Security', 24),
            ('Food', 'Food Services', 48),
            ('Transport', 'Infrastructure', 72),
            ('Hostel', 'Hostel Office', 72),
            ('Other', 'Academic Affairs', 96),
        ]
        categories = {}
        for name, dept_name, sla in category_specs:
            cat = ComplaintCategory.query.filter_by(name=name).first()
            if not cat:
                cat = ComplaintCategory(name=name)
                db.session.add(cat)
            cat.department_id = departments[dept_name].id
            cat.sla_hours = sla
            cat.is_active = True
            categories[name] = cat
        db.session.commit()
        print('  complaint categories ensured')

        # ---------------- room allocations --------------------------------
        if not RoomAllocation.query.filter_by(student_id=student.id, is_active=True).first():
            db.session.add(RoomAllocation(student_id=student.id,
                                          hostel_block_id=blocks['North Campus Hostel'].id,
                                          room_number='N-204', bed_number='A'))
        if not RoomAllocation.query.filter_by(student_id=student2.id, is_active=True).first():
            db.session.add(RoomAllocation(student_id=student2.id,
                                          hostel_block_id=blocks['East Campus Hostel'].id,
                                          room_number='E-110', bed_number='C'))
        db.session.commit()
        print('  room allocations ensured')

        # ---------------- sample complaints --------------------------------
        if not Complaint.query.filter_by(complaint_id=DEMO_COMPLAINTS[0]).first():
            c1 = Complaint(complaint_id=DEMO_COMPLAINTS[0], student_id=student.id,
                           title='WiFi Not Working in Library', category='Infrastructure',
                           description='The WiFi in the main library has been down for 3 days.',
                           status=CMPL_OPEN, category_id=categories['Infrastructure'].id,
                           created_at=datetime.utcnow() - timedelta(days=5))
            db.session.add(c1)
            db.session.flush()
            route_complaint(c1)
            apply_complaint_sla(c1)
            db.session.add(ComplaintHistory(
                complaint_id=c1.id, from_status=None, to_status=CMPL_OPEN,
                changed_by_id=student.id, note='Complaint submitted by student.',
                created_at=c1.created_at))

        if not Complaint.query.filter_by(complaint_id=DEMO_COMPLAINTS[1]).first():
            c2 = Complaint(complaint_id=DEMO_COMPLAINTS[1], student_id=student.id,
                           title='Broken Chair in Classroom 101', category='Maintenance',
                           description='Chair number 5 in classroom 101 is broken.',
                           status=CMPL_RESOLVED, category_id=categories['Maintenance'].id,
                           resolution_notes='The chair was replaced on site.',
                           created_at=datetime.utcnow() - timedelta(days=20),
                           resolved_at=datetime.utcnow() - timedelta(days=18))
            db.session.add(c2)
            db.session.flush()
            db.session.add(ComplaintHistory(
                complaint_id=c2.id, from_status=None, to_status=CMPL_OPEN,
                changed_by_id=student.id, note='Complaint submitted by student.',
                created_at=c2.created_at))
            db.session.add(ComplaintHistory(
                complaint_id=c2.id, from_status=CMPL_OPEN, to_status=CMPL_IN_PROGRESS,
                changed_by_id=staff.id, note='Repair raised with the maintenance team.',
                created_at=c2.created_at + timedelta(days=1)))
            db.session.add(ComplaintHistory(
                complaint_id=c2.id, from_status=CMPL_IN_PROGRESS, to_status=CMPL_RESOLVED,
                changed_by_id=staff.id,
                note='The chair was replaced on site.',
                created_at=c2.resolved_at))

        # One complaint for the second student so ownership checks are testable.
        if not Complaint.query.filter_by(title='Mess food quality').first():
            c3 = Complaint(complaint_id='CMP-2024-003', student_id=student2.id,
                           title='Mess food quality', category='Food',
                           description='Food quality in the hostel mess has declined this week.',
                           status=CMPL_IN_PROGRESS, category_id=categories['Food'].id,
                           created_at=datetime.utcnow() - timedelta(days=3))
            db.session.add(c3)
            db.session.flush()
            route_complaint(c3)
            apply_complaint_sla(c3)
            db.session.add(ComplaintHistory(
                complaint_id=c3.id, from_status=None, to_status=CMPL_OPEN,
                changed_by_id=student2.id, note='Complaint submitted by student.',
                created_at=c3.created_at))
        db.session.commit()
        print('  sample complaints ensured')

        # ---------------- sample hostel requests ---------------------------
        if not HostelRequest.query.filter_by(request_id=DEMO_HOSTEL_REQUESTS[0]).first():
            db.session.add(HostelRequest(
                request_id=DEMO_HOSTEL_REQUESTS[0], student_id=student.id,
                hostel_name='North Campus Hostel', room_preference='Single',
                reason='Need a quiet environment for studies.', status=HR_APPROVED,
                hostel_block_id=blocks['North Campus Hostel'].id,
                request_type=REQ_ALLOCATION))

        if not HostelRequest.query.filter_by(request_id=DEMO_HOSTEL_REQUESTS[1]).first():
            db.session.add(HostelRequest(
                request_id=DEMO_HOSTEL_REQUESTS[1], student_id=student2.id,
                hostel_name='East Campus Hostel', room_preference='Double',
                reason='Would like to share with a friend.', status=HR_PENDING,
                hostel_block_id=blocks['East Campus Hostel'].id,
                request_type=REQ_ALLOCATION))

        if not HostelRequest.query.filter_by(request_type=REQ_MAINTENANCE).first():
            db.session.add(HostelRequest(
                request_id='HST-2024-003', student_id=student.id,
                hostel_name='North Campus Hostel', room_preference='N/A',
                reason='The geyser in the washroom is not working.',
                status=HR_PENDING, hostel_block_id=blocks['North Campus Hostel'].id,
                request_type=REQ_MAINTENANCE, issue_type='Electrical',
                location='North Campus Hostel - Block B, Washroom'))

        if not HostelRequest.query.filter_by(request_type=REQ_OUTPASS).first():
            db.session.add(HostelRequest(
                request_id='HST-2024-004', student_id=student2.id,
                hostel_name='East Campus Hostel', room_preference='N/A',
                reason='Attending a family function out of town.',
                status=HR_PENDING, hostel_block_id=blocks['East Campus Hostel'].id,
                request_type=REQ_OUTPASS,
                start_date=date.today() + timedelta(days=3),
                end_date=date.today() + timedelta(days=5)))
        db.session.commit()
        print('  sample hostel requests ensured')

        # ---------------- hostel notice ------------------------------------
        if not HostelNotice.query.first():
            db.session.add(HostelNotice(
                hostel_block_id=blocks['North Campus Hostel'].id,
                title='Water supply maintenance',
                message='Water supply will be interrupted on Friday from 9 AM to 1 PM '
                        'for routine maintenance.',
                created_by_id=warden.id))

        # ---------------- sample lost & found items ------------------------
        if not LostFoundItem.query.filter_by(item_id=DEMO_ITEMS[0]).first():
            db.session.add(LostFoundItem(
                item_id=DEMO_ITEMS[0], student_id=student.id, item_name='Black Backpack',
                item_type=ITEM_LOST,
                description='Black backpack with a laptop inside.',
                location='Main Library', date=date.today() - timedelta(days=5),
                status=LF_OPEN, item_category='Bags'))

        if not LostFoundItem.query.filter_by(item_id=DEMO_ITEMS[1]).first():
            db.session.add(LostFoundItem(
                item_id=DEMO_ITEMS[1], student_id=student.id, item_name='iPhone 13',
                item_type=ITEM_FOUND,
                description='White iPhone 13 found near the cafeteria.',
                location='Cafeteria', date=date.today() - timedelta(days=4),
                status=LF_OPEN, item_category='Electronics'))

        if not LostFoundItem.query.filter_by(item_name='Blue Water Bottle').first():
            db.session.add(LostFoundItem(
                item_id='LF-2024-003', student_id=student2.id, item_name='Blue Water Bottle',
                item_type=ITEM_FOUND,
                description='Steel blue bottle with a college sticker.',
                location='Central Sports Complex', date=date.today() - timedelta(days=2),
                status=LF_OPEN, item_category='Personal'))

        if not LostFoundItem.query.filter_by(item_name='House Keys').first():
            db.session.add(LostFoundItem(
                item_id='LF-2024-004', student_id=student2.id, item_name='House Keys',
                item_type=ITEM_FOUND,
                description='Two keys on a red keychain.',
                location='Hostel Main Gate', date=date.today() - timedelta(days=1),
                status=LF_OPEN, item_category='Personal'))
        db.session.commit()
        print('  sample lost & found items ensured')

        # A pending claim gives the Lost & Found staff queue something to review.
        bottle = LostFoundItem.query.filter_by(item_name='Blue Water Bottle').first()
        if bottle and not Claim.query.filter_by(item_id=bottle.id).first():
            db.session.add(Claim(item_id=bottle.id, student_id=student.id,
                                 identifying_info='Steel blue bottle with my name sticker '
                                                  'on the bottom.',
                                 status=CLAIM_PENDING))
            db.session.commit()
        print('  sample claim ensured')

        # ---------------- notifications -------------------------------------
        if not Notification.query.filter_by(ntype='system').first():
            for uid, title, msg in [
                (student.id, 'Welcome to CampusSync',
                 'Use the sidebar to submit complaints, hostel requests and lost & found reports.'),
                (student.id, 'Hostel Notice: Water supply maintenance',
                 'Water supply will be interrupted on Friday from 9 AM to 1 PM.'),
                (staff.id, 'Complaints awaiting review',
                 'Complaints routed to your department are listed on your dashboard.'),
                (lostfound.id, 'Claims awaiting review',
                 'New claim requests appear on your dashboard for review.'),
                (warden.id, 'Hostel requests pending',
                 'Requests for your hostel blocks are listed on your dashboard.'),
            ]:
                db.session.add(Notification(user_id=uid, title=title, message=msg,
                                            ntype='system'))
        db.session.commit()
        print('  notifications ensured')

        print()
        print('Database initialised with demo data.')
        print()
        print('Demo accounts:')
        for u in User.query.filter(User.username.in_(
                ['student', 'admin', 'warden', 'staff', 'lostfound'])).order_by(User.id):
            print('  %-10s %-18s role=%s' % (u.username, u.role, u.role_label))
        print()
        print('Totals: users=%d departments=%d categories=%d blocks=%d complaints=%d '
              'hostel_requests=%d items=%d claims=%d notifications=%d audit_logs=%d' % (
                  User.query.count(), Department.query.count(),
                  ComplaintCategory.query.count(), HostelBlock.query.count(),
                  Complaint.query.count(), HostelRequest.query.count(),
                  LostFoundItem.query.count(), Claim.query.count(),
                  Notification.query.count(), AuditLog.query.count()))


if __name__ == '__main__':
    main()