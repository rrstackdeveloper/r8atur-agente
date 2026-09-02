# agent/main.py — Servidor FastAPI + Webhook de WhatsApp para R8ATUR
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from agent.brain import generar_respuesta, extraer_escalado
from agent.memory import (
    inicializar_db, guardar_mensaje, obtener_historial,
    obtener_modo, establecer_modo, establecer_handoff,
    obtener_handoff_status, listar_conversaciones, obtener_historial_completo,
)
from agent.providers import obtener_proveedor
from agent.providers.base import ProveedorWhatsApp

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor: ProveedorWhatsApp | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global proveedor
    proveedor = obtener_proveedor()
    await inicializar_db()
    PORT = os.getenv("PORT", "8000")
    logger.info(f"Servidor Naylan (R8ATUR) en puerto {PORT}")
    logger.info(f"Proveedor: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="Naylan — Agente de R8ATUR",
    version="1.0.0",
    lifespan=lifespan
)


async def notificar_agentes(telefono: str, resumen: dict):
    """Envía notificación de handoff a los números de agentes configurados."""
    agent_phones_str = os.getenv("AGENT_PHONES", "").strip()
    if not agent_phones_str:
        logger.warning("AGENT_PHONES no configurado — handoff creado pero agentes no notificados")
        return

    base_url = os.getenv("BASE_URL", "").rstrip("/")
    link = f"{base_url}/admin?conv={telefono}" if base_url else "Revisar el dashboard de Naylan"

    servicio = resumen.get("servicio", "No especificado")
    motivo = resumen.get("motivo", "No especificado")
    prioridad_raw = resumen.get("prioridad", "normal")
    prioridad = {"alta": "🔴 Alta", "media": "🟡 Media", "normal": "🟢 Normal"}.get(prioridad_raw, prioridad_raw)
    resumen_texto = resumen.get("resumen", "Sin resumen")

    notif = (
        f"🔔 *Nueva solicitud de atención humana — R8A*\n\n"
        f"📱 WhatsApp: {telefono}\n"
        f"🛠 Servicio: {servicio}\n"
        f"📋 Motivo: {motivo}\n"
        f"⚡ Prioridad: {prioridad}\n\n"
        f"📝 Resumen:\n{resumen_texto}\n\n"
        f"🔗 Abrir conversación:\n{link}"
    )

    for agent_phone in agent_phones_str.split(","):
        agent_phone = agent_phone.strip()
        if agent_phone and proveedor:
            ok = await proveedor.enviar_mensaje(agent_phone, notif)
            logger.info(f"Notificación a agente {agent_phone}: {'ok' if ok else 'error'}")


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
                # Establecer estado WAITING_HUMAN y notificar agentes
                resumen_str = str(escalado)
                await establecer_handoff(
                    msg.telefono,
                    modo="bot",  # Naylan sigue respondiendo hasta que un agente tome
                    handoff_status="WAITING_HUMAN",
                    handoff_summary=resumen_str,
                )
                logger.info(f"Handoff creado para {msg.telefono}: {escalado}")
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


def _check_admin(x_admin_key: str | None) -> None:
    password = os.getenv("ADMIN_PASSWORD", "")
    if not password or x_admin_key != password:
        raise HTTPException(status_code=401, detail="No autorizado")


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Naylan Admin — R8ATUR</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;height:100vh;display:flex;flex-direction:column}
#login{display:flex;align-items:center;justify-content:center;height:100vh;background:#128C7E}
#login-box{background:white;padding:2rem;border-radius:12px;width:320px;box-shadow:0 4px 20px rgba(0,0,0,.2)}
#login-box h2{color:#128C7E;margin-bottom:.5rem;text-align:center}
#login-box p{text-align:center;color:#666;margin-bottom:1.5rem;font-size:.9rem}
#login-box input{width:100%;padding:.75rem;border:1px solid #ddd;border-radius:8px;margin-bottom:1rem;font-size:1rem;outline:none}
#login-box input:focus{border-color:#128C7E}
#login-box button{width:100%;padding:.75rem;background:#128C7E;color:white;border:none;border-radius:8px;font-size:1rem;cursor:pointer}
#login-box button:hover{background:#0e7065}
#login-error{color:#e53e3e;font-size:.85rem;text-align:center;margin-top:.5rem;display:none}
#app{display:none;height:100vh;flex-direction:column}
header{background:#128C7E;color:white;padding:.75rem 1.5rem;display:flex;align-items:center;justify-content:space-between}
header h1{font-size:1.1rem}
#logout-btn{background:rgba(255,255,255,.2);border:none;color:white;padding:.4rem .9rem;border-radius:6px;cursor:pointer;font-size:.85rem}
.content{display:flex;flex:1;overflow:hidden}
#conv-list{width:300px;min-width:300px;background:white;border-right:1px solid #e2e8f0;overflow-y:auto}
#conv-list h3{padding:1rem;font-size:.8rem;color:#666;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e2e8f0}
.conv-item{padding:.85rem 1rem;cursor:pointer;border-bottom:1px solid #f0f2f5;transition:background .15s}
.conv-item:hover{background:#f7f8fa}
.conv-item.active{background:#e6f3f2;border-left:3px solid #128C7E}
.conv-phone{font-weight:600;font-size:.9rem;color:#1a202c}
.conv-meta{display:flex;align-items:center;gap:.5rem;margin-top:.25rem}
.conv-time{font-size:.72rem;color:#999}
.badge{font-size:.65rem;padding:.15rem .5rem;border-radius:10px;font-weight:700;text-transform:uppercase}
.badge-BOT_ACTIVE{background:#c6f6d5;color:#276749}
.badge-WAITING_HUMAN{background:#fef3c7;color:#92400e}
.badge-HUMAN_ACTIVE{background:#fed7d7;color:#9b2c2c}
.badge-RESOLVED{background:#e2e8f0;color:#4a5568}
#chat-panel{flex:1;display:flex;flex-direction:column;background:#e5ddd5}
#chat-header{background:white;padding:.75rem 1.25rem;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;justify-content:space-between}
#chat-header-left{flex:1}
#chat-phone{font-weight:600;color:#1a202c}
#chat-count{font-size:.8rem;color:#999}
#chat-actions{display:flex;gap:.5rem;align-items:center}
#status-badge{font-size:.7rem;padding:.2rem .6rem;border-radius:10px;font-weight:700;text-transform:uppercase}
.action-btn{padding:.45rem 1rem;border:none;border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:600}
#tomar-btn{background:#d69e2e;color:white;display:none}
#tomar-btn:hover{background:#b7791f}
#devolver-btn{background:#38a169;color:white;display:none}
#devolver-btn:hover{background:#276749}
#messages{flex:1;overflow-y:auto;padding:1rem;display:flex;flex-direction:column;gap:.4rem}
.msg-wrap{display:flex;flex-direction:column}
.msg{max-width:70%;padding:.6rem .9rem;border-radius:10px;font-size:.88rem;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.msg-user{align-self:flex-end;background:#dcf8c6;border-bottom-right-radius:2px}
.msg-assistant{align-self:flex-start;background:white;border-bottom-left-radius:2px}
.msg-time{font-size:.68rem;color:#999;margin-top:.2rem}
.msg-time-right{text-align:right}
#input-area{background:white;padding:.75rem 1rem;display:flex;gap:.75rem;align-items:flex-end;border-top:1px solid #e2e8f0}
#msg-input{flex:1;padding:.6rem .9rem;border:1px solid #e2e8f0;border-radius:20px;font-size:.9rem;resize:none;min-height:42px;max-height:120px;outline:none;font-family:inherit}
#msg-input:focus{border-color:#128C7E}
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
    <input type="password" id="pw" placeholder="Contraseña" onkeydown="if(event.key==='Enter')login()">
    <button onclick="login()">Entrar</button>
    <p id="login-error">Contraseña incorrecta</p>
  </div>
</div>
<div id="app">
  <header>
    <h1>Naylan Admin — R8ATUR</h1>
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
            <div id="chat-count"></div>
          </div>
          <div id="chat-actions">
            <span id="status-badge"></span>
            <button id="tomar-btn" class="action-btn" onclick="tomarConv()">👤 Tomar conversación</button>
            <button id="devolver-btn" class="action-btn" onclick="devolverConv()">🤖 Devolver a Naylan</button>
          </div>
        </div>
        <div id="messages"></div>
        <div id="input-area">
          <textarea id="msg-input" placeholder="Escribe un mensaje como agente..." rows="1"
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
let key=null,phone=null,handoffStatus='BOT_ACTIVE',timer=null;

function login(){
  const pw=document.getElementById('pw').value;
  if(!pw)return;
  fetch('/admin/api/conversaciones',{headers:{'X-Admin-Key':pw}}).then(r=>{
    if(r.ok){key=pw;localStorage.setItem('ak',pw);showApp();}
    else{document.getElementById('login-error').style.display='block';}
  }).catch(()=>{document.getElementById('login-error').style.display='block';});
}
function logout(){localStorage.removeItem('ak');key=null;phone=null;clearInterval(timer);document.getElementById('app').style.display='none';document.getElementById('login').style.display='flex';}
function showApp(){
  document.getElementById('login').style.display='none';
  document.getElementById('app').style.display='flex';
  loadConvs();
  timer=setInterval(refresh,5000);
  // Auto-seleccionar conversación desde ?conv= en la URL
  const p=new URLSearchParams(window.location.search).get('conv');
  if(p){setTimeout(()=>selectConvByPhone(p),800);}
}

async function loadConvs(){
  const r=await fetch('/admin/api/conversaciones',{headers:{'X-Admin-Key':key}});
  if(!r.ok){logout();return;}
  const data=await r.json();
  const c=document.getElementById('conv-items');
  if(!data.length){c.innerHTML='<p style="padding:1rem;color:#999;font-size:.85rem">Sin conversaciones aún</p>';return;}
  c.innerHTML=data.map(d=>`<div class="conv-item ${d.telefono===phone?'active':''}" onclick="selectConv('${d.telefono}','${d.handoff_status||'BOT_ACTIVE'}')">
    <div class="conv-phone">${d.telefono}</div>
    <div class="conv-meta">
      <span class="badge badge-${d.handoff_status||'BOT_ACTIVE'}">${fmtStatus(d.handoff_status||'BOT_ACTIVE')}</span>
      <span class="conv-time">${fmtTime(d.ultimo_mensaje)}</span>
    </div>
  </div>`).join('');
}

async function selectConv(t,hs){
  phone=t;handoffStatus=hs||'BOT_ACTIVE';
  document.getElementById('empty-state').style.display='none';
  document.getElementById('chat-content').style.display='flex';
  document.getElementById('chat-phone').textContent=t;
  updateStatusUI();
  await loadChat();
  await loadConvs();
}

async function selectConvByPhone(p){
  const r=await fetch('/admin/api/conversaciones',{headers:{'X-Admin-Key':key}});
  if(!r.ok)return;
  const data=await r.json();
  const conv=data.find(d=>d.telefono===p||d.telefono===p.replace('+',''));
  if(conv)await selectConv(conv.telefono,conv.handoff_status||'BOT_ACTIVE');
}

async function loadChat(){
  if(!phone)return;
  const r=await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/historial',{headers:{'X-Admin-Key':key}});
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
  const inp=document.getElementById('msg-input');
  const txt=inp.value.trim();if(!txt)return;
  document.getElementById('send-btn').disabled=true;
  inp.value='';inp.style.height='auto';
  await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/mensaje',{
    method:'POST',headers:{'X-Admin-Key':key,'Content-Type':'application/json'},body:JSON.stringify({texto:txt})
  });
  document.getElementById('send-btn').disabled=false;
  await loadChat();inp.focus();
}

async function tomarConv(){
  if(!phone)return;
  await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/tomar',{
    method:'POST',headers:{'X-Admin-Key':key,'Content-Type':'application/json'},body:'{}'
  });
  handoffStatus='HUMAN_ACTIVE';
  updateStatusUI();await loadConvs();
}

async function devolverConv(){
  if(!phone)return;
  await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/devolver',{
    method:'POST',headers:{'X-Admin-Key':key,'Content-Type':'application/json'},body:'{}'
  });
  handoffStatus='BOT_ACTIVE';
  updateStatusUI();await loadConvs();
}

function updateStatusUI(){
  const badge=document.getElementById('status-badge');
  const tomarBtn=document.getElementById('tomar-btn');
  const devolverBtn=document.getElementById('devolver-btn');
  badge.textContent=fmtStatus(handoffStatus);
  badge.className='badge badge-'+handoffStatus;
  tomarBtn.style.display=handoffStatus==='WAITING_HUMAN'?'inline-block':'none';
  devolverBtn.style.display=handoffStatus==='HUMAN_ACTIVE'?'inline-block':'none';
}

async function refresh(){
  if(!phone){await loadConvs();return;}
  // Actualizar estado desde servidor
  const r=await fetch('/admin/api/conversaciones',{headers:{'X-Admin-Key':key}});
  if(!r.ok){logout();return;}
  const data=await r.json();
  const conv=data.find(d=>d.telefono===phone);
  if(conv&&conv.handoff_status!==handoffStatus){
    handoffStatus=conv.handoff_status||'BOT_ACTIVE';
    updateStatusUI();
  }
  const c=document.getElementById('conv-items');
  if(c)c.innerHTML=data.map(d=>`<div class="conv-item ${d.telefono===phone?'active':''}" onclick="selectConv('${d.telefono}','${d.handoff_status||'BOT_ACTIVE'}')">
    <div class="conv-phone">${d.telefono}</div>
    <div class="conv-meta">
      <span class="badge badge-${d.handoff_status||'BOT_ACTIVE'}">${fmtStatus(d.handoff_status||'BOT_ACTIVE')}</span>
      <span class="conv-time">${fmtTime(d.ultimo_mensaje)}</span>
    </div>
  </div>`).join('');
  await loadChat();
}

function fmtStatus(s){
  const m={'BOT_ACTIVE':'🤖 Bot','WAITING_HUMAN':'⏳ Esperando','HUMAN_ACTIVE':'👤 Humano','RESOLVED':'✅ Resuelto'};
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
  if(s){
    key=s;
    fetch('/admin/api/conversaciones',{headers:{'X-Admin-Key':key}})
      .then(r=>{if(r.ok)showApp();else{key=null;localStorage.removeItem('ak');}})
      .catch(()=>{key=null;localStorage.removeItem('ak');});
  }
};
</script>
</body>
</html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    if not os.getenv("ADMIN_PASSWORD"):
        raise HTTPException(status_code=404)
    return HTMLResponse(_ADMIN_HTML)


@app.get("/admin/api/conversaciones")
async def admin_listar(x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    return await listar_conversaciones()


@app.get("/admin/api/conversaciones/{telefono}/historial")
async def admin_historial(telefono: str, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    return await obtener_historial_completo(telefono)


@app.post("/admin/api/conversaciones/{telefono}/mensaje")
async def admin_enviar(telefono: str, body: MensajeAdmin, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    if proveedor is None:
        raise HTTPException(status_code=503, detail="Proveedor no inicializado")
    ok = await proveedor.enviar_mensaje(telefono, body.texto)
    if ok:
        await guardar_mensaje(telefono, "assistant", body.texto)
    return {"ok": ok}


@app.post("/admin/api/conversaciones/{telefono}/modo")
async def admin_modo(telefono: str, body: ModoPayload, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    if body.modo not in ("bot", "humano"):
        raise HTTPException(status_code=400, detail="modo debe ser 'bot' o 'humano'")
    await establecer_modo(telefono, body.modo)
    return {"ok": True, "modo": body.modo}


@app.post("/admin/api/conversaciones/{telefono}/tomar")
async def admin_tomar(telefono: str, x_admin_key: str | None = Header(default=None)):
    """Agente toma la conversación → Naylan se pausa para este hilo."""
    _check_admin(x_admin_key)
    await establecer_handoff(telefono, modo="humano", handoff_status="HUMAN_ACTIVE")
    logger.info(f"Agente tomó conversación {telefono} → HUMAN_ACTIVE")
    return {"ok": True, "handoff_status": "HUMAN_ACTIVE"}


@app.post("/admin/api/conversaciones/{telefono}/devolver")
async def admin_devolver(telefono: str, x_admin_key: str | None = Header(default=None)):
    """Agente devuelve la conversación a Naylan."""
    _check_admin(x_admin_key)
    await establecer_handoff(telefono, modo="bot", handoff_status="BOT_ACTIVE")
    logger.info(f"Conversación {telefono} devuelta a Naylan → BOT_ACTIVE")
    return {"ok": True, "handoff_status": "BOT_ACTIVE"}
