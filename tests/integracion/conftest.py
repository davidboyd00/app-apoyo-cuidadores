"""Fixture del stack local para las pruebas de integración (EDT 5.1.1).

Requiere el binario de PostgREST (v12). Se busca en la variable POSTGREST_BIN
o en el PATH; si no está, estas pruebas se saltan. En CI el workflow lo descarga.

    POSTGREST_BIN=/ruta/postgrest python -m pytest -m integracion
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests.integracion.stack import levantar_stack


@pytest.fixture(scope="session")
def stack():
    binario = os.environ.get("POSTGREST_BIN") or shutil.which("postgrest")
    if not binario:
        pytest.skip("Falta PostgREST (definir POSTGREST_BIN) para las pruebas de integración")
    with levantar_stack(binario) as s:
        yield s
