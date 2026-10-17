"""Retire the legacy Connect Dojo API while retaining archived tables.

CON-469: Dojo owns its current funnel. Keep database rows/tables intact for
rollback; a later separately reviewed core baseline excludes this schema from
the public snapshot. Reverse restores model state without touching data.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("connect", "0039_notification_taxonomy")]
    operations = [migrations.SeparateDatabaseAndState(
        database_operations=[],
        state_operations=[
            migrations.DeleteModel(name="DojoValidationEvent"),
            migrations.DeleteModel(name="DojoWaitlistSignup"),
        ],
    )]
