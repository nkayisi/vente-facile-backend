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


def noms_libres_par_handler():
    """
    Chaque nom qu'un corps de handler LIT sans l'avoir lié, avec sa ligne.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE CONTRÔLE D'AU-DESSUS NE VOIT QUE LES IMPORTS ÉCRITS. IL NE VOIT PAS  │
    │ UN NOM QU'ON A OUBLIÉ D'IMPORTER.                                        │
    │                                                                          │
    │ `requisition.create` appelait `assert_warehouse_allowed_for_membership`   │
    │ - une fonction qui EXISTE dans `apps.core.warehouse_scope` - sans jamais  │
    │ l'importer. Il n'y avait donc aucun `ImportFrom` à résoudre : le          │
    │ balayage précédent passait au vert, `tsc` n'a rien à dire d'un fichier    │
    │ Python, et le module se charge sans broncher. Le premier magasinier qui   │
    │ a demandé du réapprovisionnement a reçu                                   │
    │ « name '…' is not defined », et sa demande est partie en quarantaine.    │
    │                                                                          │
    │ MESURÉ sur le vrai serveur, par une poussée réelle, pas déduit.           │
    └──────────────────────────────────────────────────────────────────────────┘

    On ne refait pas un analyseur de portées : on lit les noms GLOBAUX du
    module (fonctions, classes, imports de tête, constantes) et les liaisons
    LOCALES de la fonction (paramètres, affectations, imports différés, boucles,
    gestionnaires d'exception, compréhensions), et tout ce qui n'y figure pas
    est signalé. Les fausses alertes qui en resteraient se voient d'un coup
    d'oeil, là où un nom oublié ne se voit qu'au comptoir.
    """
    arbre = ast.parse(SOURCE.read_text(encoding='utf-8'))

    globaux = set(dir(__builtins__)) if isinstance(__builtins__, dict) is False else set(__builtins__)
    import builtins
    globaux |= set(dir(builtins))

    for noeud in arbre.body:
        if isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            globaux.add(noeud.name)
        elif isinstance(noeud, ast.ImportFrom):
            globaux |= {a.asname or a.name for a in noeud.names}
        elif isinstance(noeud, ast.Import):
            globaux |= {(a.asname or a.name).split('.')[0] for a in noeud.names}
        elif isinstance(noeud, ast.Assign):
            for cible in noeud.targets:
                for n in ast.walk(cible):
                    if isinstance(n, ast.Name):
                        globaux.add(n.id)
        elif isinstance(noeud, ast.AnnAssign) and isinstance(noeud.target, ast.Name):
            globaux.add(noeud.target.id)

    libres = []
    for fonction in arbre.body:
        if not isinstance(fonction, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        lies = set(globaux)
        # Les paramètres, y compris ceux qui ne s'écrivent qu'en mot-clé.
        args = fonction.args
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            lies.add(a.arg)
        if args.vararg:
            lies.add(args.vararg.arg)
        if args.kwarg:
            lies.add(args.kwarg.arg)

        for interne in ast.walk(fonction):
            if isinstance(interne, ast.ImportFrom):
                lies |= {a.asname or a.name for a in interne.names}
            elif isinstance(interne, ast.Import):
                lies |= {(a.asname or a.name).split('.')[0] for a in interne.names}
            elif isinstance(interne, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                lies.add(interne.name)
            elif isinstance(interne, ast.Name) and isinstance(interne.ctx, (ast.Store, ast.Del)):
                lies.add(interne.id)
            elif isinstance(interne, ast.ExceptHandler) and interne.name:
                lies.add(interne.name)
            elif isinstance(interne, ast.Lambda):
                la = interne.args
                for a in (*la.posonlyargs, *la.args, *la.kwonlyargs):
                    lies.add(a.arg)

        for interne in ast.walk(fonction):
            if isinstance(interne, ast.Name) and isinstance(interne.ctx, ast.Load):
                if interne.id not in lies:
                    libres.append((fonction.name, interne.lineno, interne.id))

    return libres


class NomsLibresDesHandlersTests(SimpleTestCase):
    """Un nom qu'un handler lit sans l'avoir lié n'existe nulle part."""

    def test_le_balayage_MORD(self):
        """
        Sans ce contrôle, un analyseur devenu trop permissif rendrait toujours
        une liste vide et le test passerait sans rien démontrer. On vérifie
        qu'il voit bien des corps de handler - donc au moins une fonction
        décorée et plusieurs dizaines de lectures de noms.
        """
        arbre = ast.parse(SOURCE.read_text(encoding='utf-8'))
        corps = [
            n for n in arbre.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.assertGreater(
            len(corps), 40,
            f"Seulement {len(corps)} fonctions trouvées dans {SOURCE} : "
            "le balayage ne balaie plus rien.",
        )

    def test_aucun_handler_ne_lit_un_nom_qui_n_EXISTE_PAS(self):
        libres = noms_libres_par_handler()
        details = [
            f"{SOURCE.name}:{ligne} ({fonction}) : '{nom}' n'est ni importé, "
            "ni défini, ni un paramètre"
            for fonction, ligne, nom in libres
        ]
        self.assertEqual(libres, [], "\n".join(details))
