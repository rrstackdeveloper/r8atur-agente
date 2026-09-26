# agent/memory.py — Memoria de conversaciones con SQLite/PostgreSQL
import os
import logging
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, Boolean, select, Integer, func, text, update, inspect
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("agentkit")

_engine = None
_async_session = None


def _get_database_url() -> str:
    for var in ("DATABASE_URL", "DATABASE_PUBLIC_URL", "POSTGRES_URL"):
        url = os.getenv(var, "").strip()
        if url and url not in ("", "sqlite+aiosqlite:///./agentkit.db"):
            break
    else:
        url = "sqlite+aiosqlite:///./agentkit.db"
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def get_engine():
    global _engine, _async_session
    if _engine is None:
        _engine = create_async_engine(_get_database_url(), echo=False)
        _async_session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine


def get_session():
    get_engine()
    return _async_session


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    # autor: None/"bot" = Naylan, "agente" = escrito por un humano del equipo.
    # Naylan necesita distinguirlos para no asumir como propio lo que dijo un agente.
    autor: Mapped[str | None] = mapped_column(String(20), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MensajeProcesado(Base):
    """
    IDs de mensajes de WhatsApp ya procesados. Meta entrega los webhooks
    "al menos una vez": sin esta tabla un reintento hace que Naylan
    responda dos veces al mismo mensaje.
    """
    __tablename__ = "mensajes_procesados"

    mensaje_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    procesado_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Agente(Base):
    """Agente humano autorizado para atender conversaciones."""
    __tablename__ = "agentes"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(300))
    rol: Mapped[str] = mapped_column(String(50), default="agente")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Si tiene fecha, el agente cambió su password a mano y el seed por
    # env vars NO debe sobreescribirla en el siguiente arranque.
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgenteSesion(Base):
    """Sesión activa de un agente (token de acceso con expiración)."""
    __tablename__ = "agente_sesiones"

    token: Mapped[str] = mapped_column(String(100), primary_key=True)
    agente_id: Mapped[str] = mapped_column(String(50))
    agente_nombre: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ConversacionModo(Base):
    __tablename__ = "conversacion_modo"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    modo: Mapped[str] = mapped_column(String(20), default="bot")  # bot | humano
    handoff_status: Mapped[str] = mapped_column(String(30), default="BOT_ACTIVE")
    assigned_agent: Mapped[str | None] = mapped_column(String(100), nullable=True)
    handoff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    handoff_priority: Mapped[str] = mapped_column(String(20), default="NORMAL")  # NORMAL|HIGH|CRITICAL
    nombre_perfil: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    # Crear tablas en su propia transacción (aislada de las migraciones)
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Migraciones de columnas. Se inspecciona el esquema real y solo se agregan
    # las que faltan: "ADD COLUMN IF NOT EXISTS" es válido en PostgreSQL pero NO
    # en SQLite, donde fallaría en silencio dejando la columna sin crear.
    # Tipos usados: TIMESTAMP y BOOLEAN son válidos en ambos motores.
    columnas = [
        ("conversacion_modo", "handoff_status", "VARCHAR(30) DEFAULT 'BOT_ACTIVE'"),
        ("conversacion_modo", "assigned_agent", "VARCHAR(100)"),
        ("conversacion_modo", "handoff_summary", "TEXT"),
        ("conversacion_modo", "handoff_priority", "VARCHAR(20) DEFAULT 'NORMAL'"),
        ("conversacion_modo", "notification_sent", "BOOLEAN DEFAULT FALSE"),
        ("conversacion_modo", "notification_sent_at", "TIMESTAMP"),
        ("conversacion_modo", "claimed_at", "TIMESTAMP"),
        ("conversacion_modo", "resolved_at", "TIMESTAMP"),
        ("conversacion_modo", "nombre_perfil", "VARCHAR(200)"),
        ("mensajes", "autor", "VARCHAR(20)"),
        ("agentes", "password_changed_at", "TIMESTAMP"),
    ]
    for tabla, columna, tipo in columnas:
        try:
            async with get_engine().begin() as conn:
                existentes = await conn.run_sync(
                    lambda sync_conn: [
                        c["name"] for c in inspect(sync_conn).get_columns(tabla)
                    ]
                )
                if columna in existentes:
                    continue
                await conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}"))
                logger.info(f"Migración: columna {tabla}.{columna} agregada")
        except Exception as e:
            logger.warning(f"Migración {tabla}.{columna} omitida: {e}")

    # Normalización de teléfonos: unificar registros con y sin prefijo '+'
    # 1. En conversacion_modo (PK=telefono): eliminar duplicados sin '+' donde ya existe con '+'
    # 2. En conversacion_modo: agregar '+' a los restantes sin '+'
    # 3. En mensajes: agregar '+' a todos los teléfonos sin '+'
    for sql in [
        "DELETE FROM conversacion_modo WHERE telefono NOT LIKE '+%' AND ('+' || telefono) IN (SELECT telefono FROM conversacion_modo WHERE telefono LIKE '+%')",
        "UPDATE conversacion_modo SET telefono = '+' || telefono WHERE telefono NOT LIKE '+%' AND telefono != ''",
        "UPDATE mensajes SET telefono = '+' || telefono WHERE telefono NOT LIKE '+%' AND telefono != ''",
    ]:
        try:
            async with get_engine().begin() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass


async def actualizar_nombre_perfil(telefono: str, nombre: str | None):
    """Guarda o actualiza el nombre de perfil de WhatsApp del cliente."""
    if not nombre:
        return
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        if registro:
            registro.nombre_perfil = nombre
            registro.updated_at = datetime.utcnow()
        else:
            session.add(ConversacionModo(
                telefono=telefono,
                nombre_perfil=nombre,
                updated_at=datetime.utcnow(),
            ))
        await session.commit()


async def guardar_mensaje(telefono: str, role: str, content: str, autor: str | None = None):
    """
    Guarda un mensaje. `autor="agente"` marca los que escribió un humano
    del equipo desde el dashboard (ver obtener_historial).
    """
    async with get_session()() as session:
        session.add(Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            autor=autor,
            timestamp=datetime.utcnow()
        ))
        await session.commit()


# Prefijo que ve Naylan (no el cliente ni el dashboard) para los mensajes
# que escribió un agente humano durante un handoff.
MARCA_AGENTE = "[Mensaje enviado por un agente humano del equipo R8A]: "


async def registrar_mensaje_procesado(mensaje_id: str) -> bool:
    """
    Registra un mensaje_id de WhatsApp como procesado.
    Retorna True si es la primera vez (hay que procesarlo) y False si ya
    estaba registrado (reintento de Meta — hay que ignorarlo).
    """
    if not mensaje_id:
        return True  # sin ID no podemos deduplicar; procesamos
    async with get_session()() as session:
        try:
            session.add(MensajeProcesado(mensaje_id=mensaje_id, procesado_at=datetime.utcnow()))
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def limpiar_mensajes_procesados(dias: int = 7) -> int:
    """Borra IDs de mensajes procesados con más de N días. Retorna cuántos borró."""
    limite = datetime.utcnow() - timedelta(days=dias)
    async with get_session()() as session:
        result = await session.execute(
            select(MensajeProcesado).where(MensajeProcesado.procesado_at < limite)
        )
        viejos = result.scalars().all()
        for m in viejos:
            await session.delete(m)
        await session.commit()
        return len(viejos)


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    async with get_session()() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()
        mensajes.reverse()
        return [
            {
                "role": msg.role,
                "content": (MARCA_AGENTE + msg.content) if msg.autor == "agente" else msg.content,
            }
            for msg in mensajes
        ]


async def limpiar_historial(telefono: str):
    async with get_session()() as session:
        result = await session.execute(select(Mensaje).where(Mensaje.telefono == telefono))
        for msg in result.scalars().all():
            await session.delete(msg)
        await session.commit()


async def horas_desde_ultimo_mensaje_cliente(telefono: str) -> float | None:
    """Retorna cuántas horas pasaron desde el último mensaje del cliente (role='user').
    Retorna None si no hay mensajes del cliente."""
    async with get_session()() as session:
        result = await session.execute(
            select(Mensaje.timestamp)
            .where(Mensaje.telefono == telefono, Mensaje.role == "user")
            .order_by(Mensaje.timestamp.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        delta = datetime.utcnow() - row
        return delta.total_seconds() / 3600


async def obtener_modo(telefono: str) -> str:
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        return registro.modo if registro else "bot"


async def establecer_modo(telefono: str, modo: str):
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        if registro:
            registro.modo = modo
            registro.updated_at = datetime.utcnow()
        else:
            session.add(ConversacionModo(telefono=telefono, modo=modo, updated_at=datetime.utcnow()))
        await session.commit()


_UNSET = object()  # Sentinel para distinguir "no especificado" de None explícito


async def establecer_handoff(
    telefono: str,
    modo: str,
    handoff_status: str,
    assigned_agent: str | None | object = _UNSET,
    handoff_summary: str | None | object = _UNSET,
    handoff_priority: str = "NORMAL",
):
    """
    Actualiza modo y estado de handoff de una conversación.
    Si assigned_agent=None se pasa explícitamente, borra el campo (limpia el agente).
    Si no se pasa (sentinel _UNSET), no toca el campo existente.
    """
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        if registro:
            registro.modo = modo
            registro.handoff_status = handoff_status
            registro.handoff_priority = handoff_priority
            if assigned_agent is not _UNSET:
                registro.assigned_agent = assigned_agent  # type: ignore[assignment]
            if handoff_summary is not _UNSET:
                registro.handoff_summary = handoff_summary  # type: ignore[assignment]
            # Al cerrar o devolver la conversación, resetear notificación para
            # que un próximo handoff del mismo cliente sí notifique a los agentes.
            if handoff_status in ("BOT_ACTIVE", "RESOLVED"):
                registro.notification_sent = False
                registro.notification_sent_at = None
            registro.updated_at = datetime.utcnow()
        else:
            session.add(ConversacionModo(
                telefono=telefono,
                modo=modo,
                handoff_status=handoff_status,
                assigned_agent=assigned_agent if assigned_agent is not _UNSET else None,  # type: ignore[arg-type]
                handoff_summary=handoff_summary if handoff_summary is not _UNSET else None,  # type: ignore[arg-type]
                handoff_priority=handoff_priority,
                updated_at=datetime.utcnow(),
            ))
        await session.commit()


async def atomic_claim_conversation(telefono: str, agent_name: str) -> dict:
    """
    Reclama atómicamente una conversación para un agente.
    Tiene éxito si el estado es WAITING_HUMAN, BOT_ACTIVE, RESOLVED,
    o si no existe registro (conversación nueva — equivale a BOT_ACTIVE).
    Retorna {"success": True} o {"success": False, "reason": "ya_tomada"}.
    """
    now = datetime.utcnow()
    async with get_session()() as session:
        # Intentar UPDATE sobre estados tomables
        result = await session.execute(
            update(ConversacionModo)
            .where(
                ConversacionModo.telefono == telefono,
                ConversacionModo.handoff_status.in_(["WAITING_HUMAN", "BOT_ACTIVE", "RESOLVED"])
            )
            .values(
                modo="humano",
                handoff_status="HUMAN_ACTIVE",
                assigned_agent=agent_name,
                claimed_at=now,
                updated_at=now,
            )
        )
        await session.commit()

        if result.rowcount > 0:
            return {"success": True}

        # rowcount=0: el registro no existe (nuevo) o ya está HUMAN_ACTIVE
        check = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = check.scalar_one_or_none()

        if registro is None:
            # Conversación nueva sin fila en BD — crear directamente como HUMAN_ACTIVE
            session.add(ConversacionModo(
                telefono=telefono,
                modo="humano",
                handoff_status="HUMAN_ACTIVE",
                assigned_agent=agent_name,
                claimed_at=now,
                updated_at=now,
            ))
            await session.commit()
            return {"success": True}

        # Existe pero en HUMAN_ACTIVE → ya tomada por otro agente
        return {"success": False, "reason": "ya_tomada"}


async def marcar_notificacion_enviada(telefono: str):
    """Marca que ya se envió notificación de handoff para esta conversación."""
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        if registro:
            registro.notification_sent = True
            registro.notification_sent_at = datetime.utcnow()
            registro.updated_at = datetime.utcnow()
            await session.commit()


async def obtener_handoff_status(telefono: str) -> str:
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        return registro.handoff_status if registro else "BOT_ACTIVE"


async def obtener_handoff_resumen(telefono: str) -> str | None:
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        return registro.handoff_summary if registro else None


async def obtener_registro_completo(telefono: str) -> ConversacionModo | None:
    """Retorna el registro completo de ConversacionModo para un teléfono."""
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        return result.scalar_one_or_none()


async def listar_conversaciones() -> list[dict]:
    async with get_session()() as session:
        query = (
            select(
                Mensaje.telefono,
                func.max(Mensaje.timestamp).label("ultimo_timestamp"),
                func.count(Mensaje.id).label("total_mensajes"),
                func.sum(
                    func.cast(Mensaje.role == "user", Integer)
                ).label("mensajes_entrantes"),
            )
            .group_by(Mensaje.telefono)
            .order_by(func.max(Mensaje.timestamp).desc())
        )
        result = await session.execute(query)
        rows = result.all()

        phones = [row.telefono for row in rows]
        estados: dict[str, ConversacionModo] = {}
        if phones:
            modo_result = await session.execute(
                select(ConversacionModo).where(ConversacionModo.telefono.in_(phones))
            )
            estados = {m.telefono: m for m in modo_result.scalars().all()}

        return [
            {
                "telefono": row.telefono,
                "ultimo_mensaje": row.ultimo_timestamp.isoformat() if row.ultimo_timestamp else None,
                "total_mensajes": row.total_mensajes,
                "mensajes_entrantes": int(row.mensajes_entrantes or 0),
                "modo": estados[row.telefono].modo if row.telefono in estados else "bot",
                "handoff_status": estados[row.telefono].handoff_status if row.telefono in estados else "BOT_ACTIVE",
                "handoff_priority": estados[row.telefono].handoff_priority if row.telefono in estados else "NORMAL",
                "assigned_agent": estados[row.telefono].assigned_agent if row.telefono in estados else None,
                "nombre_perfil": estados[row.telefono].nombre_perfil if row.telefono in estados else None,
            }
            for row in rows
        ]


async def obtener_historial_completo(telefono: str, limite: int = 50) -> list[dict]:
    async with get_session()() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()
        mensajes.reverse()
        return [
            {
                "role": msg.role,
                "content": msg.content,
                "autor": msg.autor,
                "timestamp": msg.timestamp.isoformat(),
            }
            for msg in mensajes
        ]


# ─── Autenticación individual de agentes ─────────────────────────────────────

def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 con salt aleatorio. Sin dependencias externas."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 260000)
    return f"pbkdf2:{salt}:{dk.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        _, salt, stored = hashed.split(":")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 260000)
        return secrets.compare_digest(dk.hex(), stored)
    except Exception:
        return False


async def crear_agente(agente_id: str, nombre: str, password: str, rol: str = "agente") -> bool:
    """
    Crea el agente si no existe. Si ya existe, sincroniza su password desde la
    env var SALVO que el agente la haya cambiado a mano (password_changed_at):
    de lo contrario cada redeploy revertiría el cambio del agente.
    Retorna True si fue creado.
    """
    async with get_session()() as session:
        result = await session.execute(select(Agente).where(Agente.id == agente_id))
        existing = result.scalar_one_or_none()
        if existing:
            existing.nombre = nombre
            if existing.password_changed_at is None:
                existing.password_hash = hash_password(password)
            await session.commit()
            return False
        session.add(Agente(
            id=agente_id,
            nombre=nombre,
            password_hash=hash_password(password),
            rol=rol,
            enabled=True,
            created_at=datetime.utcnow(),
        ))
        await session.commit()
        return True


async def validar_credenciales(username: str, password: str) -> "Agente | None":
    """Valida usuario y contraseña. Retorna el objeto Agente o None."""
    async with get_session()() as session:
        result = await session.execute(
            select(Agente).where(Agente.id == username.lower().strip(), Agente.enabled == True)
        )
        agente = result.scalar_one_or_none()
        if agente and verify_password(password, agente.password_hash):
            return agente
        return None


async def crear_sesion(agente_id: str, agente_nombre: str) -> tuple[str, datetime]:
    """Crea una sesión de 24 horas. Limpia sesiones expiradas del mismo agente."""
    token = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(hours=24)
    async with get_session()() as session:
        # Limpiar sesiones expiradas del agente
        result = await session.execute(
            select(AgenteSesion).where(
                AgenteSesion.agente_id == agente_id,
                AgenteSesion.expires_at < datetime.utcnow(),
            )
        )
        for s in result.scalars().all():
            await session.delete(s)
        session.add(AgenteSesion(
            token=token,
            agente_id=agente_id,
            agente_nombre=agente_nombre,
            created_at=datetime.utcnow(),
            expires_at=expires_at,
        ))
        await session.commit()
    return token, expires_at


async def validar_token(token: str) -> "Agente | None":
    """Valida un token de sesión. Retorna el Agente o None si expiró/inválido."""
    async with get_session()() as session:
        result = await session.execute(
            select(AgenteSesion).where(
                AgenteSesion.token == token,
                AgenteSesion.expires_at > datetime.utcnow(),
            )
        )
        sesion = result.scalar_one_or_none()
        if not sesion:
            return None
        agente_result = await session.execute(
            select(Agente).where(Agente.id == sesion.agente_id, Agente.enabled == True)
        )
        return agente_result.scalar_one_or_none()


async def invalidar_sesion(token: str) -> None:
    """Elimina un token de sesión (logout)."""
    async with get_session()() as session:
        result = await session.execute(
            select(AgenteSesion).where(AgenteSesion.token == token)
        )
        sesion = result.scalar_one_or_none()
        if sesion:
            await session.delete(sesion)
            await session.commit()


async def cambiar_password(agente_id: str, nuevo_password: str) -> bool:
    """Cambia el password de un agente. Retorna False si no existe."""
    async with get_session()() as session:
        result = await session.execute(select(Agente).where(Agente.id == agente_id))
        agente = result.scalar_one_or_none()
        if not agente:
            return False
        agente.password_hash = hash_password(nuevo_password)
        agente.password_changed_at = datetime.utcnow()
        await session.commit()
        return True
