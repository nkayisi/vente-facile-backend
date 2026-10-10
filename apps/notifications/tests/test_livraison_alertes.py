"""
Une alerte prévient quelqu'un, et pas n'importe qui.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE MODÈLE EXISTAIT, LA LIVRAISON N'EXISTAIT PAS - ET RIEN NE LA SURVEILLAIT.│
│                                                                              │
│ `apps/notifications` n'avait AUCUN test. Sept producteurs d'alertes, un seul │
│ `Notification.objects.create` dans tout le backend (l'abonnement qui         │
│ expire), et six familles d'alertes qui ne se voyaient qu'en ouvrant l'écran  │
│ qui les liste - c'est-à-dire par celui qui les cherchait déjà.               │
└──────────────────────────────────────────────────────────────────────────────┘

**Les rôles sont BORNÉS** : c'est tout l'objet de ces contrôles. Un propriétaire
reçoit tout, donc un test écrit en propriétaire ne démontre rien de la règle de
périmètre.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.inventory.models import Stock, StockBatch, Warehouse
from apps.notifications.models import Alert, Notification
from apps.notifications.services import alerter, destinataires, notifier
from apps.notifications.tasks import check_expiring_products, check_low_stock_alerts
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users, make_user


class PerimetreDesDestinatairesTests(TestCase):
    """Qui reçoit quoi. La règle est posée une seule fois, et c'est ici."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.depot_b = Warehouse.objects.create(
            organization=self.org, name='Boutique Sud', code='BS',
        )

        # Un gérant par dépôt : c'est la paire qui prouve la règle.
        self.gerant_a = OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        )
        self.gerant_a.assigned_warehouses.set([self.warehouse])

        self.manager_b = make_user('manb@vf.test', 'Man', 'B')
        self.gerant_b = OrganizationMembership.objects.create(
            user=self.manager_b, organization=self.org,
            role=OrganizationMembership.Role.MANAGER, is_active=True,
        )
        self.gerant_b.assigned_warehouses.set([self.depot_b])

    def _alerte(self, **extra):
        champs = {
            'alert_type': Alert.AlertType.LOW_STOCK,
            'severity': Alert.Severity.HIGH,
            'title': 'Stock bas', 'message': 'Rayon sous son seuil',
        }
        champs.update(extra)
        return Alert.objects.create(organization=self.org, **champs)

    def test_un_gerant_n_est_prevenu_que_pour_SES_depots(self):
        """
        Le contrôle central du lot. Sans lui, un gérant reçoit les ruptures
        d'un magasin où il ne met pas les pieds - ou, bien pire, ne reçoit pas
        les siennes et croit que tout va bien.
        """
        alerte = self._alerte(data={'warehouse_id': str(self.warehouse.id)})
        recus = {m.user_id for m in destinataires(alerte)}

        self.assertIn(self.manager.id, recus)
        self.assertNotIn(self.manager_b.id, recus)

    def test_le_proprietaire_est_prevenu_pour_TOUS_les_depots(self):
        """
        Son accès vient de son RÔLE, pas d'une affectation : il n'en a aucune,
        et le croiser avec un dépôt le ferait disparaître de ses propres
        alertes.
        """
        alerte = self._alerte(data={'warehouse_id': str(self.depot_b.id)})
        self.assertIn(self.owner.id, {m.user_id for m in destinataires(alerte)})

    def test_un_role_borne_SANS_affectation_ne_recoit_rien(self):
        """
        C'est la règle déjà tenue partout ailleurs. Le prévenir l'enverrait
        buter sur un 403 en ouvrant l'alerte.
        """
        self.gerant_a.assigned_warehouses.clear()
        alerte = self._alerte(data={'warehouse_id': str(self.warehouse.id)})
        self.assertNotIn(self.manager.id, {m.user_id for m in destinataires(alerte)})

    def test_une_alerte_SANS_depot_ne_se_borne_a_aucun(self):
        """
        ⚠ `None` ne veut PAS dire « tous les dépôts » : il veut dire que
        l'alerte ne porte sur aucun. Les confondre ferait taire une alerte
        d'établissement pour tout rôle borné.
        """
        alerte = self._alerte(data={})
        recus = {m.user_id for m in destinataires(alerte)}
        self.assertIn(self.manager.id, recus)
        self.assertIn(self.manager_b.id, recus)

    def test_le_droit_d_etre_prevenu_est_le_droit_de_VOIR(self):
        """
        Un caissier n'a pas `stock.view` : une rupture ne le concerne pas, et
        l'alerte lui serait de toute façon refusée à l'ouverture.
        """
        alerte = self._alerte(data={'warehouse_id': str(self.warehouse.id)})
        self.assertNotIn(
            self.cashier_a.id, {m.user_id for m in destinataires(alerte)}
        )

    def test_un_droit_accorde_a_la_main_est_HONORE(self):
        """
        `extra_permissions` est ce qui distingue cette règle d'une table de
        rôles : un marchand qui accorde `stock.view` à son caissier veut qu'il
        soit prévenu des ruptures.
        """
        membership = OrganizationMembership.objects.get(
            user=self.cashier_a, organization=self.org
        )
        membership.extra_permissions = ['stock.view']
        membership.save(update_fields=['extra_permissions'])
        membership.assigned_warehouses.set([self.warehouse])

        alerte = self._alerte(data={'warehouse_id': str(self.warehouse.id)})
        self.assertIn(
            self.cashier_a.id, {m.user_id for m in destinataires(alerte)}
        )

    def test_un_abonnement_qui_expire_ne_va_qu_au_PROPRIETAIRE(self):
        """
        `subscription.view` n'est accordée qu'à lui - mesuré sur le catalogue -
        et c'est lui qui peut payer.
        """
        alerte = self._alerte(
            alert_type=Alert.AlertType.SUBSCRIPTION_EXPIRING, data={},
        )
        recus = {m.user_id for m in destinataires(alerte)}
        self.assertEqual(recus, {self.owner.id})

    def test_un_compte_desactive_ne_recoit_plus_rien(self):
        self.gerant_a.is_active = False
        self.gerant_a.save(update_fields=['is_active'])
        alerte = self._alerte(data={'warehouse_id': str(self.warehouse.id)})
        self.assertNotIn(self.manager.id, {m.user_id for m in destinataires(alerte)})


class LivraisonTests(TestCase):
    """L'alerte produit des notifications, et une seule fois."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        ).assigned_warehouses.set([self.warehouse])

    def test_alerter_cree_l_alerte_ET_previent(self):
        """
        Le point d'entrée unique : il n'existe plus de chemin qui crée une
        alerte sans décider qui la reçoit.
        """
        alerte = alerter(
            self.org,
            alert_type=Alert.AlertType.LOW_STOCK,
            severity=Alert.Severity.HIGH,
            title='Stock bas', message='Rayon sous son seuil',
            data={'warehouse_id': str(self.warehouse.id)},
        )
        self.assertTrue(Alert.objects.filter(id=alerte.id).exists())
        self.assertTrue(
            Notification.objects.filter(data__alert_id=str(alerte.id)).exists()
        )

    def test_la_notification_PORTE_son_alerte(self):
        """
        `Notification` n'a aucune clé vers `Alert` : `data.alert_id` est à la
        fois la clé d'idempotence et le lien dont l'écran se sert pour mener à
        l'alerte.
        """
        alerte = alerter(
            self.org,
            alert_type=Alert.AlertType.OUT_OF_STOCK,
            severity=Alert.Severity.CRITICAL,
            title='Rupture', message='Rayon vide',
            data={'warehouse_id': str(self.warehouse.id)},
        )
        n = Notification.objects.filter(user=self.manager).first()
        self.assertEqual(n.data['alert_id'], str(alerte.id))
        self.assertEqual(n.data['alert_type'], Alert.AlertType.OUT_OF_STOCK)
        # Une alerte critique ne se présente pas comme une information.
        self.assertEqual(n.notification_type, Notification.NotificationType.ERROR)

    def test_une_SECONDE_livraison_ne_sonne_pas_deux_fois(self):
        """
        Une tâche Celery repasse sur une alerte encore active à chaque
        battement. Un marchand qui reçoit trois fois la même rupture cesse de
        les lire.
        """
        alerte = alerter(
            self.org,
            alert_type=Alert.AlertType.LOW_STOCK,
            severity=Alert.Severity.HIGH,
            title='Stock bas', message='Rayon sous son seuil',
            data={'warehouse_id': str(self.warehouse.id)},
        )
        avant = Notification.objects.count()
        notifier(alerte)
        self.assertEqual(Notification.objects.count(), avant)


class AlerteDeStockTests(TestCase):
    """La tâche de stock : par entrepôt, et l'aggravation est un fait neuf."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.gerant = OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        )
        self.gerant.assigned_warehouses.set([self.warehouse])

        self.produit = Product.objects.create(
            organization=self.org, name='Lait 1L', sku='LAIT',
            slug='lait-1l', track_inventory=True,
            reorder_point=Decimal('12.000'),
            cost_price=Decimal('1000'), selling_price=Decimal('1500'),
        )
        self.rayon = Stock.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.warehouse, quantity=Decimal('5.000'),
        )

    def test_l_alerte_NOMME_son_entrepot(self):
        """
        Sans ce champ, le service de livraison ne peut pas la borner à un dépôt,
        et la règle de périmètre de T14 est intenable.
        """
        check_low_stock_alerts()
        alerte = Alert.objects.get(alert_type=Alert.AlertType.LOW_STOCK)
        self.assertEqual(alerte.data['warehouse_id'], str(self.warehouse.id))
        self.assertIn(self.warehouse.name, alerte.title)

    def test_elle_est_LIVREE(self):
        check_low_stock_alerts()
        self.assertTrue(
            Notification.objects.filter(user=self.manager).exists(),
            "L'alerte a été créée et n'a prévenu personne : c'est le défaut "
            "que ce lot referme.",
        )

    def test_un_SECOND_battement_ne_recree_rien(self):
        check_low_stock_alerts()
        check_low_stock_alerts()
        self.assertEqual(
            Alert.objects.filter(alert_type=Alert.AlertType.LOW_STOCK).count(), 1
        )
        self.assertEqual(Notification.objects.filter(user=self.manager).count(), 1)

    def test_UN_RAYON_QUI_SE_VIDE_EST_UN_FAIT_NOUVEAU(self):
        """
        ⚠ Le défaut que la déduplication d'origine cachait : elle couvrait les
        DEUX types ensemble, si bien qu'un produit déjà signalé « bas » qui
        tombe à zéro ne produisait plus rien. Le marchand n'apprenait jamais la
        rupture.
        """
        check_low_stock_alerts()
        self.rayon.quantity = Decimal('0.000')
        self.rayon.save(update_fields=['quantity'])
        check_low_stock_alerts()

        self.assertTrue(
            Alert.objects.filter(
                alert_type=Alert.AlertType.OUT_OF_STOCK,
                status=Alert.Status.ACTIVE,
            ).exists(),
            "La rupture n'a produit aucune alerte : l'aggravation est perdue.",
        )
        # ⚠ Et l'ancienne se RÉSOUT : un rayon vide n'est plus « bas », et
        # laisser les deux actives ferait compter deux fois le même rayon.
        self.assertEqual(
            Alert.objects.get(alert_type=Alert.AlertType.LOW_STOCK).status,
            Alert.Status.RESOLVED,
        )

    def test_un_produit_NON_SUIVI_ne_declenche_rien(self):
        """Le critère du serveur en a trois, et il faut les trois."""
        self.produit.track_inventory = False
        self.produit.save(update_fields=['track_inventory'])
        check_low_stock_alerts()
        self.assertEqual(Alert.objects.count(), 0)

    def test_un_produit_SANS_seuil_ne_declenche_rien(self):
        self.produit.reorder_point = Decimal('0.000')
        self.produit.save(update_fields=['reorder_point'])
        check_low_stock_alerts()
        self.assertEqual(Alert.objects.count(), 0)

    def test_le_gerant_de_l_AUTRE_depot_n_est_pas_prevenu(self):
        autre = Warehouse.objects.create(
            organization=self.org, name='Boutique Sud', code='BS',
        )
        manb = make_user('manb@vf.test', 'Man', 'B')
        mb = OrganizationMembership.objects.create(
            user=manb, organization=self.org,
            role=OrganizationMembership.Role.MANAGER, is_active=True,
        )
        mb.assigned_warehouses.set([autre])

        check_low_stock_alerts()
        self.assertTrue(Notification.objects.filter(user=self.manager).exists())
        self.assertFalse(Notification.objects.filter(user=manb).exists())


class AlerteDePeremptionTests(TestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        ).assigned_warehouses.set([self.warehouse])

        produit = Product.objects.create(
            organization=self.org, name='Yaourt', sku='YAO', slug='yaourt',
            track_inventory=True, cost_price=Decimal('500'),
            selling_price=Decimal('800'),
        )
        StockBatch.objects.create(
            organization=self.org, product=produit, warehouse=self.warehouse,
            batch_number='L1', quantity=Decimal('10.000'),
            expiry_date=timezone.localdate() + timedelta(days=3),
        )

    def test_un_lot_qui_perime_NOMME_son_depot_et_PREVIENT(self):
        check_expiring_products()
        alerte = Alert.objects.get(alert_type=Alert.AlertType.EXPIRING_PRODUCT)
        self.assertEqual(alerte.data['warehouse_id'], str(self.warehouse.id))
        self.assertTrue(Notification.objects.filter(user=self.manager).exists())


class ApiTests(TestCase):
    """
    Lire ce qu'on a reçu, et les alertes de son périmètre.

    Il n'y avait NI vue, NI serializer, NI route : une alerte produite n'était
    consultable sur aucune surface.
    """

    def setUp(self):
        from rest_framework.test import APIClient

        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.gerant = OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        )
        self.gerant.assigned_warehouses.set([self.warehouse])

        self.depot_b = Warehouse.objects.create(
            organization=self.org, name='Boutique Sud', code='BS',
        )

        self.mienne = alerter(
            self.org,
            alert_type=Alert.AlertType.LOW_STOCK, severity=Alert.Severity.HIGH,
            title='Stock bas ici', message='Rayon sous son seuil',
            data={'warehouse_id': str(self.warehouse.id)},
        )
        self.autre = alerter(
            self.org,
            alert_type=Alert.AlertType.LOW_STOCK, severity=Alert.Severity.HIGH,
            title='Stock bas ailleurs', message='Rayon sous son seuil',
            data={'warehouse_id': str(self.depot_b.id)},
        )

        self.api = APIClient()
        self.api.force_authenticate(user=self.manager)
        self.entetes = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def test_je_ne_lis_QUE_mes_notifications(self):
        r = self.api.get('/api/v1/notifications/', **self.entetes)
        self.assertEqual(r.status_code, 200)
        titres = [n['title'] for n in r.data['results']]
        self.assertIn('Stock bas ici', titres)
        self.assertNotIn('Stock bas ailleurs', titres)

    def test_le_decompte_de_la_pastille(self):
        r = self.api.get('/api/v1/notifications/unread-count/', **self.entetes)
        self.assertEqual(r.data['count'], 1)

    def test_marquer_lu(self):
        n = Notification.objects.get(user=self.manager)
        r = self.api.post(f'/api/v1/notifications/{n.id}/mark-read/', **self.entetes)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Notification.objects.get(id=n.id).is_read)

    def test_marquer_tout_lu_TOUCHE_updated_at(self):
        """
        ⚠ Un `update()` nu ne le toucherait pas, et une ligne dont
        l'horodatage ne bouge pas est invisible au tirage, définitivement.
        """
        n = Notification.objects.get(user=self.manager)
        avant = n.updated_at
        r = self.api.post('/api/v1/notifications/mark-all-read/', **self.entetes)
        self.assertEqual(r.data['updated'], 1)
        self.assertGreater(Notification.objects.get(id=n.id).updated_at, avant)

    def test_la_liste_des_alertes_est_BORNEE_a_mon_perimetre(self):
        """
        La même règle que la livraison, pas un second calcul : deux règles
        donneraient une alerte reçue et introuvable dans la liste.
        """
        r = self.api.get('/api/v1/alerts/', **self.entetes)
        self.assertEqual(r.status_code, 200)
        titres = [a['title'] for a in r.data['results']]
        self.assertEqual(titres, ['Stock bas ici'])

    def test_accuser_reception_n_est_pas_RESOUDRE(self):
        r = self.api.post(
            f'/api/v1/alerts/{self.mienne.id}/acknowledge/', **self.entetes
        )
        self.assertEqual(r.status_code, 200)
        self.mienne.refresh_from_db()
        self.assertEqual(self.mienne.status, Alert.Status.ACKNOWLEDGED)
        self.assertEqual(self.mienne.acknowledged_by, self.manager)

    def test_une_alerte_ne_se_cree_ni_ne_se_supprime_par_l_API(self):
        """
        Elle naît d'un FAIT constaté par une tâche. Un bouton « Résolu »
        laisserait croire au marchand qu'il a traité ce qu'il a masqué.
        """
        r = self.api.post('/api/v1/alerts/', {'title': 'À la main'},
                          format='json', **self.entetes)
        self.assertIn(r.status_code, (403, 405))
        r = self.api.delete(f'/api/v1/alerts/{self.mienne.id}/', **self.entetes)
        self.assertIn(r.status_code, (403, 405))

    def test_un_caissier_lit_SA_boite_sans_droit_de_rubrique(self):
        """
        Le filtre est l'IDENTITÉ, pas un droit : exiger une permission de
        rubrique fermerait sa propre boîte à un caissier.
        """
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(user=self.cashier_a)
        r = api.get('/api/v1/notifications/', **self.entetes)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['results'], [])


class CircuitApprobationTests(TestCase):
    """
    Les deux déclencheurs que le retour de test nomme : « demandes
    d'approbation […] plus un message quand la décision est prise ».

    ⚠ Aucun des deux n'existait, et le circuit était entièrement là : cinq
    transitions sur une dépense, quatre sur une réquisition, partagées par la
    vue et par le journal. Ce qui manquait est qu'une décision ATTENDE sans que
    personne ne le sache, et qu'une fois prise elle ne revienne jamais à celui
    qui avait demandé.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.gerant = OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        )
        self.gerant.assigned_warehouses.set([self.warehouse])

        self.magasinier = make_user('stock@vf.test', 'Stock', 'Keeper')
        mk = OrganizationMembership.objects.create(
            user=self.magasinier, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        mk.assigned_warehouses.set([self.warehouse])

        self.produit = Product.objects.create(
            organization=self.org, name='Lait 1L', sku='LAIT', slug='lait-1l',
            track_inventory=True, reorder_point=Decimal('12.000'),
            cost_price=Decimal('1000'), selling_price=Decimal('1500'),
        )
        Stock.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.warehouse, quantity=Decimal('5.000'),
        )

    def _requisition(self):
        from apps.purchases.services import create_requisition

        return create_requisition(
            self.org, warehouse=self.warehouse,
            lignes=[{'product': self.produit, 'quantity_requested': Decimal('24')}],
            requested_by=self.magasinier,
        )

    def test_une_demande_soumise_PREVIENT_qui_peut_decider(self):
        from apps.purchases.services import transition_requisition

        req = self._requisition()
        transition_requisition(req, 'submit', user=self.magasinier)

        self.assertTrue(
            Notification.objects.filter(
                user=self.manager, data__kind='approval_request'
            ).exists(),
            "Une décision attend et personne ne le sait.",
        )

    def test_elle_ne_revient_PAS_a_celui_qui_demande(self):
        """
        Il sait ce qu'il vient de soumettre. Lui renvoyer sa propre demande
        fait du bruit, et le bruit est ce qui apprend à ne plus lire les
        alertes.

        ┌──────────────────────────────────────────────────────────────────┐
        │ L'AUTEUR DOIT ÊTRE QUELQU'UN QUI PEUT DÉCIDER, sinon ce test ne  │
        │ démontre rien.                                                   │
        │                                                                  │
        │ Écrit avec le MAGASINIER comme auteur, il passait même en        │
        │ retirant l'exclusion `sauf` : le magasinier n'a pas              │
        │ `requisitions.approve`, donc le filtre de droit l'écartait déjà. │
        │ Il passait pour une raison qui n'était pas la sienne - le piège  │
        │ que ce dépôt a déjà payé sur `permissions.test.ts`. Vérifié par  │
        │ mutation : avec un gérant, il tombe.                             │
        │                                                                  │
        │ Et c'est un cas RÉEL : un gérant crée et soumet ses propres      │
        │ demandes, et il a le droit de les trancher.                      │
        └──────────────────────────────────────────────────────────────────┘
        """
        from apps.purchases.services import create_requisition, transition_requisition

        req = create_requisition(
            self.org, warehouse=self.warehouse,
            lignes=[{'product': self.produit, 'quantity_requested': Decimal('24')}],
            requested_by=self.manager,
        )
        transition_requisition(req, 'submit', user=self.manager)
        self.assertFalse(
            Notification.objects.filter(
                user=self.manager, data__kind='approval_request'
            ).exists(),
            "Le gérant a reçu sa propre demande d'approbation.",
        )

    def test_un_magasinier_n_est_pas_prevenu_d_une_demande_a_approuver(self):
        """
        ⚠ LE DROIT D'ÊTRE PRÉVENU EST ICI LE DROIT DE TRANCHER, et c'est le
        seul cas où il diffère du droit de voir. `requisitions.approve` ne va
        pas au magasinier : prévenir qui ne peut pas décider ne sert à rien.
        """
        from apps.purchases.services import transition_requisition

        autre = make_user('stock2@vf.test', 'Stock', 'Deux')
        m2 = OrganizationMembership.objects.create(
            user=autre, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        m2.assigned_warehouses.set([self.warehouse])

        req = self._requisition()
        transition_requisition(req, 'submit', user=self.magasinier)
        self.assertFalse(Notification.objects.filter(user=autre).exists())

    def test_LA_DECISION_PRISE_revient_a_celui_qui_avait_demande(self):
        """
        Sans ce message, celui qui a soumis rouvre son écran toutes les heures
        pour savoir où il en est.
        """
        from apps.purchases.services import transition_requisition

        req = self._requisition()
        transition_requisition(req, 'submit', user=self.magasinier)
        transition_requisition(req, 'approve', user=self.owner)

        n = Notification.objects.filter(
            user=self.magasinier, data__kind='approval_decision'
        ).first()
        self.assertIsNotNone(n, "La décision n'est jamais revenue au demandeur.")
        self.assertTrue(n.data['approved'])
        self.assertIn(req.reference, n.title)

    def test_un_REJET_revient_avec_son_motif(self):
        from apps.purchases.services import transition_requisition

        req = self._requisition()
        transition_requisition(req, 'submit', user=self.magasinier)
        transition_requisition(
            req, 'reject', user=self.owner, notes='Trop cher ce mois-ci'
        )

        n = Notification.objects.filter(
            user=self.magasinier, data__kind='approval_decision'
        ).first()
        self.assertFalse(n.data['approved'])
        self.assertIn('Trop cher ce mois-ci', n.message)

    def test_la_decision_ne_va_PAS_a_tous_les_gerants(self):
        """
        C'est SA demande : le fan-out par droit enverrait la nouvelle à tous les
        gérants de l'établissement.
        """
        from apps.purchases.services import transition_requisition

        req = self._requisition()
        transition_requisition(req, 'submit', user=self.magasinier)
        Notification.objects.all().delete()
        transition_requisition(req, 'approve', user=self.owner)

        destinataires_reels = set(
            Notification.objects.values_list('user_id', flat=True)
        )
        self.assertEqual(destinataires_reels, {self.magasinier.id})


class SortieDeStockTests(TestCase):
    """Le cinquième déclencheur : « sortie de stock »."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        ).assigned_warehouses.set([self.warehouse])

        self.produit = Product.objects.create(
            organization=self.org, name='Lait 1L', sku='LAIT', slug='lait-1l',
            track_inventory=True, cost_price=Decimal('1000'),
            selling_price=Decimal('1500'),
        )
        self.rayon = Stock.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.warehouse, quantity=Decimal('50.000'),
        )

    def _mouvement(self, genre):
        from apps.inventory.models import StockMovement
        from apps.notifications.services import sortie_de_stock

        m = StockMovement.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.warehouse, movement_type=genre,
            quantity=Decimal('3.000'), quantity_before=Decimal('50.000'),
            quantity_after=Decimal('47.000'), created_by=self.manager,
            notes='Carton tombé',
        )
        sortie_de_stock(m)
        return m

    def test_une_PERTE_est_signalee(self):
        self._mouvement('damage')
        alerte = Alert.objects.get(data__kind='stock_exit')
        self.assertEqual(alerte.severity, Alert.Severity.HIGH)
        self.assertIn('Carton tombé', alerte.message)
        self.assertTrue(Notification.objects.filter(user=self.manager).exists())

    def test_UNE_VENTE_N_EST_PAS_SIGNALEE(self):
        """
        ⚠ Chaque encaissement sort du stock : prévenir à chaque fois ferait de
        l'alerte le bruit de fond du comptoir, et personne ne verrait plus la
        perte qui compte.
        """
        self._mouvement('sale')
        self.assertFalse(Alert.objects.filter(data__kind='stock_exit').exists())

    def test_UNE_ENTREE_N_EST_PAS_SIGNALEE(self):
        self._mouvement('purchase')
        self.assertFalse(Alert.objects.filter(data__kind='stock_exit').exists())

    def test_elle_NOMME_son_depot_et_sa_quantite(self):
        self._mouvement('expired')
        alerte = Alert.objects.get(data__kind='stock_exit')
        self.assertEqual(alerte.data['warehouse_id'], str(self.warehouse.id))
        self.assertIn(self.warehouse.name, alerte.message)
        self.assertEqual(alerte.data['quantity'], 3.0)
