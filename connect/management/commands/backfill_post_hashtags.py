"""Backfill PostHashtag rows for posts created before hashtag signals existed."""

from django.core.management.base import BaseCommand

from connect.models import Post
from connect.signals import extract_hashtags, sync_post_hashtags


class Command(BaseCommand):
    help = "Backfill hashtag links for existing posts using connect.signals.sync_post_hashtags."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count posts containing hashtags without writing PostHashtag rows.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Iterator chunk size for scanning posts (default: 500).",
        )
        parser.add_argument(
            "--progress-every",
            type=int,
            default=1000,
            help="Write a progress line every N scanned posts (default: 1000; 0 disables).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = max(1, options["batch_size"])
        progress_every = max(0, options["progress_every"])

        scanned = tagged = synced = 0
        queryset = Post.objects.order_by("id")
        for post in queryset.iterator(chunk_size=batch_size):
            scanned += 1
            has_tags = bool(extract_hashtags(post.content))
            if has_tags:
                tagged += 1
            if not dry_run:
                sync_post_hashtags(post)
                synced += 1
            if progress_every and scanned % progress_every == 0:
                action = "would process" if dry_run else "processed"
                self.stdout.write(f"Scanned {scanned} posts; {tagged} contain hashtags; {action} {synced if not dry_run else tagged}.")

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry run complete: scanned {scanned} posts; {tagged} contain hashtags and would be processed."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Backfill complete: scanned {scanned} posts; processed {synced}; {tagged} contain hashtags."
                )
            )
