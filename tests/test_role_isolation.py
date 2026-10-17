"""
test_role_isolation.py — Regression: verify role-separated entrypoint and
supervisor configs do not leak processes across roles.

CON-219: The backend image must run only the processes for its declared role.
This test parses the entrypoint and supervisor configs to assert:
- web role runs only Daphne + MCP (no Celery worker, beat, or migrate)
- worker role runs only Celery worker
- beat role runs only Celery beat
- migrate role runs only migrations
- Default role is web (backward compatible)
- Invalid role fails closed
"""

import os
import re
import subprocess
import tempfile


ENTRYPOINT_PATH = os.path.join(os.path.dirname(__file__), "..", "docker-entrypoint.sh")
SUPERVISOR_WEB_PATH = os.path.join(os.path.dirname(__file__), "..", "supervisord.web.conf")
SUPERVISOR_FULL_PATH = os.path.join(os.path.dirname(__file__), "..", "supervisord.conf")


def test_entrypoint_exists():
    assert os.path.isfile(ENTRYPOINT_PATH), f"Entrypoint not found: {ENTRYPOINT_PATH}"
    assert os.access(ENTRYPOINT_PATH, os.X_OK), "Entrypoint must be executable"


def test_supervisor_web_exists():
    assert os.path.isfile(SUPERVISOR_WEB_PATH), f"Web supervisor not found: {SUPERVISOR_WEB_PATH}"


def test_default_role_is_web():
    """Default CONNECT_ROLE must be web for backward compatibility."""
    with open(ENTRYPOINT_PATH) as f:
        content = f.read()
    # Find the default assignment
    match = re.search(r'ROLE="\$\{CONNECT_ROLE:-(\w+)\}"', content)
    assert match is not None, "Could not find CONNECT_ROLE default assignment"
    assert match.group(1) == "web", f"Default role should be 'web', got '{match.group(1)}'"


def test_invalid_role_fails_closed():
    """Invalid CONNECT_ROLE must exit with error."""
    result = subprocess.run(
        ["bash", "-c", f"CONNECT_ROLE=invalid bash {ENTRYPOINT_PATH} 2>&1 || true"],
        capture_output=True, text=True, timeout=5,
    )
    assert "FATAL" in result.stdout or "FATAL" in result.stderr, (
        f"Invalid role should fail closed. stdout: {result.stdout}, stderr: {result.stderr}"
    )


def test_web_role_no_celery_in_supervisor():
    """Web supervisor config must NOT contain celery-worker or celery-beat programs."""
    with open(SUPERVISOR_WEB_PATH) as f:
        content = f.read()
    assert "[program:celery-worker]" not in content, (
        "Web supervisor must not contain celery-worker"
    )
    assert "[program:celery-beat]" not in content, (
        "Web supervisor must not contain celery-beat"
    )
    assert "[program:migrate]" not in content, (
        "Web supervisor must not contain migrate"
    )


def test_web_role_has_daphne_and_mcp():
    """Web supervisor config must contain Daphne and MCP programs."""
    with open(SUPERVISOR_WEB_PATH) as f:
        content = f.read()
    assert "[program:django]" in content, "Web supervisor must contain django (Daphne)"
    assert "[program:mcp-server]" in content, "Web supervisor must contain mcp-server"


def test_entrypoint_has_all_roles():
    """Entrypoint must handle all four roles: web, worker, beat, migrate."""
    with open(ENTRYPOINT_PATH) as f:
        content = f.read()
    for role in ("web", "worker", "beat", "migrate"):
        assert f'  {role})' in content, (
            f"Entrypoint must handle role '{role}'"
        )


def test_web_role_dispatch_uses_web_supervisor():
    """Web role dispatch must use supervisord.web.conf."""
    with open(ENTRYPOINT_PATH) as f:
        content = f.read()
    assert "supervisord.web.conf" in content, (
        "Web role must use supervisord.web.conf"
    )


def test_worker_role_direct_exec():
    """Worker role must exec celery worker directly (no supervisor)."""
    with open(ENTRYPOINT_PATH) as f:
        content = f.read()
    assert "celery -A connectmanager worker" in content, (
        "Worker role must exec celery worker"
    )


def test_beat_role_direct_exec():
    """Beat role must exec celery beat directly (no supervisor)."""
    with open(ENTRYPOINT_PATH) as f:
        content = f.read()
    assert "celery -A connectmanager beat" in content, (
        "Beat role must exec celery beat"
    )


def test_migrate_role_no_supervisor():
    """Migrate role must run manage.py migrate directly and exit."""
    with open(ENTRYPOINT_PATH) as f:
        content = f.read()
    assert "python manage.py migrate" in content, (
        "Migrate role must run manage.py migrate"
    )
    assert "exit 0" in content, "Migrate role must exit after completion"


def test_full_supervisor_still_has_all_programs():
    """The full supervisord.conf (for legacy/direct use) must still have all programs."""
    with open(SUPERVISOR_FULL_PATH) as f:
        content = f.read()
    assert "[program:celery-worker]" in content
    assert "[program:celery-beat]" in content
    assert "[program:migrate]" in content
    assert "[program:django]" in content
    assert "[program:mcp-server]" in content


def test_entrypoint_syntax():
    """Entrypoint must pass bash syntax check."""
    result = subprocess.run(
        ["bash", "-n", ENTRYPOINT_PATH],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, (
        f"Entrypoint syntax error: {result.stderr}"
    )


def test_web_supervisor_syntax():
    """Web supervisor config must be valid (parsable by supervisor)."""
    # Supervisor configs are INI-like; check basic structure
    with open(SUPERVISOR_WEB_PATH) as f:
        content = f.read()
    assert "[supervisord]" in content, "Must have [supervisord] section"
    assert "[program:" in content, "Must have at least one [program:x] section"
