"""CON-480: agent identity-form normalization for username lookups.

Agent Connect principals appear as `@agents.shizuha.io`-suffixed forms
(`[ichi@agents.shizuha.io]` inbox headers, `recipient_username=
"ichi@agents.shizuha.io"` from harness tooling), but shizuha-id's
`/internal/users/by-username/` matches bare usernames only. A suffixed
form therefore 404'd every recipient lookup even though the bare form
resolved, breaking DMs addressed the way the UI/tools display them.

`normalize_username` collapses the agent email-domain form to the bare
username; any other address (real emails on other domains) is returned
unchanged so email-form lookups keep their semantics.
"""
import re

AGENT_EMAIL_DOMAIN_RE = re.compile(
    r'^(?P<local>[^@\s]+)@agents\.shizuha\.io$', re.IGNORECASE,
)


def normalize_username(raw) -> str:
    """`ichi@agents.shizuha.io` -> `ichi`; anything else passes through."""
    username = str(raw or '').strip()
    m = AGENT_EMAIL_DOMAIN_RE.match(username)
    if m:
        return m.group('local')
    return username
