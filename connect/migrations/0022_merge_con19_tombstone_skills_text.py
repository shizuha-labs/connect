from django.db import migrations


class Migration(migrations.Migration):
    # CON-58: resolve the two 0021 leaf nodes left after CON-19 (0021_con19_tombstone)
    # and CON-58 (0021_socialprofile_skills_text) merged via separate 0020 lineages.
    # Both were graph leaves, so `migrate connect` raised "multiple leaf nodes in the
    # migration graph". This merge node converges them (no schema operations).
    dependencies = [
        ('connect', '0021_con19_tombstone'),
        ('connect', '0021_socialprofile_skills_text'),
    ]

    operations = []
