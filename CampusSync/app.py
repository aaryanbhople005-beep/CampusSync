"""CampusSync - An Integrated University Service Management System.

Application factory.  Existing behaviour is preserved; blueprints are added for
the role-specific areas described in the SRS.
"""

from flask import Flask, render_template, request, redirect, url_for, session

from config import Config
from models import db, ROLE_HOME


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    # --- existing blueprints (kept) -------------------------------------
    from routes.auth import auth_bp
    from routes.student import student_bp
    from routes.admin import admin_bp

    # --- new role blueprints added for the SRS upgrade -------------------
    from routes.warden import warden_bp
    from routes.staff import staff_bp
    from routes.lostfound import lostfound_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(warden_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(lostfound_bp)
    app.register_blueprint(admin_bp)

    @app.route('/')
    def index():
        user_id = session.get('user_id')
        if not user_id:
            return redirect(url_for('auth.login'))

        # Re-verify the role from the database rather than trusting the session.
        from models import User
        user = db.session.get(User, user_id)
        if not user or not user.is_active:
            session.clear()
            return redirect(url_for('auth.login'))

        endpoint = ROLE_HOME.get(user.role)
        if endpoint:
            return redirect(url_for(endpoint))
        return redirect(url_for('auth.login'))

    @app.errorhandler(404)
    def not_found(e):
        return render_template('error.html', code=404,
                               message='The page you requested was not found.'), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template('error.html', code=403,
                               message='You are not authorized to access this page.'), 403

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        return render_template('error.html', code=500,
                               message='An unexpected error occurred. Please try again.'), 500

    # Keep the schema up to date on every start-up (idempotent, non-destructive).
    from migrations import run_migrations
    from security import seed_default_settings

    with app.app_context():
        run_migrations()
        seed_default_settings()

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(debug=True, host='0.0.0.0', port=5000)