"""
Le périmètre a deux natures, et aucun appel ne peut l'oublier.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LES ANCIENS NOMS SONT SUPPRIMÉS, PAS RENOMMÉS, ET ILS NE REVIENNENT PAS.    │
│                                                                              │
│ `accessible_warehouse_ids` et `assert_warehouse_allowed_for_*` ne savaient   │
│ pas si l'on LISAIT ou si l'on AGISSAIT : la case « consulter tous les        │
│ entrepôts » ne pouvait pas exister avec eux. Leur suppression a forcé la    │
│ revue de chaque site d'appel ; ce test empêche qu'on les réintroduise par   │
│ un import recopié d'un ancien fichier.                                       │
│                                                                              │
│ ET CHAQUE FILTRE DIT SA NATURE. Un site de lecture mis en action prive un    │
│ membre d'une donnée ; un site d'action mis en lecture le laisse agir dans    │
│ un entrepôt qui ne lui est pas affecté. Aucun défaut n'est sûr, donc il n'y │
│ en a aucun : `nature=` est obligatoire, et l'AST le vérifie.                 │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ L'AST et non des lignes : un appel s'écrit sur une ou sept lignes.
"""
import ast
from pathlib import Path

from django.test import SimpleTestCase

RACINE = Path(__file__).resolve().parents[2]

DISPARUS = {
    'accessible_warehouse_ids',
    'assert_warehouse_allowed_for_request',
    'assert_warehouse_allowed_for_membership',
    'restrict_visibility_for_membership',
    'restrict_visibility_for_request',
}
EXIGENT_NATURE = {
    'filter_queryset_by_warehouse_ids',
    'filter_queryset_by_related_warehouse',
    'filter_stock_transfer_queryset',
    'filter_product_queryset',
    'filter_cash_movement_queryset',
    'borner_visibilite',
}


def _fichiers():
    for chemin in sorted(RACINE.rglob('*.py')):
        if '/migrations/' in str(chemin):
            continue
        yield chemin


def _nom_appele(noeud):
    if isinstance(noeud.func, ast.Name):
        return noeud.func.id
    if isinstance(noeud.func, ast.Attribute):
        return noeud.func.attr
    return None


class DoctrineDuPerimetreTests(SimpleTestCase):

    def test_le_balayage_balaie_quelque_chose(self):
        """Un balayage qui ne trouve rien passe au vert sans rien prouver."""
        self.assertGreater(sum(1 for _ in _fichiers()), 150)

    def test_aucun_ancien_nom_ne_revient(self):
        fautes = []
        for chemin in _fichiers():
            if chemin.name == Path(__file__).name:
                continue
            arbre = ast.parse(chemin.read_text(encoding='utf-8'))
            for noeud in ast.walk(arbre):
                if isinstance(noeud, ast.ImportFrom):
                    for alias in noeud.names:
                        if alias.name in DISPARUS:
                            fautes.append(f'{chemin}:{noeud.lineno} importe {alias.name}')
                elif isinstance(noeud, ast.Call) and _nom_appele(noeud) in DISPARUS:
                    fautes.append(f'{chemin}:{noeud.lineno} appelle {_nom_appele(noeud)}')
        self.assertEqual(fautes, [], '\n'.join(fautes))

    def test_chaque_filtre_dit_sa_nature(self):
        fautes = []
        vus = 0
        for chemin in _fichiers():
            if chemin.name == 'warehouse_scope.py' or '/tests/' in str(chemin):
                continue
            arbre = ast.parse(chemin.read_text(encoding='utf-8'))
            for noeud in ast.walk(arbre):
                if isinstance(noeud, ast.Call) and _nom_appele(noeud) in EXIGENT_NATURE:
                    vus += 1
                    if 'nature' not in {k.arg for k in noeud.keywords}:
                        fautes.append(
                            f'{chemin}:{noeud.lineno} {_nom_appele(noeud)} sans `nature=`'
                        )
        # Le compte d'abord : un balayage qui ne voit aucun appel ne prouve rien.
        self.assertGreater(vus, 15)
        self.assertEqual(fautes, [], '\n'.join(fautes))

    def test_chaque_lecture_dit_son_etendue(self):
        """
        ⚠ Une lecture sans étendue montrerait TOUT par défaut : le caissier qui a
        « Voir tout dans son entrepôt » verrait les ventes de ses collègues sur
        un écran sans filtre, c'est-à-dire exactement ce que la règle du
        2026-11-16 ferme. Seule une ACTION écrite en toutes lettres s'en passe.
        """
        fautes = []
        vus = 0
        for chemin in _fichiers():
            if chemin.name == 'warehouse_scope.py' or '/tests/' in str(chemin):
                continue
            arbre = ast.parse(chemin.read_text(encoding='utf-8'))
            for noeud in ast.walk(arbre):
                if not isinstance(noeud, ast.Call):
                    continue
                nom = _nom_appele(noeud)
                mots = {k.arg: k.value for k in noeud.keywords}
                if nom in EXIGENT_NATURE:
                    nature = mots.get('nature')
                    a_etendue = 'etendue' in mots
                elif nom == 'entrepots_du_perimetre':
                    nature = noeud.args[1] if len(noeud.args) > 1 else mots.get('nature')
                    a_etendue = len(noeud.args) > 2 or 'etendue' in mots
                else:
                    continue
                vus += 1
                if isinstance(nature, ast.Name) and nature.id == 'ACTION':
                    continue
                if not a_etendue:
                    fautes.append(f'{chemin}:{noeud.lineno} {nom} lit sans `etendue=`')
        self.assertGreater(vus, 20)
        self.assertEqual(fautes, [], '\n'.join(fautes))
