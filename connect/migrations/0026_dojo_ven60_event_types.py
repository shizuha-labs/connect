# Generated for DOJO-9 / VEN-60 paid funnel event instrumentation.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0025_dojo_validation'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dojovalidationevent',
            name='event_type',
            field=models.CharField(
                choices=[
                    ('visit', 'Visit'),
                    ('signup', 'Signup'),
                    ('qualified', 'Qualified signup'),
                    ('reply', 'Qualitative reply'),
                    ('cta_click', 'CTA click'),
                    ('dojo_landing_view', 'Dojo landing view'),
                    ('waitlist_signup_submitted', 'Waitlist signup submitted'),
                    ('qualified_signup', 'Qualified signup (VEN-60)'),
                    ('interview_setup_started', 'Interview setup started'),
                    ('interview_session_started', 'Interview session started'),
                    ('interview_turn_submitted', 'Interview turn submitted'),
                    ('interview_session_completed', 'Interview session completed'),
                    ('report_viewed', 'Report viewed'),
                    ('free_limit_hit', 'Free limit hit'),
                    ('upgrade_clicked', 'Upgrade clicked'),
                    ('plan_selected', 'Plan selected'),
                    ('payment_link_requested', 'Payment link requested'),
                    ('payment_initiated', 'Payment initiated'),
                    ('payment_success', 'Payment success'),
                    ('payment_failed', 'Payment failed'),
                    ('payment_expired', 'Payment expired'),
                    ('refund', 'Refund'),
                    ('paid_session_started', 'Paid session started'),
                    ('second_session_completed', 'Second session completed'),
                ],
                db_index=True,
                max_length=40,
            ),
        ),
    ]
