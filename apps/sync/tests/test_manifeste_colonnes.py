"""
Une liste de colonnes FIGÉE ne nomme jamais une colonne que `ALL` écarterait.

┌──────────────────────────────────────────────────────────────────────────┐
│ LE TERMINAL INSÈRE LES COLONNES DU MANIFESTE, TELLES QUELLES.            │
│                                                                          │
│ Son schéma local est engendré depuis ce manifeste ; une colonne qu'il ne │
│ connaît pas fait échouer l'insertion (« table has no column named … »),  │
│ et le tirage de la table s'arrête, sur tout le parc. Une liste figée     │
│ (`fields=…`) sert à NE PAS exposer une colonne neuve aux terminaux       │
│ anciens ; elle ne doit donc jamais en exposer une que `ALL` n'exposait   │
│ pas. La première version figée de `memberships` et `quotations` nommait  │
│ `organization_id`, que `NEVER_SENT` écarte depuis toujours.               │
└──────────────────────────────────────────────────────────────────────────┘
"""
from django.test import SimpleTestCase

from apps.sync.pull import ALL, PULL_TABLES, resolve_fields


def _toutes_les_tables():
    for table in PULL_TABLES:
        yield table.name, table.get_model(), table.fields
        for enfant in table.children:
            yield enfant.table, enfant.get_model(), enfant.fields


class ListeFigeeTests(SimpleTestCase):

    def test_le_balayage_voit_des_listes_figees(self):
        figees = [nom for nom, _, champs in _toutes_les_tables() if champs != ALL]
        self.assertGreaterEqual(len(figees), 3, figees)

    def test_une_liste_figee_ne_nomme_rien_que_ALL_ecarte(self):
        fautifs = []
        for nom, modele, champs in _toutes_les_tables():
            if champs == ALL:
                continue
            permis = set(resolve_fields(modele, ALL))
            for colonne in champs:
                if colonne not in permis:
                    fautifs.append(f'{nom}.{colonne}')
        self.assertEqual(fautifs, [])
