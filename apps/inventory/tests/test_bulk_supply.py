"""
L'approvisionnement de MASSE : la feuille, puis son envoi.

Couvre ce qui, sans filet, se casse en silence : un article à stock nul qui
disparaît de la feuille même qu'on vient remplir, une erreur de saisie dont on
ne sait pas quelle ligne corriger, une moitié de livraison enregistrée, et deux
lignes sur le même article dont le coût moyen dépend de l'ordre d'exécution.

⚠ LE RÔLE EST BORNÉ, JAMAIS `owner`. C'est la règle du chantier de parité, et
  son oubli avait caché pendant tout un lot un refus qui frappait tous les
  non-propriétaires. Le MAGASINIER est le rôle le plus faible qui porte
  `stock_movements.create` : c'est lui qui reçoit les livraisons.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.bulk_supply import LIGNES_MAX, build_supply_sheet, create_bulk_supply
from apps.inventory.models import Stock, StockMovement
from apps.organizations.models import OrganizationMembership
from apps.products.models import Category, Product, Unit
from apps.sales.tests._helpers import make_org_with_users, make_user


class _AppMasseSetup(APITestCase):
    """Deux catégories dont une imbriquée, et trois articles dont un neuf."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        # ⚠ LE MAGASINIER EST LE RÔLE LE PLUS FAIBLE QUI PORTE
        #   `stock_movements.create` : c'est lui qui reçoit les livraisons. Le
        #   helper ne le fabrique pas.
        self.stock_keeper = make_user('sk@appromasse.test', 'Stock', 'Keeper')
        m = OrganizationMembership.objects.create(
            user=self.stock_keeper, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        m.assigned_warehouses.add(self.warehouse)

        self.unite = Unit.objects.create(
            organization=self.org, name='BOUTEILLE', symbol='bt',
        )
        self.casier = Unit.objects.create(
            organization=self.org, name='CASIER', symbol='cs',
        )
        self.boissons = Category.objects.create(
            organization=self.org, name='Boissons', slug='boissons',
        )
        self.sodas = Category.objects.create(
            organization=self.org, name='Sodas', slug='sodas', parent=self.boissons,
        )
        self.hygiene = Category.objects.create(
            organization=self.org, name='Hygiène', slug='hygiene',
        )

        # Un article conditionné, avec du stock.
        self.biere = self._article('Bière 65cl', 'BIE-65', self.boissons, facteur=12)
        self._stock(self.biere, 30)
        # Un article simple de la SOUS-catégorie, avec du stock.
        self.soda = self._article('Soda', 'SOD-01', self.sodas)
        self._stock(self.soda, 8)
        # ⚠ UN ARTICLE NEUF, SANS AUCUNE LIGNE DE STOCK. C'est lui qui éprouve
        #   le joint EXTERNE : il doit apparaître dans la feuille, à zéro.
        self.savon = self._article('Savon', 'SAV-01', self.hygiene)

    def _article(self, nom, sku, categorie, facteur=None):
        return Product.objects.create(
            organization=self.org, name=nom, slug=sku.lower(), sku=sku,
            unit=self.unite, category=categorie,
            packaging_unit=self.casier if facteur else None,
            units_per_package=facteur,
            selling_mode='wholesale_and_retail' if facteur else 'retail_only',
            cost_price=Decimal('500'), selling_price=Decimal('800'),
            package_cost_price=Decimal('5500') if facteur else None,
            wholesale_price=Decimal('9000') if facteur else None,
            reorder_point=Decimal('10'), track_inventory=True, is_active=True,
        )

    def _stock(self, produit, quantite, paquets=None, vrac=None):
        return Stock.objects.create(
            organization=self.org, product=produit, warehouse=self.warehouse,
            quantity=Decimal(quantite),
            package_quantity=Decimal(paquets) if paquets is not None else Decimal('0'),
            loose_quantity=Decimal(vrac) if vrac is not None else Decimal(quantite),
            avg_cost=Decimal('500'),
        )

    @property
    def _entetes(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}


class FeuilleDApprovisionnementTests(_AppMasseSetup):

    def test_UN_ARTICLE_SANS_STOCK_EST_DANS_LA_FEUILLE(self):
        """
        ⚠ C'EST L'INVARIANT CENTRAL DE LA FEUILLE.

        `target_products` de l'inventaire exige `quantity__gt=reserved_quantity` :
        on ne compte que ce qu'on a. Recopier ce filtre ici ferait disparaître
        exactement les articles qu'on vient réapprovisionner, et le PREMIER
        approvisionnement d'une référence neuve serait impossible.
        """
        feuille = build_supply_sheet(self.org, self.warehouse, scope='all')
        par_sku = {ligne['sku']: ligne for ligne in feuille['lines']}

        self.assertIn('SAV-01', par_sku, "L'article neuf a disparu de la feuille.")
        self.assertEqual(par_sku['SAV-01']['stock'], '0')
        self.assertEqual(par_sku['SAV-01']['stock_packages'], '0')

    def test_une_categorie_ramene_son_SOUS_ARBRE(self):
        # Filtrer « Boissons » doit ramener « Boissons > Sodas » : sans la
        # descendance, la feuille sous-estime en silence.
        feuille = build_supply_sheet(
            self.org, self.warehouse, scope='category', categories=[str(self.boissons.id)],
        )
        skus = {ligne['sku'] for ligne in feuille['lines']}
        self.assertEqual(skus, {'BIE-65', 'SOD-01'})

    def test_la_ligne_porte_le_partage_du_rayon_et_ses_libelles(self):
        # « 15 casiers + 10 bouteilles » se lit, « 370 » non : c'est ce partage
        # qui permet de juger ce qu'il faut commander.
        Stock.objects.filter(product=self.biere).update(
            package_quantity=Decimal('2'), loose_quantity=Decimal('6'),
            quantity=Decimal('30'),
        )
        feuille = build_supply_sheet(self.org, self.warehouse, scope='all')
        ligne = next(x for x in feuille['lines'] if x['sku'] == 'BIE-65')

        self.assertEqual(ligne['packaging_factor'], 12)
        self.assertEqual(ligne['package_unit_label'], 'CASIER')
        self.assertEqual(ligne['retail_unit_label'], 'BOUTEILLE')
        # Les libellés sortent du `PackagingService`, pas d'une mise en forme
        # écrite ici : c'est lui qui connaît le nom que le marchand a donné.
        self.assertIn('CASIER', ligne['stock_display'])
        # Les quatre prix de la fiche pré-remplissent les blocs de prix.
        self.assertEqual(ligne['cost_price'], '500.00')
        self.assertEqual(ligne['package_cost_price'], '5500.00')

    def test_un_service_n_entre_PAS_dans_la_feuille(self):
        # `track_inventory=False` : un service n'a pas de stock. Même critère que
        # `target_products` et `lignes_proposees`.
        Product.objects.create(
            organization=self.org, name='Livraison', slug='liv', sku='LIV-01',
            unit=self.unite, category=self.hygiene, track_inventory=False,
            selling_mode='retail_only',
            is_active=True, cost_price=Decimal('0'), selling_price=Decimal('2000'),
        )
        feuille = build_supply_sheet(self.org, self.warehouse, scope='all')
        self.assertNotIn('LIV-01', {x['sku'] for x in feuille['lines']})

    def test_le_perimetre_LOW_suit_le_critere_des_alertes(self):
        # Une seule source du « sous le seuil » : un article qui déclenche une
        # alerte et qui n'apparaîtrait pas ici ferait douter des deux écrans.
        # Le soda est à 8 pour un seuil de 10 ; la bière à 30.
        feuille = build_supply_sheet(self.org, self.warehouse, scope='low')
        self.assertEqual({x['sku'] for x in feuille['lines']}, {'SOD-01'})

    def test_une_feuille_TRONQUEE_le_DIT(self):
        """
        ⚠ Tronquer en SILENCE ferait croire la livraison saisie en entier.
        """
        for i in range(LIGNES_MAX):
            self._article(f'Article {i}', f'ART-{i:04d}', self.hygiene)
        feuille = build_supply_sheet(self.org, self.warehouse, scope='all')

        self.assertTrue(feuille['truncated'])
        self.assertEqual(feuille['count'], LIGNES_MAX)
        self.assertEqual(feuille['limit'], LIGNES_MAX)

    def test_la_feuille_exige_LE_DROIT_DE_CREER(self):
        # Elle ne sert qu'à préparer une écriture : l'ouvrir à `stock.view`
        # donnerait un formulaire que son lecteur ne peut pas soumettre.
        self.client.force_authenticate(self.cashier_a)
        r = self.client.get(
            '/api/v1/stock-movements/supply-sheet/',
            {'warehouse': str(self.warehouse.id)}, **self._entetes,
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_un_entrepot_absent_est_un_400_et_non_un_500(self):
        self.client.force_authenticate(self.stock_keeper)
        r = self.client.get('/api/v1/stock-movements/supply-sheet/', **self._entetes)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('warehouse', r.data)

    def test_un_entrepot_ILLISIBLE_est_un_400_et_non_un_500(self):
        self.client.force_authenticate(self.stock_keeper)
        r = self.client.get(
            '/api/v1/stock-movements/supply-sheet/',
            {'warehouse': 'oops'}, **self._entetes,
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)


class EnvoiDUneLivraisonTests(_AppMasseSetup):

    def _envoyer(self, lignes, **extra):
        self.client.force_authenticate(self.stock_keeper)
        corps = {
            'warehouse': str(self.warehouse.id),
            'movement_type': 'purchase',
            'lines': lignes,
        }
        corps.update(extra)
        return self.client.post(
            '/api/v1/stock-movements/bulk/', corps, format='json', **self._entetes,
        )

    def test_une_livraison_entiere_passe_en_UN_envoi(self):
        r = self._envoyer([
            {'product': str(self.biere.id), 'package_quantity': '2', 'loose_quantity': '5',
             'package_unit_cost': '5500'},
            {'product': str(self.savon.id), 'quantity': '40', 'unit_cost': '300'},
        ])
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(r.data['created'], 2)

        # Le partage scellé/vrac est celui du `PackagingService`, pas une
        # arithmétique réécrite ici : 2 casiers de 12 plus 5 bouteilles.
        biere = Stock.objects.get(product=self.biere, warehouse=self.warehouse)
        self.assertEqual(biere.quantity, Decimal('59.000'))
        mouvement = StockMovement.objects.get(product=self.biere)
        self.assertEqual(mouvement.input_package_quantity, Decimal('2.000'))
        self.assertEqual(mouvement.input_loose_quantity, Decimal('5.000'))
        self.assertEqual(mouvement.packaging_factor, 12)

        # L'article NEUF obtient sa ligne de stock.
        savon = Stock.objects.get(product=self.savon, warehouse=self.warehouse)
        self.assertEqual(savon.quantity, Decimal('40.000'))

    def test_UNE_LIGNE_A_ZERO_EST_IGNOREE_PAS_REFUSEE(self):
        """
        Une feuille pré-remplie de quarante articles dont on n'en reçoit que
        douze est le cas ORDINAIRE : refuser obligerait à effacer les
        vingt-huit autres à la main.
        """
        r = self._envoyer([
            {'product': str(self.savon.id), 'quantity': '40', 'unit_cost': '300'},
            {'product': str(self.soda.id), 'quantity': '0'},
            {'product': str(self.biere.id)},
        ])
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(r.data['created'], 1)
        self.assertFalse(StockMovement.objects.filter(product=self.soda).exists())

    def test_une_feuille_ENTIEREMENT_vide_est_refusee_en_le_disant(self):
        # Sinon l'envoi réussirait sans rien faire, et le magasinier croirait sa
        # livraison enregistrée.
        r = self._envoyer([
            {'product': str(self.savon.id), 'quantity': '0'},
            {'product': str(self.soda.id)},
        ])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('lines', r.data)

    def test_LES_ERREURS_SONT_INDEXEES_PAR_LIGNE_ET_TOUTES_COLLECTEES(self):
        """
        ⚠ Lever à la PREMIÈRE ferait corriger une erreur à la fois, soit quarante
          allers-retours sur une feuille de quarante lignes ; lever SANS index
          ferait lire « Indiquez une quantité » sans savoir laquelle.
        """
        r = self._envoyer([
            {'product': str(self.savon.id), 'quantity': '40', 'unit_cost': '300'},
            # Un conditionnement saisi sur un article qui n'en a pas.
            {'product': str(self.soda.id), 'package_quantity': '3'},
            {'product': str(self.biere.id), 'quantity': '-5'},
        ])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        erreurs = r.data['lines']
        # Les DEUX fautives sont nommées, à leur index, et la bonne est absente.
        self.assertIn('1', {str(k) for k in erreurs})
        self.assertIn('2', {str(k) for k in erreurs})
        self.assertNotIn('0', {str(k) for k in erreurs})

    def test_UN_REFUS_N_ENREGISTRE_RIEN_DU_TOUT(self):
        """
        ⚠ Une livraison est un TOUT : la moitié d'un bon enregistrée est pire
          que rien - le magasinier ne sait plus ce qui est entré, et reprendre
          la saisie fait entrer deux fois ce qui est déjà passé.
        """
        avant = StockMovement.objects.count()
        r = self._envoyer([
            {'product': str(self.savon.id), 'quantity': '40', 'unit_cost': '300'},
            {'product': str(self.soda.id), 'package_quantity': '3'},
        ])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(StockMovement.objects.count(), avant)
        self.assertFalse(Stock.objects.filter(product=self.savon).exists())

    def test_DEUX_LIGNES_SUR_LE_MEME_ARTICLE_SONT_REFUSEES_EN_LE_NOMMANT(self):
        # Elles donneraient deux mouvements dont le coût moyen pondéré dépend de
        # l'ordre d'exécution, et le rayon finirait avec un `avg_cost` que
        # personne ne peut reconstituer.
        r = self._envoyer([
            {'product': str(self.savon.id), 'quantity': '40', 'unit_cost': '300'},
            {'product': str(self.savon.id), 'quantity': '10', 'unit_cost': '900'},
        ])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        message = str(r.data['lines'])
        self.assertIn('ligne 1', message)

    def test_un_type_de_SORTIE_est_refuse(self):
        # Ce n'est pas un panier d'écritures hétérogènes, c'est une livraison.
        r = self._envoyer(
            [{'product': str(self.savon.id), 'quantity': '40'}],
            movement_type='sale',
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('movement_type', r.data)

    def test_le_perimetre_entrepot_est_oppose(self):
        # Le magasinier n'est affecté qu'à l'entrepôt principal.
        from apps.inventory.models import Warehouse

        autre = Warehouse.objects.create(
            organization=self.org, name='Dépôt Nord', code='WH-NORD',
        )
        self.client.force_authenticate(self.stock_keeper)
        r = self.client.post(
            '/api/v1/stock-movements/bulk/',
            {
                'warehouse': str(autre.id), 'movement_type': 'purchase',
                'lines': [{'product': str(self.savon.id), 'quantity': '5'}],
            },
            format='json', **self._entetes,
        )
        self.assertIn(
            r.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
            r.data,
        )
        self.assertFalse(StockMovement.objects.filter(warehouse=autre).exists())

    def test_la_note_de_l_envoi_descend_sur_les_lignes_muettes(self):
        r = self._envoyer(
            [
                {'product': str(self.savon.id), 'quantity': '40'},
                {'product': str(self.soda.id), 'quantity': '5', 'notes': 'lot abîmé'},
            ],
            notes='Livraison du 8 novembre',
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(
            StockMovement.objects.get(product=self.savon).notes,
            'Livraison du 8 novembre',
        )
        # Une note de LIGNE n'est pas écrasée : c'est elle qui est précise.
        self.assertEqual(
            StockMovement.objects.get(product=self.soda).notes, 'lot abîmé',
        )


class ServiceDirectTests(_AppMasseSetup):
    """Le service, hors HTTP : c'est ce chemin que le journal emprunte."""

    def test_le_service_s_appelle_SANS_requete(self):
        # ⚠ Le journal n'a pas de `request` au sens du périmètre : le passer à
        #   `None` doit laisser le service écrire, exactement comme les autres
        #   actes dont le contrôle d'entrepôt vit en amont.
        mouvements = create_bulk_supply(
            organization=self.org, user=self.stock_keeper,
            warehouse=self.warehouse, movement_type='purchase',
            lines=[{'product': str(self.savon.id), 'quantity': '12', 'unit_cost': '250'}],
            request=None,
        )
        self.assertEqual(len(mouvements), 1)
        self.assertEqual(
            Stock.objects.get(product=self.savon).quantity, Decimal('12.000'),
        )


class AtomiciteDeLaLivraisonTests(_AppMasseSetup):
    """
    ⚠ CE QUE LE `atomic` PROTÈGE VRAIMENT, ET QU'AUCUNE VALIDATION NE COUVRE.

    Les erreurs de SAISIE sont collectées avant toute écriture : à ce stade le
    `atomic` ne sert à rien. Ce qu'il tient, c'est un échec SURVENU EN COURS DE
    BOUCLE - un verrou, une contrainte, un périmètre refusé sur la deuxième
    ligne alors que la première est déjà passée. Sans lui, la moitié d'un bon de
    livraison reste enregistrée, et reprendre la saisie fait entrer deux fois ce
    qui est déjà passé.

    Une mutation ne le démontre pas : retirer le `atomic` laisse les tests verts,
    parce que rien n'échoue en cours de boucle dans le cas nominal. Il faut donc
    faire échouer la deuxième écriture.
    """

    def test_un_echec_sur_la_DEUXIEME_ligne_n_enregistre_RIEN(self):
        from unittest.mock import patch

        from apps.inventory.stock_movements import create_stock_movement as reel

        appels = {'n': 0}

        def echoue_a_la_seconde(serializer, **kwargs):
            appels['n'] += 1
            if appels['n'] == 2:
                raise RuntimeError("le rayon a bougé sous nous")
            return reel(serializer, **kwargs)

        with patch(
            'apps.inventory.stock_movements.create_stock_movement',
            side_effect=echoue_a_la_seconde,
        ):
            with self.assertRaises(RuntimeError):
                create_bulk_supply(
                    organization=self.org, user=self.stock_keeper,
                    warehouse=self.warehouse, movement_type='purchase',
                    lines=[
                        {'product': str(self.savon.id), 'quantity': '40'},
                        {'product': str(self.soda.id), 'quantity': '10'},
                    ],
                    request=None,
                )

        # La PREMIÈRE ligne était passée : elle doit avoir été défaite.
        self.assertEqual(appels['n'], 2)
        self.assertFalse(
            Stock.objects.filter(product=self.savon).exists(),
            "La première ligne est restée : la livraison a été enregistrée à moitié.",
        )
        self.assertEqual(StockMovement.objects.count(), 0)


class EntreeNegativeTests(_AppMasseSetup):
    """
    ⚠ DÉFAUT ANTÉRIEUR, TROUVÉ EN CONSTRUISANT L'APPROVISIONNEMENT DE MASSE.

    `StockMovementCreateSerializer` normalisait le signe des SORTIES et ne
    disait rien des ENTRÉES : un « achat » de -5 passait, et
    `create_stock_movement` l'appliquait tel quel - l'achat RETIRAIT du stock.
    Il est éprouvé sur le chemin article par article, celui où il vivait.
    """

    def test_une_entree_NEGATIVE_est_refusee_article_par_article(self):
        self.client.force_authenticate(self.stock_keeper)
        r = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.savon.id), 'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase', 'quantity': '-5',
            },
            format='json', **self._entetes,
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertIn('quantity', r.data)
        self.assertFalse(Stock.objects.filter(product=self.savon).exists())

    def test_une_SORTIE_garde_sa_normalisation_de_signe(self):
        """
        ⚠ LE CONTRÔLE : la convention des sorties ne doit PAS être cassée par
          le refus ci-dessus. L'écran envoie un nombre positif, le serveur le
          stocke négatif - c'est ce qui fait baisser le rayon.
        """
        self._stock(self.savon, 20)
        self.client.force_authenticate(self.stock_keeper)
        r = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.savon.id), 'warehouse': str(self.warehouse.id),
                'movement_type': 'damage', 'quantity': '5',
            },
            format='json', **self._entetes,
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(
            StockMovement.objects.get(product=self.savon).quantity, Decimal('-5.000'),
        )
        self.assertEqual(
            Stock.objects.get(product=self.savon).quantity, Decimal('15.000'),
        )


class SensDesMouvementsTests(_AppMasseSetup):
    """
    « Ce qui sort du stock » était écrit DEUX FOIS, et les deux avaient divergé.

    `SORTIES_FIFO` comptait `production_out` ; l'`outgoing_types` du serializer,
    non. Le signe n'y était donc pas normalisé - positif, un `production_out`
    AJOUTAIT du stock pendant que la consommation FIFO le traitait en sortie -
    et depuis que l'entrée négative est refusée, sa seule écriture correcte
    l'était aussi, sous un message parlant d'« entrée ».
    """

    def test_les_deux_listes_ne_peuvent_plus_diverger(self):
        from apps.inventory.models import (
            STOCK_IN_MOVEMENT_TYPES, STOCK_OUT_MOVEMENT_TYPES, StockMovement,
        )
        from apps.inventory.stock_movements import SORTIES_FIFO

        self.assertEqual(list(SORTIES_FIFO), list(STOCK_OUT_MOVEMENT_TYPES))

        # ⚠ ET LA PARTITION EST EXHAUSTIVE À UNE EXCEPTION NOMMÉE : `unpack` ne
        #   change aucun total, il déplace du scellé vers le vrac. Sans ce
        #   contrôle, un type ajouté au modèle demain n'aurait de sens dans
        #   AUCUNE des deux listes, et son signe ne serait normalisé nulle part.
        tous = {t for t, _ in StockMovement.MovementType.choices}
        classes = set(STOCK_IN_MOVEMENT_TYPES) | set(STOCK_OUT_MOVEMENT_TYPES)
        self.assertEqual(
            tous - classes, {StockMovement.MovementType.UNPACK},
            "Un type de mouvement n'est ni une entrée ni une sortie : "
            "rangez-le, ou nommez l'exception ici.",
        )
        self.assertEqual(
            set(STOCK_IN_MOVEMENT_TYPES) & set(STOCK_OUT_MOVEMENT_TYPES), set(),
        )

    def test_une_sortie_de_production_normalise_son_signe(self):
        """
        Aucun client ne la propose aujourd'hui, et c'est précisément pourquoi
        elle se vérifie ici : un type qu'on n'emprunte pas est un type dont on
        ne découvre le sens faux qu'au premier marchand qui l'emprunte.
        """
        self._stock(self.savon, 20)
        self.client.force_authenticate(self.stock_keeper)
        r = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.savon.id), 'warehouse': str(self.warehouse.id),
                'movement_type': 'production_out', 'quantity': '5',
            },
            format='json', **self._entetes,
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(
            StockMovement.objects.get(product=self.savon).quantity, Decimal('-5.000'),
        )
        self.assertEqual(
            Stock.objects.get(product=self.savon).quantity, Decimal('15.000'),
        )


class ReportDesPrixTests(_AppMasseSetup):
    """
    ⚠ LE REPORT DES PRIX ÉTAIT REFUSÉ POUR TOUT LE MONDE, SUR CE CHEMIN SEUL.

    `_validate_product_prices` résout l'organisation par
    `self.context['organization']` puis, à défaut, par la vue. `create_bulk_supply`
    construit le serializer lui-même : il n'a pas de `view`, et le contexte ne
    portait pas l'organisation. Le contrôle de droit REFUSE quand il ne trouve
    rien à quoi opposer `products.edit` - « un contrôle de droit qui ignore sur
    quoi il porte refuse » - donc toute ligne cochée « reporter les prix » sortait
    en 400, propriétaire compris. Sur le terminal, où la livraison est UN seul
    acte, ce refus condamnait les quarante autres lignes.

    Le magasinier est le rôle le plus faible qui porte À LA FOIS
    `stock_movements.create` et `products.edit` : c'est lui qui reçoit les
    livraisons, et c'est lui qui doit pouvoir retarifer.
    """

    def test_LE_REPORT_DES_PRIX_PASSE_SUR_UNE_LIVRAISON(self):
        self.client.force_authenticate(self.stock_keeper)
        r = self.client.post(
            '/api/v1/stock-movements/bulk/',
            {
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'lines': [{
                    'product': str(self.savon.id),
                    'quantity': '10',
                    'unit_cost': '600',
                    'update_product_prices': True,
                    'selling_price': '950',
                }],
            },
            format='json', **self._entetes,
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)

        self.savon.refresh_from_db()
        self.assertEqual(self.savon.cost_price, Decimal('600.00'))
        self.assertEqual(self.savon.selling_price, Decimal('950.00'))

    def test_un_role_SANS_products_edit_est_refuse_PAR_CHAMP(self):
        """
        ⚠ LE CONTRÔLE : le refus doit rester une erreur de CHAMP, et porter sur
          la ligne. Un 403 dirait « vous ne pouvez pas créer de mouvement », ce
          qui est faux - il suffit de décocher la case.
        """
        self.client.force_authenticate(self.cashier_a)
        membership = OrganizationMembership.objects.get(
            user=self.cashier_a, organization=self.org,
        )
        membership.extra_permissions = ['stock_movements.create']
        membership.save(update_fields=['extra_permissions'])

        r = self.client.post(
            '/api/v1/stock-movements/bulk/',
            {
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'lines': [{
                    'product': str(self.savon.id),
                    'quantity': '10',
                    'unit_cost': '600',
                    'update_product_prices': True,
                }],
            },
            format='json', **self._entetes,
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        # L'index est la position dans l'envoi, telle que `create_bulk_supply`
        # la pose : un entier côté serveur, une chaîne après le rendu JSON.
        erreurs = {str(k): v for k, v in r.data['lines'].items()}
        self.assertIn('update_product_prices', erreurs['0'])
        self.assertFalse(StockMovement.objects.exists())
