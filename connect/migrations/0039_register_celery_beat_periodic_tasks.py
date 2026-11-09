"""
CON-389: register celery beat_schedule entries with django_celery_beat.

Prod beat runs with ``--scheduler django_celery_beat.schedulers:DatabaseScheduler``
(deploy chart, shizuha-connect values.yaml).  That scheduler reads its schedule
from the ``django_celery_beat`` tables and IGNORES ``app.conf.beat_schedule`` —
so entries that only exist in code silently never fire.  Live evidence
(CON-389, sara 2026-09-15): zero ``ingest-public-swe-jobs`` batches in any
06:20 IST window since 08-30 even though the entry + task shipped on
``b86322e``/``827da60``.

This migration upserts every ``beat_schedule`` entry as a first-class
PeriodicTask row (update_or_create keyed by name — code stays authoritative
and drift self-heals on migrate).  Idempotent: safe on every re-run and on
environments where rows already exist.

Historical models (``apps.get_model``) are mandatory here: importing the
contemporary ``django_celery_beat.models`` inside a RunPython queries columns
(e.g. ``solar_id``) that do not exist yet at this graph position (CI run 730).
Only fields present since ``django_celery_beat`` 0001 are touched: name, task,
crontab, interval.  The per-schedule ``timezone`` column does NOT exist at
this graph position (CI run 732) — crontab evaluation falls back to
``CELERY_TIMEZONE`` (Asia/Kolkata, connectmanager/settings.py), so 06:20
fires in IST.  Interval period is set as its raw choice string.
"""


from django.db import migrations


def _crontab(apps, minute, hour="*", day_of_week="*", day_of_month="*", month_of_year="*"):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute=minute,
        hour=hour,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
    )
    return schedule


def _interval(apps, every, period):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=every,
        period=period,
    )
    return schedule


def register_periodic_tasks(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    PeriodicTask.objects.update_or_create(
        name="relay-security-event-outbox",
        defaults={
            "task": "connect.tasks.relay_security_event_outbox",
            "interval": _interval(apps, 15, "seconds"),
            "crontab": None,
        },
    )
    PeriodicTask.objects.update_or_create(
        name="calculate-trending-topics",
        defaults={
            "task": "connect.tasks.calculate_trending_topics",
            "crontab": _crontab(apps, minute="*/5"),
            "interval": None,
        },
    )
    PeriodicTask.objects.update_or_create(
        name="cleanup-stale-presence-sessions",
        defaults={
            "task": "connect.tasks.cleanup_stale_presence_sessions",
            "crontab": _crontab(apps, minute="*"),
            "interval": None,
        },
    )
    PeriodicTask.objects.update_or_create(
        name="expire-stories",
        defaults={
            "task": "connect.tasks.expire_stories",
            "crontab": _crontab(apps, minute=0),
            "interval": None,
        },
    )
    PeriodicTask.objects.update_or_create(
        name="cleanup-feed-items",
        defaults={
            "task": "connect.tasks.cleanup_old_feed_items",
            "crontab": _crontab(apps, hour=3, minute=0),
            "interval": None,
        },
    )
    PeriodicTask.objects.update_or_create(
        name="update-profile-stats",
        defaults={
            "task": "connect.tasks.batch_update_profile_stats",
            "crontab": _crontab(apps, minute=30),
            "interval": None,
        },
    )
    PeriodicTask.objects.update_or_create(
        name="post-daily-standup-prompt",
        defaults={
            "task": "connect.tasks.post_daily_standup_prompt",
            "crontab": _crontab(apps, hour=9, minute=17, day_of_week="mon-fri"),
            "interval": None,
        },
    )
    # CON-389: the daily public-SWE ingest — the entry that motivated this
    # migration (never fired in prod under DatabaseScheduler).
    PeriodicTask.objects.update_or_create(
        name="ingest-public-swe-jobs",
        defaults={
            "task": "connect.tasks.ingest_public_swe_jobs_daily",
            "crontab": _crontab(apps, hour=6, minute=20),
            "interval": None,
        },
    )


def unregister_periodic_tasks(apps, schema_editor):
    # Reverse is conservative: only remove rows this migration owns by name.
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(
        name__in=[
            "relay-security-event-outbox",
            "calculate-trending-topics",
            "cleanup-stale-presence-sessions",
            "expire-stories",
            "cleanup-feed-items",
            "update-profile-stats",
            "post-daily-standup-prompt",
            "ingest-public-swe-jobs",
        ]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("connect", "0042_disable_retired_jobs_schedule"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(register_periodic_tasks, unregister_periodic_tasks),
    ]
