from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0035_allow_multiple_applications_per_company'),
    ]

    operations = [
        migrations.AlterField(
            model_name='jobapplication',
            name='company',
            field=models.ForeignKey(
                blank=True,
                help_text='Denormalized from job.company for listing filters.',
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='job_applications',
                to='connect.companypage',
            ),
        ),
    ]
