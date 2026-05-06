# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0004_simplify_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="subscription_floor_tier",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
