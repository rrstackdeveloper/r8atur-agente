# agent/brain.py — Cerebro del agente: conexión con Claude API
import os
import re
import json
import yaml
import logging
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

    system_prompt = cargar_system_prompt()

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
