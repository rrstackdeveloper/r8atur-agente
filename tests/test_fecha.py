# tests/test_fecha.py — Naylan debe saber en qué fecha vive
# Bug real: un cliente pidió vuelos para "primera quincena de diciembre" y
# Naylan resumió "dezembro/2025" estando en noviembre de 2026.

import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key"


def test_01_contexto_temporal_incluye_anio_y_fecha_actual():
    from agent.brain import contexto_temporal

    texto = contexto_temporal()
    ahora = datetime.now()
    assert str(ahora.year) in texto, "falta el año actual"
    assert "fecha" in texto.lower() or "hoy" in texto.lower()


def test_02_contexto_temporal_instruye_como_resolver_fechas_relativas():
    from agent.brain import contexto_temporal

    texto = contexto_temporal().lower()
    # Debe decirle qué hacer con un mes sin año y prohibir años pasados
    assert "sin año" in texto or "sin indicar el año" in texto
    assert "anterior" in texto or "pasado" in texto


def test_03_el_system_prompt_enviado_a_claude_lleva_la_fecha():
    """La fecha debe llegar en la llamada real a la API, no solo existir."""
    import agent.brain as brain

    capturado = {}

    async def fake_create(**kwargs):
        capturado.update(kwargs)
        class Blk:
            type = "text"
            text = "ok"
        class Usage:
            input_tokens = 1
            output_tokens = 1
        class Resp:
            content = [Blk()]
            usage = Usage()
        return Resp()

    with patch.object(brain.client.messages, "create", AsyncMock(side_effect=fake_create)):
        import asyncio
        asyncio.run(brain.generar_respuesta("quiero volar en diciembre", []))

    system = capturado.get("system", "")
    assert str(datetime.now().year) in system, (
        "el system prompt que recibe Claude no incluye el año actual"
    )
    # Y sigue llevando la identidad de Naylan
    assert "Naylan" in system


def test_04_la_fecha_no_reemplaza_el_prompt_del_negocio():
    import agent.brain as brain

    completo = brain.cargar_system_prompt_completo()
    assert "Naylan" in completo
    assert "R8A" in completo
    assert str(datetime.now().year) in completo
    # El prompt del negocio es largo; la fecha es un añadido, no un reemplazo
    assert len(completo) > 3000, "parece que se perdió el prompt del negocio"
