"""PLAT-1278: Connect invariant metrics for Prometheus (PLAT-1255 pattern).

Read-only self-checks exposing invariant drift so the existing
Alertmanager/critical-alert-notifier path can DM the on-call lead. They must
not file Pulse tasks or mutate messaging state.

Invariants asserted here:
- connect_dm_cross_org_conversations: ZERO recently-active direct conversations
  whose two participants have known, disjoint organization sets (CON-129
  regression guard: fleet agents must never DM users outside their orgs).
  Basis: organization_id claims captured on PresenceSession rows (CON-14) —
  Connect has no local per-user org mapping (CON-79/CON-101), so this is the
  best local signal. Pairs where either side has no known org are counted in
  connect_dm_cross_org_unknown so the guard's blind spot stays visible instead
  of silently passing.
- connect_bridge_outbound_stuck: no BridgeOutboundEvent sits in 'pending'
  beyond the provider-acceptance window; the oldest age gauge feeds a
  backlog-age alert.
- connect_agent_unread_backlog: no agent with a live presence session
  accumulates a large, aging unread backlog — a connected-but-not-consuming
  (dead consumer) signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

DM_ACTIVITY_WINDOW = timedelta(hours=24)
PRESENCE_ORG_WINDOW = timedelta(days=7)
ACTIVE_PRESENCE_WINDOW = timedelta(minutes=10)
BRIDGE_PENDING_GRACE = timedelta(minutes=10)
UNREAD_BACKLOG_THRESHOLD = 25
UNREAD_BACKLOG_AGE = timedelta(minutes=30)
ACTIVE_SESSION_STATUSES = ('online', 'away', 'degraded')


@dataclass(frozen=True)
class ConnectInvariantMetrics:
    cross_org_conversations: int
    cross_org_unknown: int
    bridge_outbound_stuck: int
    bridge_outbound_oldest_age_seconds: int
    bridge_outbound_retrying: int
    bridge_outbound_retrying_oldest_age_seconds: int
    bridge_outbound_dead_lettered: int
    agent_unread_backlog: int
    probe_errors: int


def _presence_org_map(user_ids, now):
    """Known org sets per user from recent presence sessions (CON-14 claims)."""
    from connect.models.messaging import PresenceSession

    org_map = {}
    rows = PresenceSession.objects.filter(
        user_id__in=user_ids,
        last_heartbeat_at__gte=now - PRESENCE_ORG_WINDOW,
        organization_id__isnull=False,
    ).values_list('user_id', 'organization_id')
    for user_id, org_id in rows:
        org_map.setdefault(user_id, set()).add(org_id)
    return org_map


def _cross_org_counts(now):
    from connect.models.messaging import Conversation, ConversationParticipant

    conversations = list(
        Conversation.objects.filter(
            conversation_type='direct',
            last_message_at__gte=now - DM_ACTIVITY_WINDOW,
        ).values_list('id', flat=True)
    )
    if not conversations:
        return 0, 0
    participants = ConversationParticipant.objects.filter(
        conversation_id__in=conversations,
    ).values_list('conversation_id', 'user_id')
    by_conversation = {}
    for conversation_id, user_id in participants:
        by_conversation.setdefault(conversation_id, []).append(user_id)
    all_user_ids = {uid for uids in by_conversation.values() for uid in uids}
    org_map = _presence_org_map(all_user_ids, now)

    cross_org = unknown = 0
    for user_ids in by_conversation.values():
        if len(user_ids) < 2:
            continue
        a, b = user_ids[0], user_ids[1]
        orgs_a, orgs_b = org_map.get(a), org_map.get(b)
        if not orgs_a or not orgs_b:
            unknown += 1
        elif orgs_a.isdisjoint(orgs_b):
            cross_org += 1
    return cross_org, unknown


def _oldest_age_seconds(qs, now):
    oldest = qs.order_by('created_at').values_list('created_at', flat=True).first()
    return int((now - oldest).total_seconds()) if oldest else 0


def _bridge_backlog(now):
    from connect.models.messaging import BridgeOutboundEvent

    stuck = BridgeOutboundEvent.objects.filter(
        status=BridgeOutboundEvent.Status.PENDING,
        created_at__lt=now - BRIDGE_PENDING_GRACE,
    )
    retrying = BridgeOutboundEvent.objects.filter(status=BridgeOutboundEvent.Status.RETRYING)
    dead_lettered = BridgeOutboundEvent.objects.filter(status=BridgeOutboundEvent.Status.DEAD_LETTERED)
    return (
        stuck.count(),
        _oldest_age_seconds(stuck, now),
        retrying.count(),
        _oldest_age_seconds(retrying, now),
        dead_lettered.count(),
    )


def _agent_unread_backlog(now):
    from connect.models.messaging import ConversationParticipant, PresenceSession

    active_agents = set(
        PresenceSession.objects.filter(
            is_agent=True,
            status__in=ACTIVE_SESSION_STATUSES,
            last_heartbeat_at__gte=now - ACTIVE_PRESENCE_WINDOW,
        ).values_list('user_id', flat=True)
    )
    if not active_agents:
        return 0
    from django.db.models import Q

    return (
        ConversationParticipant.objects.filter(
            participant_type='agent',
            user_id__in=active_agents,
            unread_count__gte=UNREAD_BACKLOG_THRESHOLD,
            has_left=False,
        )
        .filter(Q(last_read_at__isnull=True) | Q(last_read_at__lt=now - UNREAD_BACKLOG_AGE))
        .count()
    )


def collect_connect_invariant_metrics(now=None) -> ConnectInvariantMetrics:
    """Collect the PLAT-1278 Connect self-check metrics (read-only)."""
    now = now or timezone.now()
    probe_errors = 0

    try:
        cross_org, cross_org_unknown = _cross_org_counts(now)
    except Exception:
        cross_org, cross_org_unknown = 0, 0
        probe_errors += 1
    try:
        (
            bridge_stuck,
            bridge_oldest_age,
            bridge_retrying,
            bridge_retrying_oldest_age,
            bridge_dead_lettered,
        ) = _bridge_backlog(now)
    except Exception:
        bridge_stuck, bridge_oldest_age = 0, 0
        bridge_retrying, bridge_retrying_oldest_age, bridge_dead_lettered = 0, 0, 0
        probe_errors += 1
    try:
        agent_backlog = _agent_unread_backlog(now)
    except Exception:
        agent_backlog = 0
        probe_errors += 1

    return ConnectInvariantMetrics(
        cross_org_conversations=cross_org,
        cross_org_unknown=cross_org_unknown,
        bridge_outbound_stuck=bridge_stuck,
        bridge_outbound_oldest_age_seconds=bridge_oldest_age,
        bridge_outbound_retrying=bridge_retrying,
        bridge_outbound_retrying_oldest_age_seconds=bridge_retrying_oldest_age,
        bridge_outbound_dead_lettered=bridge_dead_lettered,
        agent_unread_backlog=agent_backlog,
        probe_errors=probe_errors,
    )


def render_invariant_metrics(metrics: ConnectInvariantMetrics) -> str:
    """Render Prometheus text exposition without new runtime dependencies."""
    lines = [
        '# HELP connect_dm_cross_org_conversations Recently-active direct conversations between users with known, disjoint org sets (CON-129 guard; must stay 0).',
        '# TYPE connect_dm_cross_org_conversations gauge',
        f'connect_dm_cross_org_conversations {metrics.cross_org_conversations}',
        '# HELP connect_dm_cross_org_unknown Recently-active direct conversations where at least one participant has no known org (guard blind spot, kept visible).',
        '# TYPE connect_dm_cross_org_unknown gauge',
        f'connect_dm_cross_org_unknown {metrics.cross_org_unknown}',
        '# HELP connect_bridge_outbound_stuck Bridge outbound events pending provider acceptance beyond the grace window.',
        '# TYPE connect_bridge_outbound_stuck gauge',
        f'connect_bridge_outbound_stuck {metrics.bridge_outbound_stuck}',
        '# HELP connect_bridge_outbound_oldest_age_seconds Age of the oldest stuck pending bridge outbound event.',
        '# TYPE connect_bridge_outbound_oldest_age_seconds gauge',
        f'connect_bridge_outbound_oldest_age_seconds {metrics.bridge_outbound_oldest_age_seconds}',
        '# HELP connect_bridge_outbound_retrying Bridge outbound events delayed under CON-164 provider/backpressure retry semantics.',
        '# TYPE connect_bridge_outbound_retrying gauge',
        f'connect_bridge_outbound_retrying {metrics.bridge_outbound_retrying}',
        '# HELP connect_bridge_outbound_retrying_oldest_age_seconds Age of the oldest retrying outbound bridge event.',
        '# TYPE connect_bridge_outbound_retrying_oldest_age_seconds gauge',
        f'connect_bridge_outbound_retrying_oldest_age_seconds {metrics.bridge_outbound_retrying_oldest_age_seconds}',
        '# HELP connect_bridge_outbound_dead_lettered Bridge outbound events in terminal dead-lettered state.',
        '# TYPE connect_bridge_outbound_dead_lettered gauge',
        f'connect_bridge_outbound_dead_lettered {metrics.bridge_outbound_dead_lettered}',
        '# HELP connect_agent_unread_backlog Agents with a live presence session but a large aging unread backlog (dead-consumer signal).',
        '# TYPE connect_agent_unread_backlog gauge',
        f'connect_agent_unread_backlog {metrics.agent_unread_backlog}',
        '# HELP connect_invariant_probe_errors Invariant collection sections that failed this scrape (fail-loud).',
        '# TYPE connect_invariant_probe_errors gauge',
        f'connect_invariant_probe_errors {metrics.probe_errors}',
    ]
    return '\n'.join(lines) + '\n'
