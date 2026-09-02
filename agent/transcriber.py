# agent/transcriber.py — Transcripción de audios con OpenAI Whisper
import io
import os
import logging
import httpx
from openai import AsyncOpenAI

logger = logging.getLogger("agentkit")

_openai_client = None


def _get_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client


async def transcribir_audio(media_url: str, account_sid: str, auth_token: str) -> str | None:
    """
    Descarga un audio de Twilio y lo transcribe con Whisper.
    Retorna el texto transcrito, o None si falla.
    """
    try:
        # Descargar audio — Twilio requiere autenticación básica
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(media_url, auth=(account_sid, auth_token))
            if r.status_code != 200:
                logger.error(f"Error descargando audio: {r.status_code}")
                return None
            audio_bytes = r.content
            content_type = r.headers.get("content-type", "audio/ogg")

        # Determinar extensión según content-type
        extensiones = {
            "audio/ogg": "ogg",
            "audio/mpeg": "mp3",
            "audio/mp4": "mp4",
            "audio/webm": "webm",
            "audio/amr": "amr",
        }
        ext = extensiones.get(content_type.split(";")[0].strip(), "ogg")

        # Transcribir con Whisper
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = f"audio.{ext}"

        transcript = await _get_client().audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="es",
        )
        texto = transcript.text.strip()
        logger.info(f"Audio transcrito ({len(audio_bytes)} bytes): {texto}")
        return texto

    except Exception as e:
        logger.error(f"Error transcribiendo audio: {e}")
        return None


async def transcribir_audio_meta(media_id: str, access_token: str) -> str | None:
    """
    Descarga un audio de Meta Cloud API y lo transcribe con Whisper.
    Meta requiere: 1) obtener URL via Graph API, 2) descargar con Bearer token.
    """
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Obtener URL real del audio desde Graph API
            r = await client.get(
                f"https://graph.facebook.com/v21.0/{media_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if r.status_code != 200:
                logger.error(f"Error obteniendo URL de audio Meta: {r.status_code}")
                return None
            media_url = r.json().get("url")
            if not media_url:
                return None

            # Descargar el audio con el token
            r2 = await client.get(
                media_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if r2.status_code != 200:
                logger.error(f"Error descargando audio Meta: {r2.status_code}")
                return None
            audio_bytes = r2.content
            content_type = r2.headers.get("content-type", "audio/ogg")

        extensiones = {
            "audio/ogg": "ogg",
            "audio/mpeg": "mp3",
            "audio/mp4": "mp4",
            "audio/webm": "webm",
            "audio/amr": "amr",
        }
        ext = extensiones.get(content_type.split(";")[0].strip(), "ogg")

        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = f"audio.{ext}"

        transcript = await _get_client().audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="es",
        )
        texto = transcript.text.strip()
        logger.info(f"Audio Meta transcrito ({len(audio_bytes)} bytes): {texto}")
        return texto

    except Exception as e:
        logger.error(f"Error transcribiendo audio Meta: {e}")
        return None
