"""Retire private employment models without dropping archived source tables."""
from django.db import migrations, models


def require_no_unmapped_experience_links(apps, schema_editor):
    Experience = apps.get_model('connect', 'Experience')
    if Experience.objects.filter(company_page__isnull=False).exists():
        raise RuntimeError('Map existing Experience company links to Guild before retiring jobs state')


def require_no_new_employer_refs_on_rollback(apps, schema_editor):
    Experience = apps.get_model('connect', 'Experience')
    if Experience.objects.filter(employer_id__isnull=False).exists():
        raise RuntimeError('Preserve new external employer references before reversing core migration')


class Migration(migrations.Migration):
    dependencies = [('connect', '0040_retire_legacy_dojo_state')]
    operations = [
        migrations.RunPython(require_no_unmapped_experience_links, migrations.RunPython.noop),
        migrations.AddField(model_name='experience', name='employer_id', field=models.UUIDField(
            null=True, blank=True, help_text='External Guild Employer UUID; unresolved references retain company_name')),
        migrations.SeparateDatabaseAndState(database_operations=[], state_operations=[
            migrations.RemoveField(model_name='experience', name='company_page'),
            migrations.DeleteModel(name='JobApplication'),
            migrations.DeleteModel(name='JobSave'),
            migrations.DeleteModel(name='CompanyAdmin'),
            migrations.DeleteModel(name='CompanyFollow'),
            migrations.DeleteModel(name='JobListing'),
            migrations.DeleteModel(name='CompanyPage'),
        ]),
        migrations.RunPython(migrations.RunPython.noop, require_no_new_employer_refs_on_rollback),
    ]
