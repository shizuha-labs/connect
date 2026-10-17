"""CON-231: operator probe for DM replay backlogs that exceed the reconnect cap.

`connect_dm_replay_capped_total` tells you a cap was hit; it cannot tell you
whether the affected user is DRAINING or stuck. This command answers that by
counting each user's eligible undelivered candidates against their own cap,
using the same predicate the consumer applies (connect.services.dm_replay), so
the two cannot drift.

Run it twice around a reconnect/cursor-sync to prove progress:

    python manage.py dm_replay_backlog                  # everyone over cap
    python manage.py dm_replay_backlog --user 42        # one user
    python manage.py dm_replay_backlog --all            # include under-cap users
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Report users whose eligible undelivered DM replay backlog exceeds the cap.'

    def add_arguments(self, parser):
        parser.add_argument('--user', type=int, default=None,
                            help='Only inspect this user id.')
        parser.add_argument('--all', action='store_true',
                            help='Include users whose backlog is within the cap.')
        parser.add_argument('--json', action='store_true',
                            help='Emit JSON lines instead of a table.')

    def handle(self, *args, **options):
        from connect.consumers import _replay_limits_for
        from connect.models import ConversationParticipant
        from connect.services.dm_replay import eligible_candidate_queryset

        User = get_user_model()
        users = User.objects.all()
        if options['user'] is not None:
            users = users.filter(id=options['user'])

        now = timezone.now()
        rows = []
        for user in users.iterator():
            participants = list(
                ConversationParticipant.objects.filter(user_id=user.id, has_left=False)
                .only('conversation_id', 'last_read_at', 'cleared_at')
            )
            if not participants:
                continue

            # Agent-ness is NOT a User field. The consumer derives it from the
            # `is_agent` JWT claim (consumers.py:1072), which no offline probe
            # can see, so the durable equivalent is the participant row's
            # `participant_type`. Getting this wrong would silently apply the
            # human cap to agents, whose cap is deliberately much tighter.
            is_agent = any(p.participant_type == 'agent' for p in participants)
            limit, floor = _replay_limits_for(is_agent)
            if limit <= 0:
                continue

            candidates = eligible_candidate_queryset(user.id, participants, floor)
            # Count one past the cap only — a precise count over a deep backlog
            # is expensive and the decision only needs "over or not", plus depth.
            total = candidates.count()
            if total <= limit and not options['all']:
                continue

            oldest = candidates.last()
            newest = candidates.first()
            rows.append({
                'user_id': user.id,
                'username': getattr(user, 'username', None),
                'user_role': 'agent' if is_agent else 'human',
                'candidate_limit': limit,
                'eligible_undelivered': total,
                'over_cap_by': max(0, total - limit),
                'conversations': len(participants),
                'oldest_candidate_age_s': (now - oldest.created_at).total_seconds() if oldest else None,
                'newest_candidate_age_s': (now - newest.created_at).total_seconds() if newest else None,
            })

        rows.sort(key=lambda r: r['eligible_undelivered'], reverse=True)

        if options['json']:
            for row in rows:
                self.stdout.write(json.dumps(row))
            return

        if not rows:
            self.stdout.write('No user has an eligible undelivered backlog over the replay cap.')
            return

        self.stdout.write(
            f'{"user":>8}  {"role":<6} {"backlog":>8} {"cap":>5} {"over":>6} '
            f'{"convs":>6}  oldest_age_s'
        )
        for r in rows:
            self.stdout.write(
                f'{r["user_id"]:>8}  {r["user_role"]:<6} {r["eligible_undelivered"]:>8} '
                f'{r["candidate_limit"]:>5} {r["over_cap_by"]:>6} {r["conversations"]:>6}  '
                f'{r["oldest_candidate_age_s"]}'
            )
        self.stdout.write('')
        self.stdout.write(
            'Re-run after a reconnect/cursor-sync: `backlog` must fall. If it does not, '
            'the drain is stuck rather than slow.'
        )
