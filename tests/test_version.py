# tests/test_version.py — El health endpoint debe decir qué versión está viva.
# Sin esto no hay forma de confirmar desde fuera si un deploy entró: el health
# respondía 200 idéntico con el código viejo y con el nuevo.

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["WHATSAPP_PROVIDER"] = "meta"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key"
os.environ["ADMIN_PASSWORD"] = "test-password-123"


def test_01_version_desde_railway():
    """Railway expone el commit desplegado en RAILWAY_GIT_COMMIT_SHA."""
    import agent.main as main

    with patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": "0b65fcbdeadbeef1234567890"}):
        assert main._version_actual() == "0b65fcb"


def test_02_version_sin_railway_no_queda_vacia():
    import agent.main as main

    entorno = {k: v for k, v in os.environ.items() if k != "RAILWAY_GIT_COMMIT_SHA"}
    with patch.dict(os.environ, entorno, clear=True):
        v = main._version_actual()
    assert v, "la versión nunca debe quedar vacía"


@pytest.mark.asyncio
async def test_03_health_incluye_version_y_conserva_lo_anterior():
    import agent.main as main

    with patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": "abc1234ffffff"}):
        r = await main.health_check()

    assert r["version"] == "abc1234"
    # No romper lo que ya devolvía
    assert r["status"] == "ok"
    assert r["agente"] == "Naylan"
    assert r["negocio"] == "R8ATUR"
