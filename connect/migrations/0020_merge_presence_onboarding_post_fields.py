# Empty merge migration: CON-14 presence-session org migration and the
# CON-24/CON-33/CON-41 social-profile/post chain both branched from 0016.
# Keeping a single leaf lets Django's test runner and deploy migrate without
# needing an interactive makemigrations --merge step.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0017_presencesession_organization_id'),
        ('connect', '0019_post_author_username'),
    ]

    operations = []
