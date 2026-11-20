"""
Ce que la caisse ANNONCE comme fonds d'ouverture est ce que l'ouverture APPLIQUE.

Le dialogue du back-office disait « solde d'ouverture à zéro » pendant que le
serveur reprenait, devise par devise, ce qui avait été compté à la dernière
clôture (vu au navigateur le 2026-11-20 : 1 772,40 $ repris sous un texte qui
annonçait zéro). La caisse expose désormais `inherited_opening_balances`,
calculé par la même fonction que l'ouverture.

Rôle BORNÉ : un caissier, celui qui ouvre la caisse au comptoir.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.models import RegisterSession, RegisterSessionCurrencyBalance

from ._helpers import make_org_with_users


class FondsHeriteTests(APITestCase):
    def setUp(self):
        self.__dict__.update(make_org_with_users())
        self.client.force_authenticate(user=self.cashier_a)
        self.entete = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}
        self.principale = self.org.currency or 'CDF'
        self.autre = 'USD' if self.principale != 'USD' else 'CDF'

    def _cloture(self, comptes):
        prev = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='closed',
        )
        for devise, compte in comptes.items():
            RegisterSessionCurrencyBalance.objects.create(
                organization=self.org, session=prev, currency=devise,
                opening_balance=Decimal('0'), expected_balance=Decimal('999'),
                counted_balance=compte,
            )
        return prev

    def _annonce(self):
        rep = self.client.get('/api/v1/registers/', **self.entete)
        self.assertEqual(rep.status_code, status.HTTP_200_OK, rep.content)
        lignes = rep.data['results'] if isinstance(rep.data, dict) else rep.data
        caisse = next(r for r in lignes if r['id'] == str(self.register.id))
        return {l['currency']: Decimal(l['amount']) for l in caisse['inherited_opening_balances']}

    def _ouvrir(self, corps=None):
        rep = self.client.post(
            '/api/v1/register-sessions/open/',
            {'register': str(self.register.id), **(corps or {})},
            format='json', **self.entete,
        )
        self.assertEqual(rep.status_code, status.HTTP_201_CREATED, rep.content)
        session = RegisterSession.objects.get(id=rep.data['id'])
        return {cb.currency: cb.opening_balance for cb in session.currency_balances.all()}

    def test_une_caisse_jamais_cloturee_n_annonce_rien(self):
        self.assertEqual(self._annonce(), {})

    def test_l_annonce_est_ce_que_l_ouverture_applique(self):
        self._cloture({self.principale: Decimal('1772.40'), self.autre: Decimal('50.00')})
        annonce = self._annonce()
        self.assertEqual(annonce, {self.principale: Decimal('1772.40'), self.autre: Decimal('50.00')})
        self.assertEqual(self._ouvrir(), annonce)

    def test_le_compte_prime_sur_l_attendu(self):
        # L'attendu de la clôture vaut 999 : c'est le COMPTÉ qui est repris.
        self._cloture({self.principale: Decimal('10.00')})
        self.assertEqual(self._annonce(), {self.principale: Decimal('10.00')})

    def test_une_saisie_remplace_l_annonce_pour_sa_seule_devise(self):
        self._cloture({self.principale: Decimal('1772.40'), self.autre: Decimal('50.00')})
        appliques = self._ouvrir({'opening_balances': [{'currency': self.autre, 'amount': '20'}]})
        self.assertEqual(appliques[self.autre], Decimal('20.00'))
        self.assertEqual(appliques[self.principale], Decimal('1772.40'))
