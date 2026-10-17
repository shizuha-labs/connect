from django.db import migrations, models


class Migration(migrations.Migration):
    """CON-24: add onboarding_state to SocialProfile for wizard step tracking.

    Renumbered from the original 0003 (which branched off 0002_meetings and
    created a dual-leaf with 0003_conversationparticipant_cleared_at_and_more,
    breaking `migrate`). Chains off the current leaf 0016 so the graph stays
    single-leaf.
    """

    dependencies = [
        ('connect', '0016_direct_message_idempotency'),
    ]

    operations = [
        migrations.AddField(
            model_name='socialprofile',
            name='onboarding_state',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Onboarding wizard progress: {completed_steps: [...], completed_at: null|ISO8601}',
            ),
        ),
    ]
