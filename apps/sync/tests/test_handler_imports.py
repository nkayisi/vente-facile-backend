"""
Tout import écrit DANS un handler désigne un symbole qui existe.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN HANDLER JAMAIS EXÉCUTÉ PEUT PORTER N'IMPORTE QUEL NOM FANTÔME.           │
│                                                                              │
│ `income_category.create` et `expense_category.create` importaient            │
│ `IncomeCategorySerializer` et `ExpenseCategorySerializer`. Ces noms          │
│ n'existent pas : `apps.cashbook.serializers` déclare `…ListSerializer`,      │
│ `…CreateSerializer` et `…DetailSerializer`.                                  │
│                                                                              │
│ Un import écrit à l'intérieur d'une fonction ne s'évalue qu'à l'APPEL. Le    │
│ module se charge donc sans broncher, le registre des actes est complet, et   │
│ toute la suite est verte. Le premier marchand qui crée une catégorie au      │
│ comptoir déclenche l'`ImportError` - que `_classify` ne reconnaît pas, donc  │
│ verdict `retry`. L'opération repart à CHAQUE synchronisation, indéfiniment,  │
│ pour un acte qui ne passera jamais. C'est le martèlement que le contrat de   │
│ §5.5 existe pour interdire, atteint par un chemin que rien ne surveillait.   │
│                                                                              │
│ `test_operation_permissions.py` ne pouvait PAS l'attraper : il croise        │
│ `HANDLER_PERMISSIONS` avec les `action_permissions` des vues, c'est-à-dire   │
│ deux tables STATIQUES. Il ne fait entrer aucun corps de handler.             │
│                                                                              │
│ Ce test-ci résout chaque import à plat, sans rien exécuter : il n'a besoin   │
│ ni de base, ni de requête, ni d'un lot d'opérations.                         │
└──────────────────────────────────────────────────────────────────────────────┘
"""
import ast
import importlib
import inspect
from pathlib import Path

from django.test import SimpleTestCase

from apps.sync import handlers as module_handlers

SOURCE = Path(inspect.getfile(module_handlers))


def imports_dans_les_corps():
    """
    Chaque `from X import a, b` écrit DANS une fonction, avec sa ligne.

    On lit l'AST et non les lignes : un `from x import (\\n a,\\n b,\\n)` tient
    sur plusieurs lignes, et une expression régulière n'en verrait que la
    première. C'est la leçon déjà payée par `test_bulk_write_visibility`.

    Les imports de TÊTE de module sont volontairement ignorés : ils sont
    évalués au chargement, donc déjà éprouvés par le simple fait que la suite
    démarre. Ce sont les imports différés qui échappent à tout.
    """
    arbre = ast.parse(SOURCE.read_text(encoding='utf-8'))
    trouves = []
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for interne in ast.walk(noeud):
            if isinstance(interne, ast.ImportFrom) and interne.module:
                for alias in interne.names:
                    trouves.append((noeud.name, interne.lineno, interne.module, alias.name))
    return trouves


class ImportsDesHandlersTests(SimpleTestCase):
    """Contrôles statiques : ils ne touchent ni la base ni le réseau."""

    def test_le_balayage_MORD(self):
        """
        Sans ce contrôle, une expression qui ne trouve rien ferait passer le
        test suivant sur un ensemble vide, et ne prouverait rien. Ce dépôt l'a
        déjà payé trois fois (`test_users_scope`, `test_bulk_write_visibility`,
        `test_jour_du_marchand`).
        """
        trouves = imports_dans_les_corps()
        self.assertGreater(
            len(trouves), 30,
            f"Seulement {len(trouves)} imports différés trouvés dans {SOURCE} : "
            "le balayage ne balaie plus rien.",
        )

    def test_chaque_import_differe_designe_un_symbole_qui_EXISTE(self):
        """
        Le seul contrôle qui aurait attrapé le défaut, et il tient en deux
        lignes : importer le module, puis `getattr`.
        """
        manquants = []
        for fonction, ligne, module, symbole in imports_dans_les_corps():
            try:
                cible = importlib.import_module(module)
            except ImportError as exc:
                manquants.append(
                    f"{SOURCE.name}:{ligne} ({fonction}) : module '{module}' introuvable - {exc}"
                )
                continue
            if not hasattr(cible, symbole):
                manquants.append(
                    f"{SOURCE.name}:{ligne} ({fonction}) : "
                    f"'{module}' ne déclare aucun '{symbole}'"
                )
        self.assertEqual(manquants, [], "\n".join(manquants))
