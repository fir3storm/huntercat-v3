# Generated by Django 3.2.4 on 2024-05-02 14:01

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('startScan', '0056_auto_20231201_2354'),
    ]

    operations = [
        migrations.AlterField(
            model_name='endpoint',
            name='techs',
            field=models.ManyToManyField(blank=True, related_name='techs', to='startScan.Technology'),
        ),
    ]
