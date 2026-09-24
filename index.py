"""Vercel WSGI entrypoint: requires persistent PostgreSQL, even without system env vars."""
from app import create_app

app = create_app({"HOSTED": True, "AUTO_MIGRATE": False})
