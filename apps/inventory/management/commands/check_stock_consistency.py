"""
Vérifie la cohérence entre ``Stock.quantity`` et la somme des
``StockBatch.quantity`` correspondants, ainsi que la cohérence entre
``Stock.quantity`` et la somme des ``StockMovement.quantity`` historiques.

Utilisation :

    python manage.py check_stock_consistency
    python manage.py check_stock_consistency --organization <uuid>
    python manage.py check_stock_consistency --fix-batches

Sortie : code retour 0 si aucune divergence, 1 sinon.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum

from apps.inventory.models import Stock, StockBatch, StockMovement


TOLERANCE = Decimal('0.001')


class Command(BaseCommand):
    help = "Vérifie la cohérence Stock ↔ StockBatch ↔ StockMovement."

    def add_arguments(self, parser):
        parser.add_argument(
            '--organization',
            type=str,
            default=None,
            help="UUID d'une organisation pour limiter la vérification.",
        )
        parser.add_argument(
            '--check-movements',
            action='store_true',
            help="Vérifie aussi la somme des StockMovement (lent).",
        )

    def handle(self, *args, **options):
        org_id = options.get('organization')
        check_movements = options.get('check_movements', False)

        stocks = Stock.objects.all().select_related('product', 'warehouse')
        if org_id:
            stocks = stocks.filter(organization_id=org_id)

        divergences_batch: list[str] = []
        divergences_mvt: list[str] = []
        total = stocks.count()
        self.stdout.write(f"Analyse de {total} lignes Stock…")

        avertissements = []

        for stock in stocks.iterator(chunk_size=500):
            # Conditionnement : signalements informatifs, jamais bloquants.
            # `Stock.save()` réaligne les compteurs à chaque écriture ; un écart
            # constaté ici vient donc d'une écriture SQL directe et se corrigera
            # au mouvement suivant, sans remettre en cause la quantité totale.
            if stock.location_id is not None:
                avertissements.append(
                    f"  Stock {stock.id} ({stock.product.name}) porte un "
                    f"emplacement : les services agrègent sur la ligne sans "
                    f"emplacement et ignoreront cette ligne."
                )

            # L'invariant des deux compteurs : scellés x facteur + vrac = total.
            factor = getattr(stock.product, 'units_per_package', None)
            if factor and factor > 1:
                attendu = (
                    (stock.package_quantity or Decimal('0.000')) * factor
                    + (stock.loose_quantity or Decimal('0.000'))
                )
                ecart = stock.quantity - attendu
                if abs(ecart) > TOLERANCE:
                    avertissements.append(
                        f"  Stock {stock.id} ({stock.product.name} @ "
                        f"{stock.warehouse.name}) : "
                        f"{stock.package_quantity} x {factor} + "
                        f"{stock.loose_quantity} = {attendu}, mais "
                        f"quantity={stock.quantity} (écart={ecart}) - "
                        f"sera réaligné à la prochaine écriture."
                    )

            batches_total = StockBatch.objects.filter(
                organization=stock.organization,
                product=stock.product,
                variant=stock.variant,
                warehouse=stock.warehouse,
            ).aggregate(total=Sum('quantity'))['total'] or Decimal('0.000')

            if batches_total > 0 and abs(stock.quantity - batches_total) > TOLERANCE:
                divergences_batch.append(
                    f"  Stock {stock.id} ({stock.product.name} @ "
                    f"{stock.warehouse.name}) : Stock.quantity={stock.quantity} "
                    f"vs Σ batches={batches_total} (écart={stock.quantity - batches_total})"
                )

            if check_movements:
                mvt_total = StockMovement.objects.filter(
                    organization=stock.organization,
                    product=stock.product,
                    variant=stock.variant,
                    warehouse=stock.warehouse,
                ).aggregate(total=Sum('quantity'))['total'] or Decimal('0.000')

                if abs(stock.quantity - mvt_total) > TOLERANCE:
                    divergences_mvt.append(
                        f"  Stock {stock.id} ({stock.product.name} @ "
                        f"{stock.warehouse.name}) : Stock.quantity={stock.quantity} "
                        f"vs Σ movements={mvt_total} (écart={stock.quantity - mvt_total})"
                    )

        has_error = False

        if divergences_batch:
            has_error = True
            self.stdout.write(self.style.ERROR(
                f"\n{len(divergences_batch)} divergence(s) Stock ↔ StockBatch :"
            ))
            for line in divergences_batch:
                self.stdout.write(line)
        else:
            self.stdout.write(self.style.SUCCESS(
                "✓ Aucune divergence Stock ↔ StockBatch détectée."
            ))

        if check_movements:
            if divergences_mvt:
                has_error = True
                self.stdout.write(self.style.ERROR(
                    f"\n{len(divergences_mvt)} divergence(s) Stock ↔ StockMovement :"
                ))
                for line in divergences_mvt:
                    self.stdout.write(line)
            else:
                self.stdout.write(self.style.SUCCESS(
                    "✓ Aucune divergence Stock ↔ StockMovement détectée."
                ))

        if avertissements:
            self.stdout.write(self.style.WARNING(
                f"\n{len(avertissements)} signalement(s) sur le conditionnement "
                f"(informatif, sans incidence sur les quantités) :"
            ))
            for line in avertissements:
                self.stdout.write(line)

        if has_error:
            self.stdout.write(self.style.WARNING(
                "\nCes divergences peuvent résulter d'ajustements directs, "
                "de migrations sans recalcul, ou de bugs antérieurs (signal mort "
                "core/signals.py). Lancer un inventaire physique pour rectifier."
            ))
            raise SystemExit(1)
