from app.startup_diagnostics import report_startup_failure, startup_failure_application

try:
    from app.runtime_config import load_runtime_config

    load_runtime_config()

    from a2wsgi import ASGIMiddleware
    from app.db import init_db
    from app.main import app as fastapi_app
    from app.main import ensure_initial_admin

    init_db()
    ensure_initial_admin()
    application = ASGIMiddleware(fastapi_app)
except Exception as error:
    application = startup_failure_application
    report_startup_failure(error)
