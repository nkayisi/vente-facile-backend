"""
Réaligne les plans sur des devises actives du modèle Currency.

Utile après désactivation d'une devise ou pour forcer le passage à USD.
La migration 0005_plan_currency_foreign_key mappe déjà les anciens codes ISO ;
cette commande sert d'outil de maintenance opérationnelle.
"""
from django.core.management.base import BaseCommand

from apps.settings.models import Currency
from apps.subscriptions.models import Plan


class Command(BaseCommand):
    help = (
        "Assigne la devise USD aux plans dont la devise liée est inactive. "
        "Utilisez --dry-run pour lister sans modifier."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Affiche les plans concernés sans enregistrer.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        usd = Currency.objects.filter(code="USD", is_active=True).first()
        if not usd:
            self.stderr.write(
                self.style.ERROR(
                    "Aucune devise USD active. Créez ou réactivez USD dans `currencies`."
                )
            )
            return

        qs = Plan.objects.select_related("currency").filter(currency__is_active=False)
        count = qs.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS("Aucun plan avec devise inactive."))
            return

        self.stdout.write(f"Plans concernés : {count}")
        for plan in qs:
            self.stdout.write(f"  - {plan.code} ({plan.name}) → USD")
            if not dry_run:
                plan.currency = usd
                plan.save(update_fields=["currency", "updated_at"])

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry-run : aucune modification."))
        else:
            self.stdout.write(self.style.SUCCESS(f"{count} plan(s) mis à jour."))
