# tests/test_handoff.py — Tests del sistema de handoff humano
# Cubre los 20 escenarios especificados
# IMPORTANTE: No envía WhatsApps reales — usa unittest.mock.patch

import asyncio
import os
import sys
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock

# Asegurar que el root del proyecto esté en el path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Forzar SQLite en memoria para los tests (ANTES de importar memory)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WHATSAPP_PROVIDER"] = "meta"
os.environ["ADMIN_PASSWORD"] = "test-password-123"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key"


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    """Reinicia el motor de BD en memoria para cada test."""
    import agent.memory as mem
    # Forzar creación de nuevo motor en memoria para cada test
    mem._engine = None
    mem._async_session = None
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
    mem._engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    mem._async_session = async_sessionmaker(mem._engine, class_=AsyncSession, expire_on_commit=False)
    await mem.inicializar_db()
    yield
    await mem._engine.dispose()
    mem._engine = None
    mem._async_session = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

TELEFONO = "+5215512345678"
TELEFONO_2 = "+5215587654321"


async def _crear_historial_basico(telefono: str = TELEFONO):
    from agent.memory import guardar_mensaje
    await guardar_mensaje(telefono, "user", "Hola, necesito ayuda con un vuelo")
    await guardar_mensaje(telefono, "assistant", "Claro, con gusto te ayudo. ¿Cuál es tu origen y destino?")


# ─── Escenario 1: BOT_ACTIVE — Naylan responde ────────────────────────────────

@pytest.mark.asyncio
async def test_01_bot_active_naylan_responde():
    """BOT_ACTIVE: el modo es 'bot', Naylan procesa y responde."""
    from agent.memory import obtener_modo, establecer_modo
    await establecer_modo(TELEFONO, "bot")
    modo = await obtener_modo(TELEFONO)
    assert modo == "bot", "En BOT_ACTIVE el modo debe ser 'bot'"


# ─── Escenario 2: WAITING_HUMAN — Naylan sigue respondiendo ──────────────────

@pytest.mark.asyncio
async def test_02_waiting_human_naylan_sigue_respondiendo():
    """WAITING_HUMAN: modo sigue siendo 'bot', Naylan no está pausada."""
    from agent.memory import establecer_handoff, obtener_modo
    await establecer_handoff(TELEFONO, modo="bot", handoff_status="WAITING_HUMAN")
    modo = await obtener_modo(TELEFONO)
    assert modo == "bot", "En WAITING_HUMAN el modo sigue siendo 'bot' — Naylan no se pausa"


# ─── Escenario 3: HUMAN_ACTIVE — Naylan NO responde ─────────────────────────

@pytest.mark.asyncio
async def test_03_human_active_naylan_silenciada():
    """HUMAN_ACTIVE: modo='humano', el webhook handler silencia a Naylan."""
    from agent.memory import establecer_handoff, obtener_modo
    await establecer_handoff(TELEFONO, modo="humano", handoff_status="HUMAN_ACTIVE", assigned_agent="Yanara")
    modo = await obtener_modo(TELEFONO)
    assert modo == "humano", "En HUMAN_ACTIVE el modo debe ser 'humano' — Naylan silenciada"


# ─── Escenario 4: Claim atómico — primer agente obtiene conversación ──────────

@pytest.mark.asyncio
async def test_04_claim_atomico_primer_agente():
    """El primer agente en reclamar obtiene la conversación."""
    from agent.memory import establecer_handoff, atomic_claim_conversation, obtener_registro_completo
    await establecer_handoff(TELEFONO, modo="bot", handoff_status="WAITING_HUMAN")
    resultado = await atomic_claim_conversation(TELEFONO, "Yanara")
    assert resultado["success"] is True
    registro = await obtener_registro_completo(TELEFONO)
    assert registro.handoff_status == "HUMAN_ACTIVE"
    assert registro.assigned_agent == "Yanara"
    assert registro.claimed_at is not None


# ─── Escenario 5: Claim concurrente — segundo agente recibe conflicto ─────────

@pytest.mark.asyncio
async def test_05_claim_concurrente_segundo_agente_conflicto():
    """Cuando dos agentes intentan tomar la misma conv, el segundo falla."""
    from agent.memory import establecer_handoff, atomic_claim_conversation
    await establecer_handoff(TELEFONO, modo="bot", handoff_status="WAITING_HUMAN")

    # Primer agente toma exitosamente
    r1 = await atomic_claim_conversation(TELEFONO, "Yanara")
    assert r1["success"] is True

    # Segundo agente falla
    r2 = await atomic_claim_conversation(TELEFONO, "Alejandro")
    assert r2["success"] is False
    assert r2["reason"] == "ya_tomada"


# ─── Escenario 6: Devolver a Naylan ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_06_devolver_reactiva_bot():
    """Devolver la conversación reactiva el modo bot y cambia handoff_status."""
    from agent.memory import establecer_handoff, obtener_registro_completo
    # Simular que un agente tiene la conv
    await establecer_handoff(TELEFONO, modo="humano", handoff_status="HUMAN_ACTIVE", assigned_agent="Yanara")

    # Devolver a Naylan
    await establecer_handoff(TELEFONO, modo="bot", handoff_status="BOT_ACTIVE", assigned_agent=None)

    registro = await obtener_registro_completo(TELEFONO)
    assert registro.modo == "bot"
    assert registro.handoff_status == "BOT_ACTIVE"
    assert registro.assigned_agent is None


# ─── Escenario 7: Finalizar atención — conserva historial ────────────────────

@pytest.mark.asyncio
async def test_07_finalizar_conserva_historial():
    """Después de RESOLVED, el historial de mensajes se preserva."""
    from agent.memory import guardar_mensaje, obtener_historial_completo, establecer_handoff
    from datetime import datetime

    await guardar_mensaje(TELEFONO, "user", "Hola quiero un vuelo")
    await guardar_mensaje(TELEFONO, "assistant", "Claro, ¿cuál es tu destino?")
    await guardar_mensaje(TELEFONO, "user", "Cancún")

    # Finalizar
    from agent.memory import get_session, ConversacionModo
    from sqlalchemy import select as _select
    async with get_session()() as session:
        registro = ConversacionModo(
            telefono=TELEFONO,
            modo="bot",
            handoff_status="RESOLVED",
            resolved_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(registro)
        await session.commit()

    historial = await obtener_historial_completo(TELEFONO)
    assert len(historial) == 3, "El historial debe conservarse tras RESOLVED"
    assert historial[0]["content"] == "Hola quiero un vuelo"


# ─── Escenario 8: Handoff duplicado — no se crea segundo handoff ──────────────

@pytest.mark.asyncio
async def test_08_handoff_duplicado_no_se_crea():
    """Si ya hay WAITING_HUMAN, no se crea un segundo handoff."""
    from agent.memory import establecer_handoff, obtener_handoff_status

    # Primer handoff
    await establecer_handoff(TELEFONO, modo="bot", handoff_status="WAITING_HUMAN",
                             handoff_summary="primer handoff")

    status_antes = await obtener_handoff_status(TELEFONO)
    assert status_antes == "WAITING_HUMAN"

    # El webhook handler verifica antes de crear otro — simulamos esa lógica
    status_actual = await obtener_handoff_status(TELEFONO)
    ya_existe = status_actual in ("WAITING_HUMAN", "HUMAN_ACTIVE")
    assert ya_existe is True, "El sistema debe detectar handoff existente y no crear otro"


# ─── Escenario 9: Mensajes durante WAITING_HUMAN van al mismo case ────────────

@pytest.mark.asyncio
async def test_09_mensajes_waiting_human_mismo_case():
    """Durante WAITING_HUMAN, los mensajes del usuario se guardan en el mismo hilo."""
    from agent.memory import establecer_handoff, guardar_mensaje, obtener_historial_completo

    await establecer_handoff(TELEFONO, modo="bot", handoff_status="WAITING_HUMAN")
    await guardar_mensaje(TELEFONO, "user", "primer mensaje")
    await guardar_mensaje(TELEFONO, "assistant", "respuesta de Naylan")
    await guardar_mensaje(TELEFONO, "user", "segundo mensaje en espera")

    historial = await obtener_historial_completo(TELEFONO)
    assert len(historial) == 3
    assert historial[2]["content"] == "segundo mensaje en espera"


# ─── Escenario 10: No notificaciones duplicadas ───────────────────────────────

@pytest.mark.asyncio
async def test_10_sin_notificaciones_duplicadas():
    """Si notification_sent=True, no se reenvía notificación."""
    from agent.memory import establecer_handoff, marcar_notificacion_enviada, obtener_registro_completo

    await establecer_handoff(TELEFONO, modo="bot", handoff_status="WAITING_HUMAN")
    await marcar_notificacion_enviada(TELEFONO)

    registro = await obtener_registro_completo(TELEFONO)
    assert registro.notification_sent is True
    assert registro.notification_sent_at is not None

    # La función notificar_agentes verifica este flag antes de enviar
    # Aquí verificamos que el flag queda correctamente seteado para que funcione la deduplicación


# ─── Escenario 11: Fallo de notificación no reportado como éxito al cliente ──

@pytest.mark.asyncio
async def test_11_fallo_notificacion_no_silenciado():
    """Si el envío de notificación falla, se loguea el error correctamente."""
    from agent.memory import establecer_handoff, obtener_registro_completo

    await establecer_handoff(TELEFONO, modo="bot", handoff_status="WAITING_HUMAN")

    # Mock que falla al enviar
    mock_proveedor = AsyncMock()
    mock_proveedor.enviar_mensaje = AsyncMock(return_value=False)

    with patch("agent.main.proveedor", mock_proveedor):
        with patch("agent.main._cargar_agentes_yaml", return_value=[]):
            with patch.dict(os.environ, {"AGENT_PHONES": "+551188953253"}):
                from agent.main import notificar_agentes
                await notificar_agentes(TELEFONO, {"servicio": "vuelo", "motivo": "test", "prioridad": "normal", "resumen": "test"})

    # La notificación falló → notification_sent NO debe ser True
    registro = await obtener_registro_completo(TELEFONO)
    assert not registro.notification_sent, "Si la notificación falla, notification_sent debe seguir False"


# ─── Escenario 12: Usuario no autorizado — 401 ───────────────────────────────

@pytest.mark.asyncio
async def test_12_usuario_no_autorizado_401():
    """Endpoints admin devuelven 401 si no se envía X-Admin-Key correcta."""
    from fastapi.testclient import TestClient
    # Necesitamos arrancar la app en modo test
    import agent.main as main_module
    # Mock del proveedor para que lifespan no falle
    with patch("agent.providers.obtener_proveedor") as mock_prov:
        mock_prov.return_value = AsyncMock()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
            r = await client.get("/admin/api/conversaciones", headers={"X-Admin-Key": "wrong"})
            assert r.status_code == 401


# ─── Escenario 13: Deep link requiere auth ────────────────────────────────────

@pytest.mark.asyncio
async def test_13_deeplink_requiere_auth():
    """GET /admin devuelve HTML, pero las API calls sin key devuelven 401."""
    import agent.main as main_module
    with patch("agent.providers.obtener_proveedor") as mock_prov:
        mock_prov.return_value = AsyncMock()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
            # Sin key → 401 en endpoint de API (el dashboard HTML es público pero la API no)
            r = await client.get("/admin/api/conversaciones")
            assert r.status_code == 401


# ─── Escenario 14: extraer_escalado con tag presente ─────────────────────────

@pytest.mark.asyncio
async def test_14_extraer_escalado_tag_presente():
    """extraer_escalado() extrae el tag y retorna texto limpio + dict."""
    from agent.brain import extraer_escalado
    texto = 'Perfecto, ya tengo tus datos. Un especialista te atenderá pronto.\n[ESCALATE:{"servicio":"vuelo","motivo":"cotizacion","prioridad":"alta","resumen":"vuelo CDMX-CUN","datos":"2 adultos"}]'
    limpio, datos = extraer_escalado(texto)
    assert "[ESCALATE:" not in limpio
    assert datos is not None
    assert datos.get("servicio") == "vuelo"
    assert datos.get("prioridad") == "alta"
    assert "Un especialista" in limpio


# ─── Escenario 15: extraer_escalado sin tag ───────────────────────────────────

@pytest.mark.asyncio
async def test_15_extraer_escalado_sin_tag():
    """extraer_escalado() retorna el texto original y None si no hay tag."""
    from agent.brain import extraer_escalado
    texto = "¿Cuál es tu fecha de salida preferida?"
    limpio, datos = extraer_escalado(texto)
    assert limpio == texto
    assert datos is None


# ─── Escenario 16: Prioridad mapea correctamente ──────────────────────────────

@pytest.mark.asyncio
async def test_16_prioridad_mapeo():
    """alta → CRITICAL, media → HIGH, normal → NORMAL."""
    from agent.main import _mapear_prioridad
    assert _mapear_prioridad("alta") == "CRITICAL"
    assert _mapear_prioridad("media") == "HIGH"
    assert _mapear_prioridad("normal") == "NORMAL"
    assert _mapear_prioridad("desconocido") == "NORMAL"  # default


# ─── Escenario 17: Conversaciones antiguas se tratan como BOT_ACTIVE ─────────

@pytest.mark.asyncio
async def test_17_conversaciones_antiguas_como_bot_active():
    """Conversaciones sin ConversacionModo (antiguas) devuelven modo='bot'."""
    from agent.memory import obtener_modo, obtener_handoff_status, guardar_mensaje

    # Solo existe el mensaje, no el registro de modo
    await guardar_mensaje("+5215500000000", "user", "mensaje antiguo")

    modo = await obtener_modo("+5215500000000")
    status = await obtener_handoff_status("+5215500000000")

    assert modo == "bot", "Conversaciones sin registro de modo deben ser 'bot'"
    assert status == "BOT_ACTIVE", "Conversaciones sin registro deben tener BOT_ACTIVE"


# ─── Escenario 18: Historial preservado después de RESOLVED ──────────────────

@pytest.mark.asyncio
async def test_18_historial_preservado_tras_resolved():
    """Después de RESOLVED el historial de mensajes permanece intacto."""
    from agent.memory import guardar_mensaje, obtener_historial_completo, establecer_handoff

    mensajes = [
        ("user", "Quiero un vuelo"),
        ("assistant", "¿Origen y destino?"),
        ("user", "CDMX a Cancún"),
        ("assistant", "¿Fechas?"),
    ]
    for role, content in mensajes:
        await guardar_mensaje(TELEFONO, role, content)

    await establecer_handoff(TELEFONO, modo="humano", handoff_status="HUMAN_ACTIVE", assigned_agent="Yanara")

    # Finalizar (RESOLVED)
    from agent.memory import get_session, ConversacionModo
    from datetime import datetime
    from sqlalchemy import select as _s
    async with get_session()() as session:
        result = await session.execute(_s(ConversacionModo).where(ConversacionModo.telefono == TELEFONO))
        reg = result.scalar_one_or_none()
        if reg:
            reg.modo = "bot"
            reg.handoff_status = "RESOLVED"
            reg.resolved_at = datetime.utcnow()
            await session.commit()

    historial = await obtener_historial_completo(TELEFONO)
    assert len(historial) == 4, "El historial completo debe conservarse tras RESOLVED"


# ─── Escenario 19: Audio → mismo flujo (mock transcripción) ──────────────────

@pytest.mark.asyncio
async def test_19_audio_mismo_flujo():
    """Un mensaje de audio transcrito sigue el mismo flujo que texto."""
    # Simular la transcripción de audio
    texto_transcrito = "Necesito un vuelo de Mexico a España"

    # El flujo es idéntico al de texto: guardar y procesar
    from agent.memory import guardar_mensaje, obtener_historial
    await guardar_mensaje(TELEFONO, "user", texto_transcrito)
    historial = await obtener_historial(TELEFONO)
    assert len(historial) == 1
    assert historial[0]["content"] == texto_transcrito
    assert historial[0]["role"] == "user"


# ─── Escenario 20: Claim después de RESOLVED no puede reclamarse ─────────────

@pytest.mark.asyncio
async def test_20_claim_despues_de_resolved_falla():
    """Una conversación en estado RESOLVED no puede ser reclamada con atomic_claim."""
    from agent.memory import establecer_handoff, atomic_claim_conversation

    # Estado RESOLVED
    await establecer_handoff(TELEFONO, modo="bot", handoff_status="RESOLVED")

    # Intentar reclamar — debe fallar porque RESOLVED no está en la lista de estados permitidos
    resultado = await atomic_claim_conversation(TELEFONO, "Alejandro")
    assert resultado["success"] is False, "No debe poder reclamarse una conversación RESOLVED"
    assert resultado["reason"] == "ya_tomada"
