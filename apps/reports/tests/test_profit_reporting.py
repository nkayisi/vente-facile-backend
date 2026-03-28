"""Tests pour CA HT, CMV historique et remise globale (rapports bénéfices)."""
import uuid
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.organizations.models import Organization
from apps.products.models import Product
from apps.sales.models import Sale, SaleItem
from apps.sales.profit_allocation import allocated_line_ht_revenues_for_sale, effective_unit_cost


class ProfitAllocationTests(TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.org = Organization.objects.create(
            name="Test Org",
            slug=f"test-org-{suffix}",
            business_type="boutique",
        )
        self.p_a = Product.objects.create(
            organization=self.org,
            name="Product A",
            slug=f"pa-{suffix}",
            sku=f"SKU-A-{suffix}",
            cost_price=Decimal("10.00"),
            selling_price=Decimal("100.00"),
            track_inventory=False,
        )
        self.p_b = Product.objects.create(
            organization=self.org,
            name="Product B",
            slug=f"pb-{suffix}",
            sku=f"SKU-B-{suffix}",
            cost_price=Decimal("20.00"),
            selling_price=Decimal("200.00"),
            track_inventory=False,
        )

    def _make_sale(self, reference_suffix: str, **sale_kwargs):
        ref = f"S-{reference_suffix}-{uuid.uuid4().hex[:6]}"
        defaults = {
            "organization": self.org,
            "reference": ref,
            "status": "completed",
            "sale_date": timezone.now(),
            "discount_percentage": Decimal("0"),
        }
        defaults.update(sale_kwargs)
        return Sale.objects.create(**defaults)

    def test_effective_unit_cost_prefers_line_snapshot(self):
        sale = self._make_sale("snap")
        item = SaleItem(
            sale=sale,
            organization=self.org,
            product=self.p_a,
            quantity=Decimal("2"),
            unit_price=Decimal("50.00"),
            cost_price=Decimal("7.00"),
            discount_percentage=Decimal("0"),
            tax_rate=Decimal("0"),
        )
        item.save()
        self.p_a.cost_price = Decimal("999.00")
        self.p_a.save()
        item.refresh_from_db()
        self.assertEqual(effective_unit_cost(item), Decimal("7.00"))

    def test_effective_unit_cost_falls_back_to_product(self):
        sale = self._make_sale("fb")
        item = SaleItem(
            sale=sale,
            organization=self.org,
            product=self.p_a,
            quantity=Decimal("1"),
            unit_price=Decimal("50.00"),
            cost_price=Decimal("0.00"),
            discount_percentage=Decimal("0"),
            tax_rate=Decimal("0"),
        )
        item.save()
        self.assertEqual(effective_unit_cost(item), Decimal("10.00"))

    def test_global_discount_allocation_sums_to_target_ht(self):
        sale = self._make_sale(
            "glob",
            subtotal=Decimal("300.00"),
            discount_amount=Decimal("30.00"),
            tax_amount=Decimal("0.00"),
            total=Decimal("270.00"),
            discount_percentage=Decimal("10.00"),
        )
        SaleItem.objects.create(
            sale=sale,
            organization=self.org,
            product=self.p_a,
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            cost_price=Decimal("10.00"),
            discount_percentage=Decimal("0"),
            tax_rate=Decimal("0"),
        )
        SaleItem.objects.create(
            sale=sale,
            organization=self.org,
            product=self.p_b,
            quantity=Decimal("1"),
            unit_price=Decimal("200.00"),
            cost_price=Decimal("20.00"),
            discount_percentage=Decimal("0"),
            tax_rate=Decimal("0"),
        )
        sale.refresh_from_db()
        alloc = allocated_line_ht_revenues_for_sale(sale)
        self.assertEqual(len(alloc), 2)
        self.assertEqual(sum(r for _, r in alloc), Decimal("270.00"))
        by_sku = {a.product.sku: r for a, r in alloc}
        self.assertEqual(by_sku[self.p_a.sku], Decimal("90.00"))
        self.assertEqual(by_sku[self.p_b.sku], Decimal("180.00"))

    def test_product_revenues_sum_matches_global_ht(self):
        """Somme des CA HT alloués par produit = somme (sale.subtotal - sale.discount_amount)."""
        from collections import defaultdict

        sale = self._make_sale(
            "sum",
            subtotal=Decimal("300.00"),
            discount_amount=Decimal("30.00"),
            tax_amount=Decimal("0.00"),
            total=Decimal("270.00"),
            discount_percentage=Decimal("10.00"),
        )
        SaleItem.objects.create(
            sale=sale,
            organization=self.org,
            product=self.p_a,
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            cost_price=Decimal("10.00"),
            discount_percentage=Decimal("0"),
            tax_rate=Decimal("0"),
        )
        SaleItem.objects.create(
            sale=sale,
            organization=self.org,
            product=self.p_b,
            quantity=Decimal("1"),
            unit_price=Decimal("200.00"),
            cost_price=Decimal("20.00"),
            discount_percentage=Decimal("0"),
            tax_rate=Decimal("0"),
        )

        sale.refresh_from_db()
        expected_global = sale.subtotal - sale.discount_amount

        items = SaleItem.objects.filter(sale=sale).select_related("product", "sale")
        sale_ids = list(items.values_list("sale_id", flat=True).distinct())
        sales_by_id = {s.id: s for s in Sale.objects.filter(id__in=sale_ids).prefetch_related("items__product")}
        by_sale_lines = defaultdict(list)
        for row in items.order_by("sale_id", "id"):
            by_sale_lines[row.sale_id].append(row)

        product_revenue = defaultdict(lambda: Decimal("0"))
        for sid, lines in by_sale_lines.items():
            s = sales_by_id[sid]
            alloc = {i.id: r for i, r in allocated_line_ht_revenues_for_sale(s)}
            for item in lines:
                product_revenue[str(item.product_id)] += alloc.get(item.id, Decimal("0"))

        self.assertEqual(sum(product_revenue.values()), expected_global)

    def test_margin_uses_ht_not_ttc_when_tax(self):
        sale = self._make_sale(
            "tax",
            subtotal=Decimal("100.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("16.00"),
            total=Decimal("116.00"),
            discount_percentage=Decimal("0"),
        )
        SaleItem.objects.create(
            sale=sale,
            organization=self.org,
            product=self.p_a,
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            cost_price=Decimal("40.00"),
            discount_percentage=Decimal("0"),
            tax_rate=Decimal("16.00"),
        )
        sale.refresh_from_db()
        alloc = allocated_line_ht_revenues_for_sale(sale)
        self.assertEqual(alloc[0][1], Decimal("100.00"))
        gross = alloc[0][1] - effective_unit_cost(alloc[0][0]) * alloc[0][0].quantity
        self.assertEqual(gross, Decimal("60.00"))
