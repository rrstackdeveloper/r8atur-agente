# agent/providers/twilio.py — Adaptador para Twilio WhatsApp
import os
import logging
import base64
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorTwilio(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Twilio."""

    def __init__(self):
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.phone_number = os.getenv("TWILIO_PHONE_NUMBER")

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload form-encoded de Twilio. Soporta texto y audios."""
        form = await request.form()
        texto = form.get("Body", "").strip()
        telefono = form.get("From", "").replace("whatsapp:", "")
        mensaje_id = form.get("MessageSid", "")
        num_media = int(form.get("NumMedia", "0"))

        # Si hay un audio adjunto y el texto está vacío, transcribir
        if num_media > 0 and not texto:
            content_type = form.get("MediaContentType0", "")
            media_url = form.get("MediaUrl0", "")

            if content_type.startswith("audio/") and media_url:
                from agent.transcriber import transcribir_audio
                transcripcion = await transcribir_audio(
                    media_url, self.account_sid, self.auth_token
                )
                if transcripcion:
                    texto = transcripcion
                    logger.info(f"Audio de {telefono} transcrito: {texto}")
                else:
                    logger.warning(f"No se pudo transcribir el audio de {telefono}")
                    return []

        if not texto:
            return []

        return [MensajeEntrante(
            telefono=telefono,
            texto=texto,
            mensaje_id=mensaje_id,
            es_propio=False,
        )]

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía mensaje via Twilio API."""
        if not all([self.account_sid, self.auth_token, self.phone_number]):
            logger.warning("Variables de Twilio no configuradas")
            return False
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        auth = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        data = {
            "From": f"whatsapp:{self.phone_number}",
            "To": f"whatsapp:{telefono}",
            "Body": mensaje,
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, data=data, headers=headers)
            if r.status_code != 201:
                logger.error(f"Error Twilio: {r.status_code} — {r.text}")
            return r.status_code == 201
