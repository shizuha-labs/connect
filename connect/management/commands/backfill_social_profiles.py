"""
CON-52: backfill SocialProfile rows for messageable users (especially agents)
that lack one, so people-search finds them and conversation lists render names.

Usage:
    python manage.py backfill_social_profiles            # apply
    python manage.py backfill_social_profiles --dry-run  # report only
"""
from django.core.management.base import BaseCommand

from connect.models import ConversationParticipant, SocialProfile
from connect.services.display_names import (
    is_placeholder_name,
    preferred_participant_label,
    resolve_identity_users,
)
from connect.services.profile_sync import ensure_social_profile


class Command(BaseCommand):
    help = "Backfill SocialProfile rows for conversation participants (agents) missing one (CON-52)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Report what would be created without writing.",
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        have = set(SocialProfile.objects.values_list('user_id', flat=True))
        participant_ids = set(
            ConversationParticipant.objects.values_list('user_id', flat=True).distinct()
        )
        missing = sorted(uid for uid in participant_ids if uid not in have)

        self.stdout.write(
            f"participants={len(participant_ids)} with_profile={len(have)} "
            f"missing={len(missing)}"
        )

        ensured = 0
        skipped = 0
        for uid in missing:
            if dry_run:
                self.stdout.write(f"  would ensure profile for user {uid}")
                continue
            profile = ensure_social_profile(uid)
            if profile is not None:
                ensured += 1
            else:
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f"  could not resolve/create profile for user {uid} (no username from shizuha-id)"
                ))

        verb = 'would ensure' if dry_run else 'ensured'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {len(missing) if dry_run else ensured} profiles"
            + ('' if dry_run else f"; {skipped} skipped (unresolvable)")
        ))

        # Heal stored ConversationParticipant.user_name placeholders so the
        # sidebar (which prefers user_name over participant_names) shows the
        # real person. Profiles just created above are included.
        placeholders = list(
            ConversationParticipant.objects.exclude(has_left=True)
        )
        placeholders = [
            p for p in placeholders
            if is_placeholder_name(p.user_name, p.user_id)
        ]
        if not placeholders:
            self.stdout.write('no placeholder participant names to heal')
            return
        uids = sorted({p.user_id for p in placeholders})
        profiles = {
            p.user_id: p
            for p in SocialProfile.objects.filter(user_id__in=uids).only(
                'user_id', 'username', 'display_name', 'user_email',
            )
        }
        identities = {} if dry_run else resolve_identity_users(uids)
        healed = 0
        for participant in placeholders:
            label = preferred_participant_label(
                participant, profiles=profiles, identities=identities,
            )
            if not label or is_placeholder_name(label, participant.user_id):
                continue
            if dry_run:
                self.stdout.write(
                    f"  would rename user {participant.user_id} "
                    f"{participant.user_name!r} -> {label!r}"
                )
                healed += 1
                continue
            participant.user_name = label
            participant.save(update_fields=['user_name'])
            healed += 1
        self.stdout.write(self.style.SUCCESS(
            f"{'would heal' if dry_run else 'healed'} {healed} placeholder participant names"
        ))
