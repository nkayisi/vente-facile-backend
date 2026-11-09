"""
La cagnotte de fidélité : ce qu'elle gagne, ce qu'elle plafonne, ce qu'on
convertit en y passant.

⚠ CE FICHIER GARDE LA BASCULE, PAS SEULEMENT LES RÈGLES. La migration
  `0016_cagnotte_en_argent` multiplie des SOLDES : un facteur oublié retire au
  client neuf dixièmes de ce qu'il avait gagné, et une migration déjà passée ne
  se rejoue pas. Son arithmétique vit dans `apps/settings/cagnotte_migration`
  pour cette seule raison - voir l'encadré de ce module.
"""
import pathlib
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.settings.cagnotte_migration import (
    convertir_montant, facteur_de_conversion, pourcentage_depuis_bareme,
    pourcentage_depuis_pourcentage,
)
from apps.settings.models import (
    MAX_REDEMPTION_PERCENT_CEILING, CustomerLoyalty, LoyaltyProgram,
)
from apps.contacts.models import Customer
from apps.sales.tests._helpers import make_org_with_users


class ConversionDeLaBasculeTests(TestCase):
    """L'arithmétique qui a transformé des points en argent."""

    def test_un_point_valant_dix_multiplie_le_solde_par_dix(self):
        facteur = facteur_de_conversion(Decimal('10.00'))
        self.assertEqual(facteur, Decimal('10.00'))
        self.assertEqual(convertir_montant(Decimal('40.00'), facteur), Decimal('400.00'))

    def test_un_point_valant_un_laisse_le_solde_en_place(self):
        facteur = facteur_de_conversion(Decimal('1.00'))
        self.assertEqual(convertir_montant(Decimal('2687.22'), facteur), Decimal('2687.22'))

    def test_une_valeur_absente_ou_absurde_ne_convertit_rien(self):
        """
        On préfère ne rien convertir plutôt qu'écrire un solde faux.

        Une division par zéro ou une multiplication par un négatif ne décrivent
        aucune cagnotte réelle ; `1.00` était de toute façon le défaut du champ.
        """
        for valeur in (None, Decimal('0'), Decimal('-5')):
            with self.subTest(valeur=valeur):
                self.assertEqual(facteur_de_conversion(valeur), Decimal('1.00'))

    def test_le_bareme_par_tranches_devient_son_taux_moyen(self):
        """
        « 1 point par 1 000, 1 point = 10 » rend exactement 1 %.

        C'est la fixture de `test_loyalty_invoice` : une vente de 2 000 valait
        2 points, soit 20 en valeur, et 1 % de 2 000 fait 20. L'équivalence est
        exacte sur une tranche pleine, et c'est tout ce qu'on promet.
        """
        self.assertEqual(
            pourcentage_depuis_bareme(1, Decimal('10.00'), Decimal('1000.00')),
            Decimal('1.00'),
        )

    def test_un_bareme_sans_tranche_ne_laisse_pas_le_programme_sans_gain(self):
        """`amount_per_unit` nul : on retombe sur 1 %, pas sur zéro."""
        self.assertEqual(
            pourcentage_depuis_bareme(1, Decimal('10.00'), Decimal('0')),
            Decimal('1.00'),
        )

    def test_le_mode_pourcentage_se_convertit_AUSSI(self):
        """
        ⚠ C'EST LE MODE QU'ON OUBLIE, et l'oublier divise le gain par dix.

        `points_percentage` comptait en POINTS, pas en argent : 1 % avec
        `point_value = 10` rendait 100 points sur une facture de 10 000, soit
        1 000 en valeur - c'est-à-dire 10 % de la facture. Reprendre « 1 » tel
        quel ferait gagner 100 là où le client gagnait 1 000, pendant que son
        SOLDE, lui, aurait bien été multiplié par dix. Les deux moitiés du même
        programme ne parleraient plus de la même unité.
        """
        self.assertEqual(
            pourcentage_depuis_pourcentage(Decimal('1.00'), Decimal('10.00')),
            Decimal('10.00'),
        )

    def test_un_point_valant_un_laisse_le_pourcentage_en_place(self):
        """L'ancien défaut du champ : 1 point = 1 unité, rien à convertir."""
        self.assertEqual(
            pourcentage_depuis_pourcentage(Decimal('2.50'), Decimal('1.00')),
            Decimal('2.50'),
        )

    def test_un_point_valant_moins_d_une_unite_abaisse_le_pourcentage(self):
        """
        L'écart joue dans les DEUX sens, et c'est ce qui le rend dangereux : un
        point à 0,10 faisait gagner dix fois MOINS que son réglage n'annonçait.
        """
        self.assertEqual(
            pourcentage_depuis_pourcentage(Decimal('5.00'), Decimal('0.10')),
            Decimal('0.50'),
        )

    def test_un_pourcentage_converti_reste_dans_les_bornes_du_champ(self):
        """
        Sans bornage, le programme converti serait INÉDITABLE depuis l'écran -
        et un pourcentage nul laisserait un programme sans gain.
        """
        self.assertEqual(
            pourcentage_depuis_pourcentage(Decimal('50.00'), Decimal('10.00')),
            Decimal('100.00'),
        )
        self.assertEqual(
            pourcentage_depuis_pourcentage(Decimal('0'), Decimal('10.00')),
            Decimal('1.00'),
        )

    def test_un_bareme_absurde_reste_dans_les_bornes_du_champ(self):
        """
        Sans bornage, le programme converti serait INÉDITABLE depuis l'écran :
        ses validateurs refuseraient la valeur que la migration a écrite.
        """
        enorme = pourcentage_depuis_bareme(1000, Decimal('100.00'), Decimal('1.00'))
        self.assertEqual(enorme, Decimal('100.00'))
        minuscule = pourcentage_depuis_bareme(1, Decimal('0.01'), Decimal('1000000.00'))
        self.assertEqual(minuscule, Decimal('0.01'))


class ConvertirN_AdresseQueDesChampsQuiEXISTENTTests(TestCase):
    """
    Tout champ que `convertir` lit ou écrit existe-t-il À CET INSTANT ?

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ C'EST LA MOITIÉ QUI MANQUAIT, ET ELLE A COÛTÉ UNE PANNE DE PRODUCTION.  │
    │                                                                          │
    │ `LaMigrationConvertitLesDEUXModesTests` vérifie que les deux branches    │
    │ ÉCRIVENT `earn_percent`. Le mode pourcentage le faisait bien - et LISAIT │
    │ `programme.points_percentage`, un nom que le `RenameField` posé en       │
    │ opération 1 avait déjà fait disparaître de l'état historique. Le         │
    │ déploiement a donné `AttributeError: 'LoyaltyProgram' object has no      │
    │ attribute 'points_percentage'`, `entrypoint.sh` est en `set -e`, donc    │
    │ Gunicorn n'a jamais démarré : plus personne ne pouvait se connecter.     │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ POURQUOI TOUTE LA SUITE ÉTAIT VERTE. `convertir` ne fait QUE boucler sur
      des `objects.all()`, et une base de test est VIDE : les trois corps de
      boucle ne s'exécutent jamais. Un `RunPython` de conversion n'est donc
      éprouvé par AUCUN test de ce dépôt, aujourd'hui comme demain. Et même
      localement, les fixtures n'avaient que des programmes au mode par défaut
      (`fixed_per_amount`), donc la branche fautive - celle du mode POURCENTAGE
      - n'avait jamais été exécutée nulle part.

    ⚠ ON NE TESTE DONC PAS L'EFFET, ON TESTE L'ADRESSAGE : chaque attribut
      touché sur une instance, chaque nom passé à `values_list` et chaque entrée
      d'`update_fields` doit figurer dans l'état du modèle à la position du
      `RunPython`. L'arithmétique, elle, est couverte plus haut sur les
      fonctions pures. C'est sans base de données, et cela vaut pour toute
      migration future qui renomme puis convertit.
    """

    APP = 'settings'

    def _etat_au_runpython(self):
        """L'état des modèles tel que `convertir` le verra, et pas un autre."""
        from django.db import migrations as dj
        from django.db.migrations.loader import MigrationLoader

        loader = MigrationLoader(None, ignore_no_migrations=True)
        cle = (self.APP, '0016_cagnotte_en_argent')
        self.assertIn(cle, loader.disk_migrations, "Migration de bascule absente.")
        migration = loader.disk_migrations[cle]

        etat = loader.project_state((self.APP, '0015_horloge_metier'))

        # ⚠ On rejoue les opérations qui PRÉCÈDENT le `RunPython`, et elles
        #   seules : ce sont les renommages, et c'est exactement ce qui fait
        #   qu'un ancien nom n'existe plus quand la conversion s'exécute.
        vues = 0
        for operation in migration.operations:
            if isinstance(operation, dj.RunPython):
                break
            operation.state_forwards(self.APP, etat)
            vues += 1
        else:  # pragma: no cover
            self.fail("Aucun `RunPython` dans la migration de bascule.")
        self.assertGreater(vues, 0, "Aucune opération avant la conversion.")
        return etat

    @staticmethod
    def _noms_de_champs(etat_modele):
        noms = {'pk'}
        for nom, champ in etat_modele.fields.items():
            noms.add(nom)
            # Une FK s'adresse aussi par sa colonne : `organization_id`.
            if getattr(champ, 'remote_field', None) is not None:
                noms.add(f'{nom}_id')
        return noms

    def test_aucun_champ_adresse_par_la_conversion_n_a_disparu(self):
        import ast

        etat = self._etat_au_runpython()
        modeles = {
            nom.lower(): etat.models[app, nom]
            for app, nom in etat.models
            if app == self.APP
        }
        champs = {
            nom: self._noms_de_champs(modele) for nom, modele in modeles.items()
        }

        source = (
            pathlib.Path(__file__).resolve().parents[1]
            / 'migrations' / '0016_cagnotte_en_argent.py'
        ).read_text(encoding='utf-8')
        module = ast.parse(source)

        def modele_de(noeud):
            """Un `Name` qui NOMME un modèle de l'app, quelle qu'en soit l'origine.

            Reconnaître le nom plutôt que de suivre les affectations couvre d'un
            coup `X = apps.get_model(...)` et le paramètre de
            `_taux_par_organisation`, sans parser deux formes.
            """
            if isinstance(noeud, ast.Name) and noeud.id.lower() in champs:
                return noeud.id.lower()
            return None

        fautes = []

        def exiger(modele, champ, ligne, forme):
            racine = champ.split('__')[0]
            if racine not in champs[modele]:
                fautes.append(
                    f"ligne {ligne} : {forme} `{champ}` n'existe pas sur "
                    f"`{modeles[modele].name}` à cette position "
                    f"(disponibles : {', '.join(sorted(champs[modele]))})"
                )

        for fonction in [n for n in ast.walk(module) if isinstance(n, ast.FunctionDef)]:
            # Variables d'instance : `for <v> in <Modele>.objects.all()`
            instances = {}
            for boucle in [n for n in ast.walk(fonction) if isinstance(n, ast.For)]:
                if not isinstance(boucle.target, ast.Name):
                    continue
                for nom in ast.walk(boucle.iter):
                    modele = modele_de(nom)
                    if modele:
                        instances[boucle.target.id] = modele
                        break

            # Appels de queryset : les noms de champs y voyagent en CHAÎNES, donc
            # aucun `AttributeError` ne les signalerait - ils rendent un
            # `FieldError` à l'exécution, c'est-à-dire au déploiement.
            for appel in [n for n in ast.walk(fonction) if isinstance(n, ast.Call)]:
                if not isinstance(appel.func, ast.Attribute):
                    continue
                if appel.func.attr in {'values_list', 'values', 'only', 'filter', 'exclude'}:
                    modele = None
                    for nom in ast.walk(appel.func.value):
                        modele = modele_de(nom)
                        if modele:
                            break
                    if modele:
                        for arg in appel.args:
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                exiger(modele, arg.value, arg.lineno, 'le champ')
                        for mot in appel.keywords:
                            if mot.arg and mot.arg != 'flat':
                                exiger(modele, mot.arg, appel.lineno, 'le filtre')
                    continue

                # `instance.save(update_fields=[...])`
                if appel.func.attr == 'save':
                    modele = instances.get(
                        appel.func.value.id
                        if isinstance(appel.func.value, ast.Name) else None
                    )
                    if not modele:
                        continue
                    for mot in appel.keywords:
                        if mot.arg == 'update_fields' and isinstance(mot.value, (ast.List, ast.Tuple)):
                            for elt in mot.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    exiger(modele, elt.value, elt.lineno, 'update_fields')

            # Attributs touchés sur une instance : lectures ET écritures.
            appelables = {
                n.func for n in ast.walk(fonction)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            }
            for noeud in ast.walk(fonction):
                if not isinstance(noeud, ast.Attribute) or noeud in appelables:
                    continue
                if not isinstance(noeud.value, ast.Name):
                    continue
                modele = instances.get(noeud.value.id)
                if modele:
                    exiger(modele, noeud.attr, noeud.lineno, "l'attribut")

        self.assertEqual(
            fautes, [],
            "La conversion adresse des champs absents de l'état historique :\n  "
            + "\n  ".join(fautes)
            + "\n\nUn `RenameField` placé AVANT le `RunPython` fait disparaître "
              "l'ancien nom : lire l'ancien lève `AttributeError` au déploiement, "
              "et `entrypoint.sh` est en `set -e`.",
        )


class LaMigrationConvertitLesDEUXModesTests(TestCase):
    """
    ⚠ LES QUATRE TESTS CI-DESSUS ÉPROUVENT DES FONCTIONS PURES, PAS LA
      MIGRATION. C'est la limite qu'il faut nommer : le défaut d'origine n'était
      pas une arithmétique fausse, c'était une BRANCHE MANQUANTE - `convertir`
      ne convertissait que le barème par tranches et reprenait le pourcentage
      tel quel. Les helpers auraient été verts, et le taux de gain d'un
      programme en mode pourcentage divisé par `point_value`.

    On lit donc la STRUCTURE de la migration. ⚠ Garde-fou structurel, pis-aller
    assumé : `convertir` lit `point_value` et `points_calculation_type`, deux
    champs que `RemoveField` a fait disparaître du modèle réel - on ne peut plus
    l'exécuter sans un registre historique, et le dépôt n'a pas d'outillage de
    migration. Ce que ce test prouve est ce qui a manqué : que les DEUX chemins
    écrivent `earn_percent`.
    """

    def _fonction_convertir(self):
        import ast
        import pathlib

        chemin = (
            pathlib.Path(__file__).resolve().parents[1]
            / 'migrations' / '0016_cagnotte_en_argent.py'
        )
        self.assertTrue(chemin.exists(), f"Migration introuvable : {chemin}")
        arbre = ast.parse(chemin.read_text(encoding='utf-8'))
        for noeud in arbre.body:
            if isinstance(noeud, ast.FunctionDef) and noeud.name == 'convertir':
                return noeud
        self.fail("`convertir` est absente de la migration de bascule.")

    def test_les_deux_branches_ecrivent_earn_percent(self):
        import ast

        convertir = self._fonction_convertir()

        # Le `if` qui distingue les deux modes de calcul, où qu'il soit.
        tests = [
            n for n in ast.walk(convertir)
            if isinstance(n, ast.If)
            and 'points_calculation_type' in ast.dump(n.test)
        ]
        self.assertEqual(
            len(tests), 1,
            "Un seul embranchement doit distinguer les deux modes de calcul.",
        )
        branche = tests[0]

        def ecrit_earn_percent(corps):
            return any(
                isinstance(cible, ast.Attribute) and cible.attr == 'earn_percent'
                for n in ast.walk(ast.Module(body=corps, type_ignores=[]))
                if isinstance(n, ast.Assign)
                for cible in n.targets
            )

        self.assertTrue(
            ecrit_earn_percent(branche.body),
            "Le barème par tranches ne pose pas `earn_percent`.",
        )
        # ⚠ C'EST CETTE ASSERTION QUI MANQUAIT. Le mode pourcentage comptait en
        #   POINTS lui aussi : reprendre son réglage tel quel divise le gain par
        #   `point_value`, pendant que les SOLDES, eux, sont multipliés par lui.
        self.assertTrue(
            branche.orelse and ecrit_earn_percent(branche.orelse),
            "Le mode POURCENTAGE ne pose pas `earn_percent` : son réglage "
            "comptait en points, le reprendre tel quel divise le gain futur "
            "par `point_value`.",
        )

    def test_elle_emploie_les_deux_convertisseurs(self):
        """
        Les deux noms sont ceux que `cagnotte_migration` expose et que les
        quatre tests du haut éprouvent : sans eux, la branche pourrait écrire
        une arithmétique de son cru, que rien ne couvrirait.
        """
        import ast

        appels = {
            n.func.id
            for n in ast.walk(self._fonction_convertir())
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        self.assertIn('pourcentage_depuis_bareme', appels)
        self.assertIn('pourcentage_depuis_pourcentage', appels)


class GainDeCagnotteTests(TestCase):
    """`LoyaltyProgram.credit_for` : le pourcentage, et rien d'autre."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, is_active=True, earn_percent=Decimal('2.00'),
        )

    def test_deux_pour_cent_de_dix_mille_font_deux_cents(self):
        self.assertEqual(self.program.credit_for(Decimal('10000')), Decimal('200.00'))

    def test_le_pourcentage_est_continu_et_ne_tronque_aucune_tranche(self):
        """
        ⚠ C'EST LA DIFFÉRENCE AVEC LE BARÈME RETIRÉ. « X points pour CHAQUE Y »
        ne comptait pas la tranche entamée : un petit panier ne rapportait rien.
        1 % de 58 vaut 0,58, et c'est crédité.
        """
        self.program.earn_percent = Decimal('1.00')
        self.assertEqual(self.program.credit_for(Decimal('58')), Decimal('0.58'))

    def test_un_programme_INACTIF_ne_credite_rien(self):
        """
        La frontière. Sans elle, un marchand qui désactive son programme
        continuerait d'alimenter les cagnottes de ses clients.
        """
        self.program.is_active = False
        self.assertEqual(self.program.credit_for(Decimal('10000')), Decimal('0.00'))

    def test_un_montant_nul_ou_negatif_ne_credite_rien(self):
        for montant in (Decimal('0'), Decimal('-500')):
            with self.subTest(montant=montant):
                self.assertEqual(self.program.credit_for(montant), Decimal('0.00'))


class PlafondDeLaCagnotteTests(TestCase):
    """`max_redeemable_amount` : la cagnotte ne solde jamais toute une facture."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, is_active=True, earn_percent=Decimal('1.00'),
            max_redemption_percent=Decimal('50.00'),
        )

    def test_la_moitie_d_une_facture_de_deux_mille(self):
        self.assertEqual(
            self.program.max_redeemable_amount(Decimal('2000')), Decimal('1000.00'),
        )

    def test_une_valeur_ecrite_HORS_VALIDATION_est_re_bornee(self):
        """
        Le validateur ne protège que les écritures API. Un `UPDATE` direct ou
        une vieille fixture rendrait sinon la garantie caduque.
        """
        LoyaltyProgram.objects.filter(pk=self.program.pk).update(
            max_redemption_percent=Decimal('90.00'),
        )
        self.program.refresh_from_db()
        self.assertEqual(
            self.program.max_redeemable_amount(Decimal('2000')), Decimal('1400.00'),
        )
        self.assertEqual(MAX_REDEMPTION_PERCENT_CEILING, Decimal('70.00'))

    def test_il_reste_TOUJOURS_quelque_chose_a_encaisser(self):
        """L'invariant qui porte tout le plafond, sur toute une plage."""
        for pourcent in ('0.01', '25.00', '50.00', '70.00'):
            with self.subTest(pourcent=pourcent):
                self.program.max_redemption_percent = Decimal(pourcent)
                total = Decimal('12345.67')
                self.assertLess(self.program.max_redeemable_amount(total), total)


class ConfigurationReduiteTests(APITestCase):
    """Ce que l'API expose, et ce qu'elle refuse."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, is_active=True, earn_percent=Decimal('1.00'),
        )
        self.client.force_authenticate(user=self.owner)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def test_le_programme_n_expose_plus_aucun_champ_de_points(self):
        """
        ⚠ CE TEST GARDE LE VOCABULAIRE, et c'est ce qu'on a retiré. Un champ
        `point_value` ou `min_points_to_redeem` qui resurgirait au contrat
        ferait réapparaître des réglages dont le marchand ne peut pas prévoir
        l'effet.
        """
        resp = self.client.get('/api/v1/settings/loyalty-program/', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(
            set(resp.data) & {
                'point_value', 'min_points_to_redeem', 'points_calculation_type',
                'points_per_unit', 'amount_per_unit', 'points_percentage',
                'points_expiry_days',
            },
            set(),
        )
        self.assertIn('earn_percent', resp.data)
        self.assertIn('max_redemption_percent_ceiling', resp.data)

    def test_l_apercu_chiffre_dit_les_deux_reglages_sur_une_meme_facture(self):
        """
        ⚠ IL REMPLACE `calculate_points`, qui ne rendait qu'un nombre de points
        dont le marchand ne pouvait pas déduire la valeur.
        """
        resp = self.client.post(
            f'/api/v1/settings/loyalty-program/{self.program.id}/simulate/',
            {'amount': '10000'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.data['earned'], Decimal('100.00'))
        self.assertEqual(resp.data['max_redeemable'], Decimal('5000.00'))

    def test_un_plafond_au_dela_de_la_borne_est_refuse_par_un_message_de_champ(self):
        resp = self.client.patch(
            f'/api/v1/settings/loyalty-program/{self.program.id}/',
            {'max_redemption_percent': '90.00'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('max_redemption_percent', resp.data)

    def test_la_cagnotte_voyage_en_CHAINE_comme_tout_montant(self):
        """
        ⚠ ELLE SORTAIT EN NOMBRE du temps des points (`coerce_to_string=False`).
        C'est de l'argent : un solde en CDF atteint sept chiffres, et un nombre
        JavaScript y perd ses unités.
        """
        client = Customer.objects.create(
            organization=self.org, name='Nelly', code='C-CAG',
        )
        CustomerLoyalty.objects.create(
            organization=self.org, customer=client, balance=Decimal('1234567.89'),
        )
        resp = self.client.get(
            f'/api/v1/settings/customer-loyalty/?customer={client.id}', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.data[0]['balance'], '1234567.89')
        self.assertIsInstance(resp.data[0]['balance'], str)


class AjustementDeCagnotteTests(APITestCase):
    """La correction manuelle, qui a remplacé `adjust_points`."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        LoyaltyProgram.objects.create(
            organization=self.org, is_active=True, earn_percent=Decimal('1.00'),
        )
        self.customer = Customer.objects.create(
            organization=self.org, name='Nelly', code='C-AJU',
        )
        self.client.force_authenticate(user=self.owner)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _ajuster(self, montant):
        return self.client.post(
            '/api/v1/settings/customer-loyalty/adjust-balance/',
            {'customer_id': str(self.customer.id), 'amount': montant},
            format='json', **self._headers(),
        )

    def test_un_ajustement_positif_credite_la_cagnotte(self):
        resp = self._ajuster('1000')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.data['balance'], '1000.00')

    def test_un_ajustement_negatif_au_dela_du_solde_est_REFUSE(self):
        """
        Et le refus est une PHRASE, pas un 500 : `debit` lève `RefusMetier`,
        que la vue traduit. Sans cela la cagnotte passerait sous zéro.
        """
        self._ajuster('100')
        resp = self._ajuster('-500')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('error', resp.data)

    def test_un_ajustement_nul_est_refuse(self):
        resp = self._ajuster('0')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
