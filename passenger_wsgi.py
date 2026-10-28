import os
from pathlib import Path

from a2wsgi import ASGIMiddleware
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from app.db import init_db
from app.main import app as fastapi_app
from app.main import ensure_initial_admin

init_db()
ensure_initial_admin()
application = ASGIMiddleware(fastapi_app)
