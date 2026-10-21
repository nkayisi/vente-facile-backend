"""
Une réquisition supprimée en douceur sort des TROIS surfaces, pas d'une seule.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ELLE N'AVAIT PAS DE MANAGER, ET C'ÉTAIT LA SEULE DES QUATRE.                │
│                                                                              │
│ `Requisition` hérite de `TenantSoftDeleteModel` et gardait le                │
│ `models.Manager` automatique - `PurchaseOrder`, `GoodsReceipt` et            │
│ `PurchaseReturn` le déclarent tous les trois. `GET /requisitions/` rendait   │
│ donc les lignes supprimées, leur `retrieve` répondait 200, et                │
│ `_objet_de_lorg` laissait une transition du journal agir dessus.             │
│                                                                              │
│ Pendant ce temps le TIRAGE les cachait correctement (`soft_delete=True`,     │
│ `_base_manager` plus un filtre explicite) : c'était la seule des trois       │
│ surfaces à être juste, et les deux autres disaient le contraire.             │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from django.test import TestCase

from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.purchases.models import Requisition, RequisitionItem
from apps.sales.tests._helpers import make_org_with_users


class RequisitionSuppressionDouceTests(TestCase):
    """Le rôle est BORNÉ, jamais `owner` : c'est la règle de ce chantier."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.membership = OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        )
        self.membership.assigned_warehouses.set([self.warehouse])

        self.produit = Product.objects.create(
            organization=self.org, name='Savon', sku='SAV', slug='savon',
            track_inventory=True, reorder_point=Decimal('10.000'),
            cost_price=Decimal('100'), selling_price=Decimal('150'),
        )
        self.piece = Requisition.objects.create(
            organization=self.org, warehouse=self.warehouse,
            reference='REQ-2026-09999', status=Requisition.Status.DRAFT,
        )
        RequisitionItem.objects.create(
            organization=self.org, requisition=self.piece,
            product=self.produit, quantity_requested=Decimal('5.000'),
        )

    def test_le_manager_FILTRE_les_lignes_supprimees(self):
        self.assertEqual(Requisition.objects.count(), 1)
        self.piece.soft_delete()
        self.assertEqual(
            Requisition.objects.count(), 0,
            "`Requisition.objects` rend les lignes supprimées : le manager "
            "manque, et c'est le seul des quatre modèles de ce fichier.",
        )

    def test_l_echappatoire_les_RETROUVE(self):
        """Le contrôle : sans elle, la remise à zéro d'essai les laisserait."""
        self.piece.soft_delete()
        self.assertEqual(Requisition.objects.with_deleted().count(), 1)
        self.assertEqual(Requisition.objects.only_deleted().count(), 1)

    def test_le_TIRAGE_et_le_manager_disent_desormais_la_meme_chose(self):
        """
        Le tirage était juste depuis l'origine (`soft_delete=True`). Ce qui
        manquait, c'était que les deux autres surfaces le disent aussi.
        """
        from apps.sync.pull import PULL_TABLES_BY_NAME

        table = PULL_TABLES_BY_NAME['requisitions']
        self.assertTrue(table.soft_delete)

        self.piece.soft_delete()
        # Le tirage l'écarte de ses lignes...
        self.assertEqual(
            table.get_model()._base_manager.filter(
                organization=self.org, is_deleted=False
            ).count(),
            0,
        )
        # ...et le manager par défaut aussi, désormais.
        self.assertEqual(Requisition.objects.count(), 0)
