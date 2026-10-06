"""Genera `openapi.json` en la raíz del repo a partir de la FastAPI app.

Lo corremos a mano (o en un hook/CI) cada vez que cambia el contrato:

    python scripts/generate_openapi.py

Los dos frontends consumen este archivo con `openapi-typescript` para
generar sus tipos TS. Mantenerlo actualizado es parte del contrato
backend ↔ móvil ↔ web.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    # Import diferido: necesita el sys.path arriba.
    from app.main import app  # noqa: PLC0415

    spec = app.openapi()
    out = repo_root / "openapi.json"
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"escribí {out.relative_to(repo_root)} con {len(spec.get('paths', {}))} rutas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
