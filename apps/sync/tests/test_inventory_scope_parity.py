"""
Le PÉRIMÈTRE d'une session d'inventaire, par le chemin du JOURNAL.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE CONTRAT N'AVAIT AUCUN TEST, ET LE TERMINAL NE POUVAIT PAS LE TENIR.       │
│                                                                              │
│ `InventorySessionCreateSerializer` déclare `category_ids` et `product_ids`.  │
│ Le terminal envoyait `categories` et `products`. DRF IGNORE SILENCIEUSEMENT  │
│ les clés qui ne sont pas dans `Meta.fields` : les identifiants tombaient     │
│ dans le vide, `category_ids` retombait sur `[]`, et `validate` levait « Au   │
│ moins une catégorie est requise pour un inventaire par catégorie. » Verdict  │
│ `rejected`, donc quarantaine, donc une session par catégorie créée depuis un │
│ terminal n'arrivait JAMAIS.                                                  │
│                                                                              │
│ Le défaut était inoffensif tant que l'écran verrouillait le périmètre sur    │
│ `full` - et il l'était, derrière une puce décorative. Il devient vivant à la │
│ seconde où le sélecteur de périmètre existe.                                 │
│                                                                              │
│ Ce fichier tient donc le CONTRAT DE TRANSPORT de cet acte : ce que le corps  │
│ porte, et ce que le serveur en écrit.                                        │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`.** C'est la règle de tout ce chantier :
`accessible_warehouse_ids` sort en amont pour un propriétaire, et la moitié du
code de périmètre n'est alors pas exécutée. Le gérant porte `inventory.create`,
et `make_org_with_users` lui assigne l'entrepôt principal.
"""
from decimal import Decimal
from uuid import uuid4

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import InventorySession, Stock
from apps.products.models import Category, Product
from apps.sales.tests._helpers import make_org_with_users

OPERATIONS = '/api/v1/sync/operations/'


class _BaseInventaire(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.boissons = Category.objects.create(
            organization=self.org, name='Boissons', slug='boissons',
        )
        self.produit = Product.objects.create(
            organization=self.org, name='Boisson', slug='boisson', sku='B1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True, category=self.boissons,
        )
        # Le serveur refuse un inventaire sur un entrepôt sans stock DISPONIBLE
        # (`quantity__gt=reserved_quantity`) : sans cette ligne, tous les tests
        # échoueraient pour une raison qui n'est pas la leur.
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('40.000'), reserved_quantity=Decimal('0.000'),
            avg_cost=Decimal('1500.00'),
        )
        self.client.force_authenticate(user=self.manager)

    def _send(self, kind, payload):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                # `operation_id` est un UUIDField : une chaîne libre fait 500.
                'operation_id': str(uuid4()), 'kind': kind, 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-09-06T09:00:00Z',
                'payload': payload,
            }]},
            format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def _verdict(self, reponse, attendu='applied'):
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], attendu, verdict.get('errors'))
        return verdict

    def _creer(self, **extra):
        return self._send('inventory_session.create', {
            'id': str(uuid4()),
            'name': 'Inventaire du 06 septembre 2026',
            'warehouse': str(self.warehouse.id),
            'notes': '',
            **extra,
        })


class PerimetreInventaireTests(_BaseInventaire):
    def test_un_inventaire_par_CATEGORIE_porte_ses_categories(self):
        """C'est le test qui échoue sur `categories` au lieu de `category_ids`.

        Sur l'ancien corps il revient `rejected` avec « Au moins une catégorie
        est requise », et la session n'existe pas.
        """
        self._verdict(self._creer(
            scope_type='category',
            category_ids=[str(self.boissons.id)],
        ))

        session = InventorySession.objects.get()
        self.assertEqual(session.scope_type, 'category')
        self.assertEqual(
            list(session.categories.values_list('id', flat=True)),
            [self.boissons.id],
        )

    def test_un_inventaire_par_PRODUIT_porte_ses_produits(self):
        self._verdict(self._creer(
            scope_type='product',
            product_ids=[str(self.produit.id)],
        ))

        session = InventorySession.objects.get()
        self.assertEqual(session.scope_type, 'product')
        self.assertEqual(
            list(session.products.values_list('id', flat=True)),
            [self.produit.id],
        )

    def test_un_inventaire_COMPLET_ne_porte_aucun_perimetre(self):
        """Le cas qui marchait déjà : c'est le contrôle, pas la découverte."""
        self._verdict(self._creer(scope_type='full'))

        session = InventorySession.objects.get()
        self.assertEqual(session.scope_type, 'full')
        self.assertEqual(session.categories.count(), 0)
        self.assertEqual(session.products.count(), 0)

    def test_un_perimetre_par_categorie_SANS_categorie_est_refuse(self):
        """Refus DÉTERMINISTE : il ne doit jamais repartir en `retry`.

        L'écran ferme le bouton, mais un corps mis en file par une version
        antérieure du terminal pourrait encore se présenter ainsi.
        """
        self._verdict(self._creer(scope_type='category'), 'rejected')
        self.assertFalse(InventorySession.objects.exists())

    def test_une_categorie_SANS_stock_dans_ce_depot_est_refusee(self):
        """Le serveur oppose le stock DISPONIBLE, pas l'existence de la catégorie.

        L'écran ne propose donc que les catégories qui en ont : lui laisser
        offrir les autres ferait découvrir le refus après coup, en quarantaine.
        """
        vide = Category.objects.create(
            organization=self.org, name='Épicerie', slug='epicerie',
        )
        self._verdict(self._creer(
            scope_type='category', category_ids=[str(vide.id)],
        ), 'rejected')

    def test_le_NOM_du_terminal_est_repris_tel_quel(self):
        """Le serveur n'auto-génère un nom que s'il n'en reçoit aucun.

        Le terminal en envoie toujours un, composé de la date du jour LOCALE :
        l'auto-génération du serveur emploie `timezone.now()`, donc la date UTC,
        et daterait de la veille un inventaire créé après 23 h à Kinshasa.
        """
        self._verdict(self._creer(scope_type='full'))
        self.assertEqual(
            InventorySession.objects.get().name,
            'Inventaire du 06 septembre 2026',
        )


class CorpsDuTerminalTests(_BaseInventaire):
    """
    Le piège, épinglé sur le serveur pour qu'il ne se retende pas.

    Ces deux tests envoient EXACTEMENT ce que le terminal envoyait avant ce
    lot. Ils démontrent que DRF ne se plaint pas de la clé inconnue : il la
    jette, puis refuse pour une raison qui ne désigne pas la cause.
    """

    def test_la_cle_categories_est_JETEE_et_le_refus_ne_la_nomme_pas(self):
        verdict = self._verdict(self._creer(
            scope_type='category',
            categories=[str(self.boissons.id)],  # l'ANCIENNE clé du terminal
        ), 'rejected')

        # Le message parle d'une catégorie manquante alors qu'on en a envoyé
        # une : c'est ce décalage qui rend le défaut si coûteux à diagnostiquer.
        self.assertIn('category_ids', str(verdict.get('errors')))
        self.assertFalse(InventorySession.objects.exists())

    def test_la_cle_products_est_JETEE_de_la_meme_facon(self):
        self._verdict(self._creer(
            scope_type='product',
            products=[str(self.produit.id)],  # l'ANCIENNE clé du terminal
        ), 'rejected')
        self.assertFalse(InventorySession.objects.exists())


class TransitionsSerialisablesTests(_BaseInventaire):
    """
    Les CINQ transitions rendent un corps JSON, et rien d'autre.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `inventory_session.cancel` BOUCLAIT EN `retry`, INDÉFINIMENT.            │
    │                                                                          │
    │ Il passait `serialiser=False`, si bien qu'`authoritative` recevait le    │
    │ RETOUR BRUT du service - et `cancel_inventory_session` rend l'objet      │
    │ `InventorySession`, pas un dictionnaire. Le rendu JSON levait alors      │
    │ « Object of type InventorySession is not JSON serializable », que        │
    │ `_classify` range en `unexpected`, donc en `retry` : l'opération restait │
    │ en file et repartait à chaque synchronisation, pour toujours.            │
    │                                                                          │
    │ Conséquence au comptoir : une session d'inventaire lancée depuis un      │
    │ terminal ne pouvait plus JAMAIS être annulée depuis ce terminal - et une │
    │ session en cours VERROUILLE le stock de ses produits, donc bloque la     │
    │ vente. Le magasinier voyait « 1 opération en attente » sans fin.         │
    │                                                                          │
    │ `serialiser=False` reste juste pour `count`, dont le service rend bien   │
    │ un dictionnaire (`{'status': 'counted', …}`).                            │
    │                                                                          │
    │ Trouvé en annulant une session sur l'émulateur, pas à la relecture.      │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def _demarrer(self):
        self._verdict(self._creer(scope_type='full'))
        session = InventorySession.objects.get()
        self._verdict(self._send('inventory_session.start', {
            'id': str(uuid4()), 'session': str(session.id),
        }))
        session.refresh_from_db()
        return session

    def test_annuler_rend_un_corps_JSON_et_ne_boucle_pas(self):
        import json

        session = self._demarrer()
        reponse = self._send('inventory_session.cancel', {
            'id': str(uuid4()), 'session': str(session.id),
        })
        verdict = self._verdict(reponse)

        # Le corps doit passer le rendu JSON : c'est très exactement ce qui
        # échouait, et l'échec ne se voyait que dans le journal du serveur.
        json.dumps(verdict)

        session.refresh_from_db()
        self.assertEqual(session.status, 'cancelled')
        # Une session annulée DÉVERROUILLE son stock : sans cela, les produits
        # visés restent invendables sans que rien ne l'explique.
        self.assertFalse(session.is_stock_locked)

    def test_les_cinq_transitions_rendent_un_corps_JSON(self):
        """Le garde-fou : aucune ne doit rendre un objet Django.

        Chacune est jouée dans l'ordre où elle est permise, puis son verdict est
        passé au rendu JSON - le contrôle que le serveur fait de toute façon en
        répondant.
        """
        import json

        session = self._demarrer()

        ligne = session.counts.first()
        self.assertIsNotNone(ligne, "le démarrage doit engendrer la feuille")

        etapes = [
            ('inventory_session.count', {
                'counts': [{'id': str(ligne.id), 'quantity_counted': '40'}],
            }),
            ('inventory_session.submit', {}),
            ('inventory_session.validate', {}),
        ]
        for kind, extra in etapes:
            with self.subTest(kind=kind):
                verdict = self._verdict(self._send(kind, {
                    'id': str(uuid4()), 'session': str(session.id), **extra,
                }))
                json.dumps(verdict)
