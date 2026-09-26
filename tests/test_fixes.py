# tests/test_fixes.py — Tests de los 8 fallos detectados en auditoría
# IMPORTANTE: No envía WhatsApps reales — usa unittest.mock.patch

import hashlib
import hmac
import json
import os
import sys
import time
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WHATSAPP_PROVIDER"] = "meta"
os.environ["ADMIN_PASSWORD"] = "test-password-123"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key"

TELEFONO = "+5215512345678"


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    """Reinicia el motor de BD en memoria para cada test."""
    import agent.memory as mem
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
    mem._engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    mem._async_session = async_sessionmaker(mem._engine, class_=AsyncSession, expire_on_commit=False)
    await mem.inicializar_db()
    yield
    await mem._engine.dispose()
    mem._engine = None
    mem._async_session = None


# ─── Fallo 1: el envío de texto que falla debe informar al agente ─────────────

@pytest.mark.asyncio
async def test_01_envio_fallido_devuelve_error_no_ok_silencioso():
    """
    Si Meta rechaza el mensaje del agente, el endpoint debe responder con un
    error HTTP (no un 200 con ok:false) para que el dashboard pueda avisar.
    """
    from fastapi import HTTPException
    import agent.main as main
    from agent.memory import guardar_mensaje

    # Ventana de 24h válida: el cliente acaba de escribir, así que el fallo
    # viene de Meta (token, número inválido, etc.), no de la ventana.
    await guardar_mensaje(TELEFONO, "user", "hola")

    proveedor_falso = AsyncMock()
    proveedor_falso.enviar_mensaje = AsyncMock(return_value=False)

    with patch.object(main, "proveedor", proveedor_falso):
        with pytest.raises(HTTPException) as exc:
            await main.admin_enviar(
                telefono=TELEFONO,
                body=main.MensajeAdmin(texto="hola"),
                agente={"id": "jose", "nombre": "Jose A.", "tipo": "agente"},
            )
    assert exc.value.status_code == 502
    assert "no pudo entregar" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_02_envio_fallido_fuera_de_ventana_24h_explica_el_motivo():
    """Fuera de la ventana de 24h el error debe decir por qué, como ya hace media."""
    from fastapi import HTTPException
    import agent.main as main
    from agent.memory import guardar_mensaje

    # Mensaje del cliente hace 30 horas → fuera de la ventana
    from datetime import datetime, timedelta
    from agent.memory import get_session, Mensaje
    async with get_session()() as s:
        s.add(Mensaje(telefono=TELEFONO, role="user", content="hola",
                      timestamp=datetime.utcnow() - timedelta(hours=30)))
        await s.commit()

    proveedor_falso = AsyncMock()
    proveedor_falso.enviar_mensaje = AsyncMock(return_value=False)

    with patch.object(main, "proveedor", proveedor_falso):
        with pytest.raises(HTTPException) as exc:
            await main.admin_enviar(
                telefono=TELEFONO,
                body=main.MensajeAdmin(texto="hola"),
                agente={"id": "jose", "nombre": "Jose A.", "tipo": "agente"},
            )
    assert exc.value.status_code == 422
    assert "24" in str(exc.value.detail)


# ─── Fallo 2: firma del webhook ───────────────────────────────────────────────

def test_03_firma_valida_se_acepta():
    from agent.providers.meta import verificar_firma
    secret = "mi-app-secret"
    body = b'{"entry":[]}'
    firma = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verificar_firma(body, firma, secret) is True


def test_04_firma_invalida_se_rechaza():
    from agent.providers.meta import verificar_firma
    secret = "mi-app-secret"
    body = b'{"entry":[]}'
    assert verificar_firma(body, "sha256=" + "0" * 64, secret) is False
    assert verificar_firma(body, None, secret) is False
    assert verificar_firma(body, "basura", secret) is False


def test_05_payload_manipulado_se_rechaza():
    """Un atacante que cambia el cuerpo sin conocer el secreto debe fallar."""
    from agent.providers.meta import verificar_firma
    secret = "mi-app-secret"
    original = b'{"to":"+521111"}'
    firma = "sha256=" + hmac.new(secret.encode(), original, hashlib.sha256).hexdigest()
    manipulado = b'{"to":"+529999"}'
    assert verificar_firma(manipulado, firma, secret) is False


@pytest.mark.asyncio
async def test_06_webhook_rechaza_firma_invalida_con_403():
    """Con META_APP_SECRET configurado, un POST sin firma válida recibe 403."""
    from fastapi import HTTPException
    import agent.main as main

    proveedor_falso = AsyncMock()
    proveedor_falso.parsear_webhook = AsyncMock(return_value=[])

    class RequestFalso:
        headers = {}
        async def body(self):
            return b'{"entry":[]}'

    with patch.dict(os.environ, {"META_APP_SECRET": "secreto"}):
        with patch.object(main, "proveedor", proveedor_falso):
            with pytest.raises(HTTPException) as exc:
                await main.webhook_handler(RequestFalso())
    assert exc.value.status_code == 403
    proveedor_falso.parsear_webhook.assert_not_called()


# ─── Fallo 3: privilegios de reset-password ───────────────────────────────────

@pytest.mark.asyncio
async def test_07_agente_normal_no_puede_resetear_password_de_otro():
    from fastapi import HTTPException
    import agent.main as main
    from agent.memory import crear_agente

    await crear_agente("alejandro", "Alejandro", "pass-ceo", "ceo")
    await crear_agente("yanara", "Yanara", "pass-comercial", "comercial")

    with pytest.raises(HTTPException) as exc:
        await main.admin_reset_password(
            agente_id="alejandro",
            body=main.ResetPasswordPayload(password="secuestrado"),
            agente={"id": "yanara", "nombre": "Yanara", "tipo": "agente"},
        )
    assert exc.value.status_code == 403

    # La contraseña original sigue funcionando
    from agent.memory import validar_credenciales
    assert await validar_credenciales("alejandro", "pass-ceo") is not None


@pytest.mark.asyncio
async def test_08_agente_puede_cambiar_su_propia_password():
    import agent.main as main
    from agent.memory import crear_agente, validar_credenciales

    await crear_agente("yanara", "Yanara", "vieja", "comercial")
    r = await main.admin_reset_password(
        agente_id="yanara",
        body=main.ResetPasswordPayload(password="nueva-clave"),
        agente={"id": "yanara", "nombre": "Yanara", "tipo": "agente"},
    )
    assert r["ok"] is True
    assert await validar_credenciales("yanara", "nueva-clave") is not None


@pytest.mark.asyncio
async def test_09_admin_si_puede_resetear_password_de_otro():
    import agent.main as main
    from agent.memory import crear_agente, validar_credenciales

    await crear_agente("yanara", "Yanara", "vieja", "comercial")
    r = await main.admin_reset_password(
        agente_id="yanara",
        body=main.ResetPasswordPayload(password="reseteada"),
        agente={"id": "admin", "nombre": "Admin", "tipo": "admin"},
    )
    assert r["ok"] is True
    assert await validar_credenciales("yanara", "reseteada") is not None


# ─── Fallo 4: el reinicio no debe revertir una password cambiada ──────────────

@pytest.mark.asyncio
async def test_10_seed_no_revierte_password_cambiada_manualmente():
    """
    Un agente cambia su contraseña; el siguiente arranque (seed desde env vars)
    NO debe devolverla a la de la variable de entorno.
    """
    from agent.memory import crear_agente, cambiar_password, validar_credenciales
    import agent.main as main

    await crear_agente("jose", "Jose A.", "password-de-env", "desarrollo")
    await cambiar_password("jose", "la-que-yo-elegi")

    with patch.dict(os.environ, {"AGENT_JOSE_PASSWORD": "password-de-env"}):
        await main.seed_agentes_desde_config()

    assert await validar_credenciales("jose", "la-que-yo-elegi") is not None
    assert await validar_credenciales("jose", "password-de-env") is None


@pytest.mark.asyncio
async def test_11_seed_sigue_sincronizando_si_nunca_se_cambio_a_mano():
    """Si el agente nunca cambió su password, la env var sigue mandando."""
    from agent.memory import crear_agente, validar_credenciales
    import agent.main as main

    await crear_agente("jose", "Jose A.", "vieja-de-env", "desarrollo")

    with patch.dict(os.environ, {"AGENT_JOSE_PASSWORD": "nueva-de-env"}):
        await main.seed_agentes_desde_config()

    assert await validar_credenciales("jose", "nueva-de-env") is not None


# ─── Fallo 5: deduplicación de mensajes ───────────────────────────────────────

@pytest.mark.asyncio
async def test_12_mensaje_repetido_se_ignora():
    from agent.memory import registrar_mensaje_procesado

    assert await registrar_mensaje_procesado("wamid.ABC123") is True   # primera vez
    assert await registrar_mensaje_procesado("wamid.ABC123") is False  # reintento


@pytest.mark.asyncio
async def test_13_reintento_de_meta_no_duplica_la_respuesta():
    """El mismo webhook entregado dos veces debe responder una sola vez."""
    import agent.main as main
    from agent.providers.base import MensajeEntrante

    msg = MensajeEntrante(telefono=TELEFONO, texto="Hola", mensaje_id="wamid.XYZ",
                          es_propio=False, nombre_perfil="Cliente")

    proveedor_falso = AsyncMock()
    proveedor_falso.parsear_webhook = AsyncMock(return_value=[msg])
    proveedor_falso.enviar_mensaje = AsyncMock(return_value=True)

    class RequestFalso:
        headers = {}
        async def body(self):
            return b"{}"

    with patch.object(main, "proveedor", proveedor_falso), \
         patch.object(main, "generar_respuesta", AsyncMock(return_value="Hola, soy Naylan")):
        await main.webhook_handler(RequestFalso())
        await main.webhook_handler(RequestFalso())

    assert proveedor_falso.enviar_mensaje.await_count == 1


@pytest.mark.asyncio
async def test_14_webhook_no_devuelve_500_para_no_provocar_reintentos():
    """Un fallo interno no debe propagarse como 500: Meta reenviaría el batch."""
    import agent.main as main

    proveedor_falso = AsyncMock()
    proveedor_falso.parsear_webhook = AsyncMock(side_effect=RuntimeError("boom"))

    class RequestFalso:
        headers = {}
        async def body(self):
            return b"{}"

    with patch.object(main, "proveedor", proveedor_falso):
        r = await main.webhook_handler(RequestFalso())
    assert r["status"] == "error"


# ─── Fallo 6: rate limit no debe perder el mensaje del cliente ────────────────

@pytest.mark.asyncio
async def test_15_mensaje_limitado_se_guarda_para_que_el_agente_lo_vea():
    import agent.main as main
    from agent.providers.base import MensajeEntrante
    from agent.memory import obtener_historial_completo

    main._rl_store.clear()
    proveedor_falso = AsyncMock()
    proveedor_falso.enviar_mensaje = AsyncMock(return_value=True)

    class RequestFalso:
        headers = {}
        async def body(self):
            return b"{}"

    with patch.object(main, "proveedor", proveedor_falso), \
         patch.object(main, "generar_respuesta", AsyncMock(return_value="ok")), \
         patch.object(main, "_RL_MAX", 2):
        for i in range(4):
            proveedor_falso.parsear_webhook = AsyncMock(return_value=[
                MensajeEntrante(telefono=TELEFONO, texto=f"mensaje {i}",
                                mensaje_id=f"wamid.{i}", es_propio=False)
            ])
            await main.webhook_handler(RequestFalso())

    historial = await obtener_historial_completo(TELEFONO)
    textos = [m["content"] for m in historial]
    # Los 4 mensajes del cliente quedan registrados, aunque solo 2 reciban respuesta
    for i in range(4):
        assert f"mensaje {i}" in textos, f"se perdió 'mensaje {i}'"


# ─── Fallo 7: Naylan no debe confundir al agente humano con ella misma ────────

@pytest.mark.asyncio
async def test_16_historial_marca_los_mensajes_del_agente_humano():
    from agent.memory import guardar_mensaje, obtener_historial

    await guardar_mensaje(TELEFONO, "user", "¿Cuánto cuesta el vuelo?")
    await guardar_mensaje(TELEFONO, "assistant", "Son USD 450", autor="agente")

    historial = await obtener_historial(TELEFONO)
    ultimo = historial[-1]["content"]
    assert "agente" in ultimo.lower(), (
        "Naylan debe saber que ese mensaje lo escribió un humano del equipo"
    )
    assert "USD 450" in ultimo


@pytest.mark.asyncio
async def test_17_dashboard_ve_el_texto_limpio_sin_la_marca():
    """La marca es solo para Naylan; el agente ve su mensaje tal como lo escribió."""
    from agent.memory import guardar_mensaje, obtener_historial_completo

    await guardar_mensaje(TELEFONO, "assistant", "Son USD 450", autor="agente")
    historial = await obtener_historial_completo(TELEFONO)
    assert historial[-1]["content"] == "Son USD 450"
    assert historial[-1]["autor"] == "agente"


@pytest.mark.asyncio
async def test_18_mensajes_de_naylan_no_llevan_marca():
    from agent.memory import guardar_mensaje, obtener_historial

    await guardar_mensaje(TELEFONO, "user", "hola")
    await guardar_mensaje(TELEFONO, "assistant", "Hola, soy Naylan")
    historial = await obtener_historial(TELEFONO)
    assert historial[-1]["content"] == "Hola, soy Naylan"


# ─── Fallo 8: fuga de memoria del rate limiter ────────────────────────────────

def test_19_rate_limiter_purga_telefonos_inactivos():
    import agent.main as main

    main._rl_store.clear()
    for i in range(500):
        main._rate_limit_ok(f"+5215500000{i:03d}")
    assert len(main._rl_store) == 500

    # Simular que pasó la ventana completa y el intervalo entre purgas
    for q in main._rl_store.values():
        for idx in range(len(q)):
            q[idx] = q[idx] - (main._RL_WINDOW + 60)
    main._RL_ULTIMA_PURGA = main._RL_ULTIMA_PURGA - (main._RL_PURGA_CADA + 1)

    main._rate_limit_ok("+521999999999")
    assert len(main._rl_store) < 10, (
        f"quedaron {len(main._rl_store)} teléfonos sin purgar"
    )


def test_20_purga_no_afecta_a_telefonos_activos():
    import agent.main as main

    main._rl_store.clear()
    main._rate_limit_ok("+521111111111")
    for i in range(50):
        main._rate_limit_ok(f"+5215511100{i:03d}")
    assert main._rate_limit_ok("+521111111111") is True
    assert "+521111111111" in main._rl_store


# ─── Migración sobre una base de datos que ya existe (caso Railway) ───────────

@pytest.mark.asyncio
async def test_21_migracion_agrega_columnas_a_bd_existente(tmp_path):
    """
    Una BD creada con el esquema anterior debe migrarse sin perder datos y
    quedar usable. `ADD COLUMN IF NOT EXISTS` no es válido en SQLite, así que
    la migración no puede depender de esa sintaxis.
    """
    import sqlite3
    import agent.memory as mem
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

    db = tmp_path / "vieja.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE mensajes (id INTEGER PRIMARY KEY AUTOINCREMENT,
          telefono VARCHAR(50), role VARCHAR(20), content TEXT, timestamp DATETIME);
        CREATE TABLE agentes (id VARCHAR(50) PRIMARY KEY, nombre VARCHAR(100),
          password_hash VARCHAR(300), rol VARCHAR(50), enabled BOOLEAN, created_at DATETIME);
        INSERT INTO mensajes (telefono, role, content, timestamp)
          VALUES ('+521', 'user', 'mensaje antiguo', '2026-01-01');
    """)
    con.commit()
    con.close()

    await mem._engine.dispose()
    mem._engine = create_async_engine(f"sqlite+aiosqlite:///{db}", echo=False)
    mem._async_session = async_sessionmaker(mem._engine, class_=AsyncSession, expire_on_commit=False)

    await mem.inicializar_db()

    # La columna nueva existe y es usable
    await mem.guardar_mensaje("+521", "assistant", "Son USD 450", autor="agente")
    # Y los datos viejos siguen ahí
    historial = await mem.obtener_historial("+521")
    assert any("mensaje antiguo" in m["content"] for m in historial)
    assert any("agente humano" in m["content"] for m in historial)

    # password_changed_at también migró
    await mem.crear_agente("jose", "Jose A.", "clave", "desarrollo")
    await mem.cambiar_password("jose", "otra")
    assert await mem.validar_credenciales("jose", "otra") is not None
