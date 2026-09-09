"""
Une ligne monétaire ne porte JAMAIS une devise vide.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LA CHAÎNE VIDE ÉTAIT LE DÉFAUT DÉCLARÉ DU MODÈLE.                           │
│                                                                              │
│ Onze champs de devise portent `blank=True, default=''`, et le commentaire de │
│ chacun affirme que la valeur est « résolue dans save() ». C'était vrai de    │
│ neuf d'entre eux et FAUX des deux du livre de caisse : `Expense` et          │
│ `CashMovement` n'avaient aucun `save()`, et s'en remettaient à leur service. │
│ Tout chemin qui ne passe pas par lui - l'admin Django, une commande de       │
│ gestion, un futur appelant - écrivait donc la chaîne vide, en silence.       │
│                                                                              │
│ Ce n'est pas un défaut d'affichage. `money(x, "")` rend le nombre SANS       │
│ SYMBOLE dans une application où le même chiffre vaut soit trois dollars,     │
│ soit trois francs. Et la rature de télémétrie du terminal ancre les montants │
│ sur leur devise : un montant sans symbole n'est pas raturé et part en clair. │
│                                                                              │
│ Ce test ne vise pas les deux modèles corrigés : il vise TOUS les modèles     │
│ porteurs d'une devise, présents et à venir. Un douzième champ ajouté sans    │
│ résolution le fera échouer en le nommant.                                    │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from django.apps import apps as django_apps
from django.db import models
from django.test import TestCase
from django.utils import timezone

from apps.cashbook.models import CashMovement, Expense, ExpenseCategory
from apps.contacts.models import Customer, CustomerTransaction, Supplier
from apps.purchases.models import PurchaseOrder
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency


class DeviseJamaisVideTests(TestCase):
    """Chaque écriture, par le chemin le plus NU possible : le modèle seul."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        # L'organisation de test est en CDF (défaut de `Organization.currency`).
        self.principale = self.org.currency

    def test_une_depense_sans_devise_prend_la_principale(self):
        categorie = ExpenseCategory.objects.create(
            organization=self.org, name='Loyer', code='LOY',
        )
        # Création par le MODÈLE, sans passer par `create_expense` : c'est
        # précisément le chemin qui écrivait la chaîne vide.
        depense = Expense.objects.create(
            organization=self.org, category=categorie, reference='DEP-1',
            description='Loyer', amount=Decimal('100.00'),
            expense_date=timezone.now(), created_by=self.owner,
        )
        depense.refresh_from_db()
        self.assertEqual(depense.currency, self.principale)
        self.assertEqual(depense.exchange_rate, Decimal('1.000000'))

    def test_un_mouvement_de_caisse_sans_devise_prend_la_principale(self):
        mouvement = CashMovement.objects.create(
            organization=self.org, reference='MVT-1',
            movement_type=CashMovement.MovementType.OTHER_IN,
            direction='in', amount=Decimal('50.00'),
            description='Apport', movement_date=timezone.now(),
            created_by=self.owner,
        )
        mouvement.refresh_from_db()
        self.assertEqual(mouvement.currency, self.principale)

    def test_une_ecriture_client_sans_devise_prend_la_principale(self):
        client = Customer.objects.create(
            organization=self.org, name='Nelly', code='C1',
        )
        ecriture = CustomerTransaction.objects.create(
            organization=self.org, customer=client,
            transaction_type=CustomerTransaction.TransactionType.CREDIT_SALE,
            amount=Decimal('10.00'), created_by=self.owner,
            # Les soldes sont normalement posés par `contacts.services` ; on
            # écrit ici par le chemin le plus NU, celui qui échappait au service.
            balance_before=Decimal('0.00'), balance_after=Decimal('10.00'),
        )
        ecriture.refresh_from_db()
        self.assertEqual(ecriture.currency, self.principale)

    def test_une_commande_fournisseur_sans_devise_prend_la_principale(self):
        fournisseur = Supplier.objects.create(
            organization=self.org, name='Fourni', code='F1',
        )
        commande = PurchaseOrder.objects.create(
            organization=self.org, supplier=fournisseur, reference='CMD-1',
            order_date=timezone.now().date(), created_by=self.owner,
        )
        commande.refresh_from_db()
        self.assertEqual(commande.currency, self.principale)

    def test_une_devise_EXPLICITE_n_est_jamais_ecrasee(self):
        """La résolution comble un vide ; elle ne décide pas à la place du caissier."""
        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar', 'symbol': '$', 'decimal_places': 2},
        )
        OrganizationCurrency.objects.create(
            organization=self.org, currency=usd, is_primary=False,
            exchange_rate=Decimal('2800.000000'), is_active=True,
        )
        mouvement = CashMovement.objects.create(
            organization=self.org, reference='MVT-USD',
            movement_type=CashMovement.MovementType.OTHER_IN,
            direction='in', amount=Decimal('50.00'), currency='USD',
            description='Apport', movement_date=timezone.now(),
            created_by=self.owner,
        )
        mouvement.refresh_from_db()
        self.assertEqual(mouvement.currency, 'USD')
        # Le taux est RELU depuis l'organisation : une devise secondaire à 1
        # est la signature d'un taux jamais renseigné, qui fausse les rapports.
        self.assertEqual(mouvement.exchange_rate, Decimal('2800.000000'))


class AucunChampDeDeviseSansResolutionTests(TestCase):
    """
    Le garde-fou de STRUCTURE : il balaie les modèles, pas une liste tenue à la
    main. Un champ de devise ajouté demain sans résolution le fera échouer.

    On n'exige pas un `save()` sur le modèle exact : la résolution peut vivre
    sur un parent (`Payment` la tire de sa vente). On exige qu'elle existe
    QUELQUE PART sur la chaîne d'héritage, ce qui est vérifiable sans exécuter
    d'écriture.
    """

    #: Champs dont la devise est portée par une FK non nulle vers une autre
    #: ligne déjà résolue. Chacun est nommé, avec l'origine de sa valeur.
    PORTES_PAR_UN_PARENT = {
        'sales.Payment.currency': 'la vente (`sale.currency`)',
        'sales.Sale.change_currency': 'la devise de la vente elle-même',
        'subscriptions.Subscription.currency': 'le plan (`plan.currency.code`)',
        'subscriptions.SubscriptionPayment.currency': "l'abonnement",
        'subscriptions.Invoice.currency': "l'abonnement",
    }

    def test_tout_champ_de_devise_est_resolu(self):
        sans_resolution = []
        for modele in django_apps.get_models():
            if not modele._meta.app_label.startswith(('sales', 'cashbook', 'contacts',
                                                      'purchases', 'subscriptions')):
                continue
            for champ in modele._meta.get_fields():
                if not isinstance(champ, models.CharField):
                    continue
                if 'currency' not in champ.name:
                    continue
                # Une devise NON NULLE et sans défaut vide ne peut pas être vide.
                if champ.default not in ('', models.NOT_PROVIDED):
                    continue
                if champ.default is models.NOT_PROVIDED and not champ.blank:
                    continue
                cle = f'{modele._meta.app_label}.{modele.__name__}.{champ.name}'
                if cle in self.PORTES_PAR_UN_PARENT:
                    continue
                # `save` défini sur le modèle lui-même, pas hérité de Model.
                if 'save' not in vars(modele):
                    sans_resolution.append(cle)

        self.assertEqual(
            sans_resolution, [],
            "Ces champs de devise acceptent la chaîne vide et aucun `save()` ne "
            "la comble. Un montant sans devise s'écrit SANS SYMBOLE, et la "
            "rature de télémétrie ne l'attrape pas. Résoudre par "
            "`CurrencyService.resolve`, comme les autres.",
        )

    def test_le_balayage_MORD(self):
        """Un balayage qui ne trouve rien passe et ne prouve rien."""
        vus = [
            f'{m._meta.app_label}.{m.__name__}.{c.name}'
            for m in django_apps.get_models()
            for c in m._meta.get_fields()
            if isinstance(c, models.CharField) and 'currency' in c.name
        ]
        self.assertGreaterEqual(
            len(vus), 10,
            f"Le balayage ne voit que {len(vus)} champs de devise ; il en existe "
            "une douzaine. Il ne mesure donc rien.",
        )
