# -*- coding: utf-8 -*-
# Generated by Django 1.10 on 2016-09-09 22:20
from __future__ import unicode_literals

from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0020_private_workshops'),
    ]

    operations = [
        migrations.AddField(
            model_name='rate',
            name='discount',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='rate',
            name='discount_code',
            field=models.SlugField(blank=True, help_text='This will be the code given to a customer receiving a discount in the form of https://workshops.qiime.org/workshop_slug/?rate=discount_code'),
        ),
    ]
