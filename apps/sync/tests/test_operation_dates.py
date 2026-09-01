"""
Une vente encaissée hors ligne porte SA date, pas celle de sa poussée.

┌──────────────────────────────────────────────────────────────────────────────┐
│ TROIS JOURS SANS RÉSEAU EMPILAIENT TROIS JOURNÉES SUR CELLE DU RETOUR.     │
│                                                                              │
│ `Sale.sale_date` était un `auto_now_add`, posé à l'INSERTION serveur, et     │
│ `occurred_at` n'y arrivait pas. Le marchand retrouvait donc, le jour de la   │
│ synchronisation, la recette de trois journées comptée sur celle-là : chiffre │
│ du jour faux, rapports faux, marge fausse, et rien pour le signaler. Le      │
│ terminal est le SEUL à savoir quand l'argent est entré.                      │
│                                                                              │
│ Le même piège tenait `Payment.paid_at`, `RegisterSession.opened_at`,         │
│ `SaleReturn.return_date`, et - par `created_at` - le journal des mouvements  │
│ de stock comme les écritures au compte d'un client, qui n'ont pas d'autre    │
│ champ de date.                                                               │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`** : c'est le caissier qui encaisse.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.contacts.models import Customer, CustomerTransaction
from apps.inventory.models import Stock, StockMovement
from apps.products.models import Product
from apps.sales.models import Payment, RegisterSession, Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users
from apps.sync.models import SyncOperation

OPERATIONS = '/api/v1/sync/operations/'

#: Trois jours avant l'exécution du test, pas une date figée : une date en dur
#: finirait par tomber dans le futur d'une machine mal réglée, et le garde-fou
#: d'avance la ferait alors ignorer - le test passerait pour la mauvaise raison.
AVANT_HIER = timezone.now() - timedelta(days=3)


class DatesDesOperationsTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='open',
        )
        self.product = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.product, warehouse=self.warehouse,
            quantity=Decimal('100.000'), avg_cost=Decimal('1500.00'),
        )
        self.customer = Customer.objects.create(
            organization=self.org, name='Client', code='C1', phone='0900000000',
            credit_limit=Decimal('1000000'),
        )
        self.client.force_authenticate(user=self.cashier_a)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _envoyer(self, kind, payload, op_id, quand=AVANT_HIER):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': kind, 'seq': 1, 'depends_on': [],
                'occurred_at': quand.isoformat() if quand else None,
                'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _panier(self, **surcharges):
        corps = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'items': [{
                'product': str(self.product.id), 'quantity': '2',
                'unit_price': '2000.00',
            }],
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'tendered_amount': '4000.00',
            }],
        }
        corps.update(surcharges)
        return corps

    def _proche(self, valeur, attendu, quoi):
        self.assertIsNotNone(valeur, f"{quoi} est absent")
        self.assertLess(
            abs(valeur - attendu), timedelta(seconds=5),
            f"{quoi} porte {valeur} au lieu de {attendu} : l'acte se range au "
            "jour de sa poussée.",
        )

    # ------------------------------------------------------------------ vente

    def test_la_vente_porte_la_date_de_l_ENCAISSEMENT(self):
        op = 'd1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1'
        reponse = self._envoyer('sale.create', self._panier(id=op), op)
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], 'applied', verdict.get('errors'))

        vente = Sale.objects.get(id=op)
        self._proche(vente.sale_date, AVANT_HIER, 'Sale.sale_date')

    def test_le_REGLEMENT_de_la_vente_porte_la_meme_date(self):
        """
        Le reçu du client porte ce jour-là, et le tiroir de ce jour-là.

        `paid_at` est écrit au fond de `SaleCreateSerializer.create()`, que le
        back-office appelle aussi : c'est précisément le chemin qu'un paramètre
        n'aurait pas traversé sans modifier un corps partagé.
        """
        op = 'd2d2d2d2-d2d2-4d2d-8d2d-d2d2d2d2d2d2'
        self._envoyer('sale.create', self._panier(id=op), op)

        reglement = Payment.objects.filter(sale_id=op).first()
        self._proche(reglement.paid_at, AVANT_HIER, 'Payment.paid_at')

    def test_le_MOUVEMENT_DE_STOCK_porte_la_meme_date(self):
        """
        `StockMovement` n'a AUCUN champ de date à lui : ses filtres, son
        journal et son export lisent `created_at`. Une entrée saisie hors ligne
        se rangeait donc au jour de la poussée dans le rapport d'appro.
        """
        op = 'd3d3d3d3-d3d3-4d3d-8d3d-d3d3d3d3d3d3'
        self._envoyer('sale.create', self._panier(id=op), op)

        mouvement = StockMovement.objects.filter(
            reference_id=op
        ).first() or StockMovement.objects.order_by('-created_at').first()
        self._proche(mouvement.created_at, AVANT_HIER, 'StockMovement.created_at')

    def test_l_ECRITURE_AU_COMPTE_du_client_porte_la_meme_date(self):
        """`CustomerTransaction` n'a pas de champ de date non plus."""
        op = 'd4d4d4d4-d4d4-4d4d-8d4d-d4d4d4d4d4d4'
        panier = self._panier(
            id=op, sale_type='credit', customer=str(self.customer.id), payments=[],
        )
        reponse = self._envoyer('sale.create', panier, op)
        self.assertEqual(
            reponse.data['results'][0]['verdict'], 'applied',
            reponse.data['results'][0].get('errors'),
        )

        ecriture = CustomerTransaction.objects.filter(customer=self.customer).first()
        self._proche(ecriture.created_at, AVANT_HIER, 'CustomerTransaction.created_at')

    # ------------------------------------------------------------------ caisse

    def test_la_SESSION_ouverte_hors_ligne_porte_son_heure_d_ouverture(self):
        self.session.status = 'closed'
        self.session.save(update_fields=['status'])

        op = 'd5d5d5d5-d5d5-4d5d-8d5d-d5d5d5d5d5d5'
        reponse = self._envoyer(
            'register_session.open',
            {'id': op, 'register': str(self.register.id), 'opening_balance': '5000'},
            op,
        )
        self.assertEqual(reponse.data['results'][0]['verdict'], 'applied')
        self._proche(
            RegisterSession.objects.get(id=op).opened_at, AVANT_HIER,
            'RegisterSession.opened_at',
        )

    def test_la_CLOTURE_porte_l_heure_du_Z_imprime(self):
        """
        Le Z se tire à la fermeture, souvent avant que le réseau ne revienne.
        Le papier porte ce jour-là ; la session doit le porter aussi.
        """
        op = 'd6d6d6d6-d6d6-4d6d-8d6d-d6d6d6d6d6d6'
        reponse = self._envoyer(
            'register_session.close', {'session': str(self.session.id)}, op,
        )
        self.assertEqual(
            reponse.data['results'][0]['verdict'], 'applied',
            reponse.data['results'][0].get('errors'),
        )
        self.session.refresh_from_db()
        self._proche(self.session.closed_at, AVANT_HIER, 'RegisterSession.closed_at')

    # ------------------------------------------------- l'heure du serveur reste

    def test_updated_at_reste_L_HEURE_DU_SERVEUR(self):
        """
        LE POINT LE PLUS IMPORTANT DE CE FICHIER.

        `updated_at` est le curseur du tirage. S'il reculait de trois jours, la
        ligne se placerait AVANT le point de reprise de tout terminal déjà passé
        par là : elle ne descendrait jamais, définitivement.
        """
        op = 'd7d7d7d7-d7d7-4d7d-8d7d-d7d7d7d7d7d7'
        self._envoyer('sale.create', self._panier(id=op), op)

        vente = Sale.objects.get(id=op)
        self.assertLess(
            timezone.now() - vente.updated_at, timedelta(seconds=30),
            "`updated_at` a suivi l'horloge de l'acte : la vente serait "
            "invisible au tirage de tous les terminaux déjà synchronisés.",
        )

    def test_la_trace_de_synchronisation_garde_l_heure_du_SERVEUR(self):
        """Le seul repère qui reste pour voir qu'une écriture est arrivée en retard."""
        op = 'd8d8d8d8-d8d8-4d8d-8d8d-d8d8d8d8d8d8'
        self._envoyer('sale.create', self._panier(id=op), op)

        trace = SyncOperation.objects.get(pk=op)
        self._proche(trace.occurred_at, AVANT_HIER, 'SyncOperation.occurred_at')
        self.assertLess(
            timezone.now() - trace.received_at, timedelta(seconds=30),
            "`received_at` doit dire quand le SERVEUR l'a appris.",
        )

    def test_une_vente_du_back_office_porte_l_heure_du_SERVEUR(self):
        """
        L'horloge ne fuit pas hors du journal.

        Un `ContextVar` mal rendu ferait porter à la vente suivante, saisie au
        back-office, la date de l'opération précédente - et rien ne le dirait.
        """
        op = 'd9d9d9d9-d9d9-4d9d-8d9d-d9d9d9d9d9d9'
        self._envoyer('sale.create', self._panier(id=op), op)

        reponse = self.client.post(
            '/api/v1/sales/', self._panier(), format='json', **self._headers()
        )
        self.assertEqual(reponse.status_code, 201, reponse.data)
        vente = Sale.objects.get(id=reponse.data['id'])
        self.assertLess(
            timezone.now() - vente.sale_date, timedelta(seconds=30),
            "L'horloge de l'acte a survécu au lot : la vente du back-office "
            "porte la date d'une opération du terminal.",
        )

    def test_une_heure_EN_AVANCE_retombe_sur_celle_du_serveur(self):
        """
        Un appareil dont l'horloge avance daterait ses ventes dans le futur :
        en tête de toutes les listes, pour toujours, et hors de tout rapport.
        """
        op = 'dadadada-dada-4dad-8dad-dadadadadada'
        self._envoyer(
            'sale.create', self._panier(id=op), op,
            quand=timezone.now() + timedelta(days=30),
        )
        vente = Sale.objects.get(id=op)
        self.assertLess(
            timezone.now() - vente.sale_date, timedelta(seconds=30),
            "Une date d'appareil aberrante a été crue.",
        )
