# agent/memory.py — Memoria de conversaciones con SQLite/PostgreSQL
import os
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, Boolean, select, Integer, func, text, update
from dotenv import load_dotenv

load_dotenv()

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
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Agente(Base):
    """Agente humano autorizado para atender conversaciones."""
    __tablename__ = "agentes"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(300))
    rol: Mapped[str] = mapped_column(String(50), default="agente")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    # Crear tablas en su propia transacción (aislada de las migraciones)
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Cada migración en su propia transacción — si falla (columna ya existe) no
    # afecta a las demás ni revierte el CREATE TABLE anterior. Crítico en PostgreSQL.
    # Tipos usados: TIMESTAMP (válido en PostgreSQL y SQLite), FALSE (válido en ambos).
    for sql in [
        "ALTER TABLE conversacion_modo ADD COLUMN IF NOT EXISTS handoff_status VARCHAR(30) DEFAULT 'BOT_ACTIVE'",
        "ALTER TABLE conversacion_modo ADD COLUMN IF NOT EXISTS assigned_agent VARCHAR(100)",
        "ALTER TABLE conversacion_modo ADD COLUMN IF NOT EXISTS handoff_summary TEXT",
        "ALTER TABLE conversacion_modo ADD COLUMN IF NOT EXISTS handoff_priority VARCHAR(20) DEFAULT 'NORMAL'",
        "ALTER TABLE conversacion_modo ADD COLUMN IF NOT EXISTS notification_sent BOOLEAN DEFAULT FALSE",
        "ALTER TABLE conversacion_modo ADD COLUMN IF NOT EXISTS notification_sent_at TIMESTAMP",
        "ALTER TABLE conversacion_modo ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP",
        "ALTER TABLE conversacion_modo ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP",
    ]:
        try:
            async with get_engine().begin() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass


async def guardar_mensaje(telefono: str, role: str, content: str):
    async with get_session()() as session:
        session.add(Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow()
        ))
        await session.commit()


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
        return [{"role": msg.role, "content": msg.content} for msg in mensajes]


async def limpiar_historial(telefono: str):
    async with get_session()() as session:
        result = await session.execute(select(Mensaje).where(Mensaje.telefono == telefono))
        for msg in result.scalars().all():
            await session.delete(msg)
        await session.commit()


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
    Solo tiene éxito si el estado actual es WAITING_HUMAN o BOT_ACTIVE.
    Retorna {"success": True} o {"success": False, "reason": "ya_tomada"}.
    """
    async with get_session()() as session:
        result = await session.execute(
            update(ConversacionModo)
            .where(
                ConversacionModo.telefono == telefono,
                ConversacionModo.handoff_status.in_(["WAITING_HUMAN", "BOT_ACTIVE"])
            )
            .values(
                modo="humano",
                handoff_status="HUMAN_ACTIVE",
                assigned_agent=agent_name,
                claimed_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()
        if result.rowcount == 0:
            return {"success": False, "reason": "ya_tomada"}
        return {"success": True}


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
                "modo": estados[row.telefono].modo if row.telefono in estados else "bot",
                "handoff_status": estados[row.telefono].handoff_status if row.telefono in estados else "BOT_ACTIVE",
                "handoff_priority": estados[row.telefono].handoff_priority if row.telefono in estados else "NORMAL",
                "assigned_agent": estados[row.telefono].assigned_agent if row.telefono in estados else None,
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
            {"role": msg.role, "content": msg.content, "timestamp": msg.timestamp.isoformat()}
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
    """Crea un agente si no existe. Retorna True si fue creado, False si ya existía."""
    async with get_session()() as session:
        result = await session.execute(select(Agente).where(Agente.id == agente_id))
        if result.scalar_one_or_none():
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
            select(Agente).where(Agente.id == username, Agente.enabled == True)
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
        await session.commit()
        return True
