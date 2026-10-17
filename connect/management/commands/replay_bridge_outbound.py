"""Bounded CON-165 outbound bridge replay command."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import dateparse

from connect.services.replay import ReplayScope, replay_outbound_bridge_events


class Command(BaseCommand):
    help = (
        'Dry-run or execute a bounded replay of queued/dead-lettered Connect '
        'BridgeOutboundEvent rows. Safe by default: pass --execute to resend.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--provider')
        parser.add_argument('--channel-key')
        parser.add_argument('--conversation-id')
        parser.add_argument('--since', help='ISO datetime lower bound for created_at')
        parser.add_argument('--until', help='ISO datetime upper bound for created_at')
        parser.add_argument('--status', action='append', dest='statuses', help='Replayable status to include; repeatable')
        parser.add_argument('--batch-size', type=int, default=100)
        parser.add_argument('--actor', required=True, help='Operator/agent initiating the replay')
        parser.add_argument('--task-key', required=True, help='Pulse task/incident key authorizing the replay')
        parser.add_argument('--reason', required=True, help='Short incident recovery reason')
        parser.add_argument('--execute', action='store_true', help='Actually resend; omitted means dry-run only')

    def _parse_dt(self, value, flag):
        if not value:
            return None
        parsed = dateparse.parse_datetime(value)
        if not parsed:
            raise CommandError(f'{flag} must be an ISO datetime')
        return parsed

    def handle(self, *args, **options):
        scope = ReplayScope(
            provider=options.get('provider'),
            channel_key=options.get('channel_key'),
            conversation_id=options.get('conversation_id'),
            since=self._parse_dt(options.get('since'), '--since'),
            until=self._parse_dt(options.get('until'), '--until'),
            statuses=tuple(options.get('statuses') or ()),
        )
        if not scope.statuses:
            scope = ReplayScope(
                provider=scope.provider,
                channel_key=scope.channel_key,
                conversation_id=scope.conversation_id,
                since=scope.since,
                until=scope.until,
            )
        result = replay_outbound_bridge_events(
            scope=scope,
            actor=options['actor'],
            task_key=options['task_key'],
            reason=options['reason'],
            batch_size=options['batch_size'],
            dry_run=not options['execute'],
        )
        self.stdout.write(
            f'dry_run={result.dry_run} candidates={result.candidates} '
            f'replayed={result.replayed} skipped={result.skipped} failed={result.failed} '
            f'bounded_batch_size={result.bounded_batch_size}'
        )
        for item in result.items:
            self.stdout.write(
                f'{item.action} event_id={item.event_id} outbound_event_id={item.outbound_event_id} '
                f'{item.status_before}->{item.status_after} provider_delivery_id={item.provider_delivery_id} '
                f'error_code={item.error_code}'
            )
