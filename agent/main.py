# agent/main.py — Servidor FastAPI + Webhook de WhatsApp para R8ATUR
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import (
    inicializar_db, guardar_mensaje, obtener_historial,
    obtener_modo, establecer_modo, listar_conversaciones, obtener_historial_completo,
)
from agent.providers import obtener_proveedor

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

# El proveedor se inicializa en lifespan para que Railway haya inyectado las vars
proveedor = None


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

            # Si un humano tomó el control, solo guardar — no responder
            modo = await obtener_modo(msg.telefono)
            if modo == "humano":
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                logger.info(f"Conversación {msg.telefono} en modo humano — Naylan silenciada")
                continue

            historial = await obtener_historial(msg.telefono)
            respuesta = await generar_respuesta(msg.texto, historial)

            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            logger.info(f"Respuesta a {msg.telefono}: {respuesta[:100]}...")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Admin Dashboard ──────────────────────────────────────────────────────────

class MensajeAdmin(BaseModel):
    texto: str


class ModoPayload(BaseModel):
    modo: str  # bot | humano


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
.modo-badge{font-size:.65rem;padding:.15rem .5rem;border-radius:10px;font-weight:700;text-transform:uppercase}
.modo-bot{background:#c6f6d5;color:#276749}
.modo-humano{background:#fed7d7;color:#9b2c2c}
#chat-panel{flex:1;display:flex;flex-direction:column;background:#e5ddd5}
#chat-header{background:white;padding:.75rem 1.25rem;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;justify-content:space-between}
#chat-phone{font-weight:600;color:#1a202c}
#chat-count{font-size:.8rem;color:#999}
#toggle-btn{padding:.5rem 1.1rem;border:none;border-radius:8px;cursor:pointer;font-size:.85rem;font-weight:600;transition:all .2s}
#toggle-btn.bot{background:#c6f6d5;color:#276749}
#toggle-btn.humano{background:#fed7d7;color:#9b2c2c}
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
          <div><div id="chat-phone"></div><div id="chat-count"></div></div>
          <button id="toggle-btn" onclick="toggleModo()"></button>
        </div>
        <div id="messages"></div>
        <div id="input-area">
          <textarea id="msg-input" placeholder="Escribe un mensaje como Naylan..." rows="1"
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
let key=null,phone=null,modo='bot',timer=null;
function login(){
  const pw=document.getElementById('pw').value;
  if(!pw)return;
  fetch('/admin/api/conversaciones',{headers:{'X-Admin-Key':pw}}).then(r=>{
    if(r.ok){key=pw;localStorage.setItem('ak',pw);showApp();}
    else{document.getElementById('login-error').style.display='block';}
  }).catch(()=>{document.getElementById('login-error').style.display='block';});
}
function logout(){localStorage.removeItem('ak');key=null;phone=null;clearInterval(timer);document.getElementById('app').style.display='none';document.getElementById('login').style.display='flex';}
function showApp(){document.getElementById('login').style.display='none';document.getElementById('app').style.display='flex';loadConvs();timer=setInterval(refresh,5000);}
async function loadConvs(){
  const r=await fetch('/admin/api/conversaciones',{headers:{'X-Admin-Key':key}});
  if(!r.ok){logout();return;}
  const data=await r.json();
  const c=document.getElementById('conv-items');
  if(!data.length){c.innerHTML='<p style="padding:1rem;color:#999;font-size:.85rem">Sin conversaciones aún</p>';return;}
  c.innerHTML=data.map(d=>`<div class="conv-item ${d.telefono===phone?'active':''}" onclick="selectConv('${d.telefono}','${d.modo}')">
    <div class="conv-phone">${fmtPhone(d.telefono)}</div>
    <div class="conv-meta"><span class="modo-badge modo-${d.modo}">${d.modo==='bot'?'🤖 Bot':'👤 Humano'}</span><span class="conv-time">${fmtTime(d.ultimo_mensaje)}</span></div>
  </div>`).join('');
}
async function selectConv(t,m){
  phone=t;modo=m;
  document.getElementById('empty-state').style.display='none';
  document.getElementById('chat-content').style.display='flex';
  document.getElementById('chat-phone').textContent=fmtPhone(t);
  updBtn(m);await loadChat();await loadConvs();
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
async function toggleModo(){
  if(!phone)return;
  const nuevo=modo==='bot'?'humano':'bot';
  await fetch('/admin/api/conversaciones/'+encodeURIComponent(phone)+'/modo',{
    method:'POST',headers:{'X-Admin-Key':key,'Content-Type':'application/json'},body:JSON.stringify({modo:nuevo})
  });
  modo=nuevo;updBtn(nuevo);await loadConvs();
}
function updBtn(m){const b=document.getElementById('toggle-btn');b.textContent=m==='bot'?'🤖 Naylan activa':'👤 Modo humano';b.className=m;}
async function refresh(){await loadConvs();if(phone)await loadChat();}
function fmtPhone(p){return p;}
function fmtTime(s){
  if(!s)return'';const d=new Date(s),n=new Date(),df=n-d;
  if(df<60000)return'ahora';if(df<3600000)return Math.floor(df/60000)+'m';
  if(df<86400000)return d.toLocaleTimeString('es',{hour:'2-digit',minute:'2-digit'});
  return d.toLocaleDateString('es',{day:'numeric',month:'short'});
}
function esc(t){return t.replace(/&/g,'&amp;').split('<').join('&lt;').split('>').join('&gt;').split('\\n').join('<br>');}
window.onload=()=>{const s=localStorage.getItem('ak');if(s){key=s;fetch('/admin/api/conversaciones',{headers:{'X-Admin-Key':key}}).then(r=>{if(r.ok)showApp();else{key=null;localStorage.removeItem('ak');}}).catch(()=>{key=null;localStorage.removeItem('ak');});}};
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
    logger.info(f"Modo de {telefono} cambiado a {body.modo}")
    return {"ok": True, "modo": body.modo}
