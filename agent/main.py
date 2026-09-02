# agent/main.py — Servidor FastAPI + Webhook de WhatsApp para R8ATUR
import os
import yaml
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from agent.brain import generar_respuesta, extraer_escalado
from agent.memory import (
    inicializar_db, guardar_mensaje, obtener_historial,
    obtener_modo, establecer_modo, establecer_handoff,
    obtener_handoff_status, listar_conversaciones, obtener_historial_completo,
    atomic_claim_conversation, marcar_notificacion_enviada, obtener_registro_completo,
    validar_token, validar_credenciales, crear_sesion, invalidar_sesion,
    crear_agente, cambiar_password,
)
from agent.providers import obtener_proveedor
from agent.providers.base import ProveedorWhatsApp

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor: ProveedorWhatsApp | None = None


async def seed_agentes_desde_config():
    """
    Crea agentes en BD desde config/agents.yaml + env vars AGENT_{ID}_PASSWORD.
    Solo crea — nunca sobreescribe passwords existentes.
    """
    try:
        with open("config/agents.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return
    for ag in data.get("agents", []):
        if not ag.get("enabled", False):
            continue
        agente_id = ag.get("id", "")
        if not agente_id:
            continue
        env_var = f"AGENT_{agente_id.upper()}_PASSWORD"
        password = os.getenv(env_var, "")
        if not password:
            logger.warning(f"Agente '{agente_id}': {env_var} no configurada — login individual deshabilitado")
            continue
        creado = await crear_agente(agente_id, ag.get("nombre", agente_id), password, ag.get("rol", "agente"))
        if creado:
            logger.info(f"Agente '{agente_id}' ({ag.get('nombre', agente_id)}) creado en BD")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global proveedor
    proveedor = obtener_proveedor()
    await inicializar_db()
    await seed_agentes_desde_config()
    PORT = os.getenv("PORT", "8000")
    logger.info(f"Servidor Naylan (R8ATUR) en puerto {PORT}")
    logger.info(f"Proveedor: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="Naylan — Agente de R8ATUR",
    version="1.0.0",
    lifespan=lifespan
)


def _cargar_agentes_yaml() -> list[dict]:
    """Lee config/agents.yaml y retorna lista de agentes habilitados."""
    try:
        with open("config/agents.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        agentes = data.get("agents", [])
        return [a for a in agentes if a.get("enabled", False)]
    except FileNotFoundError:
        return []


def _mapear_prioridad(prioridad_raw: str) -> str:
    """Mapea prioridad del JSON de escalado (alta/media/normal) → CRITICAL/HIGH/NORMAL."""
    return {"alta": "CRITICAL", "media": "HIGH", "normal": "NORMAL"}.get(
        str(prioridad_raw).lower(), "NORMAL"
    )


async def notificar_agentes(telefono: str, resumen: dict):
    """
    Envía notificación de handoff a los agentes configurados.
    Verifica notification_sent antes de enviar para evitar duplicados.
    Lee de config/agents.yaml con fallback a AGENT_PHONES env var.
    """
    # Verificar si ya se notificó (deduplicación)
    registro = await obtener_registro_completo(telefono)
    if registro and registro.notification_sent:
        logger.info(f"Notificación ya enviada para {telefono} — omitiendo duplicado")
        return

    # Obtener lista de teléfonos de agentes
    phones_agentes: list[str] = []

    # Primero intentar desde agents.yaml
    agentes_yaml = _cargar_agentes_yaml()
    if agentes_yaml:
        phones_agentes = [a["phone_e164"] for a in agentes_yaml if a.get("phone_e164")]
    else:
        # Fallback a AGENT_PHONES env var
        agent_phones_str = os.getenv("AGENT_PHONES", "").strip()
        if agent_phones_str:
            phones_agentes = [p.strip() for p in agent_phones_str.split(",") if p.strip()]

    if not phones_agentes:
        logger.warning("Sin agentes configurados — handoff creado pero agentes no notificados")
        return

    base_url = os.getenv("BASE_URL", "").rstrip("/")
    link = f"{base_url}/admin?conv={telefono}" if base_url else "Revisar el dashboard de Naylan"

    servicio = resumen.get("servicio", "No especificado")
    motivo = resumen.get("motivo", "No especificado")
    prioridad_raw = resumen.get("prioridad", "normal")
    prioridad_display = {"alta": "🔴 Alta", "media": "🟡 Media", "normal": "🟢 Normal"}.get(
        prioridad_raw, prioridad_raw
    )
    resumen_texto = resumen.get("resumen", "Sin resumen")

    notif = (
        f"🔔 *Nueva solicitud de atención humana — R8A*\n\n"
        f"📱 WhatsApp: {telefono}\n"
        f"🛠 Servicio: {servicio}\n"
        f"📋 Motivo: {motivo}\n"
        f"⚡ Prioridad: {prioridad_display}\n\n"
        f"📝 Resumen:\n{resumen_texto}\n\n"
        f"🔗 Abrir conversación:\n{link}"
    )

    notificacion_exitosa = False
    for agent_phone in phones_agentes:
        if agent_phone and proveedor:
            try:
                ok = await proveedor.enviar_mensaje(agent_phone, notif)
                if ok:
                    notificacion_exitosa = True
                    logger.info(f"Notificación enviada a agente {agent_phone}: ok")
                else:
                    # Meta puede rechazar por requerir template aprobado
                    logger.error(
                        f"Error enviando notificación a {agent_phone} — "
                        f"WHATSAPP_AGENT_NOTIFICATION_TEMPLATE_REQUIRED: "
                        f"Meta puede requerir un template aprobado para mensajes iniciados por el negocio. "
                        f"Ver: https://developers.facebook.com/docs/whatsapp/message-templates"
                    )
            except Exception as exc:
                logger.error(
                    f"Excepción enviando notificación a {agent_phone} — "
                    f"WHATSAPP_AGENT_NOTIFICATION_TEMPLATE_REQUIRED: {exc}"
                )

    if notificacion_exitosa:
        await marcar_notificacion_enviada(telefono)


@app.get("/")
async def health_check():
    return {"status": "ok", "agente": "Naylan", "negocio": "R8ATUR"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    if proveedor is None:
        return {"status": "starting"}
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request):
    if proveedor is None:
        raise HTTPException(status_code=503, detail="Servidor iniciando")
    try:
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            # HUMAN_ACTIVE: agente tomó la conversación → Naylan silenciada
            modo = await obtener_modo(msg.telefono)
            if modo == "humano":
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                logger.info(f"Conversación {msg.telefono} en HUMAN_ACTIVE — Naylan silenciada")
                continue

            historial = await obtener_historial(msg.telefono)
            respuesta_raw = await generar_respuesta(msg.texto, historial)

            # Detectar señal de escalamiento
            respuesta, escalado = extraer_escalado(respuesta_raw)

            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            if escalado is not None:
                # Verificar si ya existe un handoff activo (evitar duplicados)
                status_actual = await obtener_handoff_status(msg.telefono)
                if status_actual in ("WAITING_HUMAN", "HUMAN_ACTIVE"):
                    logger.info(
                        f"Handoff ya existe para {msg.telefono} (status={status_actual}) — omitiendo duplicado"
                    )
                else:
                    prioridad_raw = escalado.get("prioridad", "normal")
                    prioridad_db = _mapear_prioridad(prioridad_raw)
                    resumen_str = str(escalado)
                    await establecer_handoff(
                        msg.telefono,
                        modo="bot",  # Naylan sigue respondiendo hasta que un agente tome
                        handoff_status="WAITING_HUMAN",
                        handoff_summary=resumen_str,
                        handoff_priority=prioridad_db,
                    )
                    logger.info(f"Handoff creado para {msg.telefono}: {escalado} | prioridad={prioridad_db}")
                    await notificar_agentes(msg.telefono, escalado)

            logger.info(f"Respuesta a {msg.telefono}: {respuesta[:100]}...")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Admin Dashboard ──────────────────────────────────────────────────────────

class MensajeAdmin(BaseModel):
    texto: str


class ModoPayload(BaseModel):
    modo: str


class LoginPayload(BaseModel):
    username: str
    password: str


class ResetPasswordPayload(BaseModel):
    password: str


async def _get_agente(
    x_agent_token: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
) -> dict:
    """
    Dependency FastAPI para autenticación de agentes.
    Acepta X-Agent-Token (sesión individual) o X-Admin-Key (acceso maestro legacy).
    Devuelve dict con id, nombre y tipo del agente autenticado.
    """
    if x_agent_token:
        agente = await validar_token(x_agent_token)
        if agente:
            return {"id": agente.id, "nombre": agente.nombre, "tipo": "agente"}
    if x_admin_key and x_admin_key == os.getenv("ADMIN_PASSWORD", ""):
        return {"id": "admin", "nombre": "Admin", "tipo": "admin"}
    raise HTTPException(status_code=401, detail="No autorizado")


@app.post("/admin/auth/login")
async def admin_login(body: LoginPayload):
    agente = await validar_credenciales(body.username, body.password)
    if not agente:
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    token, expires_at = await crear_sesion(agente.id, agente.nombre)
    logger.info(f"Login: agente '{agente.id}' ({agente.nombre})")
    return {"token": token, "nombre": agente.nombre, "expires_at": expires_at.isoformat()}


@app.post("/admin/auth/logout")
async def admin_logout(x_agent_token: str | None = Header(default=None)):
    if x_agent_token:
        await invalidar_sesion(x_agent_token)
    return {"ok": True}


@app.post("/admin/agentes/{agente_id}/reset-password")
async def admin_reset_password(
    agente_id: str,
    body: ResetPasswordPayload,
    agente: dict = Depends(_get_agente),
):
    ok = await cambiar_password(agente_id, body.password)
    if not ok:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    logger.info(f"Password cambiado para agente '{agente_id}' por '{agente['id']}'")
    return {"ok": True}


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Naylan Admin — R8ATUR</title>
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;height:100vh;display:flex;flex-direction:column}
#login{display:flex;align-items:center;justify-content:center;height:100vh;background:#128C7E}
#login-box{background:white;padding:2rem;border-radius:12px;width:320px;box-shadow:0 4px 20px rgba(0,0,0,.2)}
#login-box h2{color:#128C7E;margin-bottom:.5rem;text-align:center}
#login-box p{text-align:center;color:#666;margin-bottom:1.5rem;font-size:.9rem}
#login-box input{width:100%;padding:.75rem;border:1px solid #ddd;border-radius:8px;margin-bottom:.75rem;font-size:1rem;outline:none}
#login-box input:focus{border-color:#128C7E}
#login-box button{width:100%;padding:.75rem;background:#128C7E;color:white;border:none;border-radius:8px;font-size:1rem;cursor:pointer}
#login-box button:hover{background:#0e7065}
#login-error{color:#e53e3e;font-size:.85rem;text-align:center;margin-top:.5rem;display:none}
#app{display:none;height:100vh;flex-direction:column}
header{background:#128C7E;color:white;padding:.75rem 1.5rem;display:flex;align-items:center;justify-content:space-between}
header h1{font-size:1.1rem}
#logout-btn{background:rgba(255,255,255,.2);border:none;color:white;padding:.4rem .9rem;border-radius:6px;cursor:pointer;font-size:.85rem}
.content{display:flex;flex:1;overflow:hidden}
#conv-list{width:300px;min-width:300px;background:white;border-right:1px solid #e2e8f0;overflow-y:auto;display:flex;flex-direction:column}
#conv-list h3{padding:.75rem 1rem;font-size:.75rem;color:#666;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e2e8f0;flex-shrink:0}
#conv-items{flex:1;overflow-y:auto}
.conv-item{padding:.85rem 1rem;cursor:pointer;border-bottom:1px solid #f0f2f5;transition:background .15s}
.conv-item:hover{background:#f7f8fa}
.conv-item.active{background:#e6f3f2;border-left:3px solid #128C7E}
.conv-phone{font-weight:600;font-size:.88rem;color:#1a202c}
.conv-meta{display:flex;align-items:center;gap:.4rem;margin-top:.3rem;flex-wrap:wrap}
.conv-time{font-size:.7rem;color:#999}
.badge{font-size:.65rem;padding:.2rem .55rem;border-radius:10px;font-weight:700;text-transform:uppercase;white-space:nowrap}
.badge-BOT_ACTIVE{background:#c6f6d5;color:#276749}
.badge-WAITING_HUMAN{background:#fef3c7;color:#92400e}
.badge-WAITING_HUMAN.critical{background:#fed7d7;color:#9b2c2c;border:1px solid #fc8181}
.badge-HUMAN_ACTIVE{background:#bee3f8;color:#2b6cb0}
.badge-RESOLVED{background:#e2e8f0;color:#4a5568}
#chat-panel{flex:1;display:flex;flex-direction:column;background:#e5ddd5;min-width:0}
#chat-header{background:white;padding:.75rem 1.25rem;border-bottom:1px solid #e2e8f0;display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:.5rem}
#chat-header-left{flex:1;min-width:0}
#chat-phone{font-weight:700;color:#1a202c;font-size:.95rem}
#chat-subtitle{font-size:.8rem;color:#666;margin-top:.15rem}
#chat-count{font-size:.75rem;color:#999;margin-top:.1rem}
#chat-actions{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.action-btn{padding:.45rem 1rem;border:none;border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:600;transition:background .15s}
#tomar-btn{background:#d69e2e;color:white}
#tomar-btn:hover{background:#b7791f}
#devolver-btn{background:#38a169;color:white}
#devolver-btn:hover{background:#276749}
#finalizar-btn{background:#3182ce;color:white}
#finalizar-btn:hover{background:#2b6cb0}
#override-warning{font-size:.75rem;color:#e53e3e;background:#fff5f5;border:1px solid #fed7d7;border-radius:6px;padding:.3rem .6rem}
#messages{flex:1;overflow-y:auto;padding:1rem;display:flex;flex-direction:column;gap:.4rem}
.msg-wrap{display:flex;flex-direction:column}
.msg{max-width:70%;padding:.6rem .9rem;border-radius:10px;font-size:.88rem;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.msg-user{align-self:flex-end;background:#dcf8c6;border-bottom-right-radius:2px}
.msg-assistant{align-self:flex-start;background:white;border-bottom-left-radius:2px}
.msg-time{font-size:.68rem;color:#999;margin-top:.2rem}
.msg-time-right{text-align:right}
#input-area{background:white;padding:.75rem 1rem;display:flex;gap:.75rem;align-items:flex-end;border-top:1px solid #e2e8f0}
#msg-input{flex:1;padding:.6rem .9rem;border:1px solid #e2e8f0;border-radius:20px;font-size:.9rem;resize:none;min-height:42px;max-height:120px;outline:none;font-family:inherit;transition:background .15s}
#msg-input:focus{border-color:#128C7E}
#msg-input:disabled{background:#f7f8fa;color:#999;cursor:not-allowed}
#send-btn{background:#128C7E;color:white;border:none;border-radius:50%;width:42px;height:42px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0}
#send-btn:disabled{background:#ccc;cursor:not-allowed}
#empty-state{flex:1;display:flex;align-items:center;justify-content:center;color:#999;font-size:.95rem}
</style>
</head>
<body>
<div id="login">
  <div id="login-box">
    <h2>Naylan Admin</h2>
    <p>Panel de administración R8ATUR</p>
    <input type="text" id="username-input" placeholder="Usuario (alejandro / yanara / jose)" autocomplete="username" onkeydown="if(event.key==='Enter')document.getElementById('pw').focus()">
    <input type="password" id="pw" placeholder="Contraseña" autocomplete="current-password" onkeydown="if(event.key==='Enter')login()">
    <button onclick="login()">Entrar</button>
    <p id="login-error">Usuario o contraseña incorrectos</p>
  </div>
</div>
<div id="app">
  <header>
    <h1>Naylan Admin — R8ATUR</h1>
    <span id="agent-display" style="font-size:.85rem;opacity:.85"></span>
    <button id="logout-btn" onclick="logout()">Salir</button>
  </header>
  <div class="content">
    <div id="conv-list">
      <h3>Conversaciones</h3>
      <div id="conv-items"></div>
    </div>
    <div id="chat-panel">
      <div id="empty-state">← Selecciona una conversación</div>
      <div id="chat-content" style="display:none;flex:1;flex-direction:column;overflow:hidden">
        <div id="chat-header">
          <div id="chat-header-left">
            <div id="chat-phone"></div>
            <div id="chat-subtitle"></div>
            <div id="chat-count"></div>
          </div>
          <div id="chat-actions">
            <span id="override-warning" style="display:none"></span>
            <button id="tomar-btn" class="action-btn" style="display:none" onclick="tomarConv()">👤 Tomar conversación</button>
            <button id="devolver-btn" class="action-btn" style="display:none" onclick="devolverConv()">🤖 Devolver a Naylan</button>
            <button id="finalizar-btn" class="action-btn" style="display:none" onclick="finalizarConv()">✅ Finalizar atención</button>
          </div>
        </div>
        <div id="messages"></div>
        <div id="input-area">
          <textarea id="msg-input" rows="1"
            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg()}"
            oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea>
          <button id="send-btn" onclick="sendMsg()">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M2 21l21-9L2 3v7l15 2-15 2v7z"/></svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
let key=null,agentName='',phone=null,handoffStatus='BOT_ACTIVE',assignedAgent=null,handoffPriority='NORMAL',timer=null,lastMsgTs=null;

function apiH(){return{'X-Agent-Token':key,'Content-Type':'application/json'};}
function apiHGet(){return{'X-Agent-Token':key};}

async function login(){
  const username=document.getElementById('username-input').value.trim();
  const pw=document.getElementById('pw').value;
  if(!username||!pw)return;
  const err=document.getElementById('login-error');
  err.style.display='none';
  try{
    const r=await fetch('/admin/auth/login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username,password:pw})
    });
    if(r.ok){
      const data=await r.json();
      key=data.token;
      agentName=data.nombre;
      localStorage.setItem('ak',data.token);
      localStorage.setItem('agentName',data.nombre);
      showApp();
    } else {
      err.style.display='block';
    }
  }catch(e){err.style.display='block';}
}

async function logout(){
  if(key){
    try{await fetch('/admin/auth/logout',{method:'POST',headers:{'X-Agent-Token':key}});}catch(_){}
  }
  localStorage.removeItem('ak');localStorage.removeItem('agentName');
  key=null;phone=null;lastMsgTs=null;clearInterval(timer);
  document.getElementById('app').style.display='none';
  document.getElementById('login').style.display='flex';
}

function showApp(){
  document.getElementById('login').style.display='none';
  document.getElementById('app').style.display='flex';
  document.getElementById('agent-display').textContent='👤 '+agentName;
  loadConvs();
  timer=setInterval(refresh,10000);
  const p=new URLSearchParams(window.location.search).get('conv');
  if(p){setTimeout(()=>selectConvByPhone(p),800);}
}

function _sortConvs(data){
  // Orden: WAITING_HUMAN CRITICAL → HIGH → NORMAL, luego HUMAN_ACTIVE, luego BOT_ACTIVE/RESOLVED
  const rank=(d)=>{
    const s=d.handoff_status||'BOT_ACTIVE';
    const p=d.handoff_priority||'NORMAL';
    if(s==='WAITING_HUMAN'){
      if(p==='CRITICAL')return 0;
      if(p==='HIGH')return 1;
      return 2;
    }
    if(s==='HUMAN_ACTIVE')return 3;
    if(s==='BOT_ACTIVE')return 4;
    return 5;
  };
  return [...data].sort((a,b)=>{
    const rd=rank(a)-rank(b);
    if(rd!==0)return rd;
    // Dentro del mismo grupo: más reciente primero
    return new Date(b.ultimo_mensaje||0)-new Date(a.ultimo_mensaje||0);
  });
}

function renderConvs(rawData){
  const data=_sortConvs(rawData);
  const c=document.getElementById('conv-items');
  if(!data.length){c.innerHTML='<p style="padding:1rem;color:#999;font-size:.85rem">Sin conversaciones aún</p>';return;}
  c.innerHTML=data.map(d=>{
    const hs=d.handoff_status||'BOT_ACTIVE';
    const hp=d.handoff_priority||'NORMAL';
    const isCritical=hs==='WAITING_HUMAN'&&hp==='CRITICAL';
    return `<div class="conv-item ${d.telefono===phone?'active':''}" onclick="selectConv('${d.telefono}','${hs}','${d.assigned_agent||''}','${hp}')">
      <div class="conv-phone">${d.telefono}</div>
      <div class="conv-meta">
        <span class="badge badge-${hs}${isCritical?' critical':''}">${fmtStatus(hs,hp)}</span>
        <span class="conv-time">${fmtTime(d.ultimo_mensaje)}</span>
      </div>
    </div>`;
  }).join('');
}

async function loadConvs(){
  const r=await fetch('/admin/api/conversaciones',{headers:apiHGet()});
  if(!r.ok){logout();return;}
  renderConvs(await r.json());
}

async function selectConv(t,hs,aa,hp){
  phone=t;
  handoffStatus=hs||'BOT_ACTIVE';
  assignedAgent=aa||null;
  handoffPriority=hp||'NORMAL';
  lastMsgTs=null; // forzar carga del historial en el próximo refresh
  document.getElementById('empty-state').style.display='none';
  document.getElementById('chat-content').style.display='flex';
  document.getElementById('chat-phone').textContent=t;
  updateStatusUI();
  await loadChat();
}

async function selectConvByPhone(p){
  const r=await fetch('/admin/api/conversaciones',{headers:apiHGet()});
  if(!r.ok)return;
  const data=await r.json();
  const conv=data.find(d=>d.telefono===p||d.telefono===p.replace('+',''));
  if(conv)await selectConv(conv.telefono,conv.handoff_status||'BOT_ACTIVE',conv.assigned_agent||'',conv.handoff_priority||'NORMAL');
}

async function loadChat(){
  if(!phone)return;
  const r=await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/historial',{headers:apiHGet()});
  if(!r.ok)return;
  const msgs=await r.json();
  const el=document.getElementById('messages');
  const atBot=el.scrollHeight-el.clientHeight<=el.scrollTop+20;
  el.innerHTML=msgs.map(m=>`<div class="msg-wrap">
    <div class="msg msg-${m.role}">${esc(m.content)}</div>
    <div class="msg-time ${m.role==='user'?'msg-time-right':''}">${m.role==='user'?'👤 Cliente':'🤖 Naylan'} · ${fmtTime(m.timestamp)}</div>
  </div>`).join('');
  if(atBot)el.scrollTop=el.scrollHeight;
  document.getElementById('chat-count').textContent=msgs.length+' mensajes';
}

async function sendMsg(){
  if(!phone)return;
  if(handoffStatus==='WAITING_HUMAN')return; // bloqueado
  const inp=document.getElementById('msg-input');
  const txt=inp.value.trim();if(!txt)return;
  document.getElementById('send-btn').disabled=true;
  inp.value='';inp.style.height='auto';
  await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/mensaje',{
    method:'POST',
    headers:apiH(),
    body:JSON.stringify({texto:txt})
  });
  document.getElementById('send-btn').disabled=false;
  await loadChat();inp.focus();
}

async function tomarConv(){
  if(!phone)return;
  const r=await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/tomar',{
    method:'POST',
    headers:apiH(),
    body:'{}'
  });
  if(r.status===409){
    const data=await r.json();
    alert('Esta conversación ya fue tomada por '+( data.assigned_agent||'otro agente'));
    await refresh();return;
  }
  handoffStatus='HUMAN_ACTIVE';
  assignedAgent=agentName;
  updateStatusUI();await loadConvs();
}

async function devolverConv(){
  if(!phone)return;
  await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/devolver',{
    method:'POST',headers:apiH(),body:'{}'
  });
  handoffStatus='BOT_ACTIVE';assignedAgent=null;
  updateStatusUI();await loadConvs();
}

async function finalizarConv(){
  if(!phone)return;
  if(!confirm('¿Finalizar la atención? La conversación volverá a Naylan y quedará como resuelta.'))return;
  await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/finalizar',{
    method:'POST',headers:apiH(),body:'{}'
  });
  handoffStatus='BOT_ACTIVE';assignedAgent=null;
  updateStatusUI();await loadConvs();
}

function updateStatusUI(){
  const tomarBtn=document.getElementById('tomar-btn');
  const devolverBtn=document.getElementById('devolver-btn');
  const finalizarBtn=document.getElementById('finalizar-btn');
  const subtitle=document.getElementById('chat-subtitle');
  const inp=document.getElementById('msg-input');
  const sendBtn=document.getElementById('send-btn');
  const overrideWarn=document.getElementById('override-warning');

  // Reset
  tomarBtn.style.display='none';
  devolverBtn.style.display='none';
  finalizarBtn.style.display='none';
  overrideWarn.style.display='none';
  inp.disabled=false;

  if(handoffStatus==='BOT_ACTIVE'){
    subtitle.textContent='🤖 Naylan activa';
    inp.placeholder='Escribe un mensaje como Naylan...';
    tomarBtn.style.display='inline-block';
  } else if(handoffStatus==='WAITING_HUMAN'){
    subtitle.textContent='🟡 Esperando agente humano';
    inp.placeholder='Toma la conversación primero...';
    inp.disabled=true;
    sendBtn.disabled=true;
    tomarBtn.style.display='inline-block';
  } else if(handoffStatus==='HUMAN_ACTIVE'){
    const aa=assignedAgent||'Agente';
    subtitle.textContent='👤 Atención humana — '+aa;
    inp.placeholder='Responder como agente...';
    devolverBtn.style.display='inline-block';
    finalizarBtn.style.display='inline-block';
    // Mostrar warning si otro agente tiene la conv
    if(assignedAgent&&assignedAgent!==agentName&&agentName!=='Agente R8A'){
      overrideWarn.textContent='Atendida por '+assignedAgent;
      overrideWarn.style.display='inline-block';
    }
  } else if(handoffStatus==='RESOLVED'){
    subtitle.textContent='✅ Conversación resuelta';
    inp.placeholder='Conversación finalizada. Puedes tomar de nuevo si es necesario.';
    tomarBtn.style.display='inline-block';
    tomarBtn.textContent='👤 Reabrir conversación';
  }

  if(handoffStatus!=='WAITING_HUMAN'){
    sendBtn.disabled=false;
  }
}

async function refresh(){
  // Una sola llamada por ciclo — los datos se reutilizan para lista y chat
  const r=await fetch('/admin/api/conversaciones',{headers:apiHGet()});
  if(!r.ok){logout();return;}
  const data=await r.json();
  renderConvs(data);
  if(!phone)return;
  const conv=data.find(d=>d.telefono===phone);
  if(!conv)return;
  const newStatus=conv.handoff_status||'BOT_ACTIVE';
  const newAgent=conv.assigned_agent||null;
  const newPriority=conv.handoff_priority||'NORMAL';
  if(newStatus!==handoffStatus||newAgent!==assignedAgent){
    handoffStatus=newStatus;assignedAgent=newAgent;handoffPriority=newPriority;
    updateStatusUI();
  }
  // Recargar historial solo si llegó un mensaje nuevo
  if(conv.ultimo_mensaje!==lastMsgTs){
    lastMsgTs=conv.ultimo_mensaje;
    await loadChat();
  }
}

function fmtStatus(s,p){
  if(s==='WAITING_HUMAN'&&p==='CRITICAL')return'🔴 URGENTE';
  const m={
    'BOT_ACTIVE':'🤖 BOT',
    'WAITING_HUMAN':'🟡 ESPERA',
    'HUMAN_ACTIVE':'👤 HUMANO',
    'RESOLVED':'✅ Resuelto'
  };
  return m[s]||s;
}
function fmtTime(s){
  if(!s)return'';const d=new Date(s),n=new Date(),df=n-d;
  if(df<60000)return'ahora';if(df<3600000)return Math.floor(df/60000)+'m';
  if(df<86400000)return d.toLocaleTimeString('es',{hour:'2-digit',minute:'2-digit'});
  return d.toLocaleDateString('es',{day:'numeric',month:'short'});
}
function esc(t){return t.replace(/&/g,'&amp;').split('<').join('&lt;').split('>').join('&gt;').split('\\n').join('<br>');}

window.onload=()=>{
  const s=localStorage.getItem('ak');
  const n=localStorage.getItem('agentName');
  if(n)agentName=n;
  if(s){
    key=s;
    fetch('/admin/api/conversaciones',{headers:apiHGet()})
      .then(r=>{if(r.ok)showApp();else{key=null;localStorage.removeItem('ak');}})
      .catch(()=>{key=null;localStorage.removeItem('ak');});
  }
};
</script>
</body>
</html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    return HTMLResponse(_ADMIN_HTML)


@app.get("/admin/api/conversaciones")
async def admin_listar(agente: dict = Depends(_get_agente)):
    return await listar_conversaciones()


@app.get("/admin/api/conversaciones/{telefono}/historial")
async def admin_historial(telefono: str, agente: dict = Depends(_get_agente)):
    return await obtener_historial_completo(telefono)


@app.post("/admin/api/conversaciones/{telefono}/mensaje")
async def admin_enviar(
    telefono: str,
    body: MensajeAdmin,
    agente: dict = Depends(_get_agente),
):
    if proveedor is None:
        raise HTTPException(status_code=503, detail="Proveedor no inicializado")
    ok = await proveedor.enviar_mensaje(telefono, body.texto)
    if ok:
        await guardar_mensaje(telefono, "assistant", body.texto)
    return {"ok": ok}


@app.post("/admin/api/conversaciones/{telefono}/modo")
async def admin_modo(
    telefono: str,
    body: ModoPayload,
    agente: dict = Depends(_get_agente),
):
    if body.modo not in ("bot", "humano"):
        raise HTTPException(status_code=400, detail="modo debe ser 'bot' o 'humano'")
    await establecer_modo(telefono, body.modo)
    return {"ok": True, "modo": body.modo}


@app.post("/admin/api/conversaciones/{telefono}/tomar")
async def admin_tomar(
    telefono: str,
    agente: dict = Depends(_get_agente),
):
    """
    Agente toma la conversación → Naylan se pausa para este hilo.
    Usa claim atómico para prevenir que 2 agentes tomen la misma conversación.
    El nombre del agente se obtiene de la sesión autenticada.
    """
    agent_name = agente["nombre"]

    resultado = await atomic_claim_conversation(telefono, agent_name)
    if not resultado["success"]:
        # Obtener quién la tiene para informar
        registro = await obtener_registro_completo(telefono)
        aa = registro.assigned_agent if registro else "otro agente"
        raise HTTPException(
            status_code=409,
            detail={"error": "ya_tomada", "assigned_agent": aa}
        )

    logger.info(f"Agente '{agent_name}' tomó conversación {telefono} → HUMAN_ACTIVE")
    return {"ok": True, "handoff_status": "HUMAN_ACTIVE", "assigned_agent": agent_name}


@app.post("/admin/api/conversaciones/{telefono}/devolver")
async def admin_devolver(telefono: str, agente: dict = Depends(_get_agente)):
    """Agente devuelve la conversación a Naylan."""
    await establecer_handoff(
        telefono, modo="bot", handoff_status="BOT_ACTIVE", assigned_agent=None
    )
    logger.info(f"Conversación {telefono} devuelta a Naylan → BOT_ACTIVE")
    return {"ok": True, "handoff_status": "BOT_ACTIVE"}


@app.post("/admin/api/conversaciones/{telefono}/finalizar")
async def admin_finalizar(telefono: str, agente: dict = Depends(_get_agente)):
    """
    Finaliza la atención humana. Marca la conversación como RESOLVED,
    libera el agente asignado y registra resolved_at para métricas.
    El historial se preserva.
    """
    from datetime import datetime as _dt
    from sqlalchemy import select as _select
    from agent.memory import ConversacionModo, get_session

    async with get_session()() as session:
        result = await session.execute(
            _select(ConversacionModo).where(ConversacionModo.telefono == telefono)
        )
        registro = result.scalar_one_or_none()
        ahora = _dt.utcnow()
        if registro:
            registro.modo = "bot"
            registro.handoff_status = "RESOLVED"
            registro.assigned_agent = None
            registro.resolved_at = ahora
            registro.updated_at = ahora
            registro.notification_sent = False
            registro.notification_sent_at = None
        else:
            session.add(ConversacionModo(
                telefono=telefono,
                modo="bot",
                handoff_status="RESOLVED",
                resolved_at=ahora,
                updated_at=ahora,
            ))
        await session.commit()

    logger.info(f"Conversación {telefono} finalizada → RESOLVED")
    return {"ok": True, "handoff_status": "RESOLVED"}
