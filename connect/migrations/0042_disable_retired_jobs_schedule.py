"""Retain legacy schedule configuration without dispatching a retired task."""
from django.db import migrations
from django.utils import timezone


def disable_retired_dispatch(apps, schema_editor):
    PeriodicTask = apps.get_model('django_celery_beat', 'PeriodicTask')
    changed = PeriodicTask.objects.filter(
        task='connect.tasks.ingest_public_swe_jobs_daily', enabled=True,
    ).update(enabled=False)
    if changed:
        PeriodicTasks = apps.get_model('django_celery_beat', 'PeriodicTasks')
        PeriodicTasks.objects.update_or_create(ident=1, defaults={'last_update': timezone.now()})


class Migration(migrations.Migration):
    dependencies = [
        ('connect', '0041_retire_jobs_state'),
        ('django_celery_beat', '0019_alter_periodictasks_options'),
    ]
    # Reversal retains the disabled configuration; restoring a writer requires
    # the archived prior flag and a deliberate data reconciliation decision.
    operations = [migrations.RunPython(disable_retired_dispatch, migrations.RunPython.noop)]
