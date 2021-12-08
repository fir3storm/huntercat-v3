# Generated by Django 3.2.4 on 2021-12-07 03:13

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scanEngine', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='InstalledExternalTools',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('logo_url', models.CharField(blank=True, max_length=200, null=True)),
                ('tool_name', models.CharField(max_length=100)),
                ('tool_category', models.CharField(blank=True, max_length=50, null=True)),
                ('active_passive', models.CharField(blank=True, max_length=50, null=True)),
                ('description', models.CharField(max_length=500)),
                ('github_url', models.CharField(max_length=80)),
                ('license_url', models.CharField(blank=True, max_length=80, null=True)),
                ('version_lookup_command', models.CharField(default='tool_name -version', max_length=50)),
                ('version_match_regex', models.CharField(default='v(\\d+\\.)?(\\d+\\.)?(\\*|\\d+)', max_length=100)),
            ],
        ),
    ]