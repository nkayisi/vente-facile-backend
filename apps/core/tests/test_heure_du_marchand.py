"""
Une heure IMPRIMÉE ou AFFICHÉE est celle du marchand, jamais celle de Greenwich.

Vu au navigateur le 2026-11-20 : le rapport de caisse imprimait « 12:28 » une
vente faite à 13 h 28 à Kinshasa, et une sortie de 00 h 32 apparaissait à
« 23:32 », plus tard que toute la journée qu'elle ouvre. Le même défaut tenait
le rapport journalier des statistiques (`.time()` d'un instant UTC) et le refus
d'ouvrir une seconde session.

Le garde-fou lit les SOURCES : toute ligne qui formate une heure (`%H`) ou
extrait `.time()` doit passer par `heure_locale` ou `timezone.localtime`.
"""
import re
from datetime import datetime, timezone as dt_tz
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.core.clock import heure_locale

APPS = Path(__file__).resolve().parents[2]
# Une heure FORMATÉE (`%H`) ou EXTRAITE d'un instant (`x.time()`, mais pas
# `time.time()`, qui est une durée en secondes).
HEURE = re.compile(r"%H|(?<!\btime)\.time\(\)")
CONVERSION = re.compile(r"heure_locale\(|localtime\(")

# Exceptions NOMMÉES, chacune avec son motif. Une ligne qui en sort sans
# raison écrite est un défaut.
EXEMPTEES = {
    # `m['time']` est une `time` déjà convertie à la source, dans
    # `StatisticsViewSet._daily_cash_movement_rows` (`heure_locale(...).time()`).
    "reports/exports.py": "m['time'].strftime(",
    # Tranches de `TruncHour`/`TruncDay` : Django les calcule dans le fuseau
    # COURANT (`TIME_ZONE`), elles sont donc déjà à l'heure du marchand.
    "reports/views.py": "fmt = '%Y-%m-%d %H:00'",
}


class HeureLocaleTests(SimpleTestCase):
    @override_settings(TIME_ZONE='Africa/Kinshasa', USE_TZ=True)
    def test_minuit_utc_est_une_heure_a_kinshasa(self):
        quand = datetime(2026, 11, 19, 23, 32, tzinfo=dt_tz.utc)
        local = heure_locale(quand)
        self.assertEqual((local.day, local.hour, local.minute), (20, 0, 32))

    def test_un_instant_naif_est_rendu_tel_quel(self):
        naif = datetime(2026, 11, 20, 12, 0)
        self.assertIs(heure_locale(naif), naif)
        self.assertIsNone(heure_locale(None))


class AucuneHeureUTCTests(SimpleTestCase):
    def test_toute_heure_formatee_passe_par_l_heure_locale(self):
        fautifs, balayes = [], 0
        for fichier in APPS.rglob('*.py'):
            chemin = str(fichier)
            if '/tests/' in chemin or '/migrations/' in chemin:
                continue
            balayes += 1
            for n, ligne in enumerate(fichier.read_text(encoding='utf-8').splitlines(), 1):
                code = ligne.split('#', 1)[0]
                rel = str(fichier.relative_to(APPS))
                if rel in EXEMPTEES and EXEMPTEES[rel] in code:
                    continue
                if HEURE.search(code) and not CONVERSION.search(code):
                    fautifs.append(f'{fichier.relative_to(APPS)}:{n}: {ligne.strip()}')
        self.assertGreater(balayes, 150, "le balayage ne balaie rien")
        self.assertEqual(fautifs, [], "Heure formatée sans conversion locale :\n" + "\n".join(fautifs))
