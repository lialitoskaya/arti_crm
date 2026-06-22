import os
import traceback
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "startup_error.log"


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietWSGIRequestHandler(WSGIRequestHandler):
    def address_string(self):
        return "local"


def write_log(text: str) -> None:
    try:
        LOG_PATH.write_text(text, encoding="utf-8")
    except Exception:
        pass


def make_absolute_file_env(name: str, default_path: Path) -> Path:
    value = os.environ.get(name)
    path = Path(value) if value else default_path
    if not path.is_absolute():
        path = BASE_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(path)
    return path


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

    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=True)

    db_path = make_absolute_file_env("DATABASE_PATH", BASE_DIR / "data" / "crm.sqlite3")
    attachments_dir = make_absolute_dir_env("CRM_CHAT_ATTACHMENTS_DIR", BASE_DIR / "chat_attachments")

    from a2wsgi import ASGIMiddleware
    from app.main import app as fastapi_app

    application = ASGIMiddleware(fastapi_app)

    write_log(
        "CRM started successfully\n"
        f"BASE_DIR={BASE_DIR}\n"
        f"DATABASE_PATH={db_path}\n"
        f"CRM_CHAT_ATTACHMENTS_DIR={attachments_dir}\n"
        f"DB_EXISTS={db_path.exists()}\n"
        "SERVER=threaded_wsgi\n"
    )

except Exception:
    error = traceback.format_exc()
    write_log(error)

    def application(environ, start_response):
        body = (
            "CRM startup error. Open startup_error.log in the site root.\n\n" + error
        ).encode("utf-8", errors="replace")
        start_response(
            "500 Internal Server Error",
            [("Content-Type", "text/plain; charset=utf-8")],
        )
        return [body]


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
