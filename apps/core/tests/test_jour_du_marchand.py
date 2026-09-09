"""
« Aujourd'hui » est le jour du MARCHAND, jamais celui de Greenwich.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUE CE TEST DÉFEND : LES ÉCRANS SE VIDAIENT UNE HEURE PAR NUIT.          │
│                                                                              │
│ `TIME_ZONE = Africa/Kinshasa` (UTC+1) et `USE_TZ = True`. Un code qui lit    │
│ « aujourd'hui » en `timezone.now().date()` obtient la date UTC, alors qu'un  │
│ filtre `__date` sur un `DateTimeField` - et toute comparaison à un           │
│ `DateField` saisi par le marchand - résout en heure LOCALE.                  │
│                                                                              │
│ Entre 23h et minuit UTC, il est déjà le lendemain à Kinshasa : la borne      │
│ désigne la veille. Mesuré : « Ventes du jour » rendait celles d'hier, les    │
│ huit onglets de rapports annonçaient « Aucune donnée », et vingt et un tests │
│ de `apps/reports` échouaient dans cette seule tranche horaire.                │
│                                                                              │
│ Le marchand qui ouvre sa caisse après minuit lit une application vide,       │
│ pendant une heure, alors qu'il vient d'encaisser.                            │
└──────────────────────────────────────────────────────────────────────────────┘

Le garde-fou lit l'AST plutôt que les lignes : `timezone.now().date()` peut
s'écrire sur deux lignes, et une recherche textuelle passerait à côté. C'est la
leçon de `test_bulk_write_visibility`, dont la première version cherchait
`\\bqueryset\\b` et ne mordait pas sur `self.get_queryset()`.
"""
import ast
import io
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase
from django.utils import timezone

#: Le dossier `apps/` : ce fichier vit dans `apps/core/tests/`.
APPS = Path(__file__).resolve().parents[2]

#: Fichiers où la date UTC est LÉGITIME, avec la raison.
#:
#: Vide aujourd'hui : aucun code de ce dépôt ne raisonne sur la journée de
#: Greenwich. Toute entrée ajoutée ici doit porter son motif, sinon elle sert de
#: dérogation muette au défaut que ce test existe pour empêcher.
EXEMPTIONS: dict = {}


def _appels_utc(chemin: Path):
    """Les `timezone.now().date()` d'un fichier, avec leur numéro de ligne."""
    arbre = ast.parse(io.open(chemin, encoding='utf-8').read())
    trouves = []
    for noeud in ast.walk(arbre):
        # On cherche l'appel `.date()` posé sur un appel `.now()`.
        if not (isinstance(noeud, ast.Call)
                and isinstance(noeud.func, ast.Attribute)
                and noeud.func.attr == 'date'):
            continue
        interne = noeud.func.value
        if (isinstance(interne, ast.Call)
                and isinstance(interne.func, ast.Attribute)
                and interne.func.attr == 'now'):
            trouves.append(noeud.lineno)
    return trouves


class JourDuMarchandTests(SimpleTestCase):

    def test_le_balayage_BALAIE_bien_le_depot(self):
        """
        ⚠ UN BALAYAGE QUI NE BALAIE RIEN PASSE ET NE PROUVE RIEN.

        La première version de ce test pointait `apps/` puis cherchait
        `apps/**/*.py` : elle balayait donc `apps/apps/`, qui n'existe pas.
        Vérifié en réintroduisant le défaut : le test restait VERT. C'est le
        piège que ce dépôt a déjà payé sur `test_bulk_write_visibility` et sur
        `test_users_scope`, et il se referme en comptant les fichiers.
        """
        self.assertGreater(len(list(self._modules())), 100)

    @staticmethod
    def _modules():
        """Les modules de production, migrations et tests exclus."""
        for chemin in APPS.glob('**/*.py'):
            relatif = str(chemin.relative_to(APPS))
            if '/tests' in relatif or relatif.startswith('tests'):
                continue
            if '/migrations/' in relatif:
                continue
            yield chemin, relatif

    def test_le_detecteur_MORD(self):
        """
        On donne la forme fautive au détecteur et on exige qu'il la voie.

        Écrite sur DEUX lignes : une expression régulière passerait à côté, et
        c'est pourquoi ce garde-fou lit l'AST.
        """
        import tempfile

        with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False,
                                         encoding='utf-8') as f:
            f.write(
                "from django.utils import timezone\n"
                "def f():\n"
                "    return timezone.now(\n"
                "    ).date()\n"  # sur DEUX lignes : une regex passerait à côté
            )
            chemin = Path(f.name)
        try:
            self.assertEqual(len(_appels_utc(chemin)), 1)
        finally:
            chemin.unlink()

    def test_aucun_module_ne_lit_la_date_UTC(self):
        fautifs = []
        for chemin, relatif in self._modules():
            if relatif in EXEMPTIONS:
                continue
            for ligne in _appels_utc(chemin):
                fautifs.append(f"{relatif}:{ligne}")

        self.assertEqual(
            fautifs,
            [],
            "`timezone.now().date()` rend la date UTC. Employer "
            "`timezone.localdate()` : entre 23h et minuit UTC il est déjà le "
            "lendemain à Kinshasa, et la journée du marchand se décale d'un "
            "jour. Si l'UTC est VOULU ici, inscrire le fichier dans "
            "EXEMPTIONS avec son motif.",
        )

    def test_les_deux_dates_DIFFERENT_bien_dans_la_tranche_fautive(self):
        """
        Sans cette mesure, le test ci-dessus pourrait défendre une règle sans
        objet. On vérifie que l'écart existe, et où.
        """
        minuit_moins_une = datetime(2026, 9, 2, 23, 22, tzinfo=ZoneInfo('UTC'))
        with patch('django.utils.timezone.now', return_value=minuit_moins_une):
            self.assertEqual(timezone.now().date(), date(2026, 9, 2))
            self.assertEqual(timezone.localdate(), date(2026, 9, 3))
