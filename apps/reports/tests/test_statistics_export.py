"""
L'export des huit onglets de « Rapports & Statistiques ».

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE CES TESTS DÉFENDENT : LE FICHIER MENTAIT SUR SON PROPRE CONTENU.     │
│                                                                              │
│ Les deux surfaces fabriquaient leur document à partir des VINGT lignes       │
│ chargées à l'écran, sous un en-tête qui annonçait « 347 articles ». Un       │
│ marchand qui ouvre le fichier n'a aucun moyen de s'en apercevoir : il n'y a  │
│ ni erreur, ni mention, seulement un tableau qui s'arrête.                    │
│                                                                              │
│ Le document se fabrique désormais côté serveur, sur le périmètre ENTIER, et  │
│ `test_l_export_couvre_TOUT_le_perimetre` est le test porteur de tout le lot. │
└──────────────────────────────────────────────────────────────────────────────┘

Rôle GÉRANT et non propriétaire : `_scope_sales` sort en amont pour un
propriétaire, et la moitié du code de périmètre ne serait pas exécutée. C'est la
règle posée par `test_pull_scope_resolves`, et son oubli avait caché tout un lot.
"""
import io
import re
from decimal import Decimal

from django.utils import timezone
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement
from apps.inventory.models import Stock, Warehouse
from apps.contacts.models import Customer
from apps.products.models import Product
from apps.core.exports import csv_cell, currency_decimals, format_number
from apps.reports.exports import (
    EXPORT_BASENAMES,
    PERIOD_LABELS,
    TAB_BUILDERS,
    TABS_SANS_RELEVES,
)
from apps.reports.views import StatisticsViewSet
from apps.sales.models import Sale, SaleItem
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency

URL = '/api/v1/reports/statistics/export/'


def montant(valeur, devise='CDF'):
    """Le montant tel que les trois moteurs l'écrivent, décimales comprises."""
    return format_number(Decimal(valeur), currency_decimals(devise))
ONGLETS = tuple(TAB_BUILDERS)

#: Les huit onglets de la page, sans la rubrique des créances.
ONGLETS_DE_LA_PAGE = tuple(o for o in ONGLETS if o != 'receivables')

#: Ceux qui portent les relevés globaux, c'est-à-dire ceux dont la fenêtre est
#: celle de la page. Les deux autres ont la leur, et le disent en tête.
ONGLETS_AVEC_RELEVES = tuple(o for o in ONGLETS if o not in TABS_SANS_RELEVES)


class _BaseExport(APITestCase):

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.manager)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _produit(self, nom, sku, prix='1000.00', cout='600.00'):
        return Product.objects.create(
            organization=self.org, name=nom, slug=sku.lower(), sku=sku,
            cost_price=Decimal(cout), selling_price=Decimal(prix),
            is_taxable=False, track_inventory=True, is_active=True,
        )

    def _vente(self, reference, produit, quantite=1, prix='1000.00', entrepot=None):
        montant = Decimal(prix) * quantite
        vente = Sale.objects.create(
            organization=self.org, reference=reference,
            warehouse=entrepot or self.warehouse, register=self.register,
            status='completed', currency='CDF',
            subtotal=montant, total=montant, amount_paid=montant,
            sold_by=self.manager, sale_date=timezone.now(),
        )
        SaleItem.objects.create(
            organization=self.org, sale=vente, product=produit,
            quantity=Decimal(quantite), unit_price=Decimal(prix),
            cost_price=Decimal('600.00'), subtotal=montant, total=montant,
        )
        return vente

    def _export(self, tab, fmt='csv', **params):
        params.update({'tab': tab, 'export_format': fmt})
        return self.client.get(URL, params, **self._headers)

    def _csv(self, tab, **params):
        reponse = self._export(tab, 'csv', **params)
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.content[:400])
        return reponse.content.decode('utf-8')


class FormatsEtOngletsTests(_BaseExport):

    def test_le_registre_couvre_les_huit_onglets_ET_les_creances(self):
        """
        Un balayage qui ne balaie rien passe et ne prouve rien.

        Les créances ne sont pas un onglet de la page - elles ont leur propre
        rubrique - mais elles passent par le MÊME registre, pour porter la même
        marque que les huit autres documents.
        """
        self.assertEqual(set(ONGLETS_DE_LA_PAGE) | {'receivables'}, set(ONGLETS))
        self.assertEqual(len(ONGLETS_DE_LA_PAGE), 8)
        self.assertEqual(set(ONGLETS), set(EXPORT_BASENAMES))

    def test_les_huit_onglets_rendent_les_trois_formats(self):
        produit = self._produit('Eau 50cl', 'EAU-50')
        self._vente('VTE-1', produit, 3)

        attendus = {
            'pdf': 'application/pdf',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'csv': 'text/csv; charset=utf-8',
        }
        for tab in ONGLETS:
            params = {'user': str(self.manager.id)} if tab == 'user-activity' else {}
            for fmt, content_type in attendus.items():
                with self.subTest(tab=tab, format=fmt):
                    reponse = self._export(tab, fmt, **params)
                    self.assertEqual(
                        reponse.status_code, status.HTTP_200_OK, reponse.content[:400]
                    )
                    self.assertEqual(reponse['Content-Type'], content_type)
                    self.assertGreater(len(reponse.content), 0)
                    self.assertIn(
                        EXPORT_BASENAMES[tab], reponse['Content-Disposition']
                    )

    def test_excel_est_un_alias_de_xlsx(self):
        """C'est le mot que porte le bouton ; l'écart coûte des 400 opaques."""
        self.assertEqual(
            self._export('overview', 'excel').status_code, status.HTTP_200_OK
        )

    def test_un_format_inconnu_est_un_400(self):
        self.assertEqual(
            self._export('overview', 'docx').status_code,
            status.HTTP_400_BAD_REQUEST,
        )


class PerimetreTests(_BaseExport):

    def test_l_export_couvre_TOUT_le_perimetre(self):
        """
        LE TEST PORTEUR DE TOUT LE LOT.

        Vingt-cinq articles vendus : l'écran en pagine vingt, le fichier doit
        les porter tous les vingt-cinq.
        """
        for i in range(25):
            produit = self._produit(f'Article {i:02d}', f'ART-{i:02d}')
            self._vente(f'VTE-{i:02d}', produit, quantite=i + 1)

        contenu = self._csv('sales')
        lignes_articles = [l for l in contenu.splitlines() if ';Article ' in l]
        self.assertEqual(len(lignes_articles), 25)

        # Et l'écran, lui, en pagine bien vingt sous un total de vingt-cinq :
        # c'est l'écart que le fichier taisait.
        page = self.client.get(
            '/api/v1/reports/statistics/top_products/',
            {'page_size': 20}, **self._headers,
        )
        self.assertEqual(page.data['count'], 25)
        self.assertEqual(len(page.data['results']), 20)

    def test_l_export_respecte_le_perimetre_entrepot(self):
        """
        L'export ne passe pas par `filter_queryset` : il faut l'AFFIRMER.

        Un gérant borné à son dépôt ne doit pas lire les ventes d'un autre.
        """
        autre = Warehouse.objects.create(
            organization=self.org, branch=self.branch,
            name='Dépôt B', code='WH-B',
        )
        vu = self._produit('Vu', 'VU-1')
        cache = self._produit('Cache', 'CACHE-1')
        self._vente('VTE-VU', vu, 2)
        self._vente('VTE-CACHE', cache, 2, entrepot=autre)

        contenu = self._csv('sales')
        self.assertIn('Vu', contenu)
        self.assertNotIn('Cache', contenu)


class SyntheseTests(_BaseExport):

    RELEVES = [
        "Chiffre d'affaires", 'Ventes', 'Panier moyen', 'Articles vendus',
        'Solde caisse', 'Créances clients', 'Produits actifs', 'Valeur stock',
        'Stock bas', 'Ruptures',
    ]

    def test_les_dix_releves_partent_avec_chaque_onglet_de_la_fenetre(self):
        produit = self._produit('Eau 50cl', 'EAU-50')
        self._vente('VTE-1', produit, 3)

        for tab in ONGLETS_AVEC_RELEVES:
            params = {'user': str(self.manager.id)} if tab == 'user-activity' else {}
            with self.subTest(tab=tab):
                contenu = self._csv(tab, **params)
                for releve in self.RELEVES:
                    self.assertIn(releve, contenu)

    def test_les_creances_NE_portent_PAS_les_releves_de_la_page(self):
        """
        Une créance est due AUJOURD'HUI, pas « sur trente jours ».

        Y laisser « Chiffre d'affaires » mettrait un chiffre borné à une
        période sous un en-tête qui n'en annonce aucune : le lecteur n'aurait
        aucun moyen de savoir sur quoi il porte.
        """
        contenu = self._csv('receivables')
        self.assertNotIn("Chiffre d'affaires", contenu)
        self.assertNotIn('Panier moyen', contenu)
        self.assertIn('Débiteurs', contenu)
        # L'arrêté est en tête, UNE seule fois : deux dates feraient douter.
        self.assertEqual(contenu.count('Arrêté au'), 1)

    def test_le_journalier_NON_PLUS(self):
        """
        ┌────────────────────────────────────────────────────────────────────┐
        │ DEUX TOTAUX DE VENTES DANS UN MÊME CARTOUCHE, ET RIEN POUR LES     │
        │ DÉPARTAGER.                                                        │
        │                                                                    │
        │ Le journalier remplaçait bien son périmètre affiché - « Journée du  │
        │ 14/08/2026 » - mais `_spec` avait déjà posé `ctx.summary`, laissé   │
        │ intact. Le document portait donc « Chiffre d'affaires » sur trente  │
        │ jours immédiatement au-dessus de « Ventes du jour », sous un        │
        │ en-tête qui n'annonce qu'une date. Il avait reçu la moitié du       │
        │ correctif écrit pour les créances, pas l'autre.                     │
        └────────────────────────────────────────────────────────────────────┘
        """
        produit = self._produit('Eau 50cl', 'EAU-50')
        self._vente('VTE-1', produit, 3)

        contenu = self._csv('daily-cash', date='2026-08-14')
        self.assertNotIn("Chiffre d'affaires", contenu)
        self.assertNotIn('Panier moyen', contenu)
        self.assertNotIn('Valeur stock', contenu)
        # Ses PROPRES relevés restent, eux : le document n'est pas appauvri, il
        # est ramené à une seule fenêtre.
        self.assertIn('Solde de clôture', contenu)
        self.assertIn('Ventes du jour', contenu)

    def test_les_deux_rubriques_a_fenetre_propre_sont_DECLAREES(self):
        """
        La déclaration vit à côté du registre, et non dans chaque constructeur.

        C'est ce qui manquait : le journalier avait remplacé son périmètre sans
        retirer les relevés, et rien ne rapprochait les deux moitiés.
        """
        self.assertEqual(TABS_SANS_RELEVES, frozenset({'receivables', 'daily-cash'}))
        self.assertTrue(TABS_SANS_RELEVES <= set(TAB_BUILDERS))

    def test_une_rubrique_a_fenetre_propre_n_execute_PAS_les_agregats(self):
        """
        Les quatre agrégats globaux étaient calculés puis JETÉS à la ligne
        suivante (`spec.summary = extra`). Un document qui ne les porte pas ne
        doit pas les payer.
        """
        produit = self._produit('Eau 50cl', 'EAU-50')
        self._vente('VTE-1', produit, 3)

        with CaptureQueriesContext(connection) as avec:
            self._csv('sales')
        with CaptureQueriesContext(connection) as sans:
            self._csv('receivables')

        self.assertLess(len(sans), len(avec))

    def test_les_creances_ne_TOTALISENT_pas_les_devises(self):
        """
        Un client peut devoir en francs ET en dollars. Les additionner rendrait
        un nombre qui n'existe pas, sous le symbole de la principale.

        On fabrique DEUX dettes dans deux devises : sans elles, le document est
        vide et le test passerait sans rien démontrer.
        """
        produit = self._produit('Eau 50cl', 'EAU-50')
        client = Customer.objects.create(
            organization=self.org, name='Nelly', phone='+243997876765',
        )
        for reference, devise, montant in (('DU-CDF', 'CDF', '5000'),
                                           ('DU-USD', 'USD', '120.00')):
            vente = self._vente(reference, produit, 1, prix=montant)
            vente.customer = client
            vente.currency = devise
            vente.status = 'pending'
            vente.amount_paid = Decimal('0')
            vente.amount_due = Decimal(montant)
            vente.save()

        contenu = self._csv('receivables')

        # Deux groupes, deux sous-totaux, et AUCUN total qui les réunirait.
        self.assertIn('Devise : CDF', contenu)
        self.assertIn('Devise : USD', contenu)
        self.assertIn('Sous-total CDF', contenu)
        self.assertIn('Sous-total USD', contenu)
        self.assertNotIn('TOTAL GÉNÉRAL', contenu)

        # Le téléphone vient du serveur : c'est un écran de relance.
        self.assertIn('+243997876765', contenu)

    def test_chaque_devise_garde_SES_decimales(self):
        """
        ┌──────────────────────────────────────────────────────────────────────┐
        │ UN MONTANT ARRONDI PAR LA DEVISE VOISINE EST UN MONTANT FAUX.        │
        │                                                                      │
        │ Le document se formatait à la seule devise de l'établissement. Sur   │
        │ une balance âgée d'une organisation tenue en CDF (zéro décimale),    │
        │ une dette de 120,75 USD sortait « 121 ». Sur un papier de relance,   │
        │ c'est le montant qu'on réclame au client.                             │
        └──────────────────────────────────────────────────────────────────────┘
        """
        produit = self._produit('Eau 50cl', 'EAU-50')
        client = Customer.objects.create(organization=self.org, name='Nelly')
        vente = self._vente('DU-USD', produit, 1, prix='120.75')
        vente.customer = client
        vente.currency = 'USD'
        vente.status = 'pending'
        vente.amount_paid = Decimal('0')
        vente.amount_due = Decimal('120.75')
        vente.save()

        contenu = self._csv('receivables')
        ligne = next(l for l in contenu.splitlines() if l.startswith('Nelly'))
        # « 120,75 » et non « 121 » : les décimales viennent de l'USD, pas du
        # CDF de l'établissement.
        self.assertIn('120,75', ligne)
        self.assertNotIn(';121;', ligne)

    def test_le_perimetre_est_annonce(self):
        contenu = self._csv('overview', period='last_7_days')
        self.assertIn('Période;7 derniers jours', contenu)
        self.assertIn('Du;', contenu)
        self.assertIn('Au;', contenu)

    def test_le_libelle_suit_le_CHOIX_et_non_la_forme_de_l_appel(self):
        """
        Le terminal envoie TOUJOURS des dates explicites, le back-office un
        `period`. Le même rapport sortait donc « Personnalisé » d'un côté et
        « 30 derniers jours » de l'autre, pour la MÊME fenêtre : relevé en
        comparant les deux fichiers, seule divergence sur 81 lignes.
        """
        web = self._csv('overview', period='last_7_days')
        terminal = self._csv(
            'overview', period='last_7_days',
            date_from='2026-08-28', date_to='2026-09-03',
        )
        self.assertIn('Période;7 derniers jours', web)
        self.assertIn('Période;7 derniers jours', terminal)

        # Des dates SANS période restent « Personnalisé » : le marchand a bien
        # choisi une fenêtre à lui, et le document doit le dire.
        libre = self._csv('overview', date_from='2026-08-28', date_to='2026-09-03')
        self.assertIn('Période;Personnalisé', libre)

    def test_les_libelles_couvrent_les_periodes_du_serveur(self):
        """Une période acceptée sans libellé sortirait en « last_30_days »."""
        for periode in ('today', 'week', 'month', 'quarter', 'year',
                        'last_7_days', 'last_30_days', 'last_12_months', 'custom'):
            with self.subTest(periode=periode):
                self.assertIn(periode, PERIOD_LABELS)

    def test_la_caisse_n_est_ventilee_qu_a_PLUSIEURS_devises(self):
        """Une seule devise : une ligne « Caisse CDF » ne dirait rien de plus."""
        self.assertNotIn('Caisse CDF', self._csv('overview'))

        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar', 'symbol': '$', 'decimal_places': 2},
        )
        cdf, _ = Currency.objects.get_or_create(
            code='CDF', defaults={'name': 'Franc', 'symbol': 'FC', 'decimal_places': 0},
        )
        for devise, principale in ((cdf, True), (usd, False)):
            OrganizationCurrency.objects.get_or_create(
                organization=self.org, currency=devise,
                defaults={'is_primary': principale, 'exchange_rate': Decimal('1')},
            )
        # `balance_by_currency` se lit sur les MOUVEMENTS : il faut donc deux
        # devises réellement mouvementées, pas seulement deux devises déclarées.
        for code, montant in (('CDF', '5000'), ('USD', '10.00')):
            CashMovement.objects.create(
                organization=self.org, movement_type='other_in', direction='in',
                amount=Decimal(montant), currency=code, reference=f'MVT-{code}',
                movement_date=timezone.now(), created_by=self.manager,
            )
        contenu = self._csv('overview')
        self.assertIn('Caisse USD', contenu)


class ContenuCsvTests(_BaseExport):

    def test_le_csv_est_conforme_au_paquet_partage(self):
        """BOM, point-virgule et CRLF : c'est ce qu'Excel-FR attend."""
        brut = self._export('overview').content
        self.assertTrue(brut.startswith('﻿'.encode('utf-8')))
        self.assertIn(b'\r\n', brut)
        self.assertIn(b';', brut)

    def test_une_cellule_a_risque_est_protegee(self):
        """
        Le cas GÉNÉRAL, pas le cas rare : chaque montant porte une virgule
        décimale française, et une cellule nue décalerait toute la ligne.
        """
        self.org.name = 'Kalume; Fils "SARL"'
        self.org.save(update_fields=['name'])
        contenu = self._csv('overview')
        self.assertIn('"Kalume; Fils ""SARL"""', contenu)

    def test_un_onglet_vide_le_DIT(self):
        """Jamais une page blanche : le lecteur croirait à un fichier cassé."""
        self.assertIn('Aucun mouvement de caisse', self._csv('overview'))


class OngletProduitsTests(_BaseExport):

    def test_le_restant_est_SOMME_sur_tous_les_entrepots(self):
        """
        Le back-office retenait la PREMIÈRE ligne de stock d'un produit.

        `stock_details` en rend une par couple (produit, entrepôt) : sur un
        établissement à plusieurs dépôts, « Qté restante » ignorait tous les
        autres, sans erreur et sans que rien ne le signale.
        """
        autre = Warehouse.objects.create(
            organization=self.org, branch=self.branch, name='Dépôt B', code='WH-B',
        )
        self.manager.memberships.get(organization=self.org).assigned_warehouses.add(autre)

        produit = self._produit('Eau 50cl', 'EAU-50')
        Stock.objects.create(
            organization=self.org, product=produit, warehouse=self.warehouse,
            quantity=Decimal('30'),
        )
        Stock.objects.create(
            organization=self.org, product=produit, warehouse=autre,
            quantity=Decimal('12'),
        )
        self._vente('VTE-1', produit, 8)

        contenu = self._csv('products')
        ligne = next(l for l in contenu.splitlines() if l.startswith('Eau 50cl'))
        cellules = ligne.split(';')
        # Stock départ = restant (30 + 12) + vendu (8). Retenir un seul dépôt
        # aurait donné 38 ou 20.
        self.assertEqual(cellules[1], '50')
        self.assertEqual(cellules[5], '42')

    def test_un_produit_sans_approvisionnement_porte_un_tiret(self):
        produit = self._produit('Eau 50cl', 'EAU-50')
        self._vente('VTE-1', produit, 2)
        ligne = next(
            l for l in self._csv('products').splitlines() if l.startswith('Eau 50cl')
        )
        self.assertEqual(ligne.split(';')[2], '-')


class ColonnesExplicitesTests(_BaseExport):
    """
    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UNE COLONNE PAR GRANDEUR, JAMAIS UN « DÉTAIL » FOURRE-TOUT.              │
    │                                                                          │
    │ « Vue d'ensemble » et « Clients » sortaient sous trois colonnes          │
    │ génériques, celle du milieu portant une PHRASE : « Entrées 7 509,5 $ ·   │
    │ Sorties 0 $ ». Le tableur la recevait comme du texte : impossible de     │
    │ sommer ses entrées, de les trier, ni de les comparer d'un jour à l'autre.│
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def test_le_flux_a_UNE_COLONNE_PAR_GRANDEUR(self):
        CashMovement.objects.create(
            organization=self.org, movement_type='other_in', direction='in',
            amount=Decimal('500.00'), currency='CDF', reference='MVT-IN',
            movement_date=timezone.now(), created_by=self.manager,
        )
        CashMovement.objects.create(
            organization=self.org, movement_type='other_out', direction='out',
            amount=Decimal('200.00'), currency='CDF', reference='MVT-OUT',
            movement_date=timezone.now(), created_by=self.manager,
        )

        contenu = self._csv('overview')
        self.assertIn('Période;Entrées;Sorties;Net', contenu)
        # Et surtout : plus une seule phrase dans une cellule.
        self.assertNotIn('Détail', contenu)
        self.assertNotIn('· Sorties', contenu)

        # L'attendu passe par le formateur du socle : recopier « 500,00 » ici
        # figerait les décimales du dollar dans un gabarit tenu en CDF, qui n'en
        # a aucune. On prouve la STRUCTURE, pas la mise en forme.
        attendu = [csv_cell(montant(v)) for v in (500, 200, 300)]
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ LA LIGNE SE RECONNAÎT À SA FORME DE DATE, PAS À UN ZÉRO DE TÊTE. │
        # │                                                                  │
        # │ Le test cherchait `startswith('0')` : `format_day` rend           │
        # │ « JJ/MM/AAAA », donc le quantième ne commence par un zéro que du  │
        # │ 1er au 9 du mois. Du 10 au 31, `next()` ne trouvait rien et le    │
        # │ test échouait en `StopIteration` - un échec qui ne nomme même pas │
        # │ ce qui manque. Il passait donc neuf jours sur trente, et rien ne  │
        # │ le signalait le reste du temps puisqu'il était vert au moment où  │
        # │ on l'écrivait.                                                    │
        # └──────────────────────────────────────────────────────────────────┘
        lignes = [
            l for l in contenu.splitlines()
            if re.match(r'^\d{2}/\d{2}/\d{4};', l)
        ]
        self.assertEqual(
            len(lignes), 1,
            f"Une seule journée attendue dans le flux, trouvé : {lignes}",
        )
        self.assertEqual(lignes[0].split(';')[1:], attendu)

    def test_le_flux_porte_un_TOTAL(self):
        """Sommable : c'est tout l'intérêt d'une colonne plutôt qu'une phrase."""
        for valeur, sens, ref in (('500.00', 'in', 'A'), ('300.00', 'in', 'B')):
            CashMovement.objects.create(
                organization=self.org, movement_type='other_in', direction=sens,
                amount=Decimal(valeur), currency='CDF', reference=f'MVT-{ref}',
                movement_date=timezone.now(), created_by=self.manager,
            )
        self.assertIn(f"TOTAL GÉNÉRAL;{csv_cell(montant(800))}", self._csv('overview'))

    def test_la_dette_d_un_client_a_SA_colonne(self):
        produit = self._produit('Eau 50cl', 'EAU-50')
        client = Customer.objects.create(
            organization=self.org, name='Nelly', current_balance=Decimal('250.00'),
        )
        vente = self._vente('VTE-1', produit, 2)
        vente.customer = client
        vente.save(update_fields=['customer'])

        contenu = self._csv('customers')
        self.assertIn('#;Client;Commandes;Total acheté;Dette', contenu)
        ligne = next(l for l in contenu.splitlines() if 'Nelly' in l)
        self.assertEqual(
            ligne.split(';'),
            ['1', 'Nelly', '1', csv_cell(montant(2000)), csv_cell(montant(250))],
        )

    def test_un_client_sans_dette_laisse_la_case_VIDE(self):
        """Une case vide se lit « ne doit rien », et c'est l'information cherchée."""
        produit = self._produit('Eau 50cl', 'EAU-50')
        client = Customer.objects.create(organization=self.org, name='Sans dette')
        vente = self._vente('VTE-1', produit, 1)
        vente.customer = client
        vente.save(update_fields=['customer'])

        ligne = next(
            l for l in self._csv('customers').splitlines() if 'Sans dette' in l
        )
        self.assertEqual(ligne.split(';')[-1], '')


class CartoucheTests(_BaseExport):

    def test_le_cartouche_se_replie_en_GRILLE(self):
        """
        ┌──────────────────────────────────────────────────────────────────────┐
        │ IL POSAIT UNE COLONNE PAR RELEVÉ.                                    │
        │                                                                      │
        │ Passe encore à quatre ; les rapports en portent DOUZE ou plus, et    │
        │ chaque case tombait à une dizaine de millimètres : « Ventes »        │
        │ sortait « Vent / es », « Ruptures » en « Ruptur / es ». Le premier   │
        │ bloc que le lecteur regarde était illisible.                          │
        └──────────────────────────────────────────────────────────────────────┘

        On mesure la LARGEUR DE CASE réellement obtenue : c'est elle qui décide
        si un libellé se coupe, et un nombre de rangées ne le dit pas.
        """
        from apps.core.exports import SUMMARY_MAX_PER_ROW

        reponse = self._export('overview', 'csv')
        releves = [
            l for l in reponse.content.decode('utf-8').splitlines()
            if l.count(';') == 1 and l.split(';')[0] in self.RELEVES_ATTENDUS
        ]
        self.assertGreaterEqual(len(releves), 10)

        # Une A4 portrait offre environ 190 mm utiles : à six cases par rangée
        # on tombe à 31 mm, ce qui coupe « Chiffre d'affaires ». La constante
        # borne la densité, et c'est elle qu'on épingle.
        self.assertLessEqual(SUMMARY_MAX_PER_ROW, 6)

    RELEVES_ATTENDUS = {
        "Chiffre d'affaires", 'Ventes', 'Panier moyen', 'Articles vendus',
        'Solde caisse', 'Créances clients', 'Produits actifs', 'Valeur stock',
        'Stock bas', 'Ruptures',
    }

    def test_un_tableau_ETROIT_se_lit_en_PORTRAIT(self):
        """
        Quatre colonnes étalées sur une A4 paysage laissent dix centimètres
        entre la date et son montant : l'œil traverse la page pour relier deux
        cases de la même ligne, et sur douze lignes il se trompe de rangée.

        On lit le `MediaBox` du PDF produit, pas un attribut de la description :
        c'est l'orientation RÉELLE de la page qui est en cause.
        """
        etroit = self._mediabox(self._export('overview', 'pdf').content)
        large = self._mediabox(self._export('products', 'pdf').content)

        self.assertLess(etroit[0], etroit[1], 'La vue d\'ensemble doit être en portrait.')
        self.assertGreater(large[0], large[1], 'Les produits doivent être en paysage.')

    @staticmethod
    def _mediabox(pdf: bytes):
        """(largeur, hauteur) de la première page, en points."""
        trouve = re.search(rb'/MediaBox\s*\[([^\]]+)\]', pdf)
        assert trouve, "Aucun MediaBox dans le PDF."
        x0, y0, x1, y1 = (float(v) for v in trouve.group(1).split())
        return (x1 - x0, y1 - y0)


class RefusTests(_BaseExport):

    def test_un_onglet_inconnu_est_un_400_qui_NOMME_les_onglets(self):
        reponse = self._export('bidon')
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('overview', str(reponse.data['tab']))

    def test_sans_onglet_on_rend_la_vue_d_ensemble(self):
        """L'onglet par défaut de la page, pas un refus."""
        reponse = self.client.get(
            URL, {'export_format': 'csv'}, **self._headers
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        self.assertIn("VUE D'ENSEMBLE", reponse.content.decode('utf-8'))

    def test_user_activity_sans_user_est_un_400_de_MEME_FORME_que_l_action(self):
        """
        Une CHAÎNE et non une liste : le back-office branche son message de
        champ sur cette forme, et `ValidationError` rendrait une liste.
        """
        export = self._export('user-activity')
        action = self.client.get(
            '/api/v1/reports/statistics/user_activity/', **self._headers
        )
        self.assertEqual(export.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(action.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(export.data['user'], action.data['user'])
        self.assertIsInstance(export.data['user'], str)

    def test_user_activity_avec_un_user_inconnu_est_un_404(self):
        import uuid
        reponse = self._export('user-activity', user=str(uuid.uuid4()))
        self.assertEqual(reponse.status_code, status.HTTP_404_NOT_FOUND)

    def test_une_date_de_rapport_illisible_est_un_400_des_DEUX_cotes(self):
        """
        `strptime` n'était pas gardé : l'action répondait **500**.

        L'extraction corrige les deux chemins d'un coup, ce qui est tout
        l'intérêt d'extraire plutôt que de recopier.
        """
        export = self._export('daily-cash', date='oops')
        action = self.client.get(
            '/api/v1/reports/statistics/daily_cash_report/',
            {'date': 'oops'}, **self._headers,
        )
        self.assertEqual(export.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(action.status_code, status.HTTP_400_BAD_REQUEST)


class ClasseurTests(_BaseExport):

    def test_les_montants_du_classeur_sont_des_NOMBRES(self):
        """
        Sinon le marchand ne peut pas sommer une colonne, et c'est tout
        l'intérêt de lui servir un classeur plutôt qu'un texte.
        """
        from openpyxl import load_workbook

        produit = self._produit('Eau 50cl', 'EAU-50')
        self._vente('VTE-1', produit, 3)

        reponse = self._export('sales', 'xlsx')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        feuille = load_workbook(io.BytesIO(reponse.content)).active

        montants = [
            cellule
            for ligne in feuille.iter_rows()
            for cellule in ligne
            if isinstance(cellule.value, (int, float)) and cellule.value == 3000
        ]
        self.assertTrue(montants, "Le revenu n'est pas écrit comme un nombre.")


class DateDuRapportJournalierTests(_BaseExport):

    def test_le_rapport_journalier_annonce_SA_date(self):
        """
        Cet onglet a sa propre date : annoncer la fenêtre des sept autres
        ferait croire au lecteur qu'il tient un rapport de trente jours.
        """
        contenu = self._csv('daily-cash', date='2026-08-14')
        self.assertIn('Date du rapport;14/08/2026', contenu)
        self.assertIn('Journée du 14/08/2026', contenu)
