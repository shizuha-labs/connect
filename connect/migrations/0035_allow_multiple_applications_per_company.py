from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0034_one_application_per_company'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='jobapplication',
            name='connect_jobapplication_applicant_company_uniq',
        ),
    ]
