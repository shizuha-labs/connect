"""Backfill PostgreSQL full-text search vectors for Connect models."""

from django.contrib.postgres.search import SearchVector
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from connect.models import Post, SocialProfile


class Command(BaseCommand):
    help = "Backfill Connect search vectors. Safe to rerun."

    def add_arguments(self, parser):
        parser.add_argument(
            "--models",
            default="posts,profiles",
            help="Comma-separated models to backfill: posts, profiles (default: posts,profiles)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows per update batch (default: 500)",
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("backfill_search_vectors requires PostgreSQL")

        batch_size = options["batch_size"]
        if batch_size <= 0:
            raise CommandError("--batch-size must be positive")

        requested = {name.strip().lower() for name in options["models"].split(",") if name.strip()}
        valid = {"posts", "profiles"}
        unknown = requested - valid
        if unknown:
            raise CommandError(f"Unknown model(s): {', '.join(sorted(unknown))}")

        if "posts" in requested:
            count = self._backfill_posts(batch_size)
            self.stdout.write(self.style.SUCCESS(f"Backfilled {count} post search vector(s)"))

        if "profiles" in requested:
            count = self._backfill_profiles(batch_size)
            self.stdout.write(self.style.SUCCESS(f"Backfilled {count} profile search vector(s)"))

    def _backfill_posts(self, batch_size):
        count = 0
        ids = list(Post.objects.order_by("pk").values_list("pk", flat=True))
        for start in range(0, len(ids), batch_size):
            batch = ids[start : start + batch_size]
            count += Post.objects.filter(pk__in=batch).update(
                search_vector=(
                    SearchVector("content", weight="A")
                    + SearchVector("author_username", weight="B")
                    + SearchVector("author_display_name", weight="B")
                )
            )
        return count

    def _backfill_profiles(self, batch_size):
        count = 0
        ids = list(SocialProfile.objects.order_by("pk").values_list("pk", flat=True))
        for start in range(0, len(ids), batch_size):
            batch = ids[start : start + batch_size]
            count += SocialProfile.objects.filter(pk__in=batch).update(
                search_vector=SearchVector("display_name", "username", "bio", "headline")
            )
        return count
