"""
Marquer un reçu imprimé : une route appelée par le web, refusée à tous.

┌──────────────────────────────────────────────────────────────────────────────┐
│ `receipt_printed` N'A JAMAIS ÉTÉ ÉCRIT DEPUIS LE BACK-OFFICE.               │
│                                                                              │
│ `mark_receipt_printed` n'était dans aucune `action_permissions`, et « action │
│ non listée = accès refusé » : 403 pour tous les rôles, propriétaire compris. │
│ Or `frontend/actions/sales.actions.ts::markReceiptPrinted` l'appelle depuis  │
│ DEUX écrans, le POS après l'encaissement et le détail de vente.              │
│                                                                              │
│ Le refus n'était pas visible : l'appel remonte un message dans un toast que  │
│ personne ne lit après une impression réussie, et le ticket, lui, est bien    │
│ sorti. Ce qui se perd est la DISTINCTION entre une première sortie et une    │
│ réimpression - la pastille DUPLICATA existe précisément pour qu'un second    │
│ exemplaire ne puisse pas passer pour l'original devant un client.            │
│                                                                              │
│ Le même oubli, sur la même vue, avait déjà fait refuser `locked_products`.   │
│ `apps/sync/tests/test_parity_contract.py` balaie désormais toutes les vues.  │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.models import RegisterSession, Sale

from ._helpers import make_cash_payment_method, make_org_with_users


class MarquerLeRecuImprimeTests(APITestCase):
    """Le rôle est BORNÉ : c'est le caissier qui imprime, pas le propriétaire."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        make_cash_payment_method(self.org)
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='open',
        )
        self.vente = Sale.objects.create(
            organization=self.org, register=self.register, session=self.session,
            warehouse=self.warehouse, sold_by=self.cashier_a,
            status=Sale.Status.COMPLETED, total=Decimal('2000.00'),
            amount_paid=Decimal('2000.00'),
        )
        self.client.force_authenticate(user=self.cashier_a)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _marquer(self):
        return self.client.post(
            f'/api/v1/sales/{self.vente.id}/mark-receipt-printed/',
            {}, format='json', **self._headers,
        )

    def test_le_caissier_peut_marquer_son_recu_imprime(self):
        self.assertFalse(self.vente.receipt_printed)

        reponse = self._marquer()
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

        self.vente.refresh_from_db()
        self.assertTrue(self.vente.receipt_printed)

    def test_le_gerant_le_peut_aussi(self):
        """Le détail de vente est un écran de gérant, et il réimprime."""
        self.client.force_authenticate(user=self.manager)
        self.assertEqual(self._marquer().status_code, status.HTTP_200_OK)

    def test_marquer_deux_fois_reste_un_SUCCES(self):
        """
        Une réimpression repasse par le même appel. Refuser la seconde ferait
        échouer un duplicata, c'est-à-dire le cas d'usage du bouton.
        """
        self.assertEqual(self._marquer().status_code, status.HTTP_200_OK)
        self.assertEqual(self._marquer().status_code, status.HTTP_200_OK)

    def test_la_vente_d_une_AUTRE_organisation_reste_hors_de_portee(self):
        """
        L'ouverture ne perce pas le cloisonnement : le périmètre reste celui
        de `get_queryset`, borné à l'organisation de l'en-tête.
        """
        from apps.inventory.models import Warehouse
        from apps.organizations.models import Branch, Organization
        from apps.sales.models import Register

        ailleurs = Organization.objects.create(name='Autre', slug='autre')
        branche = Branch.objects.create(
            organization=ailleurs, name='B', code='B', is_main=True,
        )
        depot = Warehouse.objects.create(
            organization=ailleurs, branch=branche, name='W', code='W', is_default=True,
        )
        caisse = Register.objects.create(
            organization=ailleurs, branch=branche, warehouse=depot, name='C', code='C',
        )
        vente_ailleurs = Sale.objects.create(
            organization=ailleurs, register=caisse, warehouse=depot,
            sold_by=self.cashier_a,
            status=Sale.Status.COMPLETED, total=Decimal('100.00'),
        )
        reponse = self.client.post(
            f'/api/v1/sales/{vente_ailleurs.id}/mark-receipt-printed/',
            {}, format='json', **self._headers,
        )
        self.assertEqual(reponse.status_code, status.HTTP_404_NOT_FOUND)

        vente_ailleurs.refresh_from_db()
        self.assertFalse(vente_ailleurs.receipt_printed)
