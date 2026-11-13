"""
Ramener à un nombre ENTIER de contenants les mouvements qui en portaient une fraction.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE CORRECTIF DU 10 OCTOBRE NE RÉPARE PAS LE PASSÉ.                          │
│                                                                              │
│ Trois chemins - la réception d'un transfert, la réception fournisseur et    │
│ son annulation, l'approbation d'un ajustement - DIVISAIENT la part scellée  │
│ par le facteur FIGÉ sur la ligne, alors que la part vrac venait d'un        │
│ découpage au facteur du PRODUIT. Dès qu'un marchand changeait son           │
│ conditionnement entre deux gestes, « 1,667 CARTON » partait en base, dans    │
│ `input_package_quantity`, et le journal des mouvements comme son export le  │
│ relisent pour toujours. Le code écrit juste depuis ; les lignes d'avant      │
│ restent fausses.                                                             │
└──────────────────────────────────────────────────────────────────────────────┘

Le remède est celui des trois chemins corrigés : `PackagingService.split`
REPARTAGE le total au facteur figé, rend un nombre entier de scellés et
reverse le reliquat au vrac. `scellés × facteur + vrac == |quantité|` tient
donc par construction, et `quantity`, elle, ne bouge pas : seule la LECTURE du
mouvement change, jamais ce qu'il a fait au stock.

⚠ SIMULATION PAR DÉFAUT. Rien n'est écrit sans `--appliquer` : le volume de
  production est inconnu, et une ligne de journal se corrige mais ne se rature
  pas. `--dry-run` est accepté, et ne fait que confirmer le défaut.

⚠ `updated_at` BOUGE, et c'est voulu : `stock_movements` descend au tirage, et
  une ligne corrigée dont l'horodatage resterait figé resterait fausse sur
  chaque terminal. `save(update_fields=…)` passe par `TimeStampedModel.save`.

⚠ UN MOUVEMENT SANS FACTEUR FIGÉ N'EST PAS RÉPARABLE ICI : on ne sait pas sous
  quel conditionnement il a été écrit, et en inventer un réécrirait le passé
  au catalogue d'aujourd'hui. Il est nommé, et laissé tel quel.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models.functions import Floor

from apps.inventory.models import StockMovement
from apps.inventory.packaging import PackagingService


def mouvements_fractionnaires(organization=None):
    """Les mouvements dont la part scellée n'est pas un nombre entier."""
    qs = (
        StockMovement.objects
        .exclude(input_package_quantity__isnull=True)
        .exclude(input_package_quantity=Floor('input_package_quantity'))
        .select_related('organization', 'product')
        .order_by('created_at', 'id')
    )
    if organization:
        qs = qs.filter(organization_id=organization)
    return qs


def repartage(mouvement):
    """
    `(scellés, vrac)` au facteur figé, ou `None` si ce mouvement n'en a pas.

    Fonction PURE sur l'instance : elle ne lit ni n'écrit la base.
    """
    facteur = mouvement.packaging_factor
    if not facteur or facteur < 2:
        return None
    total = abs(Decimal(mouvement.quantity or 0))
    vrac = abs(Decimal(mouvement.input_loose_quantity or 0))
    scelles, reste = PackagingService.split(total, vrac, facteur)
    if Decimal(scelles) * facteur + reste != total:
        # Ne peut pas arriver : `split` le garantit. Si cela arrive, mieux vaut
        # ne rien écrire que d'écrire un partage qui ne refait pas le total.
        return None
    return Decimal(scelles).quantize(Decimal('0.000')), reste


class Command(BaseCommand):
    help = (
        "Repartage en contenants ENTIERS les mouvements de stock qui portaient une "
        "fraction de contenant. Simulation par défaut ; --appliquer pour écrire."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--appliquer', action='store_true',
            help="Écrit les corrections. Sans elle, la commande ne fait que montrer.",
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Simulation (c'est déjà le défaut).",
        )
        parser.add_argument(
            '--organization', default=None,
            help="Limite à une organisation (son identifiant).",
        )

    def handle(self, *args, **options):
        if options['appliquer'] and options['dry_run']:
            raise CommandError("--appliquer et --dry-run s'excluent : choisissez.")
        appliquer = options['appliquer']

        a_faire, irreparables = [], []
        for m in mouvements_fractionnaires(options['organization']):
            nouveau = repartage(m)
            if nouveau is None:
                irreparables.append(m)
            else:
                a_faire.append((m, nouveau))

        if not a_faire and not irreparables:
            self.stdout.write(self.style.SUCCESS(
                "Aucun mouvement ne porte de fraction de contenant."
            ))
            return

        self.stdout.write(f"{len(a_faire)} mouvement(s) à repartager :")
        for m, (scelles, vrac) in a_faire:
            self.stdout.write(
                f"  {m.created_at:%Y-%m-%d} {m.organization.name} · {m.product.name} "
                f"· {m.movement_type} {m.quantity} : "
                f"{m.input_package_quantity} + {m.input_loose_quantity} "
                f"→ {scelles} + {vrac} (facteur {m.packaging_factor})"
            )
        for m in irreparables:
            self.stdout.write(self.style.WARNING(
                f"  NON RÉPARABLE {m.id} {m.product.name} : "
                f"{m.input_package_quantity} contenant(s), aucun facteur figé."
            ))

        if not appliquer:
            self.stdout.write(self.style.WARNING(
                "\nSimulation : rien n'a été écrit. Relancez avec --appliquer."
            ))
            return

        faits = 0
        for m, (scelles, vrac) in a_faire:
            with transaction.atomic():
                m.input_package_quantity = scelles
                m.input_loose_quantity = vrac
                m.save(update_fields=['input_package_quantity', 'input_loose_quantity'])
            faits += 1
        self.stdout.write(self.style.SUCCESS(f"\n{faits} mouvement(s) repartagé(s)."))
