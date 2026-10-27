"""
Tests de la dette fournisseur.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE FICHIER EXISTE POUR UN DÉFAUT QUI NE SE VOYAIT PAS.                      │
│                                                                              │
│ Avant ce lot, `supplier.current_balance` n'était écrit QUE pour BAISSER :    │
│ le règlement, le retour, et l'annulation en sens inverse. Rien ne le         │
│ créditait à la réception. Payer un fournisseur rendait donc son solde        │
│ négatif, indéfiniment, et le marchand lisait un crédit là où il avait une    │
│ dette. Aucune erreur, aucun journal : juste un nombre plausible et faux.     │
│                                                                              │
│ Les tests sont écrits dans l'ordre du PARCOURS - on reçoit, on paie, on      │
│ rend, on annule - parce que c'est le CUMUL qui dérive, pas une écriture      │
│ isolée.                                                                      │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.test import TestCase, TransactionTestCase

from apps.contacts.models import Supplier, SupplierBalance, SupplierTransaction
from apps.contacts.supplier_debt import (
    balances_by_currency, get_balance, recompute_primary_balance,
)
from apps.products.models import Product
from apps.purchases.models import (
    GoodsReceipt, GoodsReceiptItem, PurchaseOrder, PurchaseOrderItem,
    PurchaseReturn, PurchaseReturnItem,
)
from apps.purchases.services import (
    TransitionReceptionRefusee, completer_reception, enregistrer_reglement,
    expedier_retour, valeur_recue,
)
from apps.sales.tests._helpers import make_org_with_users


class _Socle(TestCase):
    """Une commande de 5 bidons à 8 000, en CDF (devise principale de la fixture)."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.product = Product.objects.create(
            organization=self.org, name='Bidon huile 5L', sku='OIL-5L',
            cost_price=Decimal('8000.00'), selling_price=Decimal('10000.00'),
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        self.supplier = Supplier.objects.create(
            organization=self.org, name='Fournisseur Huile SARL', code='FRN-OIL',
        )
        self.po = PurchaseOrder.objects.create(
            organization=self.org, reference='CMD-TEST-0001',
            supplier=self.supplier, warehouse=self.warehouse,
            status='confirmed', order_date=date.today(), currency='CDF',
            subtotal=Decimal('40000.00'), total=Decimal('40000.00'),
            amount_due=Decimal('40000.00'),
        )
        self.po_item = PurchaseOrderItem.objects.create(
            organization=self.org, purchase_order=self.po, product=self.product,
            quantity_ordered=Decimal('5.000'), unit_price=Decimal('8000.00'),
            subtotal=Decimal('40000.00'), total=Decimal('40000.00'),
        )

    def _reception(self, acceptee, cout=Decimal('8000.00'), reference='BR-TEST-0001'):
        grn = GoodsReceipt.objects.create(
            organization=self.org, reference=reference,
            purchase_order=self.po, warehouse=self.warehouse,
            status='draft', receipt_date=date.today(), received_by=self.owner,
        )
        GoodsReceiptItem.objects.create(
            organization=self.org, goods_receipt=grn,
            purchase_order_item=self.po_item, product=self.product,
            quantity_received=acceptee, quantity_accepted=acceptee,
            unit_cost=cout,
        )
        return grn


class DetteInscriteALaReceptionTests(_Socle):

    def test_une_reception_validee_INSCRIT_la_dette(self):
        """Le défaut central : rien ne créditait le solde du fournisseur."""
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('0.00'))

        completer_reception(self._reception(Decimal('5.000')), self.owner)

        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('40000.00'))
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.current_balance, Decimal('40000.00'))

    def test_la_dette_porte_son_ecriture_au_grand_livre(self):
        """Un solde sans grand livre ne s'explique pas."""
        grn = self._reception(Decimal('5.000'))
        completer_reception(grn, self.owner)

        txn = SupplierTransaction.objects.get(supplier=self.supplier)
        self.assertEqual(txn.transaction_type, 'receipt')
        self.assertEqual(txn.amount, Decimal('40000.00'))
        self.assertEqual(txn.balance_before, Decimal('0.00'))
        self.assertEqual(txn.balance_after, Decimal('40000.00'))
        self.assertEqual(txn.goods_receipt_id, grn.id)
        self.assertEqual(txn.purchase_order_id, self.po.id)

    def test_une_reception_en_BROUILLON_n_inscrit_rien(self):
        """On ne doit rien pour une marchandise qu'on n'a pas constatée."""
        self._reception(Decimal('5.000'))
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('0.00'))

    def test_la_dette_suit_le_cout_de_la_RECEPTION_pas_le_prix_commande(self):
        """Le fournisseur facture ce qu'il a livré, pas ce qui avait été négocié."""
        completer_reception(
            self._reception(Decimal('5.000'), cout=Decimal('9000.00')), self.owner,
        )
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('45000.00'))

    def test_une_reception_PARTIELLE_ne_doit_que_ce_qui_est_entre(self):
        completer_reception(self._reception(Decimal('2.000')), self.owner)
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('16000.00'))


class LesConditionsDeLaCommandeVoyagentTests(_Socle):

    def test_la_remise_de_ligne_reduit_la_dette(self):
        self.po_item.discount_percentage = Decimal('10.00')
        self.po_item.save()
        completer_reception(self._reception(Decimal('5.000')), self.owner)
        # 40 000 - 10 % = 36 000
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('36000.00'))

    def test_la_taxe_de_ligne_creuse_la_dette(self):
        self.po_item.tax_rate = Decimal('16.00')
        self.po_item.save()
        completer_reception(self._reception(Decimal('5.000')), self.owner)
        # 40 000 + 16 % = 46 400
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('46400.00'))

    def test_le_PORT_s_inscrit_UNE_SEULE_FOIS(self):
        """Il est porté par l'enveloppe et n'est pas divisible."""
        self.po.shipping_cost = Decimal('5000.00')
        self.po.save()

        completer_reception(self._reception(Decimal('2.000')), self.owner)
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('21000.00'))

        completer_reception(
            self._reception(Decimal('3.000'), reference='BR-TEST-0002'), self.owner,
        )
        # 16 000 + 5 000 de port, puis 24 000 SANS second port.
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('45000.00'))


class LaQuantiteRecueBougeALaValidationTests(_Socle):

    def test_un_brouillon_n_avance_PAS_la_commande(self):
        """
        Défaut d'origine : `GoodsReceiptCreateSerializer` incrémentait
        `quantity_received` dès le brouillon. Une réception saisie puis oubliée
        gonflait `quantity_pending` pour toujours.
        """
        self._reception(Decimal('5.000'))
        self.po_item.refresh_from_db()
        self.assertEqual(self.po_item.quantity_received, Decimal('0.000'))
        self.assertEqual(self.po_item.quantity_pending, Decimal('5.000'))

    def test_la_validation_avance_la_commande(self):
        completer_reception(self._reception(Decimal('3.000')), self.owner)
        self.po_item.refresh_from_db()
        self.assertEqual(self.po_item.quantity_received, Decimal('3.000'))
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'partially_received')

    def test_le_statut_se_decide_PAR_ARTICLE_jamais_sur_une_somme(self):
        """
        Défaut d'origine : la vue comparait `Sum(reçu)` à `Sum(commandé)`. Un
        article livré en excédent compensait un article qui attendait encore, la
        commande passait `received`, et la ligne manquante disparaissait de
        « commandes à réceptionner ».
        """
        # ⚠ `slug` explicite : `Product` ne le dérive pas, et deux produits
        # sans slug heurtent `unique_product_slug_per_org` sur la chaîne vide.
        autre = Product.objects.create(
            organization=self.org, name='Savon', sku='SAV-1', slug='savon',
            cost_price=Decimal('500.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        autre_item = PurchaseOrderItem.objects.create(
            organization=self.org, purchase_order=self.po, product=autre,
            quantity_ordered=Decimal('10.000'), unit_price=Decimal('500.00'),
            subtotal=Decimal('5000.00'), total=Decimal('5000.00'),
        )

        grn = GoodsReceipt.objects.create(
            organization=self.org, reference='BR-TEST-0009',
            purchase_order=self.po, warehouse=self.warehouse,
            status='draft', receipt_date=date.today(), received_by=self.owner,
        )
        # La commande porte 5 bidons + 10 savons, soit 15 unités. On reçoit 15
        # BIDONS et aucun savon : la SOMME dit donc 15 reçus pour 15 commandés,
        # et l'ancien code en concluait « reçu ». Article par article, les dix
        # savons attendent toujours.
        GoodsReceiptItem.objects.create(
            organization=self.org, goods_receipt=grn,
            purchase_order_item=self.po_item, product=self.product,
            quantity_received=Decimal('15.000'), quantity_accepted=Decimal('15.000'),
            unit_cost=Decimal('8000.00'),
        )
        completer_reception(grn, self.owner)

        self.po.refresh_from_db()
        autre_item.refresh_from_db()
        self.assertEqual(autre_item.quantity_received, Decimal('0.000'))
        # La somme dit 15 >= 15. L'article par article dit qu'il manque les savons.
        self.assertEqual(self.po.status, 'partially_received')

    def test_une_reception_deja_validee_est_refusee(self):
        grn = self._reception(Decimal('5.000'))
        completer_reception(grn, self.owner)
        with self.assertRaises(TransitionReceptionRefusee):
            completer_reception(grn, self.owner)


class LeReglementEteintLaDetteTests(_Socle):

    def setUp(self):
        super().setUp()
        completer_reception(self._reception(Decimal('5.000')), self.owner)

    def test_un_reglement_reduit_la_dette(self):
        enregistrer_reglement(
            self.org, self.supplier, Decimal('30000.00'),
            payment_date=date.today(), currency='CDF', user=self.owner,
        )
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('10000.00'))

    def test_un_reglement_s_IMPUTE_sur_la_commande_la_plus_ancienne(self):
        """
        Sans imputation, le solde baisse et chaque `amount_due` reste intact :
        la somme des commandes dues ne correspond plus au solde qu'elle résume.
        """
        paiement = enregistrer_reglement(
            self.org, self.supplier, Decimal('30000.00'),
            payment_date=date.today(), currency='CDF', user=self.owner,
        )
        self.po.refresh_from_db()
        self.assertEqual(self.po.amount_paid, Decimal('30000.00'))
        self.assertEqual(self.po.amount_due, Decimal('10000.00'))
        self.assertEqual(paiement.allocations.count(), 1)

    def test_un_SURPAIEMENT_rend_le_solde_negatif_c_est_un_acompte(self):
        enregistrer_reglement(
            self.org, self.supplier, Decimal('50000.00'),
            payment_date=date.today(), currency='CDF', user=self.owner,
        )
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('-10000.00'))

    def test_la_reference_vient_du_compteur_verrouille(self):
        """
        Les trois générateurs d'origine triaient ALPHABÉTIQUEMENT et dataient
        leur préfixe en UTC. `allocate_document_number` n'a ni l'un ni l'autre.
        """
        paiement = enregistrer_reglement(
            self.org, self.supplier, Decimal('100.00'),
            payment_date=date.today(), currency='CDF', user=self.owner,
        )
        self.assertTrue(paiement.reference.startswith('RGF-'))


class LeRetourEteintLaDetteAvantDeRendreDeLArgentTests(_Socle):

    def setUp(self):
        super().setUp()
        completer_reception(self._reception(Decimal('5.000')), self.owner)

    def _retour(self, quantite, cout=Decimal('8000.00')):
        retour = PurchaseReturn.objects.create(
            organization=self.org, reference='RTF-TEST-0001',
            supplier=self.supplier, purchase_order=self.po,
            warehouse=self.warehouse, status='approved',
            reason='Bidons percés', return_date=date.today(),
            total_amount=quantite * cout, created_by=self.owner,
        )
        PurchaseReturnItem.objects.create(
            organization=self.org, purchase_return=retour, product=self.product,
            quantity=quantite, unit_cost=cout, total=quantite * cout,
        )
        return retour

    def test_un_retour_reduit_la_dette_sans_toucher_au_tiroir(self):
        """
        Défaut d'origine : la vue enregistrait une ENTRÉE de caisse du montant
        ENTIER *et* décrémentait le solde. Le même retour comptait deux fois.
        """
        from apps.cashbook.models import CashMovement

        expedier_retour(self._retour(Decimal('2.000')), self.owner)

        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('24000.00'))
        self.assertFalse(
            CashMovement.objects.filter(
                organization=self.org, movement_type='supplier_refund',
            ).exists(),
            "Un retour qui éteint une dette ne fait entrer aucun argent au tiroir.",
        )

    def test_seul_le_RELIQUAT_revient_en_caisse(self):
        from apps.cashbook.models import CashMovement

        # La dette est de 40 000 ; on rend pour 48 000.
        expedier_retour(self._retour(Decimal('6.000')), self.owner)

        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('0.00'))
        mvt = CashMovement.objects.get(
            organization=self.org, movement_type='supplier_refund',
        )
        self.assertEqual(mvt.amount, Decimal('8000.00'))

    def test_un_retour_sans_dette_revient_ENTIEREMENT_en_caisse(self):
        from apps.cashbook.models import CashMovement

        enregistrer_reglement(
            self.org, self.supplier, Decimal('40000.00'),
            payment_date=date.today(), currency='CDF', user=self.owner,
        )
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('0.00'))

        expedier_retour(self._retour(Decimal('2.000')), self.owner)

        mvt = CashMovement.objects.get(
            organization=self.org, movement_type='supplier_refund',
        )
        self.assertEqual(mvt.amount, Decimal('16000.00'))


class LeSoldeEstTENU_PAR_DEVISE_Tests(_Socle):
    """
    Un fournisseur facturé en deux monnaies a deux dettes, et elles ne
    s'additionnent pas. C'est la règle du dépôt, et le côté client l'a déjà
    payée : « un client devant en USD et payant en francs ne voyait pas sa dette
    bouger, l'argent partait en avance CDF ».
    """

    def test_deux_devises_tiennent_deux_lignes_distinctes(self):
        from apps.contacts.supplier_debt import apply_debt

        apply_debt(self.supplier, Decimal('40000.00'), currency='CDF')
        apply_debt(self.supplier, Decimal('50.00'), currency='USD',
                   exchange_rate=Decimal('2800'))

        self.assertEqual(
            balances_by_currency(self.supplier),
            {'CDF': Decimal('40000.00'), 'USD': Decimal('50.00')},
        )
        self.assertEqual(SupplierBalance.objects.filter(supplier=self.supplier).count(), 2)

    def test_un_reglement_en_francs_NE_TOUCHE_PAS_la_dette_en_dollars(self):
        from apps.contacts.supplier_debt import apply_debt, settle_debt

        apply_debt(self.supplier, Decimal('40000.00'), currency='CDF')
        apply_debt(self.supplier, Decimal('50.00'), currency='USD',
                   exchange_rate=Decimal('2800'))

        settle_debt(self.supplier, Decimal('40000.00'), currency='CDF')

        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('0.00'))
        self.assertEqual(get_balance(self.supplier, 'USD'), Decimal('50.00'))

    def test_le_scalaire_converti_resume_les_deux_lignes(self):
        """
        `Supplier.current_balance` est une VUE agrégée. Il ne remplace pas les
        lignes par devise ; il les résume pour le tri et les rapports.
        """
        from apps.contacts.supplier_debt import apply_debt

        apply_debt(self.supplier, Decimal('40000.00'), currency='CDF')
        total = recompute_primary_balance(self.supplier, save=False)
        # La devise principale de la fixture est le CDF : la conversion est
        # neutre, et c'est ce qui rend ce contrôle lisible.
        self.assertEqual(total, Decimal('40000.00'))


class LaValorisationTests(_Socle):

    def test_une_ligne_refusee_en_entier_ne_doit_rien(self):
        grn = GoodsReceipt.objects.create(
            organization=self.org, reference='BR-TEST-0100',
            purchase_order=self.po, warehouse=self.warehouse,
            status='draft', receipt_date=date.today(), received_by=self.owner,
        )
        GoodsReceiptItem.objects.create(
            organization=self.org, goods_receipt=grn,
            purchase_order_item=self.po_item, product=self.product,
            quantity_received=Decimal('5.000'),
            quantity_accepted=Decimal('0.000'),
            quantity_rejected=Decimal('5.000'),
            unit_cost=Decimal('8000.00'),
        )
        self.assertEqual(valeur_recue(grn, inclure_port=False), Decimal('0.00'))


class LeSoldeNeCOUTE_PAS_UNE_REQUETE_PAR_LIGNE_Tests(TestCase):
    """
    Le budget de requêtes de la liste des fournisseurs.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `balances_by_currency` ÉMET UN `filter()`, DONC IL COURT-CIRCUITE LE     │
    │ PRÉCHARGEMENT.                                                           │
    │                                                                          │
    │ Exposer les soldes par devise sur la LISTE est nécessaire - un scalaire  │
    │ converti ne se lit dans aucune monnaie - mais le lire par ce helper      │
    │ faisait une requête par fournisseur, et le `prefetch_related` était payé │
    │ puis jeté. C'est le défaut exact mesuré sur `assigned_warehouses` en     │
    │ août : « le préchargement était payé puis jeté, à chaque appel ».        │
    └──────────────────────────────────────────────────────────────────────────┘

    Le test épingle l'INVARIANT - le coût ne croît pas avec le nombre de
    fournisseurs - et non un nombre absolu, qu'on relâcherait au premier champ
    ajouté.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        from apps.contacts.supplier_debt import apply_debt

        self.fournisseurs = []
        for i in range(6):
            s = Supplier.objects.create(
                organization=self.org, name=f'Fournisseur {i}', code=f'FRN-{i}',
            )
            # Deux devises chacun : c'est le cas qui multiplie les lignes.
            apply_debt(s, Decimal('1000.00'), currency='CDF')
            apply_debt(s, Decimal('10.00'), currency='USD',
                       exchange_rate=Decimal('2800'))
            self.fournisseurs.append(s)

    def _requetes(self, nombre):
        from apps.contacts.serializers import SupplierListSerializer
        from apps.contacts.models import Supplier as S

        qs = S.objects.filter(
            organization=self.org
        ).prefetch_related('balances')[:nombre]
        with self.assertNumQueries(2) as ctx:
            donnees = SupplierListSerializer(list(qs), many=True).data
        self.assertEqual(len(donnees), nombre)
        return ctx

    def test_le_cout_ne_croit_pas_avec_le_nombre_de_fournisseurs(self):
        # Deux requêtes : les fournisseurs, puis leurs soldes préchargés en un
        # seul coup. Sans la lecture par le cache, ce serait 1 + N.
        self._requetes(2)
        self._requetes(6)

    def test_les_soldes_sont_bien_rendus(self):
        from apps.contacts.serializers import SupplierListSerializer
        from apps.contacts.models import Supplier as S

        s = S.objects.filter(organization=self.org).prefetch_related('balances').first()
        donnees = SupplierListSerializer(s).data
        self.assertEqual(
            donnees['balances'],
            [
                {'currency': 'CDF', 'amount': '1000.00'},
                {'currency': 'USD', 'amount': '10.00'},
            ],
        )


class LeVerrouDeStockVIT_SOUS_UN_ATOMIC_Tests(TransactionTestCase):
    """
    L'approbation d'un retour verrouille `Stock`, donc elle doit être atomique.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `TransactionTestCase` ET SURTOUT PAS `TestCase`.                        │
    │                                                                          │
    │ `TestCase` enveloppe chaque test dans une transaction : un               │
    │ `select_for_update` y passe TOUJOURS, et le défaut est invisible. C'est  │
    │ écrit noir sur blanc dans ce dépôt depuis août, sur le même piège :      │
    │ « invisible en dev (SQLite ignore `select_for_update`) et en test        │
    │ (`APITestCase` ouvre un `atomic`) ». Les trois boutons « Payer » du      │
    │ back-office en sont morts, et le retour fournisseur l'a payé à son tour  │
    │ - un 500 que seul un essai contre le vrai serveur a montré.              │
    │                                                                          │
    │ Ce test est le SEUL de la suite à pouvoir l'attraper, et il ne tient à   │
    │ rien d'autre qu'à sa classe de base.                                      │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.product = Product.objects.create(
            organization=self.org, name='Carton de savon', sku='SAV-C',
            slug='carton-savon',
            cost_price=Decimal('5000.00'), selling_price=Decimal('7000.00'),
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        self.supplier = Supplier.objects.create(
            organization=self.org, name='Grossiste Savon', code='FRN-SAV',
        )
        from apps.inventory.models import Stock

        Stock.objects.create(
            organization=self.org, product=self.product,
            warehouse=self.warehouse, quantity=Decimal('10.000'),
            avg_cost=Decimal('5000.00'),
        )
        self.retour = PurchaseReturn.objects.create(
            organization=self.org, reference='RTF-TEST-ATOMIC',
            supplier=self.supplier, warehouse=self.warehouse,
            status='draft', reason='Cartons détrempés',
            return_date=date.today(), total_amount=Decimal('10000.00'),
            created_by=self.owner,
        )
        PurchaseReturnItem.objects.create(
            organization=self.org, purchase_return=self.retour,
            product=self.product, quantity=Decimal('2.000'),
            unit_cost=Decimal('5000.00'), total=Decimal('10000.00'),
        )

    def test_l_approbation_passe_HORS_de_toute_transaction(self):
        """
        Appelé sans transaction ambiante, le service doit aboutir.

        Sans son `@transaction.atomic`, PostgreSQL lève
        `TransactionManagementError` sur le `select_for_update` - et c'est
        exactement l'état dans lequel la VUE l'appelle, `ATOMIC_REQUESTS`
        étant à `False` (et devant y rester : le journal de synchronisation en
        dépend).
        """
        from apps.inventory.models import Stock
        from apps.purchases.services import approuver_retour

        self.assertFalse(
            transaction.get_connection().in_atomic_block,
            "Ce test ne vaut QUE hors transaction : sinon il ne prouve rien.",
        )

        approuver_retour(self.retour, self.owner)

        self.retour.refresh_from_db()
        self.assertEqual(self.retour.status, 'approved')
        stock = Stock.objects.get(
            organization=self.org, product=self.product, warehouse=self.warehouse,
        )
        self.assertEqual(stock.quantity, Decimal('8.000'))

    def test_une_reception_se_valide_HORS_de_toute_transaction(self):
        """`completer_reception` verrouille `Stock` par le même chemin."""
        from apps.inventory.models import Stock

        po = PurchaseOrder.objects.create(
            organization=self.org, reference='CMD-TEST-ATOMIC',
            supplier=self.supplier, warehouse=self.warehouse,
            status='confirmed', order_date=date.today(), currency='CDF',
            subtotal=Decimal('10000.00'), total=Decimal('10000.00'),
            amount_due=Decimal('10000.00'),
        )
        po_item = PurchaseOrderItem.objects.create(
            organization=self.org, purchase_order=po, product=self.product,
            quantity_ordered=Decimal('2.000'), unit_price=Decimal('5000.00'),
            subtotal=Decimal('10000.00'), total=Decimal('10000.00'),
        )
        grn = GoodsReceipt.objects.create(
            organization=self.org, reference='BR-TEST-ATOMIC',
            purchase_order=po, warehouse=self.warehouse,
            status='draft', receipt_date=date.today(), received_by=self.owner,
        )
        GoodsReceiptItem.objects.create(
            organization=self.org, goods_receipt=grn,
            purchase_order_item=po_item, product=self.product,
            quantity_received=Decimal('2.000'),
            quantity_accepted=Decimal('2.000'),
            unit_cost=Decimal('5000.00'),
        )

        self.assertFalse(transaction.get_connection().in_atomic_block)
        completer_reception(grn, self.owner)

        stock = Stock.objects.get(
            organization=self.org, product=self.product, warehouse=self.warehouse,
        )
        self.assertEqual(stock.quantity, Decimal('12.000'))
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('10000.00'))
