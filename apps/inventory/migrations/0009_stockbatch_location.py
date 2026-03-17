from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0008_unique_reference_per_organization'),
    ]

    operations = [
        migrations.AddField(
            model_name='stockbatch',
            name='location',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='batches',
                to='inventory.stocklocation'
            ),
        ),
    ]
