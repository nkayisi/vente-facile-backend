"""
Écrit le manifeste de tirage sur la sortie standard.

Sert à régénérer le schéma local du terminal (`pnpm db:pull-schema`) SANS
demander le mot de passe d'un marchand, et sans serveur en écoute. La source
est la même que celle de l'endpoint : il n'y a pas deux descriptions du schéma.
"""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Écrit le manifeste de tirage (JSON) sur la sortie standard."

    def add_arguments(self, parser):
        parser.add_argument(
            '--counts', action='store_true',
            help="Inclut le nombre de lignes par table (coûteux, inutile ici).",
        )

    def handle(self, *args, **options):
        from apps.sync.pull import PULL_SCHEMA_VERSION, PULL_TABLES, describe_table

        self.stdout.write(json.dumps({
            'schema_version': PULL_SCHEMA_VERSION,
            'default_page_size': 500,
            'tables': [describe_table(t) for t in PULL_TABLES],
        }, ensure_ascii=False))
