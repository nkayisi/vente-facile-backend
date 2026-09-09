"""
On ne rend pas deux fois la même marchandise.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE CONTRÔLE COMPARAIT LA QUANTITÉ RENDUE À LA QUANTITÉ VENDUE.               │
│                                                                              │
│ Il ne regardait pas ce qui avait DÉJÀ été rendu sur la ligne. Un client       │
│ rendant deux flacons pouvait donc voir le même retour enregistré, approuvé,  │
│ puis recommencé à l'identique : le stock revenait deux fois en rayon et la   │
│ caisse remboursait deux fois une marchandise vendue une seule fois. Rien ne  │
│ le signalait ; le rapprochement ne se serait fait qu'à l'inventaire suivant, │
│ où l'écart aurait été mis sur le compte d'un vol.                            │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`** : c'est la règle de ce chantier depuis le
lot 1, et son oubli avait caché un refus qui frappait tous les caissiers.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale, SaleReturn
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users

RETOURS = '/api/v1/sale-returns/'


class QuantiteRestanteTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        make_cash_payment_method(self.org)
        self.produit = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('1000.00'), cost_price=Decimal('700.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('50.000'), avg_cost=Decimal('700.00'),
        )
        self.acheteur = Customer.objects.create(
            organization=self.org, name='Client', code='C1', phone='09',
            credit_limit=Decimal('0'),
        )
        RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.owner,
            opening_balance=Decimal('0'), status='open',
        )
        # `sale_returns.create` est porté par le gérant : c'est le rôle le plus
        # faible qui puisse enregistrer un retour.
        self.client.force_authenticate(user=self.manager)
        self.vente = self._vente(quantite='5')
        self.ligne = self.vente.items.first()

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _vente(self, quantite):
        reponse = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'credit',
                'is_pos': True,
                'customer': str(self.acheteur.id),
                'items': [{
                    'product': str(self.produit.id),
                    'quantity': quantite,
                    'unit_price': '1000.00',
                }],
                'payments': [],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        return Sale.objects.get(id=reponse.data['id'])

    def _rendre(self, quantite):
        return self.client.post(
            RETOURS,
            {
                'original_sale': str(self.vente.id),
                'reason': 'Article défectueux',
                'items': [{
                    'original_item': str(self.ligne.id),
                    'quantity': quantite,
                    'unit_price': '1000.00',
                    'total': str(Decimal(quantite) * Decimal('1000.00')),
                    'restock': True,
                }],
            },
            format='json', **self._headers(),
        )

    def _dernier_retour(self):
        # `SaleReturnCreateSerializer` ne rend pas d'`id` : ses `fields` ne
        # portent que ce qu'on lui envoie. On relit donc la base.
        return SaleReturn.objects.order_by('-created_at').first()

    def _approuver(self, retour):
        reponse = self.client.post(
            f'{RETOURS}{retour.id}/approve/', {}, format='json', **self._headers()
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

    # ------------------------------------------------------------------ le défaut

    def test_un_retour_approuve_consomme_la_quantite(self):
        """
        Deux fois trois sur cinq vendus : la seconde fois doit être refusée.

        C'est le défaut lui-même : avant le correctif, les deux passaient, six
        unités revenaient en stock pour cinq vendues, et le client était
        remboursé six fois mille francs.
        """
        premier = self._rendre('3')
        self.assertEqual(premier.status_code, status.HTTP_201_CREATED, premier.data)
        self._approuver(self._dernier_retour())

        second = self._rendre('3')
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST, second.data)
        self.assertEqual(SaleReturn.objects.count(), 1)

    def test_un_BROUILLON_consomme_aussi(self):
        """
        Un brouillon n'a encore rien remis en stock, mais il est APPROUVABLE.

        Laisser passer un second brouillon sur les mêmes unités, c'est le même
        défaut pris une décision plus tard : les deux seraient approuvables, et
        rien n'empêcherait le gérant d'approuver les deux.
        """
        self.assertEqual(self._rendre('3').status_code, status.HTTP_201_CREATED)
        self.assertEqual(self._rendre('3').status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_retour_REJETE_ne_consomme_rien(self):
        """
        Un rejet n'a rien remis en stock ni remboursé : les unités sont encore
        chez le marchand, et le client peut les rendre pour de bon.
        """
        premier = self._rendre('5')
        self.assertEqual(premier.status_code, status.HTTP_201_CREATED, premier.data)
        rejet = self.client.post(
            f'{RETOURS}{self._dernier_retour().id}/reject/', {},
            format='json', **self._headers(),
        )
        self.assertEqual(rejet.status_code, status.HTTP_200_OK, rejet.data)

        second = self._rendre('5')
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)

    def test_le_RELIQUAT_reste_rendable(self):
        """Trois puis deux sur cinq : les deux passent, et rien de plus."""
        premier = self._rendre('3')
        self.assertEqual(premier.status_code, status.HTTP_201_CREATED, premier.data)
        self._approuver(self._dernier_retour())

        second = self._rendre('2')
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)

        self.assertEqual(self._rendre('1').status_code, status.HTTP_400_BAD_REQUEST)

    def test_le_message_DIT_ce_qui_reste(self):
        """
        Un refus qui se contente de « quantité supérieure » laisse le commerçant
        devant son client sans savoir quoi saisir. Le message porte les trois
        nombres : rendu, vendu, reste.
        """
        premier = self._rendre('4')
        self.assertEqual(premier.status_code, status.HTTP_201_CREATED, premier.data)
        self._approuver(self._dernier_retour())

        refus = self._rendre('2')
        self.assertEqual(refus.status_code, status.HTTP_400_BAD_REQUEST)
        message = str(refus.data['items'])
        self.assertIn('4', message)
        self.assertIn('5', message)
        self.assertIn('1', message)
        # « 2.000 » n'est pas une quantité qu'un commerçant écrit.
        self.assertNotIn('4.000', message)

    def test_la_quantite_vendue_reste_le_plafond_absolu(self):
        """Le contrôle d'origine ne doit pas disparaître avec le nouveau."""
        refus = self._rendre('6')
        self.assertEqual(refus.status_code, status.HTTP_400_BAD_REQUEST, refus.data)

    # ---------------------------------------------- deux lignes, un seul envoi

    def test_deux_lignes_du_MEME_envoi_ne_se_cumulent_pas_a_l_insu_du_controle(self):
        """
        Le contrôle photographiait l'état AVANT la requête, et comparait CHAQUE
        ligne à ce même reste.

        Deux lignes désignant la même ligne de facture passaient donc toutes
        deux, et ensemble elles rendaient plus que le vendu : l'invariant que ce
        contrôle existe pour tenir, contourné par un corps forgé ou rejoué.
        `SaleReturnItem` n'ayant aucune contrainte d'unicité, rien en aval ne le
        rattrapait.
        """
        reponse = self.client.post(
            RETOURS,
            {
                'original_sale': str(self.vente.id),
                'reason': 'Deux lignes pour le même article',
                'items': [
                    {'original_item': str(self.ligne.id), 'quantity': '3', 'restock': True},
                    {'original_item': str(self.ligne.id), 'quantity': '3', 'restock': True},
                ],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(SaleReturn.objects.count(), 0)

    def test_deux_lignes_qui_TIENNENT_dans_le_reste_passent(self):
        """Le cumul ne doit pas refuser ce qui rentre : trois plus deux sur cinq."""
        reponse = self.client.post(
            RETOURS,
            {
                'original_sale': str(self.vente.id),
                'reason': 'Deux lignes, dans le reste',
                'items': [
                    {'original_item': str(self.ligne.id), 'quantity': '3', 'restock': True},
                    {'original_item': str(self.ligne.id), 'quantity': '2', 'restock': True},
                ],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)

    # ------------------------------------- le prix est dit par le serveur

    def test_un_retour_s_enregistre_SANS_prix_ni_total(self):
        """
        `create` relit le prix sur la ligne de FACTURE et recalcule le total :
        ce que le client enverrait serait jeté.

        Les laisser obligatoires faisait refuser tout corps qui ne portait pas
        deux valeurs que le serveur n'allait pas lire - c'est ce qui rendait la
        création IMPOSSIBLE depuis le back-office, dont le dialogue n'envoie que
        la ligne, la quantité et la remise en stock.
        """
        reponse = self.client.post(
            RETOURS,
            {
                'original_sale': str(self.vente.id),
                'reason': 'Article défectueux',
                'items': [{
                    'original_item': str(self.ligne.id),
                    'quantity': '2',
                    'restock': True,
                }],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        ligne = self._dernier_retour().items.first()
        self.assertEqual(ligne.unit_price, Decimal('1000.00'))
        self.assertEqual(ligne.total, Decimal('2000.00'))

    def test_un_prix_ENVOYE_par_le_client_est_ignore(self):
        """
        Un montant de remboursement ne se prend pas dans une requête : le prix
        qui fait foi est celui auquel la marchandise a été vendue. Un client qui
        annoncerait dix fois le prix ne doit pas faire sortir dix fois l'argent.
        """
        reponse = self.client.post(
            RETOURS,
            {
                'original_sale': str(self.vente.id),
                'reason': 'Article défectueux',
                'items': [{
                    'original_item': str(self.ligne.id),
                    'quantity': '1',
                    'unit_price': '99999.00',
                    'total': '99999.00',
                    'restock': True,
                }],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        retour = self._dernier_retour()
        self.assertEqual(retour.items.first().unit_price, Decimal('1000.00'))
        self.assertEqual(retour.refund_amount, Decimal('1000.00'))
