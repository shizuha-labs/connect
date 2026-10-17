"""
CON-19: Webhook diagnostics and audit privacy (CON-16 C8-C9).

Webhook diagnostic logs strip message bodies after a defined window.
Long-term diagnostics keep metadata only. Moderation/admin audit entries
never include private message or profile content.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any


# ── Webhook diagnostic stripping (C8) ─────────────────────────────────────────

#: How long the full webhook diagnostic (including message body) is retained.
WEBHOOK_BODY_RETENTION_WINDOW = datetime.timedelta(hours=24)


@dataclasses.dataclass
class WebhookDiagnostic:
    """
    A single webhook delivery record (C8).

    Within WEBHOOK_BODY_RETENTION_WINDOW, body is available. After the window
    it is stripped to metadata only.
    """

    delivery_id: str
    webhook_id: str
    delivered_at: datetime.datetime
    http_status: int
    # body is ephemeral — only retained within the window (C8).
    body: str | None = dataclasses.field(default=None, repr=False)

    def strip_body(self) -> "WebhookDiagnostic":
        """Return a copy of this record with the body cleared (C8)."""
        return dataclasses.replace(self, body=None)

    def is_within_retention_window(self, now: datetime.datetime | None = None) -> bool:
        """Return True if the body is still within the retention window (C8)."""
        if now is None:
            now = datetime.datetime.now(tz=datetime.timezone.utc)
        age = now - self.delivered_at
        return age <= WEBHOOK_BODY_RETENTION_WINDOW


def strip_expired_bodies(
    diagnostics: list[WebhookDiagnostic],
    now: datetime.datetime | None = None,
) -> list[WebhookDiagnostic]:
    """
    Return a new list where bodies older than WEBHOOK_BODY_RETENTION_WINDOW
    have been cleared (C8). Preserves ordering and all metadata fields.
    """
    result = []
    for d in diagnostics:
        if d.is_within_retention_window(now):
            result.append(d)
        else:
            result.append(d.strip_body())
    return result


# ── Moderation/admin audit privacy (C9) ──────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class ModerationAuditEntry:
    """
    A moderation or admin audit record (C9).

    Contains only identifiers, action type, and timestamp — never private
    message content, DM bodies, profile private fields, or PII beyond user_id.
    """

    entry_id: str
    actor_id: int           # moderator/admin user ID
    target_id: int          # subject user/content ID
    action: str             # e.g. "content_removed", "account_suspended"
    recorded_at: datetime.datetime
    # reason is the moderation reason code, not a free-form message body (C9).
    reason_code: str


def build_moderation_audit(
    entry_id: str,
    actor_id: int,
    target_id: int,
    action: str,
    reason_code: str,
    private_content: Any = None,  # explicitly accepted and discarded (C9)
) -> ModerationAuditEntry:
    """
    Build a moderation audit entry, discarding any private_content (C9).

    private_content is accepted as a parameter to make the discard explicit at
    every call site — callers cannot accidentally omit it and include it elsewhere.
    """
    del private_content  # C9: private content is never stored in audit entries
    return ModerationAuditEntry(
        entry_id=entry_id,
        actor_id=actor_id,
        target_id=target_id,
        action=action,
        recorded_at=datetime.datetime.now(tz=datetime.timezone.utc),
        reason_code=reason_code,
    )
