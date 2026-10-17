"""CON-231: the single definition of "eligible undelivered DM replay candidate".

The reconnect replay path and the operator backlog probe MUST agree on what
counts as a candidate. If the probe re-implements the predicate it will drift
from the consumer, and an operator asking "is this user's backlog draining?"
would be answered from a different set of rows than the one the cap is applied
to — which is worse than having no probe.
"""
from __future__ import annotations

from django.db import models


def eligible_candidate_filter(participants, floor):
    """Build the per-conversation cutoff filter shared by replay and the probe.

    `participants` is an iterable of ConversationParticipant rows carrying
    `conversation_id`, `last_read_at` and `cleared_at`. Returns None when the
    user participates in nothing, so callers can short-circuit.

    PLAT-9268: the cutoff deliberately does NOT include ``last_read_at``. A
    read marker is a READ boundary, not a DELIVERY boundary — it advances
    whenever any newer message is read, which permanently orphaned undelivered
    messages sent during a WebSocket outage (observed 09-19: two messages
    stored with no delivery row while later messages delivered fine; the
    reconnect replay's ``max(floor, last_read_at, cleared_at)`` cutoff skipped
    both). Re-sending an already-delivered message is prevented by the
    delivery-receipt exclusion in ``eligible_candidate_queryset``, so the read
    marker is redundant for dedupe and harmful for completeness. Read-but-
    undelivered is exactly the orphan class this predicate must include.
    """
    per_conv_filters = []
    for p in participants:
        cutoff = max(
            dt for dt in (floor, p.cleared_at) if dt is not None
        )
        per_conv_filters.append(
            models.Q(conversation_id=p.conversation_id) & models.Q(created_at__gt=cutoff)
        )
    if not per_conv_filters:
        return None
    combined = per_conv_filters[0]
    for f in per_conv_filters[1:]:
        combined |= f
    return combined


def eligible_candidate_queryset(user_id, participants, floor):
    """Undelivered replay candidates for one user, newest first.

    Mirrors `_fetch_missed_messages` exactly: not deleted, not authored by this
    user, not individually deleted by them, and — critically — NOT already
    delivered. Delivered rows are excluded BEFORE the cap so a reconnect whose
    newest candidates were delivered live cannot permanently starve older
    undelivered rows.
    """
    from connect.models import DirectMessage

    combined = eligible_candidate_filter(participants, floor)
    if combined is None:
        return DirectMessage.objects.none()
    return (
        DirectMessage.objects.filter(combined)
        .filter(is_deleted=False)
        .exclude(sender_id=user_id)
        .exclude(deletions__user_id=user_id)
        .exclude(delivery_receipts__user_id=user_id)
        .order_by('-created_at')
    )
