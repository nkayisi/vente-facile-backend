"""
L'ajustement du solde d'un client : ses deux refus, sur les deux chemins.

Le terminal bloquait déjà à l'écran un montant nul et un motif vide ; le serveur
les acceptait. Le back-office (lot L11) ouvre ce geste au navigateur : la règle
doit tenir au serveur, et pour le journal du terminal, qui ne passe pas par le
serializer, au SERVICE.

Rôle BORNÉ : le gérant, le plus faible qui porte `customers.edit`.
"""
from decimal import Decimal

from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APITestCase

from apps.contacts import services as contacts_services
from apps.contacts.models import Customer, CustomerTransaction
from apps.core.models import DocumentSequence
from apps.sales.tests._helpers import make_org_with_users


class _Base(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.customer = Customer.objects.create(
            organization=self.org, name='Client Ajusté', code='CA1', phone='0900000001',
        )
        self.client.force_authenticate(user=self.manager)

    def _ajuster(self, corps):
        return self.client.post(
            f'/api/v1/customers/{self.customer.id}/adjust-balance/', corps,
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )


class RefusParLAPITests(_Base):
    def test_un_montant_nul_est_refuse(self):
        r = self._ajuster({'amount': '0', 'notes': 'Erreur de saisie du 12'})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('amount', r.data)

    def test_un_motif_absent_ou_blanc_est_refuse(self):
        for corps in ({'amount': '500'}, {'amount': '500', 'notes': '   '}):
            with self.subTest(corps=corps):
                r = self._ajuster(corps)
                self.assertEqual(r.status_code, 400, r.data)
                self.assertIn('notes', r.data)

    def test_un_refus_n_ecrit_rien_et_ne_consomme_aucun_numero(self):
        # ⚠ Un numéro alloué puis abandonné laisserait un trou dans la série AJU-.
        self._ajuster({'amount': '0', 'notes': 'Rien'})
        self._ajuster({'amount': '500'})
        self.assertFalse(CustomerTransaction.objects.filter(customer=self.customer).exists())
        self.assertFalse(
            DocumentSequence.objects.filter(organization=self.org, prefix__startswith='AJU').exists()
        )

    def test_le_controle_un_ajustement_motive_passe(self):
        r = self._ajuster({'amount': '500', 'notes': 'Ardoise papier reprise'})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['receipt_number'].startswith('AJU'))
        self.assertEqual(Decimal(r.data['balance_after']), Decimal('500'))


class RefusDuServiceTests(_Base):
    """Le chemin du journal : `customer.adjust_balance` appelle le service directement."""

    def test_le_service_refuse_un_montant_nul(self):
        with self.assertRaises(DRFValidationError):
            contacts_services.adjust_customer_balance(
                self.customer, Decimal('0'), user=self.manager, notes='Motif',
            )

    def test_le_service_refuse_un_motif_vide(self):
        with self.assertRaises(DRFValidationError):
            contacts_services.adjust_customer_balance(
                self.customer, Decimal('500'), user=self.manager, notes='  ',
            )
        self.assertFalse(CustomerTransaction.objects.filter(customer=self.customer).exists())


class OrdreDImputationExposeTests(_Base):
    """
    Le back-office annonce l'imputation d'un règlement : il lui faut l'ordre du
    serveur, `created_at`, que la liste des ventes n'exposait pas. Le type
    TypeScript le déclarait quand même, si bien que l'écran plantait sans que
    `tsc` ne dise rien.
    """

    def test_la_liste_des_ventes_porte_created_at(self):
        from apps.sales.models import Sale

        Sale.objects.create(
            organization=self.org, customer=self.customer, warehouse=self.warehouse,
            register=self.register, sold_by=self.manager, status='pending',
            total=Decimal('10.00'), amount_due=Decimal('10.00'), currency='CDF',
        )
        r = self.client.get(
            '/api/v1/sales/', {'customer': str(self.customer.id)},
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(r.status_code, 200, r.data)
        lignes = r.data.get('results', r.data)
        self.assertTrue(lignes)
        self.assertTrue(all(l.get('created_at') for l in lignes))
