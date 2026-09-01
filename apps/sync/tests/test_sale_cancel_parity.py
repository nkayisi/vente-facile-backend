"""
Parité de l'ANNULATION d'une vente.

┌──────────────────────────────────────────────────────────────────────────────┐
│ QUATRE DIVERGENCES, TOUTES SUR DE L'ARGENT.                                 │
│                                                                              │
│ Le handler réécrivait le corps de la vue au lieu de l'appeler, et avait      │
│ dérivé sur :                                                                 │
│                                                                              │
│ 1. « On n'annule que ses propres ventes » : le back-office refuse 403 à un   │
│    caissier qui touche la vente d'un autre. Le journal l'acceptait.          │
│ 2. Le MOUVEMENT DE CAISSE d'annulation, quand la vente avait été payée. Le   │
│    tiroir gardait donc un encaissement annulé, et le Z du soir constatait un │
│    écart inexplicable.                                                       │
│ 3. Le TYPE d'écriture client : `settle_debt` de type ADJUSTMENT rattachée à  │
│    la vente, contre un `adjust_balance` anonyme.                             │
│ 4. `sale.notes`, ÉCRASÉ par le motif d'annulation.                           │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement
from apps.contacts.models import Customer, CustomerTransaction
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users

OPERATIONS = '/api/v1/sync/operations/'


class AnnulationParityTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.moyen = make_cash_payment_method(self.org)
        self.produit = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('100.000'), avg_cost=Decimal('1500.00'),
        )
        self.acheteur = Customer.objects.create(
            organization=self.org, name='Client', code='C1', phone='09',
            credit_limit=Decimal('0'),
        )
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.cashier_a,
            opening_balance=Decimal('0'), status='open',
        )
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ LA RÈGLE « SES PROPRES VENTES » NE MORD QUE SUR CE CAS-LÀ.       │
        # │                                                                  │
        # │ Un caissier n'a pas `sales.cancel` par son rôle : sans droit     │
        # │ accordé, l'annulation est bloquée en amont, des deux côtés. La   │
        # │ règle ne sert donc qu'au marchand qui laisse ses caissiers       │
        # │ corriger leurs propres erreurs - et c'est exactement là qu'il ne │
        # │ faut pas qu'ils touchent aux ventes des autres.                  │
        # └──────────────────────────────────────────────────────────────────┘
        from apps.organizations.models import OrganizationMembership

        OrganizationMembership.objects.filter(
            organization=self.org, user__in=[self.cashier_a, self.cashier_b]
        ).update(extra_permissions=['sales.cancel'])
        self.client.force_authenticate(user=self.cashier_a)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _journal(self, payload, op_id):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': 'sale.cancel', 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-08-31T09:00:00Z',
                'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _vente_payee(self):
        reponse = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'retail', 'is_pos': True,
                'items': [{
                    'product': str(self.produit.id),
                    'quantity': '2', 'unit_price': '2000.00',
                }],
                'payments': [{
                    'payment_method': str(self.moyen.id),
                    'tendered_amount': '4000.00',
                }],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        return Sale.objects.get(id=reponse.data['id'])

    def test_annuler_une_vente_payee_ECRIT_LE_MOUVEMENT_DE_CAISSE(self):
        """
        Sans lui, le tiroir garde en caisse un encaissement annulé.

        Le caissier compte le soir, trouve quatre mille francs de trop, et rien
        dans le livre de caisse ne l'explique.
        """
        vente = self._vente_payee()
        avant = CashMovement.objects.count()

        op = 'a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1'
        verdict = self._journal({'sale': str(vente.id)}, op).data['results'][0]
        self.assertEqual(verdict['verdict'], 'applied', verdict.get('errors'))

        vente.refresh_from_db()
        self.assertEqual(vente.status, 'cancelled')
        self.assertGreater(
            CashMovement.objects.count(), avant,
            "Aucun mouvement de caisse d'annulation : le tiroir garde l'encaissement.",
        )

    def test_un_caissier_ne_peut_pas_annuler_la_vente_D_UN_AUTRE(self):
        """
        Les deux surfaces refusent, par deux chemins, et c'est normal.

        Le back-office rend **404** : `SaleViewSet.get_queryset` borne les
        ventes d'un caissier sans `sales.view_all` à celles qu'il a faites, si
        bien qu'il ne VOIT même pas celle d'un autre. Le journal, lui, retrouve
        la vente par son identifiant et oppose la règle : verdict `rejected`.

        Ce qui doit être identique, c'est le RÉSULTAT : la vente d'autrui n'est
        pas annulée. Le handler l'acceptait.
        """
        vente = self._vente_payee()

        self.client.force_authenticate(user=self.cashier_b)
        vue = self.client.post(
            f'/api/v1/sales/{vente.id}/cancel/', {}, format='json', **self._headers()
        )
        self.assertIn(
            vue.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            vue.data,
        )

        op = 'a2a2a2a2-a2a2-4a2a-8a2a-a2a2a2a2a2a2'
        verdict = self._journal({'sale': str(vente.id)}, op).data['results'][0]
        self.assertEqual(verdict['verdict'], 'rejected', verdict)
        vente.refresh_from_db()
        self.assertEqual(vente.status, 'completed', "La vente d'un autre a été annulée.")

    def test_un_gerant_annule_la_vente_d_un_caissier(self):
        """Le garde-fou du garde-fou : la règle ne doit pas tout bloquer."""
        vente = self._vente_payee()
        self.client.force_authenticate(user=self.manager)

        op = 'a3a3a3a3-a3a3-4a3a-8a3a-a3a3a3a3a3a3'
        verdict = self._journal({'sale': str(vente.id)}, op).data['results'][0]
        self.assertEqual(verdict['verdict'], 'applied', verdict.get('errors'))
        vente.refresh_from_db()
        self.assertEqual(vente.status, 'cancelled')

    def test_l_ecriture_client_est_RATTACHEE_a_la_vente(self):
        """
        `settle_debt` de type ADJUSTMENT, avec la vente en clé - et non un
        `adjust_balance` anonyme. Deux annulations identiques laissaient deux
        traces différentes selon la surface qui les avait faites.
        """
        vente = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'credit', 'is_pos': True,
                'customer': str(self.acheteur.id),
                'items': [{
                    'product': str(self.produit.id),
                    'quantity': '2', 'unit_price': '2000.00',
                }],
                'payments': [],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(vente.status_code, status.HTTP_201_CREATED, vente.data)
        vente = Sale.objects.get(id=vente.data['id'])
        self.assertGreater(vente.amount_due, 0)

        op = 'a4a4a4a4-a4a4-4a4a-8a4a-a4a4a4a4a4a4'
        verdict = self._journal({'sale': str(vente.id)}, op).data['results'][0]
        self.assertEqual(verdict['verdict'], 'applied', verdict.get('errors'))

        ecriture = CustomerTransaction.objects.filter(
            customer=self.acheteur,
            transaction_type=CustomerTransaction.TransactionType.ADJUSTMENT,
        ).order_by('-created_at').first()
        self.assertIsNotNone(ecriture, "Aucune écriture d'ajustement.")
        self.assertEqual(
            ecriture.sale_id, vente.id,
            "L'écriture n'est pas rattachée à la vente annulée.",
        )

    def test_le_motif_n_EFFACE_PAS_la_note_du_caissier(self):
        vente = self._vente_payee()
        vente.notes = "Client pressé, à rappeler"
        vente.save(update_fields=['notes'])

        op = 'a5a5a5a5-a5a5-4a5a-8a5a-a5a5a5a5a5a5'
        self._journal(
            {'sale': str(vente.id), 'reason': 'Erreur de saisie'}, op
        )
        vente.refresh_from_db()
        self.assertEqual(
            vente.notes, "Client pressé, à rappeler",
            "Le motif d'annulation a écrasé la note du caissier.",
        )
        self.assertIn('Erreur de saisie', vente.internal_notes)
