from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("products", "0006_product_has_expiry_date"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="category",
            name="unique_category_slug_per_org",
        ),
        migrations.RemoveConstraint(
            model_name="category",
            name="unique_category_name_per_org",
        ),
        migrations.AddConstraint(
            model_name="category",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_deleted=False),
                fields=("organization", "parent", "slug"),
                name="unique_category_slug_per_parent_org",
            ),
        ),
        migrations.AddConstraint(
            model_name="category",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_deleted=False),
                fields=("organization", "parent", "name"),
                name="unique_category_name_per_parent_org",
            ),
        ),
    ]
