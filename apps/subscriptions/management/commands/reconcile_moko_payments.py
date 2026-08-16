"""
Réconciliation des paiements MOKO encaissés sans abonnement actif.

Répare le cas signalé en production : « l'argent est prélevé mais l'abonnement
ne s'active pas ». Deux causes historiques sont couvertes :

  - un paiement resté ``PENDING`` parce que la file Redis a été vidée, que le
    worker Celery était arrêté, ou que le TTL des métadonnées a expiré ;
  - un paiement marqué ``FAILED`` à tort par l'ancienne logique de polling, qui
    invalidait un encaissement confirmé dès qu'une règle de checkout refusait
    (période expirée entre-temps, second paiement, palier déjà relevé).

La commande interroge la BASE (jamais Redis), réinterroge MOKO sur chaque
référence, et n'active que ce que MOKO confirme réellement encaissé.

Propriétés :
  - **Idempotente** : un paiement déjà ``COMPLETED`` rattaché à un abonnement
    est ignoré.
  - **Sûre pour la production** : ``--dry-run`` pour prévisualiser sans écrire.

Usage :
    python manage.py reconcile_moko_payments --dry-run
    python manage.py reconcile_moko_payments
    python manage.py reconcile_moko_payments --max-age-days 90
    python manage.py reconcile_moko_payments --reference vf_sub_xxxxx
"""
from django.core.management.base import BaseCommand

from apps.subscriptions.services import SubscriptionService


class Command(BaseCommand):
    help = (
        "Réactive les abonnements des paiements MOKO encaissés mais non activés "
        "(statut pending ou failed à tort)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Prévisualise sans rien modifier.",
        )
        parser.add_argument(
            '--max-age-days',
            type=int,
            default=30,
            help="Ancienneté maximale des paiements examinés (défaut : 30 jours).",
        )
        parser.add_argument(
            '--reference',
            type=str,
            default='',
            help="Traite une seule référence, sans filtre d'ancienneté.",
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        reference = options['reference'].strip()

        if dry_run:
            self.stdout.write(self.style.WARNING('Mode simulation : aucune écriture.'))

        report = SubscriptionService.reconcile_moko_payments(
            max_age_days=options['max_age_days'],
            dry_run=dry_run,
            reference=reference,
        )

        self.stdout.write(f"\nPaiements examinés : {report['checked']}")

        if report['reconciled']:
            self.stdout.write(self.style.SUCCESS(
                f"\nRéactivés ({len(report['reconciled'])}) :"
            ))
            for ref, detail in report['reconciled']:
                self.stdout.write(f"  {ref} : {detail}")

        if report['still_pending']:
            self.stdout.write(
                f"\nToujours en attente chez MOKO ({len(report['still_pending'])}) :"
            )
            for ref, detail in report['still_pending']:
                self.stdout.write(f"  {ref} : {detail}")

        if report['really_failed']:
            self.stdout.write(
                f"\nRéellement échoués chez MOKO ({len(report['really_failed'])}) :"
            )
            for ref, detail in report['really_failed']:
                self.stdout.write(f"  {ref} : {detail}")

        if report['unresolved']:
            self.stdout.write(self.style.ERROR(
                f"\nNon résolus, intervention manuelle requise "
                f"({len(report['unresolved'])}) :"
            ))
            for ref, detail in report['unresolved']:
                self.stdout.write(f"  {ref} : {detail}")

        if not any([report['reconciled'], report['still_pending'],
                    report['really_failed'], report['unresolved']]):
            self.stdout.write(self.style.SUCCESS(
                "\nAucun paiement à réconcilier."
            ))
