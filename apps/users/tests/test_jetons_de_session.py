"""
La marque des sessions (`apps.users.jetons`), et le seul chemin qui la pose.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN JETON QUI NAÎT SANS MARQUE EST UN JETON QU'AUCUNE RÉINITIALISATION NE     │
│ COUPE PLUS.                                                                  │
│                                                                              │
│ La marque n'existe que si l'endroit qui ouvre la session passe par           │
│ `JetonDeSession`. Un `RefreshToken.for_user` écrit demain, dans une vue      │
│ d'inscription ou de connexion, émettrait des jetons sans marque : pour un    │
│ compte jamais coupé rien ne se verrait, et pour un compte réinitialisé, sa   │
│ session suivante naîtrait refusée. Le balayage ci-dessous l'interdit.        │
└──────────────────────────────────────────────────────────────────────────────┘
"""
import ast
from datetime import datetime, timezone as tz
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.jetons import CLAIM_SESSION, JetonDeSession, marque_de_session, session_ouverte
from apps.users.models import User

RACINE = Path(__file__).resolve().parents[2]  # apps/

#: Les seuls endroits qui émettent un jeton hors de `JetonDeSession`, et pourquoi.
EXEMPTES = {
    # Le jeton de prise en main : un jeton d'ACCÈS sans rafraîchissement, qui
    # échappe aux règles du mot de passe par décision (`apps.core.authentication`).
    'platform_admin/impersonation.py',
}


class MarqueTests(SimpleTestCase):
    def test_un_compte_jamais_coupe_n_a_pas_de_marque(self):
        self.assertIsNone(marque_de_session(User(sessions_revoked_at=None)))
        self.assertTrue(session_ouverte(User(sessions_revoked_at=None), {}))

    def test_la_marque_est_exacte_a_la_microseconde(self):
        """
        ⚠ Sans flottant : `timestamp() * 1e6` arrondit à ces magnitudes, et deux
        réinitialisations à une microseconde d'écart doivent rester distinctes.
        """
        instant = datetime(2026, 11, 14, 9, 30, 12, 345678, tzinfo=tz.utc)
        voisin = instant.replace(microsecond=345679)
        marque = marque_de_session(User(sessions_revoked_at=instant))
        self.assertEqual(marque, 1794648612345678)
        self.assertNotEqual(marque, marque_de_session(User(sessions_revoked_at=voisin)))

    def test_seul_le_jeton_de_la_session_en_cours_passe(self):
        coupe = User(sessions_revoked_at=datetime(2026, 11, 14, tzinfo=tz.utc))
        marque = marque_de_session(coupe)
        self.assertTrue(session_ouverte(coupe, {CLAIM_SESSION: marque}))
        self.assertFalse(session_ouverte(coupe, {CLAIM_SESSION: marque - 1}))
        self.assertFalse(session_ouverte(coupe, {}))


class JetonDeSessionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='marque@test.vf', password='x-8421-kivu')

    def test_un_compte_jamais_coupe_recoit_un_jeton_inchange(self):
        jeton = JetonDeSession.for_user(self.user)
        self.assertNotIn(CLAIM_SESSION, jeton.payload)
        self.assertTrue(OutstandingToken.objects.filter(jti=jeton['jti'], user=self.user).exists())

    def test_la_marque_voyage_dans_le_jeton_inscrit_et_dans_l_acces(self):
        self.user.sessions_revoked_at = datetime(2026, 11, 14, 9, 30, tzinfo=tz.utc)
        self.user.save(update_fields=['sessions_revoked_at', 'updated_at'])
        jeton = JetonDeSession.for_user(self.user)
        marque = marque_de_session(self.user)
        self.assertEqual(jeton[CLAIM_SESSION], marque)
        self.assertEqual(jeton.access_token[CLAIM_SESSION], marque)
        # La copie inscrite est le jeton réellement remis, marque comprise.
        inscrit = OutstandingToken.objects.get(jti=jeton['jti'])
        self.assertEqual(RefreshToken(inscrit.token)[CLAIM_SESSION], marque)


class UnSeulCheminTests(SimpleTestCase):
    def test_aucune_session_ne_s_ouvre_hors_de_jeton_de_session(self):
        balayes, fautifs = 0, []
        for chemin in sorted(RACINE.rglob('*.py')):
            relatif = chemin.relative_to(RACINE).as_posix()
            if '/tests/' in f'/{relatif}' or '/migrations/' in f'/{relatif}':
                continue
            balayes += 1
            if relatif in EXEMPTES:
                continue
            for noeud in ast.walk(ast.parse(chemin.read_text(encoding='utf-8'))):
                if (
                    isinstance(noeud, ast.Call)
                    and isinstance(noeud.func, ast.Attribute)
                    and noeud.func.attr == 'for_user'
                    and isinstance(noeud.func.value, ast.Name)
                    and noeud.func.value.id in {'RefreshToken', 'AccessToken', 'Token'}
                ):
                    fautifs.append(f'{relatif}:{noeud.lineno} ({noeud.func.value.id}.for_user)')
        # Un balayage qui ne balaie rien passe au vert et ne prouve rien.
        self.assertGreater(balayes, 150, 'le balayage ne trouve plus les modules de apps/')
        self.assertEqual(
            fautifs, [],
            'Une session s\'ouvre sans marque : passez par apps.users.jetons.JetonDeSession.',
        )
