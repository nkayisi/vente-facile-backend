#!/usr/bin/env python3
"""
Vérifie les dérives RBAC entre:
- catalogue canonique
- permissions par rôles
- action_permissions backend
- guards frontend
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


PERM_RE = re.compile(r"[a-z_]+\.[a-z_]+")
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"


def extract_from_frontend() -> set[str]:
    perms: set[str] = set()
    for path in FRONTEND.rglob("*.tsx"):
        content = path.read_text(encoding="utf-8")
        for token in re.findall(r'permission=["\']([a-z_]+\.[a-z_]+)["\']', content):
            perms.add(token)
        for token in re.findall(r'hasPermission\(["\']([a-z_]+\.[a-z_]+)["\']\)', content):
            perms.add(token)
        for token in re.findall(r"anyPermissions:\s*\[([^\]]+)\]", content):
            perms.update(set(PERM_RE.findall(token)))
    return perms


def extract_from_backend_actions() -> set[str]:
    perms: set[str] = set()
    for path in (BACKEND / "apps").rglob("views.py"):
        content = path.read_text(encoding="utf-8")
        for block in re.findall(r"action_permissions\s*=\s*\{([^}]*)\}", content, re.S):
            perms.update(set(PERM_RE.findall(block)))
        for token in re.findall(r"require_permission\(([^)]*)\)", content):
            perms.update(set(PERM_RE.findall(token)))
    return perms


def main() -> int:
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

    import django  # pylint: disable=import-error

    django.setup()

    from apps.core.permissions_catalog import PERMISSION_CATALOG
    from apps.core.services import PermissionService

    catalog = set(PERMISSION_CATALOG)
    role_permissions = set(PermissionService.get_all_permissions())
    backend_enforced = extract_from_backend_actions()
    frontend_checked = extract_from_frontend()

    errors: list[str] = []

    frontend_not_enforced = sorted(frontend_checked - backend_enforced)
    if frontend_not_enforced:
        errors.append(
            "Permissions vérifiées côté frontend mais non enforce côté backend:\n- "
            + "\n- ".join(frontend_not_enforced)
        )

    backend_not_catalog = sorted(backend_enforced - catalog)
    if backend_not_catalog:
        errors.append(
            "Permissions enforce côté backend mais absentes du catalogue:\n- "
            + "\n- ".join(backend_not_catalog)
        )

    role_not_catalog = sorted(role_permissions - catalog)
    if role_not_catalog:
        errors.append(
            "Permissions de rôle absentes du catalogue:\n- " + "\n- ".join(role_not_catalog)
        )

    if errors:
        print("\n\n".join(errors))
        return 1

    print("Permission drift check: OK")
    print(
        f"- frontend_checked={len(frontend_checked)} backend_enforced={len(backend_enforced)} "
        f"catalog={len(catalog)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
