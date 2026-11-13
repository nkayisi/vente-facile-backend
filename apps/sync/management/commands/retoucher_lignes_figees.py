"""
Faire redescendre aux terminaux les lignes qu'une écriture en masse avait figées.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE CORRECTIF DU 31 AOÛT NE RÉPARE PAS LE PASSÉ.                              │
│                                                                              │
│ Neuf sites écrivaient par `queryset.update()`, qui ne passe pas par `save()` │
│ et laisse donc `updated_at` où il était. Le tirage pagine sur `(updated_at,  │
│ id)` : chaque ligne ainsi écrite est restée INVISIBLE aux terminaux,         │
│ définitivement - une cagnotte à 704 côté serveur et à 372 au comptoir, avec  │
│ le même horodatage. `apps.core.bulk.bulk_update_rows` ferme le défaut depuis,│
│ mais une ligne figée avant le correctif ne bouge que si on la touche.        │
└──────────────────────────────────────────────────────────────────────────────┘

On ne sait pas QUELLES lignes ces sites ont écrites : on touche donc toutes
celles des tables qu'ils visaient, et rien d'autre. Toucher veut dire poser
`updated_at` (et `sync_updated_at`) à maintenant, SANS changer une valeur : le
terminal relit la ligne au tirage suivant, et c'est tout.

⚠ LE COÛT EST UN RE-TÉLÉCHARGEMENT de ces tables sur chaque terminal, à sa
  prochaine synchronisation. C'est pourquoi les cibles sont nommées une à une
  plutôt que « toutes les tables tirées », et pourquoi les ventes ne sont
  touchées que lorsqu'un de leurs règlements porte un numéro de reçu : c'est le
  seul champ que le site fautif écrivait sur elles.

⚠ UN ENFANT NE SE TOUCHE PAS SEUL. `payments` descend IMBRIQUÉ dans sa vente,
  et le curseur est celui de la VENTE : toucher le règlement ne le ferait pas
  redescendre. On touche donc le parent.

⚠ SIMULATION PAR DÉFAUT. Rien n'est écrit sans `--appliquer`.
"""
from dataclasses import dataclass
from typing import Callable, Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.bulk import bulk_update_rows
from apps.organizations.models import Organization
from apps.sync.pull import PULL_TABLES_BY_NAME


def _ventes_a_numero_de_recu(qs):
    from apps.sales.models import Payment
    return qs.filter(
        id__in=Payment.objects.exclude(receipt_number='').values('sale_id'),
    )


@dataclass(frozen=True)
class Cible:
    #: Nom de la table de tirage, tel que le registre le connaît.
    table: str
    #: Le site qui l'écrivait en masse, pour que la sortie dise POURQUOI.
    site: str
    #: Restreint aux lignes que ce site a pu écrire, quand on le sait.
    restreindre: Optional[Callable] = None


#: Les tables que les neuf anciens `update()` visaient, et elles seules.
CIBLES = (
    Cible('organizations', "devise de l'établissement (Organization.currency)"),
    Cible('organization_currencies', "rétrogradation de l'ancienne devise principale"),
    Cible('products', "modification en lot du catalogue (bulk_update)"),
    Cible('customer_loyalty', "cagnotte gagnée (add_points)"),
    Cible('sales', "numéro de reçu posé sur les règlements",
          restreindre=_ventes_a_numero_de_recu),
)
CIBLES_PAR_TABLE = {c.table: c for c in CIBLES}


def lignes_a_toucher(cible, organization_id):
    """Le queryset des lignes de `cible` pour un établissement."""
    table = PULL_TABLES_BY_NAME[cible.table]
    qs = table.get_model()._default_manager.filter(**{table.org_field: organization_id})
    if cible.restreindre:
        qs = cible.restreindre(qs)
    return qs


class Command(BaseCommand):
    help = (
        "Touche `updated_at` des lignes que les anciennes écritures en masse avaient "
        "figées, pour qu'elles redescendent aux terminaux. Simulation par défaut."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--appliquer', action='store_true',
            help="Écrit. Sans elle, la commande ne fait que compter.",
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Simulation (c'est déjà le défaut).",
        )
        parser.add_argument(
            '--organization', default=None,
            help="Limite à une organisation (son identifiant).",
        )
        parser.add_argument(
            '--tables', default=None,
            help="Sous-ensemble des cibles, séparées par des virgules : "
                 + ', '.join(CIBLES_PAR_TABLE),
        )

    def _cibles(self, tables):
        if not tables:
            return CIBLES
        noms = [t.strip() for t in tables.split(',') if t.strip()]
        inconnues = [n for n in noms if n not in CIBLES_PAR_TABLE]
        if inconnues:
            raise CommandError(
                f"Table(s) hors des cibles : {', '.join(inconnues)}. "
                f"Cibles possibles : {', '.join(CIBLES_PAR_TABLE)}."
            )
        return tuple(CIBLES_PAR_TABLE[n] for n in noms)

    def handle(self, *args, **options):
        if options['appliquer'] and options['dry_run']:
            raise CommandError("--appliquer et --dry-run s'excluent : choisissez.")
        appliquer = options['appliquer']
        cibles = self._cibles(options['tables'])

        etablissements = Organization.objects.order_by('name')
        if options['organization']:
            etablissements = etablissements.filter(id=options['organization'])
            if not etablissements.exists():
                raise CommandError("Aucune organisation avec cet identifiant.")

        totaux = {c.table: 0 for c in cibles}
        for org in etablissements:
            for cible in cibles:
                qs = lignes_a_toucher(cible, org.id)
                if appliquer:
                    # Une transaction PAR établissement et par table : une
                    # écriture en masse sur des milliers de ventes tient ses
                    # verrous le temps de l'écriture, pas le temps de tout le
                    # parc.
                    with transaction.atomic():
                        n = bulk_update_rows(qs)
                else:
                    n = qs.count()
                if n:
                    self.stdout.write(f"  {org.name} · {cible.table} : {n} ligne(s)")
                totaux[cible.table] += n

        self.stdout.write('')
        for cible in cibles:
            self.stdout.write(f"{cible.table:24s} {totaux[cible.table]:>8d}  ({cible.site})")

        if not appliquer:
            self.stdout.write(self.style.WARNING(
                "\nSimulation : rien n'a été écrit. Relancez avec --appliquer. "
                "Chaque terminal retéléchargera ces lignes à sa synchronisation suivante."
            ))
            return
        self.stdout.write(self.style.SUCCESS(
            f"\n{sum(totaux.values())} ligne(s) touchée(s) : elles redescendront aux terminaux."
        ))
