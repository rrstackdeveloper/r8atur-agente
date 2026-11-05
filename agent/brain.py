# agent/brain.py — Cerebro del agente: conexión con Claude API
import os
import re
import json
import yaml
import logging
from datetime import datetime, timedelta, timezone
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def cargar_config_prompts() -> dict:
    """Lee la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    config = cargar_config_prompts()
    return config.get("system_prompt", "Eres Naylan, asistente de R8ATUR. Responde en español.")


# R8A opera desde Salvador, Bahia (UTC-3). Brasil no usa horario de verano
# desde 2019, asi que el offset fijo es correcto todo el año; si zoneinfo
# esta disponible se usa igual por si las reglas cambian.
_TZ_BRASIL = timezone(timedelta(hours=-3))
try:
    from zoneinfo import ZoneInfo
    _TZ_BRASIL = ZoneInfo("America/Bahia")
except Exception:  # pragma: no cover — falta tzdata en el contenedor
    pass

_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def contexto_temporal(ahora: datetime | None = None) -> str:
    """
    Bloque con la fecha actual que se agrega al system prompt en cada mensaje.

    Sin esto Claude no sabe en qué fecha vive y resuelve las referencias
    relativas ("en diciembre", "el mes que viene") con el año de su
    entrenamiento: un cliente pidió vuelos para diciembre y Naylan cotizó
    diciembre de 2025 estando ya en 2026.
    """
    ahora = ahora or datetime.now(_TZ_BRASIL)
    dia = _DIAS[ahora.weekday()]
    mes = _MESES[ahora.month - 1]
    return f"""

  ## Fecha actual — IMPORTANTE
  Hoy es {dia} {ahora.day} de {mes} de {ahora.year} (hora de Brasil).
  El año en curso es {ahora.year}. El mes en curso es {mes} de {ahora.year}.

  Reglas para interpretar fechas:
  - Si el cliente menciona un mes sin año, se refiere a la próxima vez que
    ocurra ese mes contando desde hoy. Si el mes ya pasó este año, es del año
    siguiente.
  - NUNCA asumas un año anterior al actual ({ahora.year}) para un viaje futuro.
    Un viaje no puede ser en el pasado.
  - Escribe SIEMPRE el año explícito en los resúmenes de cotización.
  - Si la fecha que entendiste queda a más de 10 meses, o la referencia del
    cliente es ambigua, confirma el año con él antes de cerrar el resumen.
  - "Fin de año" = diciembre de {ahora.year}. "Año que viene" = {ahora.year + 1}.
"""


def cargar_system_prompt_completo() -> str:
    """System prompt del negocio + la fecha actual."""
    return cargar_system_prompt() + contexto_temporal()


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Lo siento, estoy teniendo problemas técnicos. Por favor intenta de nuevo en unos minutos.")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "Disculpa, no entendí tu mensaje. ¿Podrías reformularlo?")


def extraer_escalado(response: str) -> tuple[str, dict | None]:
    """
    Detecta y extrae la señal [ESCALATE:{...}] del final de la respuesta de Claude.
    Retorna (respuesta_limpia, datos_del_handoff_o_None).
    """
    marker = "[ESCALATE:"
    idx = response.rfind(marker)
    if idx == -1:
        logger.debug("No se detectó señal [ESCALATE] en la respuesta de Claude")
        return response, None
    end_idx = response.find("]", idx + len(marker))
    if end_idx == -1:
        logger.warning("Señal [ESCALATE] incompleta — falta el cierre ']'")
        return response, None
    json_str = response[idx + len(marker):end_idx]
    clean = response[:idx].strip()
    try:
        data = json.loads(json_str)
        logger.info(f"Señal [ESCALATE] detectada: servicio={data.get('servicio')} prioridad={data.get('prioridad')}")
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Señal [ESCALATE] con JSON inválido: {e} — raw: {json_str[:100]}")
        data = {}
    return clean, data


async def generar_respuesta(mensaje: str, historial: list[dict]) -> str:
    """
    Genera una respuesta usando Claude API.

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores ya guardados en BD

    Returns:
        La respuesta generada por Naylan
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt_completo()

    mensajes = list(historial)
    mensajes.append({"role": "user", "content": mensaje})

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )
        respuesta = response.content[0].text
        logger.info(f"Tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out")
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
