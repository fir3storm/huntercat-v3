# Generated by Django 3.0.6 on 2020-05-11 14:18

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Domain',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('domain_name', models.CharField(max_length=300)),
                ('domain_description', models.TextField()),
                ('insert_date', models.DateTimeField()),
            ],
        ),
    ]
