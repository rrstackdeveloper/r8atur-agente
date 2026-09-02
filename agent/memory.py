# agent/memory.py — Memoria de conversaciones con SQLite/PostgreSQL
import os
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, Integer, func, text
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


class ConversacionModo(Base):
    __tablename__ = "conversacion_modo"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    modo: Mapped[str] = mapped_column(String(20), default="bot")  # bot | humano
    handoff_status: Mapped[str] = mapped_column(String(30), default="BOT_ACTIVE")
    assigned_agent: Mapped[str | None] = mapped_column(String(100), nullable=True)
    handoff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migraciones para columnas nuevas (no falla si ya existen)
        for sql in [
            "ALTER TABLE conversacion_modo ADD COLUMN handoff_status VARCHAR(30) DEFAULT 'BOT_ACTIVE'",
            "ALTER TABLE conversacion_modo ADD COLUMN assigned_agent VARCHAR(100)",
            "ALTER TABLE conversacion_modo ADD COLUMN handoff_summary TEXT",
        ]:
            try:
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


async def establecer_handoff(
    telefono: str,
    modo: str,
    handoff_status: str,
    assigned_agent: str | None = None,
    handoff_summary: str | None = None,
):
    """Actualiza modo y estado de handoff de una conversación."""
    async with get_session()() as session:
        result = await session.execute(
            select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        if registro:
            registro.modo = modo
            registro.handoff_status = handoff_status
            if assigned_agent is not None:
                registro.assigned_agent = assigned_agent
            if handoff_summary is not None:
                registro.handoff_summary = handoff_summary
            registro.updated_at = datetime.utcnow()
        else:
            session.add(ConversacionModo(
                telefono=telefono,
                modo=modo,
                handoff_status=handoff_status,
                assigned_agent=assigned_agent,
                handoff_summary=handoff_summary,
                updated_at=datetime.utcnow(),
            ))
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
