"""Le référentiel de devises descend bien en base, et à l'identique.

Ce que ce test protège : la table `Currency` est l'AUTORITÉ du serveur depuis que
l'inscription valide contre elle (`apps.settings.currency_validation`). Une devise
qui manquerait en base serait refusée à l'inscription alors que les deux clients
l'offrent au choix, et un `decimal_places` qui dériverait imprimerait un montant
faux sur le ticket du marchand sans rien refuser du tout.

⚠ LA RÉFÉRENCE EST LUE DANS LA MIGRATION, jamais recopiée ici. Sa liste est une
variable locale d'une fonction de migration, donc non importable : on la lit comme
du TEXTE. Une liste recopiée dans un test est exactement ce qui a laissé passer
trois codes fantômes dans `permissions.test.ts` du terminal - il validait ce qu'il
aurait dû refuser.

Le croisement avec `core/src/currencies.ts`, l'autre bout de la chaîne, vit dans
`frontend/scripts/check-devises.mjs` : le conteneur backend ne monte que
`./backend:/app`, donc un test qui lirait `../core/` échouerait en Docker.
"""
import re
from pathlib import Path

from django.test import TestCase

from apps.settings.models import Currency

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / 'migrations'
    / '0004_add_all_supported_currencies.py'
)

ENTREE = re.compile(
    r"\{'code':\s*'([A-Z]{3})',\s*'name':\s*'([^']+)',"
    r"\s*'symbol':\s*'([^']+)',\s*'decimal_places':\s*(\d+)\}"
)


def _attendu():
    source = MIGRATION.read_text(encoding='utf-8')
    return {
        code: {'name': name, 'symbol': symbol, 'decimal_places': int(dec)}
        for code, name, symbol, dec in ENTREE.findall(source)
    }


class ReferentielDeDevisesTests(TestCase):
    def test_le_balayage_MORD(self):
        """Un balayage qui ne balaie rien passe au vert sans rien démontrer."""
        self.assertTrue(MIGRATION.exists(), f"Migration introuvable : {MIGRATION}")
        self.assertGreaterEqual(len(_attendu()), 21)

    def test_chaque_devise_de_la_migration_est_en_base_et_active(self):
        manquantes = []
        for code in _attendu():
            if not Currency.objects.filter(code=code, is_active=True).exists():
                manquantes.append(code)
        self.assertEqual(
            manquantes, [],
            "Devises absentes ou inactives : l'inscription les refusera alors "
            "que les deux clients les offrent.",
        )

    def test_nom_symbole_et_decimales_sont_a_l_identique(self):
        """⚠ Les décimales d'abord : ce sont elles qu'un ticket imprime."""
        ecarts = []
        for code, attendu in _attendu().items():
            ligne = (
                Currency.objects.filter(code=code)
                .values('name', 'symbol', 'decimal_places')
                .first()
            )
            if ligne is None:
                continue  # déjà couvert par le test précédent
            for champ, valeur in attendu.items():
                if ligne[champ] != valeur:
                    ecarts.append(f"{code}.{champ} : base={ligne[champ]!r} migration={valeur!r}")
        self.assertEqual(ecarts, [])

    def test_les_devises_sans_decimale_le_restent(self):
        """Témoin lisible : ces six monnaies n'ont pas de centime.

        ⚠ Ces codes sont écrits ici VOLONTAIREMENT, et c'est la seule liste en dur
        du fichier : c'est un TÉMOIN, pas une référence. Il dit ce que le lot a
        voulu garantir, et il tomberait si la migration elle-même se mettait à
        déclarer un franc CFA à deux décimales - ce que le croisement ci-dessus,
        qui ne compare que deux fichiers l'un à l'autre, ne verrait pas.
        """
        sans_decimale = set(
            Currency.objects.filter(decimal_places=0).values_list('code', flat=True)
        )
        self.assertEqual(sans_decimale, {'CDF', 'XAF', 'XOF', 'RWF', 'UGX', 'JPY'})
