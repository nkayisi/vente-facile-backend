"""
Une écriture EN MASSE doit rester visible au tirage.

┌──────────────────────────────────────────────────────────────────────────────┐
│ `queryset.update()` NE PASSE PAS PAR `save()`, DONC PAR RIEN.               │
│                                                                              │
│ `TimeStampedModel.save` garantit qu'`updated_at` suit un                     │
│ `save(update_fields=[...])` : c'est le correctif du lot 6, et il tient. Mais │
│ `queryset.update()` court-circuite `save()` ENTIÈREMENT, `auto_now` compris. │
│ Une écriture en masse laisse donc `updated_at` où il était.                  │
│                                                                              │
│ Le tirage pagine sur un curseur `(updated_at, id)`. Une ligne dont           │
│ l'horodatage ne bouge pas est INVISIBLE au tirage, définitivement, sur tous  │
│ les terminaux à la fois. La réserve était écrite en tête de                  │
│ `apps/sync/pull.py` (« toute écriture en masse doit toucher `updated_at`     │
│ explicitement ») ; aucun des six sites ne le faisait.                        │
│                                                                              │
│ RELEVÉ SUR LA BASE DE DÉVELOPPEMENT, pas déduit : `CustomerLoyalty` de       │
│ Nelly Kayisi portait 704,13 points côté serveur et 372,65 sur le terminal,   │
│ avec le MÊME `updated_at`, égal à la date de création. Le caissier lui       │
│ refusait la moitié de sa remise, et aucune synchronisation n'y pouvait rien. │
│                                                                              │
│ Le pire des six est le catalogue : une modification de prix en lot depuis le │
│ back-office n'atteignait AUCUN terminal, et le comptoir vendait au prix      │
│ d'avant sans que rien ne le signale.                                         │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import CustomerLoyalty, LoyaltyProgram


class PointsDeFideliteTests(APITestCase):
    """Le site qui a été pris en flagrant délit."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        LoyaltyProgram.objects.create(
            organization=self.org, name='Fidélité', is_active=True,
            point_value=Decimal('1.00'), min_points_to_redeem=10,
        )
        self.client_ = Customer.objects.create(
            organization=self.org, name='Nelly', code='C1',
        )
        self.fidelite = CustomerLoyalty.objects.create(
            organization=self.org, customer=self.client_,
            current_points=Decimal('100.00'),
        )

    def test_gagner_des_points_fait_AVANCER_updated_at(self):
        avant = self.fidelite.updated_at
        self.fidelite.add_points(Decimal('50.00'))

        self.fidelite.refresh_from_db()
        self.assertEqual(self.fidelite.current_points, Decimal('150.00'))
        self.assertGreater(
            self.fidelite.updated_at, avant,
            "Les points ont bougé sans que l'horodatage suive : la ligne est "
            "invisible au tirage, et le terminal servira éternellement l'ancien "
            "solde.",
        )

    def test_la_ligne_redevient_VISIBLE_au_tirage(self):
        """
        Le test qui compte : c'est la requête du tirage, pas l'horodatage.

        `pull` sélectionne `updated_at > curseur`. Un test qui n'assertait que
        le champ passerait encore si le curseur changeait de forme.
        """
        curseur = timezone.now()
        self.fidelite.add_points(Decimal('50.00'))

        visible = CustomerLoyalty.objects.filter(
            pk=self.fidelite.pk, updated_at__gt=curseur
        ).exists()
        self.assertTrue(visible, "La ligne ne repasserait jamais au tirage.")

    def test_utiliser_des_points_fait_aussi_avancer(self):
        avant = self.fidelite.updated_at
        self.fidelite.redeem_points(Decimal('20.00'))

        self.fidelite.refresh_from_db()
        self.assertGreater(self.fidelite.updated_at, avant)


class CatalogueEnMasseTests(APITestCase):
    """
    Le site le plus coûteux : le prix que le comptoir oppose au client.

    Rôle BORNÉ, comme tout ce chantier : c'est le gérant qui modifie un
    catalogue, pas le propriétaire.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.produit = Product.objects.create(
            organization=self.org, name='Savon', sku='SAV-1', slug='sav-1',
            cost_price=Decimal('500.00'), selling_price=Decimal('800.00'),
            track_inventory=True, is_active=True,
        )
        # Une ligne de stock est INDISPENSABLE : `bulk_update` borne le queryset
        # au périmètre entrepôt du membre, et un gérant ne voit que les produits
        # présents dans les dépôts qui lui sont assignés. Sans elle, le lot ne
        # touche rien et le test passerait pour une raison qui n'est pas la sienne.
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('5.000'),
        )
        self.client.force_authenticate(user=self.manager)

    def test_une_modification_en_lot_reste_VISIBLE_au_tirage(self):
        curseur = timezone.now()

        reponse = self.client.post(
            '/api/v1/products/bulk-update/',
            {'ids': [str(self.produit.id)], 'is_featured': True},
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(reponse.data.get('updated'), 1, reponse.data)

        self.produit.refresh_from_db()
        self.assertTrue(self.produit.is_featured)
        self.assertTrue(
            Product.objects.filter(pk=self.produit.pk, updated_at__gt=curseur).exists(),
            "Le catalogue a changé et aucun terminal ne le saura : le comptoir "
            "vendra au prix d'avant, sans que rien ne le signale.",
        )


class AucunUpdateNuTests(SimpleTestCase):
    """
    Le garde-fou : plus aucun `queryset.update()` nu dans le code métier.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ UNE RÈGLE QU'IL FAUT SE RAPPELER À CHAQUE APPEL FINIT PAR ÊTRE OUBLIÉE.  │
    │                                                                          │
    │ « Toute écriture en masse doit toucher `updated_at` explicitement » est  │
    │ écrit en tête de `apps/sync/pull.py` depuis le lot 2. Les SIX sites du   │
    │ dépôt l'ignoraient, et le défaut ne se voit ni à la relecture, ni à      │
    │ l'écran, ni dans un journal : il se voit sur le terminal d'un marchand,  │
    │ des semaines plus tard, sous la forme d'un chiffre qui ne bouge plus.    │
    │                                                                          │
    │ Ce balayage lit la SOURCE. C'est grossier, et c'est le seul contrôle qui │
    │ attrape le septième site le jour où il est écrit.                        │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    #: Fichiers où un `update()` nu est LÉGITIME, chacun pour une raison nommée.
    TOLERES = {
        # Le helper lui-même : c'est lui qui pose les horodatages.
        'apps/core/bulk.py',
        # Table de journal, jamais tirée : les terminaux n'en lisent rien.
        'apps/sync/operations.py',
        'apps/sync/models.py',
        # Abonnements et paiements d'abonnement : hors manifeste de tirage,
        # arbitrage délibéré du lot 11 (« un abonnement est une relation avec
        # l'éditeur, pas une donnée de comptoir »).
        'apps/subscriptions/services.py',
        'apps/subscriptions/admin.py',
        'apps/subscriptions/tasks.py',
        # Notifications : lues en ligne, absentes du manifeste.
        'apps/notifications/tasks.py',
        'apps/notifications/views.py',
    }

    #: Ce qui trahit une chaîne de queryset dans l'expression appelée.
    #:
    #: `\bqueryset\b` ne suffisait PAS : dans `self.get_queryset().update(...)`,
    #: le souligné empêche la frontière de mot et le motif ne mordait pas. Le
    #: garde-fou passait donc au vert sur le site même qu'il devait attraper,
    #: et c'est le contrôle en sens inverse qui l'a montré.
    SIGNES_ORM = ('objects.', '.filter(', '.exclude(', '.all()', 'queryset', '_qs')

    def _sites_fautifs(self):
        """
        Les `.update()` en masse du dépôt, lus par l'AST et non ligne à ligne.

        La lecture ligne à ligne signalait une PHRASE de docstring
        (`apps/sync/pull.py` explique justement le piège). Un garde-fou qui crie
        sur de la prose finit désactivé, et c'est ainsi qu'on perd le contrôle
        avec lui. L'AST ne voit que des appels.
        """
        import ast
        from pathlib import Path

        racine = Path(__file__).resolve().parents[3]
        fautifs = []

        for fichier in sorted((racine / 'apps').rglob('*.py')):
            relatif = str(fichier.relative_to(racine))
            if relatif in self.TOLERES or '/tests' in relatif or '/migrations/' in relatif:
                continue
            source = fichier.read_text()
            try:
                arbre = ast.parse(source)
            except SyntaxError:  # pragma: no cover
                continue
            for noeud in ast.walk(arbre):
                if not isinstance(noeud, ast.Call):
                    continue
                if not isinstance(noeud.func, ast.Attribute) or noeud.func.attr != 'update':
                    continue
                # Le RECEVEUR seul : `dict.update(...)` et `set.update(...)` ne
                # portent aucun signe d'ORM, une chaîne de queryset en porte un.
                receveur = ast.get_source_segment(source, noeud.func.value) or ''
                if any(signe in receveur for signe in self.SIGNES_ORM):
                    fautifs.append(f'{relatif}:{noeud.lineno}  {receveur[:80]}.update(...)')
        return fautifs

    def test_aucun_update_nu_hors_des_sites_toleres(self):
        fautifs = self._sites_fautifs()
        self.assertEqual(
            fautifs, [],
            "Écritures en masse qui ne touchent pas `updated_at`, donc "
            "INVISIBLES au tirage, définitivement et sur tous les terminaux. "
            f"Passez par `apps.core.bulk.bulk_update_rows` : {fautifs}",
        )

    def test_le_balayage_MORD(self):
        """
        Un balayage qui ne trouve rien passe et ne prouve rien.

        On lui soumet les formes RÉELLES des six sites corrigés : s'il les
        laisse passer, le garde-fou est décoratif. La première est celle qui a
        échappé à la version précédente.
        """
        receveurs = [
            'self.get_queryset().filter(id__in=ids)',
            'type(self).objects.filter(pk=self.pk)',
            'Organization.objects.filter(id=org.id)',
            'queryset',
        ]
        for receveur in receveurs:
            with self.subTest(receveur=receveur):
                self.assertTrue(
                    any(signe in receveur for signe in self.SIGNES_ORM),
                    "Le balayage laisserait passer cette écriture.",
                )

    def test_le_balayage_ne_confond_pas_un_dictionnaire(self):
        """La réciproque : un garde-fou qui crie sur tout finit ignoré."""
        for receveur in ['contexte', 'champs', 'self.cache']:
            with self.subTest(receveur=receveur):
                self.assertFalse(any(signe in receveur for signe in self.SIGNES_ORM))
