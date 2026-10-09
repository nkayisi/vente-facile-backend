"""
Remise à zéro d'un essai : effacer les OPÉRATIONS, garder la configuration.

┌──────────────────────────────────────────────────────────────────────────────┐
│ T8 DU RETOUR DE TEST, DEMANDÉ DEUX FOIS.                                    │
│                                                                              │
│ « Supprimer les ventes et les opérations effectuées » après un essai.        │
│ L'annulation comptable existe et contrepasse ; la remise à zéro n'existait   │
│ nulle part.                                                                  │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ LE GARDE-FOU LE PLUS IMPORTANT DE CE FICHIER est le dernier : il exige que
TOUT modèle porteur d'`organization` soit classé - effacé ou gardé. Sans lui,
un modèle ajouté demain survivrait à la remise à zéro EN SILENCE, et le
marchand retrouverait des opérations qu'il croit effacées.
"""
from decimal import Decimal

from django.apps import apps
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement
from apps.organizations.services import (
    EFFACES_PAR_LA_REMISE_A_ZERO,
    GARDES_PAR_LA_REMISE_A_ZERO,
    RemiseAZeroRefusee,
    reset_trial_data,
)
from apps.products.models import Product
from apps.sales.models import Sale
from apps.sales.tests._helpers import make_org_with_users
from apps.subscriptions.models import Subscription


class RemiseAZeroTests(APITestCase):

    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']
        self.url = f'/api/v1/organizations/{self.org.id}/reset-trial/'

        # ⚠ `make_org_with_users` crée un abonnement ACTIF. Sans cette ligne,
        # les tests d'EFFET sont refusés par le verrou d'essai, et celui de
        # production passerait pour une raison qui n'est pas la sienne.
        Subscription.objects.filter(organization=self.org).update(
            status=Subscription.Status.TRIAL
        )

        self.article = Product.objects.create(
            organization=self.org, name='Savon', sku='SAV-1',
            cost_price=Decimal('10'), selling_price=Decimal('15'),
        )
        self.vente = Sale.objects.create(
            organization=self.org, warehouse=self.d['warehouse'],
            sold_by=self.d['cashier_a'], reference='VT-1',
            status=Sale.Status.COMPLETED, subtotal=Decimal('15'),
            total=Decimal('15'), amount_paid=Decimal('15'),
            currency='CDF', exchange_rate=Decimal('1'),
        )
        CashMovement.objects.create(
            organization=self.org, sale=self.vente, direction='in',
            movement_type='sale', amount=Decimal('15'),
            currency='CDF', exchange_rate=Decimal('1'),
            reference='MVT-1', description='Encaissement',
            movement_date=self.vente.sale_date,
        )

    def _poster(self, qui, **corps):
        self.client.force_authenticate(user=qui)
        return self.client.post(
            self.url, corps, format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    # --- Les trois verrous ---------------------------------------------------

    def test_seul_le_proprietaire_peut_remettre_a_zero(self):
        """
        `OrganizationViewSet` ne porte que `IsAuthenticated` et borne par
        appartenance : un gérant ATTEINT cette route. Le contrôle est donc
        explicite dans l'action, et ce test le prouve.
        """
        for cle in ('manager', 'cashier_a'):
            with self.subTest(role=cle):
                r = self._poster(self.d[cle], confirmation=self.org.name)
                self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Sale.objects.filter(pk=self.vente.pk).exists())

    def test_un_etablissement_en_PRODUCTION_est_refuse(self):
        """
        Le verrou qui compte : une vente réglée porte le papier remis au client,
        et rien ne doit pouvoir l'effacer - pas même son propriétaire.
        """
        Subscription.objects.filter(organization=self.org).update(
            status=Subscription.Status.ACTIVE
        )
        r = self._poster(self.d['owner'], confirmation=self.org.name)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("essai", r.data['detail'].lower())
        self.assertTrue(Sale.objects.filter(pk=self.vente.pk).exists())

    def test_une_confirmation_fausse_est_refusee(self):
        r = self._poster(self.d['owner'], confirmation='oui')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        # Le message DIT ce qu'on attend : un refus qui se contente de « non »
        # laisse le marchand deviner.
        self.assertIn(self.org.name, r.data['detail'])
        self.assertTrue(Sale.objects.filter(pk=self.vente.pk).exists())

    def test_une_confirmation_absente_est_refusee(self):
        r = self._poster(self.d['owner'])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Sale.objects.filter(pk=self.vente.pk).exists())

    # --- L'aperçu ------------------------------------------------------------

    def test_l_apercu_compte_SANS_rien_effacer(self):
        r = self._poster(self.d['owner'], dry_run=True)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertTrue(r.data['dry_run'])
        self.assertGreater(r.data['total'], 0)
        self.assertIn('sales.Sale', r.data['deleted'])
        # Rien n'a bougé : c'est tout l'objet d'un aperçu.
        self.assertTrue(Sale.objects.filter(pk=self.vente.pk).exists())
        self.assertTrue(CashMovement.objects.filter(organization=self.org).exists())

    def test_l_apercu_n_exige_PAS_la_confirmation(self):
        """On ne fait que compter : exiger le nom pour regarder serait absurde."""
        r = self._poster(self.d['owner'], dry_run=True)
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    # --- L'effet -------------------------------------------------------------

    def test_elle_efface_les_operations(self):
        r = self._poster(self.d['owner'], confirmation=self.org.name)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertFalse(r.data['dry_run'])
        self.assertFalse(Sale.objects.filter(organization=self.org).exists())
        self.assertFalse(CashMovement.objects.filter(organization=self.org).exists())

    def test_elle_GARDE_le_catalogue_et_la_configuration(self):
        """
        La moitié du contrat : un marchand qui remet son essai à zéro veut
        refaire ses ventes, pas resaisir son catalogue.
        """
        self._poster(self.d['owner'], confirmation=self.org.name)
        self.assertTrue(Product.objects.filter(pk=self.article.pk).exists())
        from apps.inventory.models import Warehouse
        from apps.sales.models import Register
        self.assertTrue(Warehouse.objects.filter(organization=self.org).exists())
        self.assertTrue(self.org.memberships.exists())
        self.assertTrue(Register.objects.filter(organization=self.org).exists())

    def test_elle_remet_les_COMPTEURS_de_documents_a_zero(self):
        """
        Sans cela, la numérotation reprendrait où l'essai l'avait laissée, sur
        un établissement qui n'a plus aucune pièce.
        """
        from apps.core.models import DocumentSequence
        DocumentSequence.objects.create(
            organization=self.org, prefix='VT', year=2026, last_value=42
        )
        self._poster(self.d['owner'], confirmation=self.org.name)
        self.assertFalse(DocumentSequence.objects.filter(organization=self.org).exists())

    def test_elle_ne_touche_PAS_a_une_autre_organisation(self):
        # ⚠ On monte la seconde organisation À LA MAIN : `make_org_with_users`
        # emploie des adresses FIXES, et un second appel collisionne sur
        # `users_email_key`. Une vente et son entrepôt suffisent à prouver
        # l'isolement.
        from apps.inventory.models import Warehouse
        from apps.organizations.models import Branch, Organization

        voisine = Organization.objects.create(name='Voisine', slug='voisine')
        succursale = Branch.objects.create(
            organization=voisine, name='Main', code='MAIN', is_main=True
        )
        depot = Warehouse.objects.create(
            organization=voisine, branch=succursale,
            name='WH voisine', code='WH-V', is_default=True,
        )
        vente_autre = Sale.objects.create(
            organization=voisine, warehouse=depot,
            sold_by=self.d['cashier_a'], reference='VT-AUTRE',
            status=Sale.Status.COMPLETED, subtotal=Decimal('99'),
            total=Decimal('99'), amount_paid=Decimal('99'),
            currency='CDF', exchange_rate=Decimal('1'),
        )
        self._poster(self.d['owner'], confirmation=self.org.name)
        self.assertTrue(Sale.objects.filter(pk=vente_autre.pk).exists())

    def test_le_service_leve_un_refus_DETERMINISTE(self):
        """Jamais une erreur de programmation : la vue le traduit en 400."""
        with self.assertRaises(RemiseAZeroRefusee):
            reset_trial_data(self.org, confirmation='faux')


class ClassementDesModelesTests(APITestCase):
    """
    ┌──────────────────────────────────────────────────────────────────────────┐
    │ TOUT MODÈLE PORTEUR D'`organization` EST CLASSÉ : EFFACÉ OU GARDÉ.      │
    │                                                                          │
    │ Sans ce balayage, un modèle ajouté demain survivrait à la remise à zéro  │
    │ EN SILENCE - et le marchand retrouverait des opérations qu'il croit      │
    │ effacées. C'est la forme déjà retenue pour la purge de la base locale du │
    │ terminal : une allowlist des DEUX côtés, et rien d'inclassé.             │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    #: Ce qui n'est NI une opération NI de la configuration : l'identité de
    #: l'établissement, sa relation avec l'éditeur, et les référentiels
    #: partagés. Chacun porte sa raison.
    HORS_PERIMETRE = {
        # L'établissement lui-même et ses invitations : ce n'est pas une
        # opération, et les effacer supprimerait le compte.
        'organizations.OrganizationInvitation',
        # La relation avec l'ÉDITEUR. L'effacer retirerait l'essai en cours,
        # c'est-à-dire le droit même qui autorise la remise à zéro.
        'subscriptions.Subscription',
        'subscriptions.SubscriptionPayment',
        'subscriptions.Invoice',
        # Gabarits de message et tableaux de bord enregistrés : de la
        # configuration, pas des opérations.
        'notifications.NotificationTemplate',
        'notifications.AlertRule',
        'reports.ReportTemplate',
        'reports.Dashboard',
        'reports.DashboardWidget',
        'reports.SavedReport',
        'reports.ReportExport',
        # Les appareils enrôlés : un terminal reste enrôlé après une remise à
        # zéro, sinon tout le parc devrait être ré-enrôlé à la main.
        'users.Device',
        # Devises de l'établissement : de la configuration.
        'settings.Currency',
        # Images d'articles : elles suivent l'article, qui est gardé.
        'products.ProductImage',
    }

    def test_le_balayage_trouve_bien_des_modeles(self):
        """Un balayage qui ne balaie rien passe et ne prouve rien."""
        self.assertGreater(len(self._porteurs()), 50)

    @staticmethod
    def _porteurs():
        return {
            f'{m._meta.app_label}.{m.__name__}'
            for m in apps.get_models()
            if any(f.name == 'organization' for f in m._meta.fields)
        }

    def test_aucun_modele_n_est_inclasse(self):
        effaces = {f'{a}.{n}' for a, n in EFFACES_PAR_LA_REMISE_A_ZERO}
        gardes = set(GARDES_PAR_LA_REMISE_A_ZERO)
        inclasses = self._porteurs() - effaces - gardes - self.HORS_PERIMETRE
        self.assertEqual(
            inclasses,
            set(),
            "Modèle(s) porteur(s) d'`organization` non classé(s). Les ranger "
            "dans EFFACES_PAR_LA_REMISE_A_ZERO, dans GARDES_PAR_LA_REMISE_A_ZERO, "
            "ou dans HORS_PERIMETRE de ce test AVEC sa raison écrite.",
        )

    def test_aucun_modele_n_est_dans_les_DEUX_listes(self):
        effaces = {f'{a}.{n}' for a, n in EFFACES_PAR_LA_REMISE_A_ZERO}
        self.assertEqual(effaces & set(GARDES_PAR_LA_REMISE_A_ZERO), set())
