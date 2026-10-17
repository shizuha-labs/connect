"""CON-231: operator/admin DM-replay backlog probe.

Identifies users whose eligible undelivered DM candidate set exceeds the
reconnect replay cap (`CONNECT_DM_REPLAY_LIMIT`, default 200) and reports
whether repeated reconnect/cursor-sync drains the backlog. Privacy-safe: only
a SHA-256 hash of the username is emitted (never the raw username/email), and
no high-cardinality user/conversation IDs are used as labels.

Read-only by default; pass --users to scope to specific usernames.
"""
from __future__ import annotations

import hashlib

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import models

from connect.consumers import (
    DEFAULT_DM_REPLAY_LIMIT,
    DEFAULT_DM_REPLAY_WINDOW_DAYS,
    _replay_limits_for,
)
from connect.models import ConversationParticipant, DirectMessage


def _user_hash(username: str) -> str:
    """Privacy-safe identifier: SHA-256 of the username (truncated)."""
    return hashlib.sha256(username.encode('utf-8')).hexdigest()[:16]


def _eligible_undelivered_count(user, replay_limit: int, floor) -> tuple[int, int, int]:
    """Return (eligible_count, conversation_count, capped).

    Mirrors UserChatConsumer._fetch_missed_messages: eligible = undelivered
    DMs (not deleted, not self-authored, not deleted-by-user, no delivery
    receipt) newer than each conversation's cutoff (max of replay floor,
    last_read_at, cleared_at), across all active conversations.
    """
    participants = ConversationParticipant.objects.filter(
        user_id=user.id,
        has_left=False,
    ).only('conversation_id', 'last_read_at', 'cleared_at')

    per_conv_filters = []
    for p in participants:
        cutoff = max(
            dt for dt in (floor, p.last_read_at, p.cleared_at) if dt is not None
        )
        per_conv_filters.append(
            models.Q(conversation_id=p.conversation_id) & models.Q(created_at__gt=cutoff)
        )

    if not per_conv_filters:
        return 0, 0, False

    combined = per_conv_filters[0]
    for f in per_conv_filters[1:]:
        combined |= f

    eligible = (
        DirectMessage.objects.filter(combined)
        .filter(is_deleted=False)
        .exclude(sender_id=user.id)
        .exclude(deletions__user_id=user.id)
        .exclude(delivery_receipts__user_id=user.id)
    )
    total = eligible.count()
    # capped mirrors the consumer: eligible set exceeds the cap.
    capped = total > replay_limit
    return total, len(per_conv_filters), capped


class Command(BaseCommand):
    help = (
        'Report users whose eligible undelivered DM candidate set exceeds the '
        'reconnect replay cap (CON-231). Read-only; privacy-safe user hashes.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--users', nargs='*', default=None,
            help='Scope to these usernames (default: all active users).',
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Override the replay cap (default: CONNECT_DM_REPLAY_LIMIT or 200).',
        )
        parser.add_argument(
            '--min-eligible', type=int, default=0,
            help='Only report users with at least this many eligible candidates.',
        )

    def handle(self, *args, **options):
        User = get_user_model()
        users_qs = User.objects.filter(is_active=True)
        if options.get('users'):
            users_qs = users_qs.filter(username__in=options['users'])

        replay_limit, floor = _replay_limits_for(is_agent=False)
        if options.get('limit') is not None:
            replay_limit = max(0, options['limit'])
        if replay_limit <= 0:
            self.stdout.write('Replay cap <= 0; replay is disabled. Nothing to probe.')
            return

        window_days = getattr(
            settings, 'CONNECT_DM_REPLAY_WINDOW_DAYS', DEFAULT_DM_REPLAY_WINDOW_DAYS,
        )
        self.stdout.write(
            f'DM replay backlog probe — cap={replay_limit}, window_days={window_days}'
        )
        over_cap = []
        for user in users_qs.iterator(chunk_size=500):
            eligible, conv_count, capped = _eligible_undelivered_count(
                user, replay_limit, floor,
            )
            if eligible < options['min_eligible']:
                continue
            row = {
                'user_hash': _user_hash(user.username),
                'user_role': 'agent' if getattr(user, 'is_agent', False) else 'human',
                'eligible': eligible,
                'cap': replay_limit,
                'over_cap': capped,
                'conversations': conv_count,
            }
            if capped:
                over_cap.append(row)
            self.stdout.write(
                f"  {row['user_role']:5s} {row['user_hash']} "
                f"eligible={row['eligible']} cap={row['cap']} "
                f"over_cap={row['over_cap']} conversations={row['conversations']}"
            )

        self.stdout.write('')
        if over_cap:
            self.stdout.write(
                self.style.WARNING(
                    f'{len(over_cap)} user(s) over the replay cap — '
                    'backlog may not fully drain on a single reconnect; '
                    'repeated reconnect/cursor-sync advances it.'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS('No user over the replay cap; backlogs drain within the cap.')
            )
