# agent/providers/meta.py — Adaptador para Meta WhatsApp Cloud API

import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorMeta(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando la API oficial de Meta (Cloud API)."""

    def __init__(self):
        self.access_token = os.getenv("META_ACCESS_TOKEN")
        self.phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
        self.verify_token = os.getenv("META_VERIFY_TOKEN", "agentkit-verify")
        self.api_version = "v21.0"

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """Meta requiere verificación GET con hub.verify_token."""
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if mode == "subscribe" and token == self.verify_token:
            return int(challenge)
        return None

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload anidado de Meta Cloud API."""
        body = await request.json()
        mensajes = []
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # Mapear wa_id → nombre de perfil desde el array contacts
                nombres = {
                    c.get("wa_id", ""): c.get("profile", {}).get("name")
                    for c in value.get("contacts", [])
                }
                # Log status updates (delivery receipts, errors) de mensajes enviados
                for status in value.get("statuses", []):
                    st = status.get("status")
                    wamid = status.get("id", "?")
                    recipient = status.get("recipient_id", "?")
                    if st == "failed":
                        errors = status.get("errors", [])
                        logger.error(f"Meta status FAILED — wamid={wamid}, to={recipient}, errors={errors}")
                    else:
                        logger.info(f"Meta status {st} — wamid={wamid}, to={recipient}")

                for msg in value.get("messages", []):
                    tipo = msg.get("type")
                    if tipo == "text":
                        texto = msg.get("text", {}).get("body", "")
                    elif tipo == "audio":
                        media_id = msg.get("audio", {}).get("id")
                        if not media_id or not self.access_token:
                            continue
                        from agent.transcriber import transcribir_audio_meta
                        texto = await transcribir_audio_meta(media_id, self.access_token)
                        if not texto:
                            continue
                        logger.info(f"Audio transcrito: {texto}")
                    else:
                        continue
                    raw_phone = msg.get("from", "")
                    telefono = raw_phone if raw_phone.startswith("+") else f"+{raw_phone}"
                    mensajes.append(MensajeEntrante(
                        telefono=telefono,
                        texto=texto,
                        mensaje_id=msg.get("id", ""),
                        nombre_perfil=nombres.get(raw_phone),
                        es_propio=False,
                    ))
        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía mensaje via Meta WhatsApp Cloud API."""
        if not self.access_token or not self.phone_number_id:
            logger.warning("META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no configurados")
            return False
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "text",
            "text": {"body": mensaje},
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                logger.error(f"Error Meta API: {r.status_code} — {r.text}")
            return r.status_code == 200

    async def subir_media(self, file_bytes: bytes, mime_type: str, filename: str) -> str | None:
        """Sube un archivo a Meta y retorna el media_id."""
        if not self.access_token or not self.phone_number_id:
            return None
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/media"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (filename, file_bytes, mime_type)},
            )
            if r.status_code != 200:
                logger.error(f"Error subiendo media a Meta: {r.status_code} — {r.text}")
                return None
            return r.json().get("id")

    async def enviar_media(
        self,
        telefono: str,
        media_id: str,
        tipo: str,
        caption: str = "",
        filename: str = "",
    ) -> bool:
        """Envía imagen o documento via Meta Cloud API usando un media_id."""
        if not self.access_token or not self.phone_number_id:
            return False
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        media_obj: dict = {"id": media_id}
        if caption:
            media_obj["caption"] = caption
        if tipo == "document" and filename:
            media_obj["filename"] = filename
        payload = {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": tipo,
            tipo: media_obj,
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers)
            try:
                resp_json = r.json()
            except Exception:
                resp_json = {"raw": r.text}
            if r.status_code != 200:
                logger.error(f"Error enviando media HTTP {r.status_code}: {resp_json}")
                return False
            if "error" in resp_json:
                logger.error(f"Meta API error en enviar_media: {resp_json['error']}")
                return False
            wamid = resp_json.get("messages", [{}])[0].get("id", "?")
            logger.info(f"enviar_media OK — wamid={wamid}, to={telefono}, tipo={tipo}")
            return True

    async def enviar_plantilla_handoff(
        self,
        telefono: str,
        cliente: str,
        servicio: str,
        motivo: str,
        prioridad: str,
        resumen: str,
        link: str,
    ) -> bool:
        """
        Envía notificación de handoff usando la plantilla aprobada 'handoff_agente'.
        Necesaria para contactar números que no iniciaron conversación en las últimas 24h.
        """
        if not self.access_token or not self.phone_number_id:
            return False
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "template",
            "template": {
                "name": "handoff_agente",
                "language": {"code": "es"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": cliente},
                            {"type": "text", "text": servicio},
                            {"type": "text", "text": motivo},
                            {"type": "text", "text": prioridad},
                            {"type": "text", "text": resumen[:400]},
                            {"type": "text", "text": link},
                        ],
                    }
                ],
            },
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                logger.error(f"Error plantilla handoff_agente: {r.status_code} — {r.text}")
            return r.status_code == 200
