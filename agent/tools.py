# agent/tools.py — Herramientas específicas de R8ATUR
import os
import yaml
import logging
from datetime import datetime

logger = logging.getLogger("agentkit")


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_servicios() -> list[dict]:
    """Retorna la lista de servicios de R8ATUR."""
    info = cargar_info_negocio()
    return info.get("servicios", [])


def obtener_info_contacto() -> dict:
    """Retorna la información de contacto del negocio."""
    info = cargar_info_negocio()
    negocio = info.get("negocio", {})
    return {
        "email": negocio.get("email", "r8a_agetur@r8atur.com"),
        "telefono": negocio.get("telefono", "+55 75 99877-1684"),
        "sitio_web": negocio.get("sitio_web", "https://www.r8atur.com"),
    }


def buscar_en_knowledge(consulta: str) -> str:
    """Busca información relevante en los archivos de /knowledge."""
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    return "\n---\n".join(resultados) if resultados else "No encontré información específica sobre eso."


# ── Cotizaciones ──────────────────────────────────────────────────────────────

def registrar_solicitud_cotizacion(telefono: str, tipo: str, datos: dict) -> str:
    """
    Registra una solicitud de cotización con los datos recopilados.
    Retorna un número de referencia.

    Args:
        telefono: Número del cliente
        tipo: Tipo de servicio (vuelo, hotel, seguro, divisa, recarga)
        datos: Diccionario con los datos del cliente para cotizar
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    referencia = f"R8A-{tipo.upper()[:3]}-{timestamp[-6:]}"
    logger.info(f"Nueva cotización {referencia} para {telefono}: {datos}")
    return referencia


# ── Leads y ventas ────────────────────────────────────────────────────────────

def registrar_lead(telefono: str, nombre: str, interes: str) -> bool:
    """Registra un lead interesado en servicios de R8ATUR."""
    logger.info(f"Nuevo lead: {nombre} ({telefono}) — interés: {interes}")
    return True


def escalar_a_especialista(telefono: str, contexto: str) -> str:
    """
    Escala la conversación a un especialista humano.
    Retorna el mensaje de confirmación para el cliente.
    """
    logger.info(f"Escalando {telefono} a especialista. Contexto: {contexto}")
    return (
        "Perfecto, voy a conectarte con uno de nuestros especialistas ahora. "
        "Te contactarán en breve al +55 75 99877-1684 o a tu WhatsApp. "
        "También puedes escribirnos directamente a r8a_agetur@r8atur.com ✈️"
    )
