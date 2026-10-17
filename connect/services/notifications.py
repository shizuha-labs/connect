"""CON-15 (CON-6 P2, HLD 8780fe64): notification policy engine + idempotent fanout.

Centralizes the DM-notification SUPPRESSION decision and the at-least-once dedup
that were previously scattered (and partial) across `tasks.send_dm_notification`.

The decision has two layers so every fanout channel makes a consistent decision:

* `should_notify()` — GLOBAL suppressors that apply to ALL channels, evaluated in
  HLD order (first match wins, deterministic reason): service-disabled, muted,
  blocked, read-receipt race, and active-conversation-open (B.2).
* `should_notify_channel()` — the global decision PLUS per-channel suppressors
  (B.2): per-channel user opt-out, quiet hours (interrupt channels only — mobile /
  external / agent, never the in-app dashboard stream), and external-bridge
  cooldown. Per-channel fanout adapters call this so a recipient who is e.g. in
  quiet hours still gets the in-app dashboard entry but no mobile/external push.

`mark_dispatched()` enforces the idempotency key (recipient, conversation, event,
channel) UNIQUE so an at-least-once caller (Celery retry, WS reconnect replay,
double-broadcast) fans out at most once per (recipient, message, channel).
"""
from __future__ import annotations

from datetime import timedelta

try:  # py3.9+ stdlib; connect runs py3.12
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - defensive
    ZoneInfo = None

from django.utils import timezone

# HLD channels a DM notification can fan out to. Each is gated + deduped independently.
DASHBOARD = "dashboard"   # in-app WS notification stream (non-interrupting)
MOBILE = "mobile"         # push (shizuha-id push relay)
EXTERNAL = "external"     # external bridge (telegram/discord/whatsapp), where configured
AGENT = "agent"           # agent-runtime inbox
NOTIFICATION_CHANNELS = (DASHBOARD, MOBILE, EXTERNAL, AGENT)

# Channels that are an *interrupt* (a push/relay/inbox the user is not currently
# looking at). Quiet hours suppress these but never the in-app dashboard stream.
INTERRUPT_CHANNELS = (MOBILE, EXTERNAL, AGENT)

# Freshness window for the active-conversation-open suppressor. A focus signal
# older than this is treated as stale (a backgrounded tab that never sent a blur)
# so it cannot suppress notifications forever.
ACTIVE_CONVERSATION_TTL_SECONDS = 60

# External-bridge cooldown backoff bounds (exponential per consecutive failure).
BRIDGE_COOLDOWN_BASE_SECONDS = 30
BRIDGE_COOLDOWN_MAX_SECONDS = 3600


def should_notify(participant, message, *, now=None):
    """Return (notify: bool, suppressed_reason: str|None) for one recipient — the
    GLOBAL decision shared by every channel.

    `participant` is the recipient's ConversationParticipant; `message` is the
    DirectMessage. Conditions are evaluated in HLD order; the FIRST match wins so
    the reason is deterministic. A True result means every global suppressor passed
    (per-channel suppressors are applied by `should_notify_channel`)."""
    now = now or timezone.now()

    # 1. service-disabled — recipient turned notifications off for this conversation.
    if not participant.notifications_enabled:
        return False, "service_disabled"

    # 2. muted — permanent, or timed and still within the mute window.
    if participant.is_muted and (participant.muted_until is None or participant.muted_until > now):
        return False, "muted"

    # 3. blocked — block/visibility is authoritative via privacy.allowed_delivery_user_ids.
    from connect.services.privacy import allowed_delivery_user_ids
    if participant.user_id not in set(allowed_delivery_user_ids(message)):
        return False, "blocked"

    # 4. read-receipt race — the recipient's monotonic read cursor is already at or
    #    past this message (e.g. read on another device before the notify fired), so
    #    a notification would be stale. event_id is the globally-monotonic ordering.
    if (
        message.event_id is not None
        and participant.last_read_event_id is not None
        and message.event_id <= participant.last_read_event_id
    ):
        return False, "read_race"

    # 5. active-conversation-open (B.2) — the recipient has a fresh, foreground
    #    presence session focused on THIS conversation, so they are already looking
    #    at the message live and a notification would be redundant on every channel.
    if _active_conversation_open(participant.user_id, message.conversation_id, now):
        return False, "active_conversation"

    return True, None


def should_notify_channel(participant, message, channel, *, now=None, bridge_channel=None):
    """Per-channel decision: the global `should_notify` plus the B.2 channel-scoped
    suppressors (user opt-out, quiet hours, external-bridge cooldown).

    `bridge_channel` (EXTERNAL only) names the target bridge so the cooldown check
    is scoped to it; if omitted, any active cooldown for the recipient suppresses."""
    now = now or timezone.now()

    notify, reason = should_notify(participant, message, now=now)
    if not notify:
        return False, reason

    pref = _get_preference(participant.user_id)

    # Per-channel user opt-out (independent of quiet hours).
    if pref is not None and not pref.channel_enabled(channel):
        return False, "channel_disabled"

    # Quiet hours — suppress interrupt channels only; the in-app dashboard stream is
    # non-interrupting and still records its (idempotent) dispatch.
    if channel in INTERRUPT_CHANNELS and _in_quiet_hours(pref, now):
        return False, "quiet_hours"

    # External-bridge cooldown — a flapping/down bridge backs off instead of being
    # hammered once per message.
    if channel == EXTERNAL and _in_bridge_cooldown(participant.user_id, bridge_channel, now):
        return False, "bridge_cooldown"

    return True, None


def eligible_recipients(message, *, now=None):
    """Active, non-sender participants who pass the GLOBAL decision.

    Per-channel fanout adapters should additionally call `should_notify_channel`
    (or `eligible_recipients_for_channel`) to apply the channel-scoped suppressors."""
    from connect.models import ConversationParticipant
    now = now or timezone.now()
    parts = ConversationParticipant.objects.filter(
        conversation_id=message.conversation_id, has_left=False,
    ).exclude(user_id=message.sender_id)
    out = []
    for p in parts:
        notify, _reason = should_notify(p, message, now=now)
        if notify:
            out.append(p)
    return out


def eligible_recipients_for_channel(message, channel, *, now=None, bridge_channel=None):
    """Active, non-sender participants who pass the decision FOR `channel`."""
    from connect.models import ConversationParticipant
    now = now or timezone.now()
    parts = ConversationParticipant.objects.filter(
        conversation_id=message.conversation_id, has_left=False,
    ).exclude(user_id=message.sender_id)
    out = []
    for p in parts:
        notify, _reason = should_notify_channel(
            p, message, channel, now=now, bridge_channel=bridge_channel)
        if notify:
            out.append(p)
    return out


def mark_dispatched(recipient_id, conversation_id, event_id, channel):
    """Idempotency gate. Returns True if THIS call should perform the fanout (first
    time for the key), False if it was already dispatched (dedup → caller skips).

    Keyed on (recipient, conversation, event_id, channel) UNIQUE. event_id is the
    per-message monotonic id; a message without an event_id (rare/system) is treated
    as non-idempotent (always True) so it is never silently dropped."""
    if event_id is None:
        return True
    from connect.models import NotificationDispatch
    _obj, created = NotificationDispatch.objects.get_or_create(
        recipient_id=recipient_id,
        conversation_id=conversation_id,
        event_id=event_id,
        channel=channel,
    )
    return created


# --------------------------------------------------------------------------- #
# B.2 suppressor helpers + bridge-cooldown bookkeeping
# --------------------------------------------------------------------------- #

def _active_conversation_open(user_id, conversation_id, now):
    """True if the recipient has a FRESH, online presence session foregrounding
    this conversation (within ACTIVE_CONVERSATION_TTL_SECONDS)."""
    if conversation_id is None:
        return False
    from connect.models import PresenceSession
    cutoff = now - timedelta(seconds=ACTIVE_CONVERSATION_TTL_SECONDS)
    return PresenceSession.objects.filter(
        user_id=user_id,
        active_conversation_id=conversation_id,
        active_conversation_updated_at__gte=cutoff,
        status='online',
    ).exists()


def _get_preference(user_id):
    """Return the user's NotificationPreference row or None (defaults = all on)."""
    from connect.models import NotificationPreference
    return NotificationPreference.objects.filter(user_id=user_id).first()


def _in_quiet_hours(pref, now):
    """True if `now` falls inside the user's timezone-aware quiet-hours window.
    Handles a window that crosses midnight (quiet_start > quiet_end)."""
    if pref is None or not pref.quiet_hours_enabled:
        return False
    if pref.quiet_start is None or pref.quiet_end is None:
        return False
    start, end = pref.quiet_start, pref.quiet_end
    if start == end:
        return False  # zero-length window = effectively disabled

    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(pref.timezone or 'UTC')
        except Exception:
            tz = None
    local_time = timezone.localtime(now, tz).time() if tz else now.time()

    if start < end:
        return start <= local_time < end
    # wraps midnight: active after start OR before end
    return local_time >= start or local_time < end


def _in_bridge_cooldown(recipient_id, bridge_channel, now):
    """True if the recipient's external-bridge delivery is in an active cooldown.
    Scoped to `bridge_channel` when given; otherwise any active cooldown suffices."""
    from connect.models import BridgeDeliveryCooldown
    qs = BridgeDeliveryCooldown.objects.filter(
        recipient_id=recipient_id, cooldown_until__gt=now,
    )
    if bridge_channel:
        qs = qs.filter(bridge_channel=bridge_channel)
    return qs.exists()


def record_bridge_failure(recipient_id, bridge_channel, *, now=None):
    """Record one external-bridge delivery failure and (re)arm the cooldown with
    exponential backoff bounded by BRIDGE_COOLDOWN_MAX_SECONDS."""
    now = now or timezone.now()
    from connect.models import BridgeDeliveryCooldown
    obj, _created = BridgeDeliveryCooldown.objects.get_or_create(
        recipient_id=recipient_id, bridge_channel=bridge_channel,
    )
    obj.consecutive_failures = (obj.consecutive_failures or 0) + 1
    backoff = min(
        BRIDGE_COOLDOWN_BASE_SECONDS * (2 ** (obj.consecutive_failures - 1)),
        BRIDGE_COOLDOWN_MAX_SECONDS,
    )
    obj.cooldown_until = now + timedelta(seconds=backoff)
    obj.last_failure_at = now
    obj.save(update_fields=[
        'consecutive_failures', 'cooldown_until', 'last_failure_at', 'updated_at',
    ])
    return obj


def record_bridge_success(recipient_id, bridge_channel, *, now=None):
    """Reset the cooldown row after a successful external-bridge delivery."""
    now = now or timezone.now()
    from connect.models import BridgeDeliveryCooldown
    BridgeDeliveryCooldown.objects.filter(
        recipient_id=recipient_id, bridge_channel=bridge_channel,
    ).update(consecutive_failures=0, cooldown_until=None, last_success_at=now)
