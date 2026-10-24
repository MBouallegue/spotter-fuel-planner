"""Load town coordinates (gazetteer plus overrides) into Place, replacing what is there."""

from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from planner.models import Place
from planner.services.geodata import GAZETTEER_CSV, OVERRIDES_CSV, read_gazetteer, read_overrides


class Command(BaseCommand):
    help = "Load town coordinates from the gazetteer and the overrides file. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("--gazetteer", type=Path, default=GAZETTEER_CSV)
        parser.add_argument("--overrides", type=Path, default=OVERRIDES_CSV)

    def handle(self, *args, gazetteer: Path, overrides: Path, **options):
        gazetteer_rows = read_gazetteer(gazetteer)
        override_rows = read_overrides(overrides)

        with transaction.atomic():
            Place.objects.all().delete()
            Place.objects.bulk_create(
                [Place(**vars(row)) for row in gazetteer_rows + override_rows],
                batch_size=2000,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {len(gazetteer_rows)} gazetteer towns and {len(override_rows)} overrides."
            )
        )
