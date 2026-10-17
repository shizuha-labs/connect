from django.db import migrations, models
import django.db.models.deletion


def backfill_application_company(apps, schema_editor):
    JobApplication = apps.get_model('connect', 'JobApplication')
    for row in JobApplication.objects.select_related('job').iterator():
        company_id = getattr(row.job, 'company_id', None)
        if company_id and row.company_id != company_id:
            row.company_id = company_id
            row.save(update_fields=['company_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0033_agent_processing_ack'),
    ]

    operations = [
        migrations.AddField(
            model_name='jobapplication',
            name='company',
            field=models.ForeignKey(
                blank=True,
                help_text='Denormalized from job.company; one application per applicant per company.',
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='job_applications',
                to='connect.companypage',
            ),
        ),
        migrations.RunPython(backfill_application_company, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='jobapplication',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='jobapplication',
            constraint=models.UniqueConstraint(
                fields=('job', 'applicant_id'),
                name='connect_jobapplication_job_applicant_uniq',
            ),
        ),
        migrations.AddConstraint(
            model_name='jobapplication',
            constraint=models.UniqueConstraint(
                fields=('applicant_id', 'company'),
                name='connect_jobapplication_applicant_company_uniq',
            ),
        ),
    ]
