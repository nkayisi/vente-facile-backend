"""
Le Livre de caisse et le Rapport de caisse montrent le MÊME tiroir.

┌──────────────────────────────────────────────────────────────────────────────┐
│ IL Y AVAIT DEUX RÈGLES, ET DEUX SOLDES.                                     │
│                                                                              │
│ `CashMovementViewSet` bornait strictement ; `reports/_scope_cash_movements`  │
│ portait trois clauses `isnull` de plus. Le même gérant lisait donc un solde  │
│ au Livre de caisse et un AUTRE au tableau de bord, sur deux écrans voisins   │
│ du même back-office, le même jour - et rien ne le signalait.                 │
│                                                                              │
│ Aucun test ne comparait les deux. Celui-ci le fait, et c'est sa seule        │
│ raison d'être : le corps est partagé, mais un corps partagé ne protège de    │
│ rien si un appelant se remet à filtrer de son côté.                          │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Le demandeur est un GÉRANT : c'est le seul rôle où les deux règles
divergeaient. Un propriétaire sort en amont, un caissier est borné par créateur
des deux côtés.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement
from apps.sales.models import RegisterSession
from apps.sales.tests._helpers import make_org_with_users


class UnSeulTiroirTests(APITestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']

        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.d['register'],
            opened_by=self.d['manager'], status='open',
            opening_balance=Decimal('0'),
        )
        # RATTACHÉ : un apport saisi au comptoir, dans le dépôt du gérant.
        self._mvt('MVT-RATTACHE', '500.00', session=self.session)
        # NON RATTACHÉ : ni vente, ni dépense, ni session. C'est le cas que les
        # deux surfaces traitaient différemment - une opération d'établissement.
        self._mvt('MVT-ORPHELIN', '9000.00', session=None)

        self.client.force_authenticate(user=self.d['manager'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))

    def _mvt(self, ref, montant, session):
        return CashMovement.objects.create(
            organization=self.org, movement_type='other_in', direction='in',
            amount=Decimal(montant), currency='CDF', reference=ref,
            movement_date=timezone.now(), created_by=self.d['manager'],
            session=session,
        )

    def _livre(self):
        r = self.client.get('/api/v1/cash-movements/')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content[:200])
        return {m['reference'] for m in r.data['results']}

    def _rapport(self):
        r = self.client.get(
            '/api/v1/reports/statistics/daily_cash_report/',
            {'date': timezone.localdate().isoformat()},
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content[:200])
        # `movements` est paginé : `{results: [...], count: N}`.
        return {m['reference'] for m in r.data['movements']['results']}

    def test_un_apport_sans_tiroir_est_RESERVE_AU_PROPRIETAIRE(self):
        """
        C'est la règle des dépenses d'établissement, étendue à la caisse : un
        apport qu'aucune vente, dépense ni session ne rattache appartient à
        l'établissement, pas à un dépôt.
        """
        self.assertNotIn('MVT-ORPHELIN', self._livre())

    def test_le_proprietaire_le_voit_toujours(self):
        """Le contrôle : sans lui, tout masquer passerait le test précédent."""
        self.client.force_authenticate(user=self.d['owner'])
        self.assertIn('MVT-ORPHELIN', self._livre())

    def test_LE_LIVRE_ET_LE_RAPPORT_MONTRENT_LE_MEME_ENSEMBLE(self):
        """L'invariant qui porte tout ce fichier."""
        self.assertEqual(self._livre(), self._rapport())

    def test_et_ils_montrent_bien_QUELQUE_CHOSE(self):
        """
        Un ensemble vide serait égal à un ensemble vide : le test précédent
        passerait en masquant tout. Il faut donc affirmer le contenu.
        """
        self.assertEqual(self._livre(), {'MVT-RATTACHE'})
