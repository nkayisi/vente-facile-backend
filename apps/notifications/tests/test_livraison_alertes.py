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
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.inventory.models import Stock, StockBatch, Warehouse
from apps.notifications.models import Alert, Notification
from apps.notifications.services import (
    PerimetreAlertes, alerter, destinataires, notifier,
)
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


class PerimetreCroiseTests(TestCase):
    """
    Le périmètre dit la MÊME chose en SQL et en Python.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ DEUX FORMES, UNE SEULE RÈGLE - ET C'EST CE TEST QUI LE TIENT.           │
    │                                                                          │
    │ `destinataires` juge UNE alerte contre TOUS les membres ; la liste juge  │
    │ TOUTES les alertes contre UN membre. Il faut donc les deux formes, et ce │
    │ dépôt sait ce qu'il arrive quand on les écrit séparément                 │
    │ (`membership_targets_q` : « écrits séparément, ils avaient divergé »).   │
    │                                                                          │
    │ Une divergence ici ne lève RIEN : elle montre une alerte reçue en        │
    │ notification et introuvable dans la liste, ou l'inverse - et personne ne │
    │ fait le rapprochement.                                                   │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ LES CAS LIMITES JSON SONT CEUX QUI MORDENT. `_entrepot_de_lalerte` lit
    TROIS sources, et `data` est un `JSONField` : une clé ABSENTE, un `null`
    JSON et une chaîne VIDE ne se comportent pas pareil face à `__isnull`.
    C'est précisément là qu'une des deux formes se tromperait sans bruit.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.depot_b = Warehouse.objects.create(
            organization=self.org, name='Boutique Sud', code='BS',
        )

        self.gerant_a = OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        )
        self.gerant_a.assigned_warehouses.set([self.warehouse])

        self.proprio = OrganizationMembership.objects.get(
            user=self.owner, organization=self.org
        )
        self.caissier = OrganizationMembership.objects.get(
            user=self.cashier_a, organization=self.org
        )
        self.caissier.assigned_warehouses.set([self.warehouse])

        # Un rôle borné SANS aucune affectation : il ne doit rien recevoir, et
        # c'est l'inverse de « aucune restriction ».
        self.orphelin = OrganizationMembership.objects.create(
            user=make_user('orph@vf.test', 'Or', 'Phelin'),
            organization=self.org,
            role=OrganizationMembership.Role.MANAGER, is_active=True,
        )

    def _alerte(self, **extra):
        champs = {
            'alert_type': Alert.AlertType.LOW_STOCK,
            'severity': Alert.Severity.HIGH,
            'title': 'Stock bas', 'message': 'Rayon sous son seuil',
        }
        champs.update(extra)
        return Alert.objects.create(organization=self.org, **champs)

    def _formes(self):
        """Les NEUF formes de périmètre qu'une alerte peut porter."""
        return {
            # Les trois sources que `_entrepot_de_lalerte` lit.
            'resource_warehouse': self._alerte(
                resource_type='warehouse', resource_id=self.warehouse.id,
            ),
            'data_warehouse_id': self._alerte(
                data={'warehouse_id': str(self.warehouse.id)},
            ),
            'data_warehouse': self._alerte(
                data={'warehouse': str(self.warehouse.id)},
            ),
            # Celle de l'AUTRE dépôt : elle doit sortir du périmètre du gérant A.
            'autre_depot': self._alerte(
                data={'warehouse_id': str(self.depot_b.id)},
            ),
            # Et les trois façons de ne viser AUCUN dépôt. `None` ne veut pas
            # dire « tous les dépôts » : il veut dire que l'alerte n'en vise
            # aucun, comme un abonnement qui expire.
            'aucun_depot': self._alerte(data={}),
            'cle_a_null': self._alerte(data={'warehouse_id': None}),
            'chaine_vide': self._alerte(data={'warehouse_id': ''}),
            # Un type dont la rubrique n'est PAS nommée : le propriétaire seul.
            'systeme': self._alerte(
                alert_type=Alert.AlertType.SYSTEM, data={},
            ),
            # Un type dont la rubrique est une AUTRE que le stock.
            'creance': self._alerte(
                alert_type=Alert.AlertType.PAYMENT_OVERDUE, data={},
            ),
        }

    def test_les_deux_formes_disent_la_MEME_chose(self):
        """
        Pour chaque forme et pour chaque rôle, dans les DEUX sens :
        `retient(a)` ⟺ `a` est rendue par `borner(...)`.
        """
        formes = self._formes()
        membres = {
            'proprietaire': self.proprio,
            'gerant_du_depot_A': self.gerant_a,
            'caissier': self.caissier,
            'gerant_SANS_depot': self.orphelin,
        }

        for nom_membre, membership in membres.items():
            perimetre = PerimetreAlertes(membership)
            en_sql = set(
                perimetre.borner(Alert.objects.filter(organization=self.org))
                .values_list('id', flat=True)
            )
            for nom_forme, alerte in formes.items():
                with self.subTest(membre=nom_membre, forme=nom_forme):
                    self.assertEqual(
                        perimetre.retient(alerte),
                        alerte.id in en_sql,
                        f"Le SQL et le Python divergent sur « {nom_forme} » "
                        f"pour le {nom_membre} : "
                        f"retient={perimetre.retient(alerte)}, "
                        f"en_sql={alerte.id in en_sql}.",
                    )

    def test_un_type_NON_DECLARE_dit_la_meme_chose_des_deux_cotes(self):
        """
        ┌──────────────────────────────────────────────────────────────────────┐
        │ LE CAS QUE LE CORPUS NE COUVRAIT PAS, ET C'EST POUR LUI QUE LE TEST │
        │ CROISÉ ÉTAIT VERT.                                                   │
        │                                                                      │
        │ `_formes()` n'exerce que des types DÉCLARÉS - `SYSTEM` à `None`,    │
        │ `PAYMENT_OVERDUE` à `customers.view`. Or `retient()` lit             │
        │ `.get(alert_type, None)` et rangeait un type ABSENT de la table dans │
        │ la branche du propriétaire, pendant que `types_visibles()` itérait   │
        │ les clés déclarées et ne pouvait JAMAIS le mettre dans son           │
        │ `alert_type__in`.                                                     │
        │                                                                      │
        │ Un `AlertType` ajouté sans entrée dans la table aurait donc été      │
        │ NOTIFIÉ au propriétaire et INVISIBLE dans la liste de tout le monde, │
        │ lui compris - l'`action_url` menant à un écran où l'alerte n'est     │
        │ pas. On simule ce jour-là en retirant une entrée de la table.        │
        └──────────────────────────────────────────────────────────────────────┘
        """
        from apps.notifications import services

        sans_stock_exit = {
            genre: besoin
            for genre, besoin in services.PERMISSION_PAR_ALERTE.items()
            if genre != Alert.AlertType.STOCK_EXIT
        }

        with patch.object(services, 'PERMISSION_PAR_ALERTE', sans_stock_exit):
            orpheline = self._alerte(
                alert_type=Alert.AlertType.STOCK_EXIT, data={},
            )
            membres = {
                'proprietaire': self.proprio,
                'gerant_du_depot_A': self.gerant_a,
                'caissier': self.caissier,
                'gerant_SANS_depot': self.orphelin,
            }
            for nom, membership in membres.items():
                perimetre = services.PerimetreAlertes(membership)
                en_sql = set(
                    perimetre.borner(Alert.objects.filter(organization=self.org))
                    .values_list('id', flat=True)
                )
                with self.subTest(membre=nom):
                    self.assertEqual(
                        perimetre.retient(orpheline),
                        orpheline.id in en_sql,
                        f"Un type non déclaré diverge pour le {nom} : "
                        f"retient={perimetre.retient(orpheline)}, "
                        f"en_sql={orpheline.id in en_sql}.",
                    )

    def test_la_table_des_droits_COUVRE_tout_AlertType(self):
        """
        ⚠ POUR QU'UN TYPE NEUF REÇOIVE UNE DÉCISION, pas un défaut.

        Le test ci-dessus garantit que les deux côtés s'accordent quoi qu'il
        arrive ; celui-ci garantit qu'un type ajouté à l'énumération ne tombe
        pas EN SILENCE dans « le propriétaire, et lui seul ». Qui doit être
        prévenu d'une alerte est une décision, et elle s'écrit dans la table.
        """
        from apps.notifications.services import PERMISSION_PAR_ALERTE

        self.assertEqual(
            set(PERMISSION_PAR_ALERTE) - set(Alert.AlertType.values), set(),
            "Une entrée de la table ne correspond à aucun `AlertType`.",
        )
        self.assertEqual(
            set(Alert.AlertType.values) - set(PERMISSION_PAR_ALERTE), set(),
            "Un `AlertType` n'a pas de droit déclaré : ajoutez-le à "
            "`PERMISSION_PAR_ALERTE`, avec son motif.",
        )

    def test_le_balayage_balaie_bien_quelque_chose(self):
        """Un corpus vide passerait au vert sans rien démontrer."""
        formes = self._formes()
        self.assertGreaterEqual(len(formes), 9)
        # Et les deux verdicts doivent être REPRÉSENTÉS : un corpus dont tout
        # est retenu ne prouve pas que le filtre filtre.
        perimetre = PerimetreAlertes(self.gerant_a)
        verdicts = {perimetre.retient(a) for a in formes.values()}
        self.assertEqual(verdicts, {True, False})

    def test_une_alerte_SANS_depot_reste_visible_de_tout_role_qui_en_a_le_droit(self):
        """
        ⚠ `None` ne veut PAS dire « tous les dépôts ». Les confondre ferait
        taire une alerte d'établissement pour TOUT rôle borné - et c'est ce que
        le gérant sans affectation prouve ici : il ne doit pas la perdre.
        """
        for cas, alerte in (
            ('aucune clé', self._alerte(data={})),
            ('clé à null', self._alerte(data={'warehouse_id': None})),
            ('chaîne vide', self._alerte(data={'warehouse_id': ''})),
        ):
            with self.subTest(cas=cas):
                for membership in (self.gerant_a, self.orphelin):
                    self.assertTrue(
                        PerimetreAlertes(membership).retient(alerte), cas
                    )
                    self.assertIn(
                        alerte.id,
                        PerimetreAlertes(membership)
                        .borner(Alert.objects.all())
                        .values_list('id', flat=True),
                        cas,
                    )

    def test_le_cout_de_la_liste_ne_CROIT_PAS_avec_le_nombre_d_alertes(self):
        """
        L'invariant, jamais un nombre absolu : on relâcherait le second au
        premier champ ajouté. Avant ce lot, chaque alerte coûtait sa propre
        requête d'appartenance et son préchargement.
        """
        from rest_framework.test import APIClient

        self._formes()
        # ⚠ `APIClient.force_authenticate` et NON `Client.force_login` :
        #   l'authentification est en JWT, et une session Django rend 401.
        client = APIClient()
        client.force_authenticate(user=self.manager)
        entetes = {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

        def compter():
            with CaptureQueriesContext(connection) as capture:
                r = client.get('/api/v1/alerts/', **entetes)
                self.assertEqual(r.status_code, 200, r.content[:200])
            return len(capture)

        avec_neuf = compter()
        # Trente alertes de plus, et le compte ne doit pas bouger.
        for _ in range(30):
            self._alerte(data={'warehouse_id': str(self.warehouse.id)})
        avec_trente_neuf = compter()

        self.assertLessEqual(
            avec_trente_neuf, avec_neuf,
            f"Le coût croît avec la table : {avec_neuf} puis "
            f"{avec_trente_neuf} requêtes. Le périmètre est-il repassé en "
            f"Python avant la pagination ?",
        )


class IsolationDuBattementTests(TestCase):
    """
    Une organisation qui lève ne condamne pas le reste du parc.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LA RÉSOLUTION ÉTAIT PARK-WIDE, ET POSÉE AVANT LES CRÉATIONS.            │
    │                                                                          │
    │ Le battement résolvait les alertes périmées de TOUT LE PARC en une       │
    │ écriture, puis créait les remplaçantes établissement par établissement,  │
    │ SANS AUCUNE ISOLATION. Une organisation qui levait - un `DataError`, un  │
    │ interblocage, le SIGKILL d'une limite de tâche - laissait toutes les     │
    │ suivantes avec leur ancienne alerte RÉSOLUE et aucune remplaçante : la   │
    │ liste et la pastille du marchand ne montraient plus RIEN pour un rayon   │
    │ réellement vide, ce qui est pire que l'alerte périmée qu'on venait de    │
    │ retirer.                                                                 │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        ).assigned_warehouses.set([self.warehouse])

        # Une SECONDE organisation : c'est elle qui doit survivre à l'échec de
        # la première. `make_org_with_users` emploie des adresses FIXES et ne
        # prend aucun argument - un second appel heurte `users_email_key` -
        # donc elle se monte à la main.
        from apps.organizations.models import Organization

        self.ailleurs = Organization.objects.create(name='Chez B', slug='chez-b')
        self.depot_b = Warehouse.objects.create(
            organization=self.ailleurs, name='Dépôt B', code='B',
        )

    def _rayon(self, organisation, entrepot, sku, quantite='1.000'):
        #: ⚠ LA QUANTITÉ DÉCIDE DU GENRE D'ALERTE. À zéro c'est une RUPTURE, en
        #:   dessous du seuil un STOCK BAS - et c'est le passage de l'un à
        #:   l'autre qui déclenche la résolution. Un rayon à 1 face à un seuil
        #:   de 10 ne produit donc PAS d'`OUT_OF_STOCK`, et l'assertion qui
        #:   l'attendait échouait pour une raison qui n'était pas la sienne.
        produit = Product.objects.create(
            organization=organisation, name=f'Article {sku}', sku=sku,
            slug=f'article-{sku.lower()}', track_inventory=True,
            reorder_point=Decimal('10.000'),
            cost_price=Decimal('100'), selling_price=Decimal('150'),
        )
        return Stock.objects.create(
            organization=organisation, product=produit, warehouse=entrepot,
            quantity=Decimal(quantite),
        )

    def test_une_organisation_qui_LEVE_ne_condamne_pas_les_suivantes(self):
        from apps.notifications import tasks
        from apps.notifications.services import alerter_en_lot as reel

        self._rayon(self.org, self.warehouse, 'AAA')
        self._rayon(self.ailleurs, self.depot_b, 'BBB')

        vues = []

        def refuse_la_premiere(organisation, lots, *, resoudre=None):
            """La première organisation lève ; les suivantes passent."""
            vues.append(organisation)
            if len(vues) == 1:
                raise RuntimeError('interblocage simulé')
            return reel(organisation, lots, resoudre=resoudre)

        with patch(
            'apps.notifications.services.alerter_en_lot',
            side_effect=refuse_la_premiere,
        ):
            tasks.check_low_stock_alerts()

        # Les DEUX ont été tentées - c'est l'isolation - et la seconde a bien
        # produit son alerte.
        self.assertEqual(len(vues), 2)
        self.assertTrue(
            Alert.objects.filter(
                organization=vues[1],
                alert_type=Alert.AlertType.LOW_STOCK,
            ).exists(),
            "L'échec de la première organisation a condamné la seconde.",
        )

    def test_la_limite_douce_ARRETE_la_tournee_et_le_DIT(self):
        """
        ⚠ Elle n'est pas avalée par l'isolation : sinon la levée que `_pousser`
        vient de rétablir serait ravalée ici, et la tâche irait au SIGKILL de la
        limite dure - worker mort, et rien pour dire jusqu'où.
        """
        from celery.exceptions import SoftTimeLimitExceeded

        from apps.notifications import tasks

        self._rayon(self.org, self.warehouse, 'AAA')
        self._rayon(self.ailleurs, self.depot_b, 'BBB')

        with patch(
            'apps.notifications.services.alerter_en_lot',
            side_effect=SoftTimeLimitExceeded(),
        ):
            with self.assertRaises(SoftTimeLimitExceeded):
                tasks.check_low_stock_alerts()

    def test_la_resolution_part_AVEC_les_creations_de_son_etablissement(self):
        """
        Une alerte « stock bas » devenue « rupture » se résout dans le MÊME
        `atomic` que sa remplaçante : un échec laisse alors l'ancienne ACTIVE,
        qui est le bon côté de l'erreur.
        """
        from apps.notifications import tasks

        rayon = self._rayon(self.org, self.warehouse, 'AAA', quantite='0.000')
        ancienne = Alert.objects.create(
            organization=self.org, alert_type=Alert.AlertType.LOW_STOCK,
            severity=Alert.Severity.HIGH, title='Stock bas', message='x',
            resource_type='stock', resource_id=rayon.id,
            data={'warehouse_id': str(self.warehouse.id)},
        )

        with patch(
            'apps.notifications.services.alerter_en_lot',
            side_effect=RuntimeError('interblocage simulé'),
        ):
            tasks.check_low_stock_alerts()

        ancienne.refresh_from_db()
        self.assertEqual(
            ancienne.status, Alert.Status.ACTIVE,
            "L'ancienne alerte a été résolue alors que sa remplaçante a échoué : "
            "le rayon vide n'a plus aucune alerte.",
        )


class CoutDuBattementTests(TestCase):
    """
    Le battement bas-de-stock, et ce qu'il coûte.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ ON ÉPINGLE L'INVARIANT, JAMAIS UN NOMBRE ABSOLU.                        │
    │                                                                          │
    │ Un plafond chiffré se relâche au premier champ ajouté, et plus personne  │
    │ ne sait ce qu'il protégeait. Ce qui compte est que le coût ne CROISSE    │
    │ PAS avec le nombre de rayons : c'est exactement ce qui était faux.       │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        ).assigned_warehouses.set([self.warehouse])

    def _rayons(self, combien, depart=0):
        for i in range(depart, depart + combien):
            p = Product.objects.create(
                organization=self.org, name=f'Article {i}', sku=f'A-{i}',
                slug=f'a-{i}', track_inventory=True,
                reorder_point=Decimal('10'),
                cost_price=Decimal('1'), selling_price=Decimal('2'),
            )
            Stock.objects.create(
                organization=self.org, product=p, warehouse=self.warehouse,
                quantity=Decimal('1'),
            )

    def _cout(self):
        with CaptureQueriesContext(connection) as capture:
            check_low_stock_alerts()
        return len(capture)

    def test_le_cout_ne_CROIT_PAS_avec_le_nombre_de_rayons(self):
        self._rayons(3)
        cout_trois = self._cout()

        # Les alertes de ces trois rayons existent maintenant : on les efface
        # pour que le second passage ait le même travail à faire, à l'échelle
        # près.
        Alert.objects.all().delete()
        Notification.objects.all().delete()
        self._rayons(27, depart=100)
        cout_trente = self._cout()

        self.assertLessEqual(
            cout_trente, cout_trois + 2,
            f"Le coût croît avec la table : {cout_trois} requêtes pour trois "
            f"rayons, {cout_trente} pour trente. `alerter` est-il rappelé par "
            f"rayon au lieu d'être groupé par établissement ?",
        )

    def test_le_balayage_PRODUIT_bien_des_alertes(self):
        """Un battement qui n'alerte rien aurait un coût constant, et nul."""
        self._rayons(3)
        check_low_stock_alerts()
        self.assertEqual(Alert.objects.count(), 3)
        # Et elles sont LIVRÉES : le gérant du dépôt les reçoit.
        self.assertEqual(
            Notification.objects.filter(user=self.manager).count(), 3
        )

    def _lots(self, combien, depart=0):
        """Des lots qui périment, pour le battement voisin."""
        from datetime import date

        for i in range(depart, depart + combien):
            p = Product.objects.create(
                organization=self.org, name=f'Périssable {i}', sku=f'P-{i}',
                slug=f'p-{i}', track_inventory=True,
                cost_price=Decimal('1'), selling_price=Decimal('2'),
            )
            StockBatch.objects.create(
                organization=self.org, product=p, warehouse=self.warehouse,
                batch_number=f'LOT-{i}', quantity=Decimal('5'),
                expiry_date=date.today() + timedelta(days=10),
            )

    def test_la_PEREMPTION_ne_croit_pas_non_plus_avec_la_table(self):
        """
        ┌──────────────────────────────────────────────────────────────────────┐
        │ LE BATTEMENT VOISIN PAYAIT ENCORE LE PROFIL QUE CELUI-CI A CORRIGÉ. │
        │                                                                      │
        │ `check_expiring_products` appelait `alerter` PAR LOT : chaque appel   │
        │ rejouait `membres_actifs` et son préchargement, construisait un       │
        │ `PerimetreAlertes` par membre, contrôlait l'idempotence par           │
        │ destinataire et enfilait un envoi - plus un `EXISTS` par lot.         │
        │ L'encadré de `check_low_stock_alerts` déclarait cela corrigé « par    │
        │ établissement » ; son voisin, dans le même fichier, ne l'était pas.   │
        └──────────────────────────────────────────────────────────────────────┘
        """
        self._lots(3)
        with CaptureQueriesContext(connection) as c:
            check_expiring_products()
        cout_trois = len(c)
        self.assertEqual(Alert.objects.count(), 3)

        Alert.objects.all().delete()
        Notification.objects.all().delete()
        self._lots(27, depart=100)
        with CaptureQueriesContext(connection) as c:
            check_expiring_products()
        cout_trente = len(c)

        self.assertEqual(Alert.objects.count(), 30)
        self.assertLessEqual(
            cout_trente, cout_trois + 2,
            f"Le coût croît avec la table : {cout_trois} requêtes pour trois "
            f"lots, {cout_trente} pour trente. `alerter` est-il rappelé par "
            f"lot au lieu d'être groupé par établissement ?",
        )

    def test_un_SECOND_battement_ne_sonne_pas_une_seconde_fois(self):
        """L'idempotence passe par `data.alert_id`, désormais lue en UNE fois."""
        self._rayons(3)
        check_low_stock_alerts()
        check_low_stock_alerts()
        self.assertEqual(Alert.objects.count(), 3)
        self.assertEqual(
            Notification.objects.filter(user=self.manager).count(), 3
        )


class TitreBorneTests(TestCase):
    """
    Un titre trop long AVORTE la tournée, et pouvait annuler une saisie.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ DJANGO NE VALIDE PAS SUR `create()`, ET POSTGRESQL LÈVE.                │
    │                                                                          │
    │ `Product.name`, `Warehouse.name` et `Customer.name` sont tous à 255, et  │
    │ six titres en concaténaient deux dans un `CharField(255)`. Dans le       │
    │ battement Celery, le `DataError` avorte TOUTE la tournée : aucune alerte │
    │ pour les établissements suivants. Et sur le chemin d'une sortie de       │
    │ stock, il annulait un mouvement que le magasinier venait de valider.     │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        OrganizationMembership.objects.get(
            user=self.manager, organization=self.org
        ).assigned_warehouses.set([self.warehouse])

        self.nom_long = 'A' * 255
        self.produit = Product.objects.create(
            organization=self.org, name=self.nom_long, sku='LONG-1',
            slug='long-1', track_inventory=True, reorder_point=Decimal('10'),
            cost_price=Decimal('1'), selling_price=Decimal('2'),
        )
        # Le dépôt porte aussi un nom long : c'est la somme qui déborde.
        self.warehouse.name = 'B' * 255
        self.warehouse.save(update_fields=['name'])

    def test_le_battement_SURVIT_a_un_nom_de_255_signes(self):
        Stock.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.warehouse, quantity=Decimal('1'),
        )
        # Sur le code d'origine : `value too long for type character varying(255)`.
        check_low_stock_alerts()
        alerte = Alert.objects.get()
        self.assertLessEqual(len(alerte.title), 255)

    def test_la_tournee_ne_s_ARRETE_PAS_au_premier_nom_long(self):
        """
        Le vrai dommage : les établissements suivants n'étaient plus alertés
        du tout.
        """
        Stock.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.warehouse, quantity=Decimal('1'),
        )
        court = Product.objects.create(
            organization=self.org, name='Savon', sku='SAV-1', slug='sav-1',
            track_inventory=True, reorder_point=Decimal('10'),
            cost_price=Decimal('1'), selling_price=Decimal('2'),
        )
        Stock.objects.create(
            organization=self.org, product=court,
            warehouse=self.warehouse, quantity=Decimal('1'),
        )
        check_low_stock_alerts()
        self.assertEqual(Alert.objects.count(), 2)

    def test_une_SORTIE_de_stock_n_est_pas_ANNULEE_par_son_titre(self):
        """
        ⚠ LE PIRE DES DEUX. `sortie_de_stock` était appelé DANS le `atomic` de
        `create_stock_movement` : le `DataError` du titre annulait le mouvement
        lui-même. Le magasinier voyait sa saisie disparaître.
        """
        from apps.inventory.models import StockMovement

        Stock.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.warehouse, quantity=Decimal('50'),
        )
        mouvement = StockMovement.objects.create(
            organization=self.org, product=self.produit,
            warehouse=self.warehouse, movement_type='damage',
            quantity=Decimal('-3'), quantity_before=Decimal('50'),
            quantity_after=Decimal('47'), notes='Casse',
        )
        from apps.notifications.services import sortie_de_stock

        sortie_de_stock(mouvement)
        self.assertLessEqual(len(Alert.objects.get().title), 255)

    def test_l_ELISION_est_au_MILIEU_et_garde_la_fin(self):
        """
        La fin d'un titre porte l'ENTREPÔT, et c'est lui qui rend l'alerte
        adressable : couper par la droite rendrait indistinguables tous les
        titres longs d'un même article.
        """
        from apps.notifications.services import borner_titre

        borne = borner_titre(f"Stock bas : {'A' * 255} (Dépôt Nord)")
        self.assertLessEqual(len(borne), 255)
        self.assertTrue(borne.startswith('Stock bas : A'))
        self.assertTrue(borne.endswith('(Dépôt Nord)'))
        self.assertIn('…', borne)

    def test_un_titre_COURT_n_est_pas_touche(self):
        """Le contrôle : la borne ne doit pas mutiler le cas ordinaire."""
        from apps.notifications.services import borner_titre

        self.assertEqual(borner_titre('Stock bas : Savon'), 'Stock bas : Savon')
