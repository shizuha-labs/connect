"""QA/test traffic must not land on production fleet agents.

Ni/Shion 2026-09-15: liveness/test DMs and QA probes became GLM user turns,
poisoned 168k–195k sessions, and DMd the operator pong/test-message loops.
"""
from __future__ import annotations

QA_FIXTURE_EMAIL_SUFFIXES = (
    '@qafixture.shizuha.com',
    '@qafixture.local',
)
QA_AGENT_USERNAMES = frozenset({
    'enaqa',
    'qabot',
    'qa-agent',
})


def is_qa_fixture_identity(*, email: str = '', username: str = '') -> bool:
    e = (email or '').strip().lower()
    u = (username or '').strip().lower()
    if any(e.endswith(suf) for suf in QA_FIXTURE_EMAIL_SUFFIXES):
        return True
    if u in QA_AGENT_USERNAMES:
        return True
    if u.endswith('-qa') or u.startswith('qa-'):
        return True
    local = e.split('@', 1)[0] if '@' in e else ''
    if local.endswith('-qa') or local.startswith('qa-') or local in QA_AGENT_USERNAMES:
        return True
    return False


def is_production_fleet_agent(*, participant_type: str = '', email: str = '', username: str = '') -> bool:
    if str(participant_type or '').strip().lower() != 'agent':
        return False
    return not is_qa_fixture_identity(email=email, username=username)
