"""CON-429 S2 (design §5/§9.3): notification digest producer.

Daily (configurable weekly) summary post per system channel: counts +
headlines of `informational` events in the window, with per-producer
breakdown. Replaces individual informational posts once enabled per
channel (`Conversation.digest_enabled`, default OFF until the §7.1
backfill is verified — design §10 Q2).

Hard invariants (ichi advisory anchors — each asserted by test in
tests/test_con429_digest_invariants.py):

- Anchor 1 (hard query-level `alert` exclusion): the digest queryset
  filters `notification_class='informational'` at the QUERY level — an
  `alert` row can never match, so a batching bug cannot batch an andon.
- Anchor 3 (self-exclusion): the digest queryset excludes the digest's own
  posts (identified by `media_metadata__sender_service=DIGEST_SERVICE_TAG`)
  — without this the digest grows by one post per run until it digests its
  own digest.
- Wake-relevant producers never fold (design §2.1 ichi ruling): the
  queryset excludes rows minted by wake-relevant producers — digesting
  them breaks wake semantics. Seeded from the design's verified call-site
  enumeration; the pulse-side stamping child verifies the exact tags.

Non-regression (design §8): the digest only READS informational rows and
mints ONE informational post per window. DM delivery and agent wake paths
are untouched — no WS bridge changes, no DM classification.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from connect.models import DirectMessage, VALID_NOTIFICATION_CLASSES

# CON-429 §5: the digest post is itself an informational post in the same
# conversation. Stamping this tag in `media_metadata.sender_service` is what
# makes anchor 3 (self-exclusion) a query-level filter.
DIGEST_SERVICE_TAG = 'connect-digest'
DIGEST_SENDER_NAME = 'Connect Digest'

# CON-429 §2.1 (ichi ruling): wake-relevant producers never fold into
# digests. Keyed on the origin tags the producers stamp on their ingress
# POSTs (sender_service, sender_name). Seeded from the design's verified
# call-site enumeration (fire_due_cycle / fire_recurring_due — pulse
# scheduler); the pulse-side stamping child owns tag verification.
WAKE_RELEVANT_PRODUCERS = frozenset({
    ('pulse', 'Pulse Scheduler'),
})

DIGEST_FREQUENCIES = ('daily', 'weekly')


def digest_queryset(conversation, start, end):
    """The digest window queryset — hard invariants live here.

    Anchor 1: `notification_class='informational'` is a QUERY-level filter,
    so `alert` rows can never be returned regardless of caller error.
    Anchor 3: the digest's own posts are excluded at the same level.
    Wake-relevant producers are excluded at the same level (§2.1).

    NULL-guard (live 09-17 repro, fresh stack): a bare JSON-key exclude
    ALSO drops rows whose `media_metadata` is NULL/missing — the key
    transform is NULL for those rows, `NOT (NULL = X)` is NULL, and a NULL
    WHERE condition is not TRUE. Every plain system event carries no
    metadata, so the unguarded form folded nothing, ever. Each JSON-key
    predicate is therefore paired with `sender_service__isnull=False` so a
    missing key reads FALSE, not NULL.
    """
    wake_q = Q()
    for service, name in WAKE_RELEVANT_PRODUCERS:
        wake_q |= (
            Q(media_metadata__sender_service__isnull=False)
            & Q(media_metadata__sender_service=service)
            & Q(sender_name=name)
        )
    return (
        DirectMessage.objects.filter(
            conversation=conversation,
            notification_class='informational',
            created_at__gte=start,
            created_at__lt=end,
        )
        .exclude(
            Q(media_metadata__sender_service__isnull=False)
            & Q(media_metadata__sender_service=DIGEST_SERVICE_TAG)
        )
        .exclude(wake_q)
    )


def digest_window(now=None, frequency='daily'):
    """Half-open [start, end) window ending at `now`."""
    if frequency not in DIGEST_FREQUENCIES:
        raise ValueError(f'invalid digest frequency {frequency!r}')
    end = now or timezone.now()
    start = end - (timedelta(days=1) if frequency == 'daily' else timedelta(days=7))
    return start, end


def _latest_digest_post(conversation):
    return (
        DirectMessage.objects.filter(
            conversation=conversation,
            media_metadata__sender_service=DIGEST_SERVICE_TAG,
        )
        .order_by('-created_at')
        .first()
    )


def build_digest_post(conversation, now=None, frequency='daily'):
    """Mint at most ONE digest post per window for `conversation`.

    Returns (created, post, event_count). Skips (created=False) when the
    channel has no digest-enabled flag, the window has no eligible events,
    or a digest post already covers the window (once-per-window semantics —
    a repeat run for the same window mints nothing).
    """
    if conversation.digest_enabled is not True:
        return False, None, 0
    start, end = digest_window(now=now, frequency=frequency)
    latest = _latest_digest_post(conversation)
    if latest is not None and latest.created_at >= start:
        # A digest post already covers this window — repeat run mints nothing.
        return False, None, 0
    events = list(digest_queryset(conversation, start, end).order_by('created_at'))
    if not events:
        return False, None, 0

    by_producer = {}
    for e in events:
        service = (e.media_metadata or {}).get('sender_service') or 'unknown'
        by_producer[service] = by_producer.get(service, 0) + 1
    breakdown = ', '.join(f'{k}={v}' for k, v in sorted(by_producer.items()))
    headlines = '\n'.join(
        f'- {e.content[:160]}' for e in events[-10:]
    )
    content = (
        f'Digest ({frequency}) for {start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M} UTC: '
        f'{len(events)} informational event(s) [{breakdown}]\n{headlines}'
    )
    post = DirectMessage.objects.create(
        conversation=conversation,
        sender_id=0,
        sender_email='system',
        sender_name=DIGEST_SENDER_NAME,
        message_type='system',
        content=content,
        notification_class='informational',
        media_metadata={
            'sender_service': DIGEST_SERVICE_TAG,
            'digest_window_start': start.isoformat(),
            'digest_window_end': end.isoformat(),
            'digest_frequency': frequency,
            'digest_event_count': len(events),
        },
    )
    return True, post, len(events)


__all__ = [
    'DIGEST_SERVICE_TAG',
    'DIGEST_SENDER_NAME',
    'DIGEST_FREQUENCIES',
    'WAKE_RELEVANT_PRODUCERS',
    'VALID_NOTIFICATION_CLASSES',
    'digest_queryset',
    'digest_window',
    'build_digest_post',
]
