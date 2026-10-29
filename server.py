import os
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from app.startup_diagnostics import report_startup_failure, startup_failure_application

BASE_DIR = Path(__file__).resolve().parent


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietWSGIRequestHandler(WSGIRequestHandler):
    def address_string(self):
        return "local"


def make_absolute_dir_env(name: str, default_path: Path) -> Path:
    value = os.environ.get(name)
    path = Path(value) if value else default_path
    if not path.is_absolute():
        path = BASE_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(path)
    return path


try:
    os.chdir(BASE_DIR)

    from app.runtime_config import load_runtime_config

    load_runtime_config()
    attachments_dir = make_absolute_dir_env("CRM_CHAT_ATTACHMENTS_DIR", BASE_DIR / "chat_attachments")

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or os.environ.get("APP_PORT") or 20004)
    host = os.environ.get("HOST", "127.0.0.1")

    with make_server(
        host,
        port,
        application,
        server_class=ThreadingWSGIServer,
        handler_class=QuietWSGIRequestHandler,
    ) as server:
        server.serve_forever()
