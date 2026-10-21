"""
Monte une boutique de test complète, avec un compte par rôle.

┌──────────────────────────────────────────────────────────────────────────────┐
│ POURQUOI UNE BOUTIQUE DÉDIÉE, ET PAS LES DONNÉES DE DÉVELOPPEMENT.          │
│                                                                              │
│ La moitié des défauts de périmètre de ce dépôt a échappé aux contrôles       │
│ précédents pour UNE raison : ils étaient faits en `owner`, pour qui          │
│ `accessible_warehouse_ids` sort en amont et la moitié du code de périmètre   │
│ n'est jamais exécutée. Les données de développement ne portent d'ailleurs    │
│ aucun `manager`, aucun `stock_keeper`, aucun compte sans entrepôt, et une    │
│ seule devise.                                                                │
│                                                                              │
│ Cette commande monte donc exactement ce qui manque pour éprouver une règle   │
│ de périmètre : DEUX entrepôts, DEUX devises, et SIX comptes dont un borné    │
│ par dépôt, un borné par auteur, et un sans aucune affectation.               │
└──────────────────────────────────────────────────────────────────────────────┘

    python manage.py seed_boutique_test --dry-run
    python manage.py seed_boutique_test

Idempotente : relancée, elle ne duplique rien et rend le même état.
"""
from decimal import Decimal

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

#: Le mot de passe des six comptes.
#:
#: ┌──────────────────────────────────────────────────────────────────────────────┐
#: │ IL EST PUBLIÉ DANS LA SOURCE, ET C'EST POURQUOI LA COMMANDE SE REFUSE HORS  │
#: │ DÉVELOPPEMENT.                                                               │
#: │                                                                              │
#: │ Elle crée SIX comptes, dont un `owner` d'organisation. Lancée contre la base │
#: │ de production - une méprise facile : les deux commandes sont adjacentes dans │
#: │ la documentation, et `assign_default_warehouse` est une normalisation        │
#: │ légitime de production - elle livre un accès propriétaire à quiconque lit ce │
#: │ dépôt. Et `owner@test.vf` est repris PAR COURRIEL, globalement : son mot de  │
#: │ passe est RÉINITIALISÉ s'il diffère, donc la commande peut s'emparer d'un    │
#: │ compte existant.                                                             │
#: └──────────────────────────────────────────────────────────────────────────────┘
#:
#: ⚠ LE DÉFAUT LITTÉRAL NE SERT QU'EN `DEBUG`. Le retirer rendrait la commande
#:   inutilisable pour ce qu'elle existe : éprouver les six rôles en local.
#:   `VF_SEED_PASSWORD` le remplace partout ailleurs.
MOT_DE_PASSE = os.environ.get("VF_SEED_PASSWORD") or "Test@2026"
NOM_ORG = "Boutique Test VF"

# ⚠ Le rôle le plus FAIBLE qui porte la permission de l'acte, jamais `owner`.
#   `orphelin` est le cas qui expose toute l'organisation quand une règle de
#   périmètre répond « aucune restriction » au lieu de « aucun accès ».
COMPTES = [
    ("owner@test.vf", "Awa", "Owner", "owner", []),
    ("gerant@test.vf", "Bob", "Gérant", "manager", ["WH-MAIN"]),
    ("magasinier@test.vf", "Cédric", "Magasinier", "stock_keeper", ["WH-NORD"]),
    ("caissier@test.vf", "Dina", "Caissière", "cashier", ["WH-MAIN"]),
    ("caissier2@test.vf", "Elie", "Caissier", "cashier", ["WH-NORD"]),
    ("orphelin@test.vf", "Fany", "Orpheline", "manager", []),
]

# (sku, nom, selling_mode, facteur, cout, detail, gros, actif, vendable, suivi, seuil, reappro)
CATALOGUE = [
    ("T-DET", "Savon Mbuji (détail)", "retail_only", None, 800, 1200, 0, True, True, True, 10, 50),
    ("T-GROS", "Carton de savon (gros)", "wholesale_only", 24, 17000, 0, 24000, True, True, True, 2, 10),
    ("T-MIXTE", "Bière Primus 24x33cl", "wholesale_and_retail", 24, 500, 900, 19000, True, True, True, 48, 240),
    ("T-PERIS", "Lait frais 1L", "retail_only", None, 1500, 2300, 0, True, True, True, 12, 60),
    ("T-NOSTOCK", "Service de livraison", "retail_only", None, 0, 5000, 0, True, True, False, 5, 0),
    ("T-INACTIF", "Ancien modèle (inactif)", "retail_only", None, 100, 200, 0, False, True, True, 0, 0),
    ("T-NOVENTE", "Emballage interne", "retail_only", None, 50, 0, 0, True, False, True, 0, 0),
]


class Command(BaseCommand):
    help = "Monte la boutique de test et ses six comptes de rôle (idempotente)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Montre ce qui serait fait, sans rien écrire.")
        parser.add_argument(
            "--force", action="store_true",
            help=(
                "Lancer hors développement. À n'employer qu'en sachant que la "
                "commande crée six comptes, dont un propriétaire."
            ),
        )

    def handle(self, *args, **options):
        self.sec = options["dry_run"]
        # ⚠ L'APERÇU PASSE, SEULE L'ÉCRITURE EST REFUSÉE. Le refus s'exécutait
        #   AVANT que `--dry-run` ne soit lu, si bien que l'aperçu en lecture
        #   seule - qui n'écrit rien, et qui est la façon la plus sûre
        #   d'inspecter avant de décider - était refusé avec le chemin
        #   d'écriture, alors que la docstring de ce module l'annonce.
        #   L'opérateur apprenait à tendre vers `--force` par réflexe, ce qui
        #   est exactement ce que ce refus cherche à rendre délibéré.
        if not self.sec:
            self._refuser_hors_developpement(options["force"])
        if self.sec:
            self.stdout.write(self.style.WARNING("--dry-run : aucune écriture.\n"))
        try:
            with transaction.atomic():
                self._monter()
                if self.sec:
                    raise _Annule()
        except _Annule:
            self.stdout.write(self.style.WARNING("\nAnnulé (--dry-run)."))

    # ------------------------------------------------------------------ verrou

    def _refuser_hors_developpement(self, force):
        """
        Deux verrous, comme la remise à zéro d'essai.

        ⚠ LE REFUS NOMME LA BASE VISÉE. « Je refuse » sans dire contre quoi
        ferait relancer avec `--force` par réflexe - et c'est précisément le
        geste qu'on cherche à rendre délibéré.

        ⚠ IL NE VISE QUE LE CHEMIN D'ÉCRITURE. `--dry-run` n'écrit rien : le
        refuser lui aussi retirait le seul moyen d'inspecter sans engager.
        """
        if settings.DEBUG or force:
            return
        base = settings.DATABASES["default"].get("NAME", "?")
        hote = settings.DATABASES["default"].get("HOST") or "local"
        raise CommandError(
            f"Refusé : `DEBUG` est faux, donc cette base ({base} sur {hote}) "
            "n'est pas une base de développement. Cette commande crée six "
            f"comptes dont un PROPRIÉTAIRE, au mot de passe de "
            f"{_source_du_mot_de_passe()}.\n"
            "Si c'est bien ce que vous voulez : relancez avec --force, et "
            "posez VF_SEED_PASSWORD."
        )

    # ------------------------------------------------------------------ étapes

    def _monter(self):
        from apps.organizations.models import Organization

        org = Organization.objects.filter(name=NOM_ORG).first()
        neuve = org is None
        if neuve:
            org = self._creer_org()
        else:
            self.stdout.write(f"Organisation déjà là : {org.name} ({org.id})")

        self._devises(org)
        entrepots = self._entrepots(org)
        self._caisses(org, entrepots)
        self._comptes(org, entrepots)
        refs = self._catalogue(org)
        self._stock(org, entrepots, refs)
        self._clients(org)
        self._depense_brouillon(org, entrepots)
        self._resume(org)

    def _creer_org(self):
        from apps.core.services import OrganizationService
        from apps.users.models import User

        patron, cree = User.objects.get_or_create(
            email="owner@test.vf",
            defaults={"first_name": "Awa", "last_name": "Owner", "is_active": True},
        )
        if cree or not patron.check_password(MOT_DE_PASSE):
            patron.set_password(MOT_DE_PASSE)
            patron.save()

        # ⚠ On passe par le SERVICE, jamais par un `Organization.objects.create`
        #   à la main : il monte la branche, l'entrepôt principal, la caisse, les
        #   deux moyens de paiement, les dix catégories de caisse, la devise
        #   primaire et l'abonnement d'essai. Les recréer ici donnerait une
        #   boutique qui ne ressemble pas à celle d'un vrai marchand.
        org = OrganizationService.create_organization(
            patron, NOM_ORG, "retail",
            currency="CDF", city="Kinshasa", country="RD Congo",
            phone="+243970000000", email="contact@test.vf",
            rccm="CD/KIN/RCCM/00-X-0001", id_nat="01-X-N00001X",
        )
        self.stdout.write(self.style.SUCCESS(f"Organisation créée : {org.name} ({org.id})"))
        return org

    def _devises(self, org):
        """CDF primaire, USD secondaire : le terrain de la moitié des défauts."""
        from apps.settings.models import Currency, OrganizationCurrency

        usd = Currency.objects.filter(code="USD").first()
        if not usd:
            self.stdout.write(self.style.ERROR("  USD absent de la table Currency."))
            return
        oc, cree = OrganizationCurrency.objects.get_or_create(
            organization=org, currency=usd,
            defaults={"is_primary": False, "exchange_rate": Decimal("2800"), "is_active": True},
        )
        if not cree and oc.exchange_rate != Decimal("2800"):
            oc.exchange_rate = Decimal("2800")
            oc.save(update_fields=["exchange_rate", "updated_at"])
        self.stdout.write(f"  Devises : CDF (primaire) + USD @ {oc.exchange_rate}")

    def _entrepots(self, org):
        from apps.inventory.models import Warehouse
        from apps.organizations.models import Branch

        branche = Branch.objects.filter(organization=org, is_main=True).first()
        sortie = {}
        for code, nom, defaut in [("WH-MAIN", "Dépôt Central", True),
                                  ("WH-NORD", "Boutique Nord", False)]:
            w, cree = Warehouse.objects.get_or_create(
                organization=org, code=code,
                defaults={"name": nom, "branch": branche,
                          "is_default": defaut, "is_active": True},
            )
            sortie[code] = w
            self.stdout.write(f"  Entrepôt {'créé ' if cree else 'déjà là'} : {w.name} ({code})")
        return sortie

    def _caisses(self, org, entrepots):
        from apps.organizations.models import Branch
        from apps.sales.models import Register

        branche = Branch.objects.filter(organization=org, is_main=True).first()
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ UNE CAISSE SANS ENTREPÔT N'EXISTE PAS, ET NE PEUT PAS EXISTER.   │
        # │                                                                  │
        # │ `Register.warehouse` est une FK sans `null=True` : PostgreSQL    │
        # │ refuse l'insertion (mesuré en montant cette boutique).           │
        # │                                                                  │
        # │ Conséquence à ne pas oublier : toutes les branches du terminal   │
        # │ qui traitent ce cas - le `leftJoin warehouses` de                │
        # │ `features/pos/caisse.ts`, le bandeau « Caisse sans entrepôt » de │
        # │ l'onglet de vente, le repli `entrepotInconnuAdmis` - sont du     │
        # │ code DÉFENSIF INATTEIGNABLE. Elles ne nuisent pas, mais elles ne │
        # │ peuvent pas non plus être la cause d'un défaut observé.          │
        # │                                                                  │
        # │ ⚠ Ne pas confondre avec `Sale.warehouse`, qui est bien nullable  │
        # │   (d'où `include_null_warehouse=True` sur les ventes anciennes). │
        # └──────────────────────────────────────────────────────────────────┘
        plan = [("REG-MAIN", "Caisse Centrale", "WH-MAIN"),
                ("REG-NORD", "Caisse Nord", "WH-NORD")]
        for code, nom, we in plan:
            r, cree = Register.objects.get_or_create(
                organization=org, code=code,
                defaults={"name": nom, "branch": branche,
                          "warehouse": entrepots[we], "is_active": True},
            )
            self.stdout.write(f"  Caisse {'créée ' if cree else 'déjà là'} : {r.name}")

    def _comptes(self, org, entrepots):
        from apps.core.services import PermissionService
        from apps.organizations.models import OrganizationMembership
        from apps.users.models import User

        for email, prenom, nom, role, codes in COMPTES:
            u, cree = User.objects.get_or_create(
                email=email,
                defaults={"first_name": prenom, "last_name": nom, "is_active": True})
            if cree or not u.check_password(MOT_DE_PASSE):
                u.set_password(MOT_DE_PASSE)
                u.save()
            m, _ = OrganizationMembership.objects.get_or_create(
                user=u, organization=org,
                defaults={"role": role, "is_active": True})
            if m.role != role:
                m.role = role
                m.save(update_fields=["role", "updated_at"])
            voulus = {entrepots[c] for c in codes if c in entrepots}
            m.assigned_warehouses.set(voulus)
            if role == "owner":
                PermissionService.assign_owner_permissions(u, org)
            if not u.active_organization_id:
                u.active_organization = org
                u.save(update_fields=["active_organization"])
            quoi = ", ".join(sorted(w.code for w in voulus)) or "AUCUN entrepôt"
            self.stdout.write(f"  {email:22} {role:13} [{quoi}]")

    def _catalogue(self, org):
        from apps.products.models import Category, Product, Unit

        cat, _ = Category.objects.get_or_create(
            organization=org, slug="test-general",
            defaults={"name": "Général (test)", "is_active": True})
        piece, _ = Unit.objects.get_or_create(
            organization=org, name="PIECE", defaults={"symbol": "pce"})
        carton, _ = Unit.objects.get_or_create(
            organization=org, name="CARTON", defaults={"symbol": "crt"})

        refs = {}
        for (sku, nom, mode, fact, cout, det, gros,
             actif, vendable, suivi, seuil, reappro) in CATALOGUE:
            champs = {
                # ⚠ `Product.slug` est unique par organisation et N'EST PAS
                #   dérivé : sans lui, le second article viole la contrainte sur
                #   une chaîne vide. Même piège que `Category.slug` et
                #   `Brand.slug`, déjà payé sur le terminal.
                "slug": sku.lower(),
                "name": nom, "category": cat, "unit": piece,
                "selling_mode": mode,
                "cost_price": Decimal(cout), "selling_price": Decimal(det),
                "wholesale_price": Decimal(gros),
                "is_active": actif, "is_sellable": vendable,
                "track_inventory": suivi,
                "reorder_point": seuil, "reorder_quantity": reappro,
            }
            if fact:
                champs["packaging_unit"] = carton
                champs["units_per_package"] = fact
                champs["package_cost_price"] = Decimal(cout) * fact
            p, cree = Product.objects.get_or_create(
                organization=org, sku=sku, defaults=champs)
            refs[sku] = p
            self.stdout.write(f"  Article {'créé ' if cree else 'déjà là'} : {sku:12} {nom}")
        return refs

    def _stock(self, org, entrepots, refs):
        """Du stock dans les DEUX dépôts, et un article SOUS son seuil."""
        from apps.inventory.models import Stock

        # ⚠ `quantity` DOIT valoir `paquets x facteur + vrac`, sinon `Stock.save`
        #   reporte l'écart sur le vrac en journalisant un avertissement, et la
        #   boutique ne porte plus les compteurs qu'on croyait y mettre.
        #   (entrepôt, sku, total, paquets scellés, vrac)
        plan = [
            ("WH-MAIN", "T-DET", 120, 0, 0),
            ("WH-MAIN", "T-GROS", 144, 6, 0),
            ("WH-MAIN", "T-MIXTE", 300, 12, 12),
            ("WH-MAIN", "T-PERIS", 8, 0, 0),      # seuil 12 : SOUS LE SEUIL
            ("WH-NORD", "T-DET", 40, 0, 0),
            ("WH-NORD", "T-MIXTE", 72, 3, 0),
            ("WH-NORD", "T-GROS", 24, 1, 0),
            ("WH-NORD", "T-PERIS", 0, 0, 0),      # RUPTURE
        ]
        for we, sku, qte, paquets, vrac in plan:
            w, p = entrepots.get(we), refs.get(sku)
            if not (w and p):
                continue
            st, cree = Stock.objects.get_or_create(
                organization=org, product=p, warehouse=w,
                defaults={"quantity": Decimal(qte), "package_quantity": paquets,
                          "loose_quantity": Decimal(vrac),
                          "avg_cost": p.cost_price},
            )
            if st.quantity <= 0:
                etat = "RUPTURE"
            elif p.reorder_point and st.quantity <= p.reorder_point:
                etat = "sous le seuil"
            else:
                etat = "au-dessus"
            self.stdout.write(
                f"  Stock {'créé ' if cree else 'déjà là'} : {we} {sku:10} "
                f"{st.quantity} ({etat})")

    def _clients(self, org):
        """Trois clients : plafond propre, hérité, et crédit refusé."""
        from apps.contacts.models import Customer

        plan = [
            ("CLI-PLAFOND", "Client à plafond", True, Decimal("50000")),
            ("CLI-HERITE", "Client sans plafond propre", True, Decimal("0")),
            ("CLI-REFUS", "Client sans crédit", False, Decimal("0")),
        ]
        for code, nom, autorise, plafond in plan:
            c, cree = Customer.objects.get_or_create(
                organization=org, code=code,
                defaults={"name": nom, "allow_credit": autorise,
                          "credit_limit": plafond, "phone": "+243990000000",
                          "is_active": True},
            )
            self.stdout.write(
                f"  Client {'créé ' if cree else 'déjà là'} : {code:14} "
                f"crédit={'oui' if c.allow_credit else 'NON':3} plafond={c.credit_limit}")

    def _depense_brouillon(self, org, entrepots):
        """
        Une dépense en BROUILLON, côté serveur : c'est le cas exact de T2.

        Une dépense encore dans le journal du terminal ne propose aucune
        transition, donc elle ne plantait pas. C'est après synchronisation,
        quand le serveur l'a écrite en `draft` et que « Soumettre » apparaît,
        que l'écran devenait blanc.
        """
        from apps.cashbook.models import Expense, ExpenseCategory
        from apps.users.models import User

        cat = ExpenseCategory.objects.filter(organization=org).first()
        auteur = User.objects.filter(email="caissier@test.vf").first()
        if not (cat and auteur):
            self.stdout.write(self.style.WARNING("  Dépense brouillon : catégorie ou auteur manquant."))
            return
        from django.utils import timezone

        d, cree = Expense.objects.get_or_create(
            organization=org, reference="DEP-TEST-0001",
            defaults={"category": cat, "amount": Decimal("12500"),
                      "description": "Transport (cas de test T2)",
                      "status": Expense.Status.DRAFT,
                      "currency": "CDF", "exchange_rate": Decimal("1"),
                      # ⚠ `expense_date` est obligatoire et SANS défaut : c'est
                      #   l'un des deux champs que le terminal n'envoyait pas,
                      #   et qui envoyait toute dépense en quarantaine.
                      "expense_date": timezone.localdate(),
                      "warehouse": entrepots.get("WH-MAIN"),
                      "created_by": auteur,
                      "beneficiary": "Fournisseur local"},
        )
        self.stdout.write(
            f"  Dépense {'créée ' if cree else 'déjà là'} : {d.reference} "
            f"statut={d.status} (le cas de T2)")

    def _resume(self, org):
        # ⚠ ON N'IMPRIME LE MOT DE PASSE QUE S'IL EST DÉJÀ PUBLIC. Posé par
        #   `VF_SEED_PASSWORD`, c'est un secret que l'opérateur connaît déjà :
        #   le réécrire ici le verserait dans les journaux de CI et dans
        #   l'historique du shell pour rien.
        self.stdout.write(self.style.SUCCESS(
            f"\nBoutique de test prête.\n"
            f"  Organisation : {org.name}\n"
            f"  X-Organization-ID : {org.id}\n"
            f"  Mot de passe commun : {_source_du_mot_de_passe()}\n"))


def _source_du_mot_de_passe():
    """
    D'OÙ vient le mot de passe, jamais sa VALEUR.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE REFUS PUBLIAIT LE SECRET QU'IL EXISTE POUR PROTÉGER.                 │
    │                                                                          │
    │ Il interpolait `MOT_DE_PASSE` dans son `CommandError`. Quand              │
    │ `VF_SEED_PASSWORD` EST posé - le chemin de production, c'est-à-dire le    │
    │ seul où un opérateur l'aura posé - le vrai secret partait vers stdout,    │
    │ stderr, les journaux de CI et l'historique du shell. Le refus devenait    │
    │ ainsi la fuite la plus sûre de la commande.                               │
    │                                                                          │
    │ La valeur par défaut, elle, est publiée dans ce fichier : la nommer n'y  │
    │ ajoute rien, et elle est ce dont l'opérateur a besoin en développement.   │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    if os.environ.get("VF_SEED_PASSWORD"):
        return "`VF_SEED_PASSWORD`"
    return f"« {MOT_DE_PASSE} », publié dans la source"


class _Annule(Exception):
    """Rompt la transaction du --dry-run."""
