"""Load LinkedIn login material for Connect jobs/apply.

Password never belongs in the database, wiki, git, or API responses.
Order: process env (k8s secret) → credentials file (host coordinator).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HOST_FILE = Path.home() / '.shizuha' / 'linkedin-credentials.json'
POD_FILE = Path('/var/shizuha/linkedin-credentials.json')


def _from_env() -> dict | None:
    email = (os.environ.get('LINKEDIN_EMAIL') or '').strip()
    password = os.environ.get('LINKEDIN_PASSWORD') or ''
    if not email or not password:
        return None
    return {
        'email': email,
        'password': password,
        'username': (os.environ.get('LINKEDIN_USERNAME') or email.split('@')[0]).strip(),
        'profile_url': (os.environ.get('LINKEDIN_PROFILE_URL') or '').strip(),
    }


def _from_file(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    email = (data.get('email') or '').strip()
    password = data.get('password') or ''
    if not email or not password:
        return None
    return {
        'email': email,
        'password': password,
        'username': (data.get('username') or email.split('@')[0]).strip(),
        'profile_url': (data.get('profile_url') or '').strip(),
    }


def load_linkedin_credentials() -> dict | None:
    return _from_env() or _from_file(POD_FILE) or _from_file(HOST_FILE)


def credentials_present() -> bool:
    row = load_linkedin_credentials()
    return bool(row and row.get('email') and row.get('password'))


def public_linkedin_status() -> dict:
    """Safe-to-serialize status: never includes the password."""
    row = load_linkedin_credentials()
    if not row:
        return {'has_credentials': False, 'email': '', 'profile_url': ''}
    return {
        'has_credentials': True,
        'email': row.get('email') or '',
        'profile_url': row.get('profile_url') or '',
    }
