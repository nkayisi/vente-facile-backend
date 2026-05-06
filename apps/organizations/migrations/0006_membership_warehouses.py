import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_membership_warehouses(apps, schema_editor):
    OrganizationMembership = apps.get_model("organizations", "OrganizationMembership")
    Warehouse = apps.get_model("inventory", "Warehouse")
    MembershipWarehouse = apps.get_model("organizations", "MembershipWarehouse")

    org_ids = OrganizationMembership.objects.values_list(
        "organization_id", flat=True
    ).distinct()
    for org_id in org_ids:
        default_wh = Warehouse.objects.filter(
            organization_id=org_id, is_default=True, is_deleted=False
        ).order_by("created_at").first()
        if default_wh is None:
            default_wh = Warehouse.objects.filter(
                organization_id=org_id, is_deleted=False, is_active=True
            ).order_by("created_at").first()

        memberships = OrganizationMembership.objects.filter(
            organization_id=org_id, is_active=True
        ).exclude(role="owner")

        if default_wh:
            for m in memberships.iterator():
                MembershipWarehouse.objects.get_or_create(
                    membership_id=m.id,
                    warehouse_id=default_wh.id,
                )


def reverse_clear_links(apps, schema_editor):
    MembershipWarehouse = apps.get_model("organizations", "MembershipWarehouse")
    MembershipWarehouse.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0005_organization_subscription_floor_tier"),
        ("inventory", "0009_stockbatch_location"),
    ]

    operations = [
        migrations.CreateModel(
            name="MembershipWarehouse",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "membership",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="warehouse_assignments",
                        to="organizations.organizationmembership",
                    ),
                ),
                (
                    "warehouse",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="membership_assignments",
                        to="inventory.warehouse",
                    ),
                ),
            ],
            options={
                "db_table": "membership_warehouses",
            },
        ),
        migrations.AddConstraint(
            model_name="membershipwarehouse",
            constraint=models.UniqueConstraint(
                fields=("membership", "warehouse"),
                name="uniq_membership_warehouse",
            ),
        ),
        migrations.AddField(
            model_name="organizationmembership",
            name="assigned_warehouses",
            field=models.ManyToManyField(
                blank=True,
                related_name="organization_memberships_m2m",
                through="organizations.MembershipWarehouse",
                to="inventory.warehouse",
            ),
        ),
        migrations.RunPython(backfill_membership_warehouses, reverse_clear_links),
    ]
