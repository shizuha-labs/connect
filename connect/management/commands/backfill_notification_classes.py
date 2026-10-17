"""CON-429 S2 (design §7.1–§7.3): notification-class backfill.

Scripted, idempotent, resumable classification of pre-taxonomy message
rows, keyed on the §2.1 producer mapping (sender identity).

Arbitrations adopted (design §7.2/§7.3):
- Ambiguity default: rows that cannot be confidently mapped default to
  `informational` + a logged exception list for post-run review (aoi's
  arbitration) — fail-open, digest-eligible, never badge-inflating, and
  every ambiguity is auditable and re-classifiable.
- Grandfathering: pre-taxonomy unread rows are classified for digest
  correctness but do NOT enter the "Needs action" view — their
  `notification_state` mints as `read_eligible` (excluded from
  needs-action counting). New mints from deploy time onward participate
  fully.

Idempotency/resumability: only rows with `notification_class IS NULL` are
touched — stamped rows are never re-classified, so the command can be
re-run or resumed (e.g. after a partial run) without double-mutation.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from connect.models import DirectMessage, MessageRead

# CON-429 §2.1: confident sender-identity mappings (sender_name → class).
# Pairs that cannot be confidently discriminated (e.g. 'Pulse Scheduler'
# mints both actionable reminders and wake-relevant informational cycle
# notices) are deliberately NOT listed — they fall through to the §7.2
# ambiguity default and are logged for review.
SENDER_CLASS_MAP = {
    'Shizuha Alert Router': 'alert',
    'Connect Digest': 'informational',
}


class Command(BaseCommand):
    help = (
        'CON-429 S2: backfill notification_class/notification_state on '
        'pre-taxonomy message rows (idempotent, resumable; ambiguous rows '
        'default informational + logged; pre-taxonomy unread grandfathered).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch', type=int, default=500,
            help='Rows per transaction batch (default 500).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Classify and report without writing.',
        )

    def handle(self, *args, **options):
        batch_size = options['batch']
        dry_run = options['dry_run']
        ambiguous = []
        classified = 0

        pending = (
            DirectMessage.objects.filter(notification_class__isnull=True)
            .select_related('conversation')
            .order_by('created_at')
        )
        total = pending.count()
        self.stdout.write(f'CON-429 backfill: {total} pending row(s)')

        batch = []
        for msg in pending.iterator(chunk_size=batch_size):
            cls, was_ambiguous = self._classify(msg)
            if was_ambiguous:
                ambiguous.append(
                    f'{msg.pk} sender={msg.sender_name!r} service='
                    f'{(msg.media_metadata or {}).get("sender_service")!r}'
                )
            # §7.3 grandfathering: pre-taxonomy rows with no read receipt
            # mint as read_eligible (excluded from needs-action counting);
            # rows already read mint as read.
            state = 'read' if self._is_read(msg) else 'read_eligible'
            msg.notification_class = cls
            msg.notification_state = state
            batch.append(msg)
            classified += 1
            if len(batch) >= batch_size:
                self._flush(batch, dry_run)
                batch = []

        self._flush(batch, dry_run)

        self.stdout.write(
            f'CON-429 backfill done: {classified} classified, '
            f'{len(ambiguous)} ambiguous (defaulted informational + logged)'
        )
        for line in ambiguous[:50]:
            self.stdout.write(f'  AMBIGUOUS {line}')
        if len(ambiguous) > 50:
            self.stdout.write(f'  ... and {len(ambiguous) - 50} more')

    @staticmethod
    def _classify(msg):
        """Return (class, was_ambiguous) per the §2.1 mapping.

        Order is load-bearing (rui 09-23 review 5315): the SENDER_CLASS_MAP
        lookup MUST precede the generic system-channel early return. The
        pre-taxonomy alert-router rows carry exactly the system shape
        (message_type='system', sender_id=0) with class NULL — mapping them
        first is the only path that reaches the 'Shizuha Alert Router': 'alert'
        branch; defaulting them informational beforehand silently lost the
        class AND under-reported the fold (they folded as "confident")."""
        sender_cls = SENDER_CLASS_MAP.get(msg.sender_name)
        if sender_cls is not None:
            return sender_cls, False
        if msg.message_type == 'system' and msg.sender_id == 0:
            # `_create_system_message` leg — system-channel default.
            return 'informational', False
        # §7.2 ambiguity default: informational + logged.
        return 'informational', True

    @staticmethod
    def _is_read(msg):
        """A row is treated as read when a read receipt exists for it."""
        return MessageRead.objects.filter(message_id=msg.pk).exists()

    def _flush(self, batch, dry_run):
        if not batch or dry_run:
            return
        with transaction.atomic():
            DirectMessage.objects.bulk_update(
                batch, ['notification_class', 'notification_state'], batch_size=len(batch),
            )
