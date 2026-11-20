"""
L'attendu qu'affiche une session OUVERTE est celui que la clôture écrira.

Vu au navigateur le 2026-11-20 : le dialogue de clôture annonçait
« 2 347,4 $ » (fonds + total des ventes) pour un tiroir qui contenait 1 760,40 $
et 1 322 501 FC - la vente avait été réglée en francs, la monnaie rendue en
francs, et une dépense de 12 $ était sortie. La session expose désormais
`expected_balances`, calculé par la fonction même de la clôture.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement
from apps.sales.models import RegisterSession, RegisterSessionCurrencyBalance

from ._helpers import make_org_with_users


class AttenduParDeviseTests(APITestCase):
    def setUp(self):
        self.__dict__.update(make_org_with_users())
        self.principale = self.org.currency or 'CDF'
        self.autre = 'USD' if self.principale != 'USD' else 'CDF'
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('100'), status='open',
        )
        RegisterSessionCurrencyBalance.objects.create(
            organization=self.org, session=self.session, currency=self.principale,
            opening_balance=Decimal('100'),
        )
        # Une sortie en espèces dans l'AUTRE devise (monnaie rendue, par ex.).
        CashMovement.objects.create(
            organization=self.org, session=self.session, direction='out',
            movement_type='change', amount=Decimal('30'), currency=self.autre,
            description='Monnaie rendue', created_by=self.cashier_a,
            movement_date=timezone.now(),
        )
        self.entete = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}
        self.client.force_authenticate(user=self.cashier_a)

    def _attendu_affiche(self):
        rep = self.client.get('/api/v1/register-sessions/?status=open', **self.entete)
        self.assertEqual(rep.status_code, status.HTTP_200_OK, rep.content)
        lignes = rep.data['results'] if isinstance(rep.data, dict) else rep.data
        ligne = next(l for l in lignes if l['id'] == str(self.session.id))
        return {e['currency']: Decimal(e['expected']) for e in ligne['expected_balances']}

    def test_l_attendu_affiche_est_ventile_par_devise(self):
        self.assertEqual(
            self._attendu_affiche(),
            {self.principale: Decimal('100.00'), self.autre: Decimal('-30.00')},
        )

    def test_l_attendu_affiche_est_celui_que_la_cloture_ecrit(self):
        affiche = self._attendu_affiche()
        rep = self.client.post(
            f'/api/v1/register-sessions/{self.session.id}/close/', {}, format='json', **self.entete,
        )
        self.assertEqual(rep.status_code, status.HTTP_200_OK, rep.content)
        ecrit = {
            cb.currency: cb.expected_balance
            for cb in RegisterSessionCurrencyBalance.objects.filter(session=self.session)
        }
        self.assertEqual(ecrit, affiche)
        # Une fois close, la session ne recalcule plus rien : l'attendu est figé.
        rep = self.client.get(f'/api/v1/register-sessions/{self.session.id}/', **self.entete)
        self.assertEqual(rep.data['expected_balances'], [])


class EncaissementsDuZTests(APITestCase):
    """Le Z liste ce qui est ENTRÉ : le montant remis, dans la devise du billet."""

    def setUp(self):
        from apps.sales.models import Payment, PaymentMethod, Sale

        self.__dict__.update(make_org_with_users())
        self.principale = self.org.currency or 'CDF'
        self.autre = 'USD' if self.principale != 'USD' else 'CDF'
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='open',
        )
        especes, _ = PaymentMethod.objects.get_or_create(
            organization=self.org, method_type='cash', defaults={'name': 'Espèces', 'code': 'CASH'},
        )
        vente = Sale.objects.create(
            organization=self.org, register=self.register, session=self.session,
            warehouse=self.register.warehouse, sold_by=self.cashier_a,
            currency=self.principale, total=Decimal('575'), status='completed',
        )
        Payment.objects.create(
            organization=self.org, sale=vente, payment_method=especes,
            amount=Decimal('575'), currency=self.autre, tendered_amount=Decimal('1700000'),
            status='completed',
        )
        self.client.force_authenticate(user=self.cashier_a)

    def test_le_montant_remis_dans_sa_devise(self):
        rep = self.client.get(
            f'/api/v1/register-sessions/{self.session.id}/',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(rep.status_code, status.HTTP_200_OK, rep.content)
        lignes = [(l['currency'], Decimal(l['total'])) for l in rep.data['payments_summary']]
        self.assertEqual(lignes, [(self.autre, Decimal('1700000'))])
