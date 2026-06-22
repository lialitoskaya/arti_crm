import os
from pathlib import Path

from a2wsgi import ASGIMiddleware
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from app.main import app as fastapi_app

application = ASGIMiddleware(fastapi_app)