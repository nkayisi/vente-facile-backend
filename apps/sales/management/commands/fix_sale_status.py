"""
Commande Django pour corriger le statut des ventes déjà enregistrées.
Corrige les ventes qui sont payées en totalité mais ont le statut 'partially_paid'.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from decimal import Decimal
from apps.sales.models import Sale
from apps.inventory.models import Stock, StockMovement
from apps.inventory.packaging import PackagingService


class Command(BaseCommand):
    help = 'Corrige le statut des ventes payées en totalité et met à jour le stock si nécessaire'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Affiche les ventes à corriger sans les modifier',
        )
        parser.add_argument(
            '--organization',
            type=str,
            help='ID de l\'organisation (optionnel, traite toutes si non spécifié)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        org_id = options.get('organization')

        self.stdout.write(self.style.WARNING(
            '=' * 70
        ))
        self.stdout.write(self.style.WARNING(
            'Correction du statut des ventes payées en totalité'
        ))
        self.stdout.write(self.style.WARNING(
            '=' * 70
        ))

        # Filtrer les ventes
        sales_query = Sale.objects.filter(
            status='partially_paid',
            is_deleted=False
        ).select_related('warehouse', 'customer')

        if org_id:
            sales_query = sales_query.filter(organization_id=org_id)

        # Trouver les ventes à corriger
        sales_to_fix = []
        for sale in sales_query:
            # Recalculer amount_due avec précision
            amount_due = (sale.total - sale.amount_paid).quantize(Decimal('0.01'))
            
            if sale.amount_paid >= sale.total or amount_due <= Decimal('0.01'):
                sales_to_fix.append(sale)

        if not sales_to_fix:
            self.stdout.write(self.style.SUCCESS(
                '✓ Aucune vente à corriger trouvée.'
            ))
            return

        self.stdout.write(self.style.WARNING(
            f'\nVentes à corriger : {len(sales_to_fix)}'
        ))
        self.stdout.write('')

        # Afficher les détails
        for sale in sales_to_fix:
            self.stdout.write(
                f'  • {sale.reference} - '
                f'Total: {sale.total} CDF - '
                f'Payé: {sale.amount_paid} CDF - '
                f'Dû: {sale.amount_due} CDF'
            )

        if dry_run:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                '⚠ Mode DRY-RUN : Aucune modification effectuée.'
            ))
            self.stdout.write(self.style.WARNING(
                'Exécutez sans --dry-run pour appliquer les corrections.'
            ))
            return

        # Demander confirmation
        self.stdout.write('')
        confirm = input(
            self.style.WARNING(
                f'Voulez-vous corriger ces {len(sales_to_fix)} ventes ? (oui/non) : '
            )
        )

        if confirm.lower() not in ['oui', 'yes', 'o', 'y']:
            self.stdout.write(self.style.ERROR('✗ Opération annulée.'))
            return

        # Corriger les ventes
        fixed_count = 0
        stock_updated_count = 0

        for sale in sales_to_fix:
            try:
                with transaction.atomic():
                    # Recalculer les montants
                    sale.amount_due = (sale.total - sale.amount_paid).quantize(Decimal('0.01'))
                    
                    if sale.amount_paid >= sale.total:
                        sale.change_amount = (sale.amount_paid - sale.total).quantize(Decimal('0.01'))
                        sale.amount_due = Decimal('0.00')
                    
                    # Mettre à jour le statut
                    previous_status = sale.status
                    sale.status = 'completed'
                    sale.save()

                    # Mettre à jour le stock si nécessaire
                    if sale.warehouse:
                        stock_updated = False
                        for item in sale.items.all():
                            if item.product.track_inventory:
                                # Vérifier si le mouvement existe déjà
                                existing_movement = StockMovement.objects.filter(
                                    reference_type='sale',
                                    reference_id=sale.id,
                                    product=item.product,
                                    variant=item.variant
                                ).exists()

                                if not existing_movement:
                                    # Créer ou mettre à jour le stock
                                    stock, _ = Stock.objects.select_for_update().get_or_create(
                                        organization=sale.organization,
                                        product=item.product,
                                        variant=item.variant,
                                        warehouse=sale.warehouse,
                                        defaults={'quantity': Decimal('0.000')}
                                    )

                                    quantity_before = stock.quantity
                                    # Passe par le service : écrire `quantity`
                                    # directement ferait fondre des contenants
                                    # scellés au profit du vrac.
                                    PackagingService.apply_base_delta(
                                        stock, item.product, -item.quantity,
                                    )
                                    stock.last_movement_at = timezone.now()
                                    stock.save()

                                    # Créer le mouvement de stock
                                    StockMovement.objects.create(
                                        organization=sale.organization,
                                        product=item.product,
                                        variant=item.variant,
                                        warehouse=sale.warehouse,
                                        batch=item.batch,
                                        movement_type='sale',
                                        quantity=-item.quantity,
                                        unit_cost=item.cost_price,
                                        quantity_before=quantity_before,
                                        quantity_after=stock.quantity,
                                        reference_type='sale',
                                        reference_id=sale.id,
                                        notes=f"Vente {sale.reference} (correction automatique)",
                                        created_by=sale.sold_by
                                    )
                                    stock_updated = True

                        if stock_updated:
                            stock_updated_count += 1

                    # Mettre à jour le solde client si vente à crédit
                    if sale.customer and sale.sale_type == 'credit' and previous_status != 'completed':
                        from apps.contacts.models import Customer
                        customer = Customer.objects.select_for_update().get(id=sale.customer.id)
                        # Le solde a déjà été mis à jour lors de la création, pas besoin de le refaire
                        pass

                    fixed_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(f'  ✓ {sale.reference} corrigée')
                    )

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'  ✗ Erreur pour {sale.reference}: {str(e)}')
                )

        # Résumé
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            '=' * 70
        ))
        self.stdout.write(self.style.SUCCESS(
            f'✓ Correction terminée : {fixed_count}/{len(sales_to_fix)} ventes corrigées'
        ))
        if stock_updated_count > 0:
            self.stdout.write(self.style.SUCCESS(
                f'✓ Stock mis à jour pour {stock_updated_count} ventes'
            ))
        self.stdout.write(self.style.SUCCESS(
            '=' * 70
        ))
