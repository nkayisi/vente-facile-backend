"""
Le plafond de remise, RÉSOLU par le serveur et exposé une seule fois.

┌──────────────────────────────────────────────────────────────────────────────┐
│ TROIS SURFACES RÉIMPLÉMENTAIENT LE DÉFAUT À 50.                             │
│                                                                              │
│ `max_sale_discount_percent(organization)` porte la règle : la valeur des     │
│ paramètres de l'organisation, un défaut à 50, un plancher à 0 et un plafond  │
│ à 100. Le back-office, lui, codait `MAX_SALE_DISCOUNT_PERCENT = 50` en dur   │
│ dans le POS, et le terminal bornait à 100.                                   │
│                                                                              │
│ Conséquence pratique, dans les deux sens : un marchand qui abaisse son       │
│ plafond à 20 voit le comptoir accepter 45 % puis le serveur refuser la vente │
│ ENTIÈRE, après l'annonce du prix au client ; un marchand qui le relève à 80  │
│ ne peut pas saisir la remise qu'il a lui-même autorisée.                     │
│                                                                              │
│ Le remède est celui déjà retenu pour `max_redemption_percent_ceiling` :      │
│ exposer la valeur RÉSOLUE, pas les paramètres bruts. Envoyer `settings` et   │
│ laisser chaque surface appliquer le défaut, c'est réécrire trois fois la     │
│ même règle et diverger au premier cas limite.                                │
└──────────────────────────────────────────────────────────────────────────────┘

La LISTE autant que le DÉTAIL : le POS web alimente son contexte depuis
`GET /organizations/`, et le snapshot du terminal depuis le détail. Le champ
manquant à l'une des deux laisserait sa surface sur le défaut, en silence -
c'est exactement ce qui était arrivé à l'adresse et au téléphone des reçus.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.sales.tests._helpers import make_org_with_users

DEFAUT = Decimal('50')


class PlafondDeRemiseExposeTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        # Rôle BORNÉ : c'est le caissier qui tient le comptoir, donc lui qui
        # doit recevoir le plafond.
        self.client.force_authenticate(user=self.cashier_a)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _liste(self):
        reponse = self.client.get('/api/v1/organizations/', **self._headers)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        lignes = reponse.data['results'] if 'results' in reponse.data else reponse.data
        return next(o for o in lignes if str(o['id']) == str(self.org.id))

    def _detail(self):
        reponse = self.client.get(f'/api/v1/organizations/{self.org.id}/', **self._headers)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        return reponse.data

    def test_le_defaut_est_rendu_en_liste(self):
        self.assertEqual(Decimal(str(self._liste()['max_sale_discount_percent'])), DEFAUT)

    def test_le_defaut_est_rendu_en_detail(self):
        self.assertEqual(Decimal(str(self._detail()['max_sale_discount_percent'])), DEFAUT)

    def test_la_valeur_REGLEE_prime_sur_le_defaut(self):
        self.org.settings = {'max_sale_discount_percent': 20}
        self.org.save(update_fields=['settings'])

        self.assertEqual(Decimal(str(self._liste()['max_sale_discount_percent'])), Decimal('20'))
        self.assertEqual(Decimal(str(self._detail()['max_sale_discount_percent'])), Decimal('20'))

    def test_les_deux_surfaces_rendent_LE_MEME_nombre(self):
        """
        Le garde-fou : c'est la divergence entre les deux vues qui laisserait
        une surface sur le défaut sans que rien ne le signale.
        """
        self.org.settings = {'max_sale_discount_percent': '37.5'}
        self.org.save(update_fields=['settings'])

        self.assertEqual(
            Decimal(str(self._liste()['max_sale_discount_percent'])),
            Decimal(str(self._detail()['max_sale_discount_percent'])),
        )

    def test_une_valeur_ABERRANTE_retombe_sur_la_regle_serveur(self):
        """
        Ce que l'exposition évite : un client qui recevrait `settings` brut
        devrait redécider quoi faire d'un nombre négatif ou de « beaucoup ».
        Ici le serveur a déjà tranché, et la même réponse part partout.
        """
        for brut, attendu in [(-5, DEFAUT), (250, Decimal('100')), ('zéro', DEFAUT)]:
            with self.subTest(brut=brut):
                self.org.settings = {'max_sale_discount_percent': brut}
                self.org.save(update_fields=['settings'])
                self.assertEqual(
                    Decimal(str(self._detail()['max_sale_discount_percent'])), attendu
                )
