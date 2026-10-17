"""Backfill profile skills text and weighted search vectors."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from connect.models import SocialProfile
from connect.signals import refresh_profile_search_fields


class Command(BaseCommand):
    help = "Backfill profile skills_text and weighted search vectors. Safe to rerun."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows per progress batch (default: 500)",
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("backfill_profile_search_vectors requires PostgreSQL")

        batch_size = options["batch_size"]
        if batch_size <= 0:
            raise CommandError("--batch-size must be positive")

        count = 0
        ids = list(SocialProfile.objects.order_by("pk").values_list("pk", flat=True))
        for profile_id in ids:
            refresh_profile_search_fields(profile_id, refresh_skills=True)
            count += 1
            if count % batch_size == 0:
                self.stdout.write(f"Backfilled {count} profile search vector(s)...")

        self.stdout.write(self.style.SUCCESS(f"Backfilled {count} profile search vector(s)"))
