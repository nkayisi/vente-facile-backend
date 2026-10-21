"""
L'échappatoire de la suppression douce doit ÉCHAPPER.

┌──────────────────────────────────────────────────────────────────────────────┐
│ `with_deleted()` NE RENDAIT PAS LES LIGNES SUPPRIMÉES, ET PERSONNE NE S'EN  │
│ ÉTAIT APERÇU PARCE QU'ELLE N'AVAIT AUCUN APPELANT.                          │
│                                                                              │
│ `TenantSoftDeleteManager.with_deleted()` appelait                            │
│ `TenantManager.get_queryset(self)`, dont le corps est un                     │
│ `super().get_queryset()`. Ce `super()` se résout sur le MRO de l'INSTANCE,   │
│ donc sur `SoftDeleteManager`, qui refiltre `is_deleted=False` : la méthode    │
│ refermait la porte qu'elle existait pour ouvrir.                             │
│                                                                              │
│ Elle est devenue vivante avec la remise à zéro d'essai, qui cherchait un      │
│ `all_objects` inexistant et laissait donc SURVIVRE toute pièce supprimée en  │
│ douceur - une vente sans lignes, sous des compteurs repartis de zéro.        │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ UN `super()` COOPÉRATIF NE LÈVE RIEN. Il rend simplement un queryset filtré
là où on attendait le queryset nu, et seul un test qui SUPPRIME puis compte
peut le voir.
"""
from decimal import Decimal

from django.test import TestCase

from apps.products.models import Product
from apps.sales.models import Sale
from apps.sales.tests._helpers import make_org_with_users


class EchappatoireDeSuppressionDouceTests(TestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.article = Product.objects.create(
            organization=self.org, name='Savon', sku='SAV-1', slug='sav-1',
            cost_price=Decimal('10'), selling_price=Decimal('15'),
        )
        self.vente = Sale.objects.create(
            organization=self.org, warehouse=self.d['warehouse'],
            sold_by=self.d['cashier_a'], reference='VT-1',
            status=Sale.Status.COMPLETED, subtotal=Decimal('15'),
            total=Decimal('15'), amount_paid=Decimal('15'),
            currency='CDF', exchange_rate=Decimal('1'),
        )

    def test_with_deleted_rend_bien_la_ligne_SUPPRIMEE(self):
        """Le contrôle central : sur le code d'origine il rendait ZÉRO."""
        self.vente.soft_delete()
        self.assertEqual(
            Sale.objects.with_deleted().filter(organization=self.org).count(), 1,
            "`with_deleted()` ne voit pas la ligne supprimée : l'échappatoire "
            "refiltre `is_deleted=False`. Tout appelant qui s'y fie laisse "
            "survivre ce qu'il croit effacer.",
        )

    def test_objects_la_CACHE_toujours(self):
        """Le contre-contrôle : la porte normale reste fermée."""
        self.vente.soft_delete()
        self.assertEqual(
            Sale.objects.filter(organization=self.org).count(), 0
        )

    def test_only_deleted_ne_rend_QUE_les_supprimees(self):
        autre = Sale.objects.create(
            organization=self.org, warehouse=self.d['warehouse'],
            sold_by=self.d['cashier_a'], reference='VT-2',
            status=Sale.Status.COMPLETED, subtotal=Decimal('5'),
            total=Decimal('5'), amount_paid=Decimal('5'),
            currency='CDF', exchange_rate=Decimal('1'),
        )
        self.vente.soft_delete()
        supprimees = list(
            Sale.objects.only_deleted().filter(organization=self.org)
        )
        self.assertEqual([s.pk for s in supprimees], [self.vente.pk])
        self.assertNotIn(autre.pk, [s.pk for s in supprimees])

    def test_les_TROIS_portes_se_recoupent(self):
        """
        L'invariant qui tient tout : `objects` ∪ `only_deleted` = `with_deleted`,
        et les deux premières sont disjointes. Une porte qui filtre de travers
        casse l'une des deux égalités.
        """
        Sale.objects.create(
            organization=self.org, warehouse=self.d['warehouse'],
            sold_by=self.d['cashier_a'], reference='VT-3',
            status=Sale.Status.COMPLETED, subtotal=Decimal('5'),
            total=Decimal('5'), amount_paid=Decimal('5'),
            currency='CDF', exchange_rate=Decimal('1'),
        )
        self.vente.soft_delete()

        vivantes = set(
            Sale.objects.filter(organization=self.org).values_list('pk', flat=True)
        )
        mortes = set(
            Sale.objects.only_deleted().filter(organization=self.org)
            .values_list('pk', flat=True)
        )
        toutes = set(
            Sale.objects.with_deleted().filter(organization=self.org)
            .values_list('pk', flat=True)
        )

        self.assertEqual(vivantes | mortes, toutes)
        self.assertEqual(vivantes & mortes, set())
        #: Un corpus où tout est vivant ne prouverait rien.
        self.assertEqual(len(mortes), 1)
        self.assertEqual(len(vivantes), 1)
