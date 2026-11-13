"""
Éprouver une migration de DONNÉES sur de vraies lignes, avant la production.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UNE BASE DE TEST EST VIDE, DONC UN `RunPython` N'Y EXÉCUTE RIEN.           │
│                                                                              │
│ La suite part d'une base neuve : chaque migration s'y applique, et chaque   │
│ boucle `for x in Modele.objects.all()` d'un `RunPython` y tourne sur zéro   │
│ ligne. Le 9 novembre, `settings/0016` lisait un champ que son propre         │
│ `RenameField` venait de renommer : la suite entière était verte, et la      │
│ production est tombée au premier programme de fidélité en mode             │
│ pourcentage. Aucun test du dépôt n'avait jamais exécuté une seule ligne de  │
│ cette conversion.                                                            │
│                                                                              │
│ Ce harnais ramène la base JUSTE AVANT la migration, y pose des lignes avec  │
│ les modèles HISTORIQUES de cet instant, applique la migration, et laisse le │
│ test lire ce qu'elle a écrit.                                               │
└──────────────────────────────────────────────────────────────────────────────┘

Usage :

    class MaConversionTests(MigrationDataTestCase):
        migrate_from = ('monapp', '0006_avant')
        migrate_to = ('monapp', '0007_conversion')

        def preparer(self, apps):
            Modele = apps.get_model('monapp', 'Modele')
            self.ligne = Modele.objects.create(ancien_champ=4)

        def test_la_conversion(self):
            Modele = self.apps_apres.get_model('monapp', 'Modele')
            self.assertEqual(Modele.objects.get(pk=self.ligne.pk).nouveau, 40)

⚠ `TransactionTestCase` ET NON `TestCase`. Une migration modifie le SCHÉMA, et
  un `TestCase` enveloppe tout dans une transaction qu'il annule : sous
  PostgreSQL, certaines opérations de schéma y sont interdites, et le
  `RunPython` ne verrait de toute façon pas l'état qu'il verrait en production.

⚠ LES AUTRES APPLICATIONS RESTENT À LEUR DERNIÈRE MIGRATION. Seule l'application
  éprouvée recule ; le harnais ramène toutes les autres à leur feuille, sans
  quoi les modèles historiques décriraient un `User` d'il y a trois mois, à qui
  manqueraient des colonnes NOT NULL que la base exige. Si une migration d'une
  AUTRE application dépend de celle qu'on éprouve, on ne peut pas la défaire
  sans défaire l'autre : le harnais le dit au lieu de produire un plan faux.

⚠ LA BASE EST REMISE À SA FEUILLE À LA FIN, puis vidée par `TransactionTestCase`.
  pytest-django exécute ces tests APRÈS les `TestCase`, donc ce vidage ne
  retire rien aux tests qui comptent sur les données posées par les migrations
  (les devises, notamment).
"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class MigrationDataTestCase(TransactionTestCase):
    #: Le nœud JUSTE AVANT la migration éprouvée, `(app_label, nom)`.
    migrate_from = None
    #: Le nœud de la migration éprouvée.
    migrate_to = None

    #: Les modèles historiques APRÈS la migration, posés par `setUp`.
    apps_apres = None

    #: Posé quand la base n'a pas pu revenir à sa feuille : les tests suivants
    #: de la classe s'écartent en le citant, au lieu d'échouer sur des doublons.
    _base_bloquee = None

    def preparer(self, apps):
        """Pose les lignes d'avant la migration. `apps` = modèles historiques."""

    # ------------------------------------------------------------------ plan

    @staticmethod
    def _executeur():
        # Un exécuteur NEUF à chaque étape : son chargeur relit, à la
        # construction, ce que la base a réellement appliqué.
        return MigrationExecutor(connection)

    def _cibles(self, executeur, noeud):
        """
        Toutes les feuilles du projet, sauf celle de l'application éprouvée,
        remplacée par `noeud`.

        ⚠ UNE AUTRE APPLICATION QUI DÉPEND DE LA MIGRATION ÉPROUVÉE REND LE PLAN
          IMPOSSIBLE : on ne peut pas la laisser à sa feuille ET défaire ce dont
          elle dépend. On refuse en la nommant plutôt que de laisser Django
          produire un plan contradictoire, qui échouerait plus loin sur une
          erreur de schéma sans rapport apparent.
        """
        app = noeud[0]
        graphe = executeur.loader.graph
        autres = [f for f in graphe.leaf_nodes() if f[0] != app]

        if self.migrate_to is not None and noeud == self.migrate_from:
            for feuille in autres:
                if self.migrate_to in graphe.forwards_plan(feuille):
                    raise AssertionError(
                        f"{feuille[0]}.{feuille[1]} dépend de "
                        f"{self.migrate_to[0]}.{self.migrate_to[1]} : ce harnais "
                        f"ne peut pas défaire l'une sans l'autre."
                    )
        return autres + [noeud]

    # ------------------------------------------------------------- cycle

    def setUp(self):
        super().setUp()
        if self.migrate_from is None or self.migrate_to is None:
            raise AssertionError("migrate_from et migrate_to sont obligatoires.")

        if type(self)._base_bloquee:
            self.skipTest(type(self)._base_bloquee)

        executeur = self._executeur()
        cibles_avant = self._cibles(executeur, self.migrate_from)

        # ┌──────────────────────────────────────────────────────────────────┐
        # │ UN `setUp` QUI ÉCHOUE NE DOIT PAS LAISSER LA BASE À MI-CHEMIN.   │
        # │                                                                  │
        # │ `unittest` n'appelle PAS `tearDown` quand `setUp` lève. Une      │
        # │ fixture fautive - ou, c'est tout l'objet, une migration qui lève │
        # │ - laissait donc la base à l'état d'AVANT : le vidage de          │
        # │ `TransactionTestCase` y butait sur une table que les modèles     │
        # │ courants ne connaissent plus, et chaque test suivant échouait    │
        # │ sur des doublons. Seize échecs pour une seule faute, aucun ne    │
        # │ la nommant. Relevé à la première exécution de ce harnais.        │
        # └──────────────────────────────────────────────────────────────────┘
        try:
            executeur.migrate(cibles_avant)

            # ⚠ L'état historique de TOUT LE PROJET à cet instant, pas seulement
            #   celui de l'application : `project_state` sur un nœud seul ne
            #   rendrait que ses ancêtres, donc des modèles d'il y a des mois.
            executeur = self._executeur()
            self.preparer(executeur.loader.project_state(cibles_avant).apps)

            executeur = self._executeur()
            cibles_apres = self._cibles(executeur, self.migrate_to)
            executeur.migrate(cibles_apres)
            self.apps_apres = self._executeur().loader.project_state(cibles_apres).apps
        except BaseException as echec:
            try:
                self._remettre_a_la_feuille()
            except Exception:
                # ┌──────────────────────────────────────────────────────────┐
                # │ LA MIGRATION ÉPROUVÉE LÈVE, DONC LA FEUILLE EST          │
                # │ INATTEIGNABLE : la réappliquer relève la même erreur.    │
                # │ C'est exactement le défaut que ce harnais existe pour    │
                # │ attraper, et la base reste à l'état d'avant. Les erreurs │
                # │ de vidage qui suivent EN DÉCOULENT : on le dit ici, une  │
                # │ fois, plutôt que de laisser seize échecs anonymes        │
                # │ enterrer la seule ligne qui nomme la cause.              │
                # └──────────────────────────────────────────────────────────┘
                motif = (
                    f"{self.migrate_to[0]}.{self.migrate_to[1]} lève à "
                    f"l'application ({type(echec).__name__}: {echec}). La base "
                    f"est restée à {self.migrate_from[1]} : les erreurs de "
                    f"vidage et de doublons qui suivent en découlent toutes."
                )
                type(self)._base_bloquee = motif
                raise AssertionError(motif) from echec
            raise

    def _remettre_a_la_feuille(self):
        """
        Ramène la base à sa dernière migration AVANT que `TransactionTestCase`
        ne la vide : un test suivant qui trouverait l'application éprouvée à
        mi-chemin échouerait sur une colonne absente, loin d'ici.
        """
        executeur = self._executeur()
        executeur.migrate(executeur.loader.graph.leaf_nodes())

    def tearDown(self):
        self._remettre_a_la_feuille()
        super().tearDown()
