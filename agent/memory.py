# agent/memory.py — Memoria de conversaciones con SQLite/PostgreSQL
import os
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, Integer, func
from dotenv import load_dotenv

load_dotenv()

# El engine se crea en get_engine() para que no crashee al importar
_engine = None
_async_session = None


def _get_database_url() -> str:
    # Railway puede inyectar DATABASE_URL vacío si hay conflicto con el plugin de Postgres
    # Se prueban múltiples variables en orden de preferencia
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


class ConversacionModo(Base):
    __tablename__ = "conversacion_modo"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    modo: Mapped[str] = mapped_column(String(20), default="bot")  # bot | humano
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def guardar_mensaje(telefono: str, role: str, content: str):
    async with get_session()() as session:
        mensaje = Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow()
        )
        session.add(mensaje)
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
        return [
            {"role": msg.role, "content": msg.content}
            for msg in mensajes
        ]


async def limpiar_historial(telefono: str):
    async with get_session()() as session:
        query = select(Mensaje).where(Mensaje.telefono == telefono)
        result = await session.execute(query)
        mensajes = result.scalars().all()
        for msg in mensajes:
            await session.delete(msg)
        await session.commit()


async def obtener_modo(telefono: str) -> str:
    """Retorna el modo de la conversación: 'bot' (default) o 'humano'."""
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        return registro.modo if registro else "bot"


async def establecer_modo(telefono: str, modo: str):
    """Establece el modo de la conversación (bot | humano)."""
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


async def listar_conversaciones() -> list[dict]:
    """Lista todas las conversaciones con su último mensaje y modo actual."""
    async with get_session()() as session:
        # Todos los teléfonos únicos con timestamp del último mensaje
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
        modos: dict[str, str] = {}
        if phones:
            modo_result = await session.execute(
                select(ConversacionModo).where(ConversacionModo.telefono.in_(phones))
            )
            modos = {m.telefono: m.modo for m in modo_result.scalars().all()}

        return [
            {
                "telefono": row.telefono,
                "ultimo_mensaje": row.ultimo_timestamp.isoformat() if row.ultimo_timestamp else None,
                "total_mensajes": row.total_mensajes,
                "modo": modos.get(row.telefono, "bot"),
            }
            for row in rows
        ]


async def obtener_historial_completo(telefono: str, limite: int = 50) -> list[dict]:
    """Igual que obtener_historial pero incluye timestamp — para el dashboard."""
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
