"""
Le tableau de bord d'organisation applique enfin un périmètre.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE CES TESTS FERMENT.                                                   │
│                                                                              │
│ `OrganizationViewSet.dashboard` n'appliquait AUCUN scope : un caissier y     │
│ lisait le chiffre d'affaires de toute l'organisation, alors que le même      │
│ caissier ne voit que ses propres ventes sur `/sales/`. Ce n'était pas un     │
│ manque de filtre, c'était un trou - et il était invisible parce que l'écran  │
│ affichait un nombre parfaitement plausible.                                  │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ LES REQUÊTES DE CE FICHIER N'ENVOIENT PAS `X-Organization-ID`, ET C'EST LE
CŒUR DU TEST. Le back-office ne le pose pas sur ce chemin (`getDashboardStats`
ne porte que `Authorization`), l'identifiant voyageant dans l'URL. Un test qui
l'enverrait ferait passer un correctif adossé à `_get_membership`, lequel rend
`None` sans en-tête - donc « aucun périmètre », donc le trou intact.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Warehouse
from apps.organizations.models import OrganizationMembership
from apps.sales.models import Sale
from apps.sales.tests._helpers import make_org_with_users


def _vente(org, warehouse, vendeur, montant, reference):
    return Sale.objects.create(
        organization=org,
        warehouse=warehouse,
        sold_by=vendeur,
        reference=reference,
        status=Sale.Status.COMPLETED,
        subtotal=Decimal(montant),
        total=Decimal(montant),
        amount_paid=Decimal(montant),
        currency='CDF',
        exchange_rate=Decimal('1'),
    )


# Un cache RÉEL : avec `DummyCache`, le test de fuite passerait sans rien
# démontrer, `cache.get` rendant toujours `None`.
@override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': 'test-perimetre-dashboard',
}})
class PerimetreDuTableauDeBordTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.wh_a = self.d['warehouse']
        self.wh_b = Warehouse.objects.create(
            organization=self.org, branch=self.d['branch'],
            name='Dépôt B', code='WH-B',
        )
        # Le gérant n'a QUE le dépôt A (posé par la fabrique).
        # Chaque caissier vend 100 dans le dépôt A.
        _vente(self.org, self.wh_a, self.d['cashier_a'], '100', 'VT-A-1')
        _vente(self.org, self.wh_a, self.d['cashier_b'], '100', 'VT-B-1')
        # Et le propriétaire vend 500 dans le dépôt B.
        _vente(self.org, self.wh_b, self.d['owner'], '500', 'VT-O-1')

        # URL en dur : c'est le motif de `test_dashboard.py`, et `reverse` ne
        # résout pas ces routes (elles sont incluses sans namespace).
        self.url = f'/api/v1/organizations/{self.org.id}/dashboard/'

    def _lire(self, qui, **params):
        self.client.force_authenticate(user=qui)
        reponse = self.client.get(self.url, {'period': 'month', **params})
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        # `cards.total_sales` est un OBJET `{value, variation}`, pas un scalaire.
        return Decimal(str(reponse.data['cards']['total_sales']['value']))

    def test_un_caissier_ne_lit_que_ses_propres_ventes(self):
        """Le trou : 700 pour tout le monde, alors que ce caissier a vendu 100."""
        self.assertEqual(self._lire(self.d['cashier_a']), Decimal('100.00'))
        self.assertEqual(self._lire(self.d['cashier_b']), Decimal('100.00'))

    def test_un_gerant_ne_lit_que_ses_entrepots(self):
        """Le gérant n'a que le dépôt A : les 500 du dépôt B lui échappent."""
        self.assertEqual(self._lire(self.d['manager']), Decimal('200.00'))

    def test_un_proprietaire_lit_tout(self):
        self.assertEqual(self._lire(self.d['owner']), Decimal('700.00'))

    def test_le_cache_ne_fuit_pas_entre_deux_membres(self):
        """
        Sans le périmètre dans la clé, le second lecteur reçoit le total du
        premier SOUS L'ÉTIQUETTE « vos ventes » : une fuite plus trompeuse que
        celle qu'on vient de fermer.
        """
        self.assertEqual(self._lire(self.d['owner']), Decimal('700.00'))
        # Dans la même fenêtre de 60 s, sans rien vider.
        self.assertEqual(self._lire(self.d['cashier_a']), Decimal('100.00'))
        self.assertEqual(self._lire(self.d['manager']), Decimal('200.00'))
        # Et le propriétaire relit bien le sien.
        self.assertEqual(self._lire(self.d['owner']), Decimal('700.00'))

    def test_le_proprietaire_peut_filtrer_un_entrepot(self):
        self.assertEqual(
            self._lire(self.d['owner'], warehouse=str(self.wh_b.id)),
            Decimal('500.00'),
        )

    def test_le_proprietaire_peut_filtrer_un_utilisateur(self):
        self.assertEqual(
            self._lire(self.d['owner'], user=str(self.d['cashier_a'].id)),
            Decimal('100.00'),
        )

    def test_deux_filtres_differents_ne_partagent_pas_le_cache(self):
        """Le périmètre volontaire entre dans la clé, sinon A rend les chiffres de B."""
        self.assertEqual(
            self._lire(self.d['owner'], warehouse=str(self.wh_a.id)),
            Decimal('200.00'),
        )
        self.assertEqual(
            self._lire(self.d['owner'], warehouse=str(self.wh_b.id)),
            Decimal('500.00'),
        )

    def test_un_entrepot_hors_perimetre_est_refuse_et_non_ignore(self):
        """
        Le gérant n'a pas le dépôt B. L'ignorer rendrait 200 sous une étiquette
        « Dépôt B », et rien à l'écran ne le signalerait.
        """
        self.client.force_authenticate(user=self.d['manager'])
        reponse = self.client.get(
            self.url, {'period': 'month', 'warehouse': str(self.wh_b.id)}
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('warehouse', reponse.data)

    def test_un_caissier_ne_peut_pas_viser_un_collegue(self):
        self.client.force_authenticate(user=self.d['cashier_a'])
        reponse = self.client.get(
            self.url, {'period': 'month', 'user': str(self.d['cashier_b'].id)}
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('user', reponse.data)

    def test_le_stock_suit_l_entrepot_et_ignore_l_utilisateur(self):
        """
        Un stock est un ÉTAT, pas un acte : filtrer par utilisateur ne doit pas
        le vider. Rendre zéro ferait lire « plus rien en rayon » parce qu'un
        filtre d'un autre écran a traîné.
        """
        self.client.force_authenticate(user=self.d['owner'])
        sans = self.client.get(self.url, {'period': 'month'})
        avec = self.client.get(
            self.url, {'period': 'month', 'user': str(self.d['cashier_a'].id)}
        )
        self.assertEqual(
            sans.data['inventory']['stock_value'],
            avec.data['inventory']['stock_value'],
        )
