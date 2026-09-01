"""
L'horloge de l'acte, et surtout la ligne qu'elle ne doit JAMAIS franchir.

┌──────────────────────────────────────────────────────────────────────────────┐
│ `updated_at` N'EST PAS SUR CETTE HORLOGE, ET NE DOIT JAMAIS L'ÊTRE.         │
│                                                                              │
│ Le tirage pagine sur un curseur `(updated_at, id)`. Un `updated_at` reculé   │
│ placerait la ligne AVANT le point de reprise des terminaux déjà passés par   │
│ là : ils ne la verraient jamais, définitivement, et tous à la fois. C'est le │
│ défaut de `CustomerBalance` du lot 6, mais introduit exprès et sur toutes    │
│ les tables à la fois.                                                        │
│                                                                              │
│ La tentation est réelle : le jour où quelqu'un voudra « que tout soit        │
│ cohérent », ce test dira pourquoi non.                                       │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.core.clock import horloge_de_l_acte, maintenant


class HorlogeTests(TestCase):
    def test_sans_acte_c_est_l_heure_du_serveur(self):
        avant = timezone.now()
        lu = maintenant()
        self.assertGreaterEqual(lu, avant)
        self.assertLessEqual(lu, timezone.now())

    def test_pendant_un_acte_c_est_l_heure_de_l_acte(self):
        hier = timezone.now() - timedelta(days=1)
        with horloge_de_l_acte(hier):
            self.assertEqual(maintenant(), hier)

    def test_l_horloge_est_RENDUE_a_la_sortie(self):
        """
        Les opérations d'un lot se rejouent l'une après l'autre.

        Une remise à `None` au lieu du jeton effacerait l'heure d'un appel
        englobant : la deuxième opération d'un lot imbriqué porterait alors
        l'heure du serveur sans que rien ne le signale.
        """
        avant_hier = timezone.now() - timedelta(days=2)
        hier = timezone.now() - timedelta(days=1)
        with horloge_de_l_acte(avant_hier):
            with horloge_de_l_acte(hier):
                self.assertEqual(maintenant(), hier)
            self.assertEqual(maintenant(), avant_hier)
        self.assertLess(timezone.now() - maintenant(), timedelta(seconds=5))

    def test_une_heure_EN_AVANCE_n_est_pas_crue(self):
        """
        Un appareil dont l'horloge avance daterait ses ventes dans le futur.

        Elles se rangeraient en tête de toutes les listes, pour toujours, et
        hors de tout rapport borné à aujourd'hui. Mieux vaut une date fausse de
        quelques secondes que de plusieurs jours.
        """
        with horloge_de_l_acte(timezone.now() + timedelta(days=3)):
            self.assertLess(timezone.now() - maintenant(), timedelta(seconds=5))

    def test_une_derive_de_QUELQUES_MINUTES_reste_acceptee(self):
        # Aucune horloge de terminal n'est à la seconde ; refuser une minute
        # d'avance ferait retomber tout un parc sur l'heure du serveur.
        proche = timezone.now() + timedelta(minutes=2)
        with horloge_de_l_acte(proche):
            self.assertEqual(maintenant(), proche)

    def test_une_date_ILLISIBLE_ne_fait_pas_echouer_l_acte(self):
        """`occurred_at` arrive du corps JSON et n'est validé par aucun serializer."""
        for valeur in (None, '', 'avant-hier', 12345, {'t': 1}):
            with self.subTest(valeur=valeur):
                with horloge_de_l_acte(valeur):
                    self.assertLess(
                        timezone.now() - maintenant(), timedelta(seconds=5)
                    )

    def test_une_CHAINE_ISO_est_acceptee(self):
        with horloge_de_l_acte('2026-08-29T09:15:00Z'):
            lu = maintenant()
        self.assertEqual(lu.year, 2026)
        self.assertEqual(lu.month, 8)
        self.assertEqual(lu.day, 29)

    def test_une_date_NAIVE_est_rendue_consciente(self):
        """
        Le projet tourne en `USE_TZ` : comparer une date nue lèverait.

        Un terminal qui enverrait `2026-08-29T09:15:00`, sans fuseau, ferait
        alors échouer l'opération entière au lieu de porter sa date.
        """
        with horloge_de_l_acte('2026-08-29T09:15:00'):
            self.assertIsNotNone(timezone.now().tzinfo)
            self.assertTrue(timezone.is_aware(maintenant()))


class ChampsSousHorlogeTests(TestCase):
    """
    Quels champs suivent l'horloge, et lesquels ne le doivent SOUS AUCUN
    PRÉTEXTE.
    """

    #: Les champs de date MÉTIER : ce que le marchand lit comme « quand ».
    SOUS_HORLOGE = [
        ('apps.core.models', 'TimeStampedModel', 'created_at'),
        ('apps.sales.models', 'Sale', 'sale_date'),
        ('apps.sales.models', 'Payment', 'paid_at'),
        ('apps.sales.models', 'RegisterSession', 'opened_at'),
        ('apps.sales.models', 'SaleReturn', 'return_date'),
        ('apps.inventory.models', 'StockBatch', 'received_at'),
        ('apps.inventory.models', 'StockTransfer', 'requested_at'),
    ]

    def _champ(self, chemin, modele, nom):
        from importlib import import_module
        classe = getattr(import_module(chemin), modele)
        return classe._meta.get_field(nom)

    def test_les_dates_metier_lisent_l_horloge(self):
        from apps.core.clock import maintenant as horloge

        for chemin, modele, nom in self.SOUS_HORLOGE:
            with self.subTest(modele=modele, champ=nom):
                champ = self._champ(chemin, modele, nom)
                self.assertIs(
                    champ.default, horloge,
                    f"{modele}.{nom} ne lit pas l'horloge de l'acte : une écriture "
                    "hors ligne se rangera au jour de sa poussée.",
                )
                self.assertFalse(
                    champ.auto_now_add,
                    f"{modele}.{nom} garde `auto_now_add`, qui ÉCRASE la valeur "
                    "fournie : le défaut ne serait jamais lu.",
                )
                # `auto_now_add` rendait le champ non modifiable ; le perdre le
                # ferait entrer dans tout serializer en `fields = '__all__'`,
                # où un client pourrait dater sa propre vente.
                self.assertFalse(champ.editable, f"{modele}.{nom} est devenu modifiable")

    def test_updated_at_reste_l_heure_du_SERVEUR(self):
        from apps.core.models import TimeStampedModel
        from apps.core.clock import maintenant as horloge

        champ = TimeStampedModel._meta.get_field('updated_at')
        self.assertTrue(champ.auto_now, "`updated_at` doit rester `auto_now`.")
        self.assertIsNot(
            champ.default, horloge,
            "`updated_at` est le CURSEUR du tirage. Le reculer placerait la ligne "
            "avant le point de reprise des terminaux déjà passés : ils ne la "
            "verraient jamais, définitivement et tous à la fois.",
        )

    def test_la_trace_de_synchronisation_garde_l_heure_du_serveur(self):
        from apps.sync.models import SyncOperation

        champ = SyncOperation._meta.get_field('received_at')
        self.assertTrue(
            champ.auto_now_add,
            "`received_at` répond à « quand le serveur l'a appris » : c'est le "
            "seul repère qui reste pour voir qu'une écriture est arrivée en retard.",
        )
