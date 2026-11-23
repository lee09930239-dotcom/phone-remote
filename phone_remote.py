# -*- coding: utf-8 -*-
"""手機遙控筆電 PhoneRemote v1.13 (2026-11-24)
筆電端：擷取畫面 -> JPEG 串流到手機瀏覽器；手機點擊/打字/媒體鍵 -> 注入 Windows。
只聽 127.0.0.1，外網靠 Tailscale serve / funnel 轉進來（自動 HTTPS）。"""
import asyncio, ctypes, hashlib, hmac, io, json, os, secrets, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web

VERSION = "1.13"
PORT = 8777
UPDATE_URL = "https://raw.githubusercontent.com/lee09930239-dotcom/phone-remote/HEAD/phone_remote.py"
BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "phone_remote_config.json")

def load_cfg():
    cfg = {}
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8") as f: cfg = json.load(f)
        except Exception: cfg = {}
    changed = False
    if not str(cfg.get("pin", "")).strip():
        cfg["pin"] = "".join(secrets.choice("0123456789") for _ in range(8)); changed = True
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_hex(16); changed = True
    # PIN 改了就讓舊登入失效
    pin_hash = hashlib.sha256(str(cfg["pin"]).encode()).hexdigest()
    if cfg.get("pin_hash") != pin_hash:
        cfg["pin_hash"] = pin_hash; cfg["secret"] = secrets.token_hex(16); changed = True
    if changed:
        with open(CFG_PATH, "w", encoding="utf-8") as f: json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg

CFG = load_cfg()
PIN = str(CFG["pin"]).strip()
TOKEN = hmac.new(CFG["secret"].encode(), b"phone-remote-ok", "sha256").hexdigest()
FAIL = {"n": 0, "until": 0.0, "locks": 0}   # 連續被鎖幾次（每次鎖更久）

# ---------------- Windows 輸入 ----------------
IS_WIN = os.name == "nt"
if IS_WIN:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try: user32.SetProcessDPIAware()
        except Exception: pass

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

VK = {"enter": 0x0D, "backspace": 0x08, "tab": 0x09, "esc": 0x1B, "space": 0x20, "left": 0x25, "up": 0x26,
      "right": 0x27, "down": 0x28, "pgup": 0x21, "pgdn": 0x22, "end": 0x23, "home": 0x24, "del": 0x2E,
      "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "win": 0x5B, "volmute": 0xAD, "voldown": 0xAE, "volup": 0xAF,
      "next": 0xB0, "prev": 0xB1, "stop": 0xB2, "playpause": 0xB3}
for i in range(1, 13): VK["f%d" % i] = 0x6F + i
EXT = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E, 0x5B, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}

def _vk(name):
    name = name.strip().lower()
    if name in VK: return VK[name]
    if len(name) == 1 and name.isalnum(): return ord(name.upper())
    return None

def key_combo(combo):
    """例：'alt+tab'、'win+d'、'f5'、'volup'"""
    if not IS_WIN: return
    vks = [v for v in (_vk(p) for p in combo.split("+")) if v]
    seq = [(v, 0) for v in vks] + [(v, 2) for v in reversed(vks)]
    arr = (INPUT * len(seq))()
    for i, (v, up) in enumerate(seq):
        arr[i].type = 1
        arr[i].u.ki = KEYBDINPUT(v, user32.MapVirtualKeyW(v, 0), (1 if v in EXT else 0) | up, 0, 0)
    n = user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
    print("按鍵 %-10s 送出 %d/%d%s" % (combo, n, len(seq), "" if n == len(seq) else "  ← 被擋（錯誤碼 %d）" % ctypes.GetLastError()))

def type_text(s):
    if not IS_WIN or not s: return
    s = s.replace("\r\n", "\n")
    for ch in s:
        if ch == "\n": key_combo("enter"); continue
        b = ch.encode("utf-16-le")
        for i in range(0, len(b), 2):
            code = int.from_bytes(b[i:i + 2], "little")
            arr = (INPUT * 2)()
            for j, fl in enumerate((4, 4 | 2)):  # KEYEVENTF_UNICODE, +KEYUP
                arr[j].type = 1
                arr[j].u.ki = KEYBDINPUT(0, code, fl, 0, 0)
            user32.SendInput(2, arr, ctypes.sizeof(INPUT))

MON = {"left": 0, "top": 0, "width": 1920, "height": 1080}

def _to_px(x, y):
    return int(MON["left"] + max(0.0, min(1.0, float(x))) * (MON["width"] - 1)), \
           int(MON["top"] + max(0.0, min(1.0, float(y))) * (MON["height"] - 1))

def _send_mouse(flags, dx=0, dy=0, data=0):
    arr = (INPUT * 1)()
    arr[0].type = 0
    arr[0].u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    user32.SendInput(1, arr, ctypes.sizeof(INPUT))

def _move_to(px, py):
    # 用 SendInput 真的「移動」滑鼠（Win11 工作列等 XAML 介面需要收到移動事件才會接受點擊）
    vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    ax = int((px - vx) * 65535 / max(1, vw - 1)); ay = int((py - vy) * 65535 / max(1, vh - 1))
    _send_mouse(0x0001 | 0x8000 | 0x4000, ax, ay)  # MOVE | ABSOLUTE | VIRTUALDESK
    user32.SetCursorPos(px, py)

def mouse(kind, x=None, y=None, b="left", d=0):
    if not IS_WIN: return
    if x is not None and y is not None:
        _move_to(*_to_px(x, y))
        if kind in ("click", "dbl", "down"): time.sleep(0.03)
    down, up = (0x02, 0x04) if b == "left" else (0x08, 0x10)
    if kind == "click": _send_mouse(down); time.sleep(0.02); _send_mouse(up)
    elif kind == "dbl":
        for _ in range(2): _send_mouse(down); _send_mouse(up); time.sleep(0.05)
    elif kind == "down": _send_mouse(down)
    elif kind == "up": _send_mouse(up)
    elif kind == "scroll": _send_mouse(0x0800, data=int(d) * 120)

def caret_active():
    """點擊後檢查：游標是不是落在可以打字的地方（有閃爍的文字游標）"""
    if not IS_WIN: return False
    time.sleep(0.3)
    class GTI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]
    g = GTI(); g.cbSize = ctypes.sizeof(GTI)
    if not user32.GetGUIThreadInfo(0, ctypes.byref(g)): return False
    if g.hwndCaret: return True
    if g.hwndFocus:
        buf = ctypes.create_unicode_buffer(64); user32.GetClassNameW(g.hwndFocus, buf, 64)
        return buf.value.lower().startswith(("edit", "richedit", "scintilla"))
    return False

# ---------------- 截圖（固定一條執行緒，mss 不跨執行緒）----------------
POOL = ThreadPoolExecutor(max_workers=1)
_tl = threading.local()

def grab(quality, max_w):
    import mss
    from PIL import Image, ImageDraw
    if not hasattr(_tl, "sct"): _tl.sct = (getattr(mss, "MSS", None) or mss.mss)()
    m = _tl.sct.monitors[1]
    MON.update(left=m["left"], top=m["top"], width=m["width"], height=m["height"])
    shot = _tl.sct.grab(m)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if IS_WIN:  # 畫出滑鼠位置（截圖本身不含游標）
        pt = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(pt))
        cx, cy = pt.x - m["left"], pt.y - m["top"]
        r = max(6, m["width"] // 200)
        ImageDraw.Draw(img).ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 40, 40), width=3)
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), Image.BILINEAR)
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()

# ---------------- 網頁 ----------------
def authed(req): return hmac.compare_digest(req.cookies.get("pr_tok", ""), TOKEN)

async def index(req):
    return web.Response(text=APP_HTML if authed(req) else LOGIN_HTML.replace("{{MSG}}", ""), content_type="text/html")

async def login(req):
    # 先讀完表單，之後「檢查鎖定→比對→記錄」中間沒有 await，同時送一大堆猜測也會被同一個鎖擋下
    try: pin = str((await req.post()).get("pin", "")).strip()[:64]
    except Exception: pin = ""
    now = time.time()
    if now < FAIL["until"]:
        return web.Response(text=LOGIN_HTML.replace("{{MSG}}", "錯誤太多次，請 %d 分鐘後再試" % (int(FAIL["until"] - now) // 60 + 1)), content_type="text/html")
    if pin and hmac.compare_digest(pin.encode(), PIN.encode()):
        FAIL["n"] = 0; FAIL["locks"] = 0
        ua = req.headers.get("User-Agent", "")[:120]
        print("✅ 有裝置登入成功：%s  %s" % (time.strftime("%m/%d %H:%M"), ua))
        if CFG.get("notify", "outlook") == "outlook" and CFG.get("login_notify", True):
            threading.Thread(target=notify_login, args=(ua,), daemon=True).start()
        r = web.HTTPFound("/")
        r.set_cookie("pr_tok", TOKEN, max_age=30 * 86400, httponly=True, samesite="Strict", secure=req.headers.get("X-Forwarded-Proto") == "https")
        raise r
    FAIL["n"] += 1
    print("⚠️ PIN 錯誤（第 %d 次）" % FAIL["n"])
    if FAIL["n"] >= 5:
        FAIL["n"] = 0; FAIL["locks"] += 1
        mins = min(60, 5 * 2 ** (FAIL["locks"] - 1))   # 5 → 10 → 20 → 40 → 60 分
        FAIL["until"] = now + mins * 60
        print("🔒 PIN 錯太多次，鎖定 %d 分鐘。不是你自己打錯的話，建議關掉黑色視窗（網址會換掉）" % mins)
    await asyncio.sleep(1)
    return web.Response(text=LOGIN_HTML.replace("{{MSG}}", "PIN 錯誤"), content_type="text/html")

async def manifest(req):
    return web.json_response({"name": "手機遙控", "short_name": "遙控", "start_url": "/", "display": "fullscreen",
                              "orientation": "landscape", "background_color": "#000000", "theme_color": "#000000"})

async def logout(req):
    r = web.HTTPFound("/"); r.del_cookie("pr_tok"); raise r

def logout_all():
    """換一組 secret：所有手機的登入都失效（PIN 不變），伺服器重開後生效"""
    CFG["secret"] = secrets.token_hex(16)
    with open(CFG_PATH, "w", encoding="utf-8") as f: json.dump(CFG, f, ensure_ascii=False, indent=2)

async def ws_handler(req):
    if not authed(req): raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(req)
    loop = asyncio.get_running_loop()
    last_bat = 0.0
    async for msg in ws:
        if msg.type != web.WSMsgType.TEXT: continue
        try:
            m = json.loads(msg.data); t = m.get("t")
            if t == "frame":
                if time.time() - last_bat > 15:
                    last_bat = time.time(); pct, ac = battery()
                    if pct is not None: await ws.send_str(json.dumps({"t": "bat", "p": pct, "ac": ac}))
                q = max(20, min(90, int(m.get("q", 60)))); w = max(480, min(2560, int(m.get("w", 1280))))
                try: data = await loop.run_in_executor(POOL, grab, q, w)
                except Exception as e:
                    await ws.send_str(json.dumps({"t": "err", "msg": "截圖失敗（螢幕鎖定或休眠？）%s" % e})); await asyncio.sleep(1); continue
                await ws.send_bytes(data)
            elif t in ("click", "dbl", "down", "move", "up"):
                mouse(t, m.get("x"), m.get("y"), m.get("b", "left"))
                if t == "click" and m.get("b", "left") == "left":
                    async def _chk():
                        if await loop.run_in_executor(None, caret_active) and not ws.closed: await ws.send_str('{"t":"caret"}')
                    asyncio.ensure_future(_chk())
            elif t == "scroll": mouse("scroll", d=m.get("d", 0))
            elif t == "key": key_combo(str(m.get("k", "")))
            elif t == "ping":
                print("收到按鈕測試訊號 ✔"); await ws.send_str(json.dumps({"t": "ver", "v": VERSION}))
            elif t == "update":
                try:
                    v = await loop.run_in_executor(None, do_update)
                    print("手機要求更新 → 已下載 v%s，重新啟動" % v)
                    await ws.send_str(json.dumps({"t": "upd", "ok": True, "v": v}))
                    loop.call_later(1.0, os._exit, 3)
                except Exception as e:
                    await ws.send_str(json.dumps({"t": "upd", "ok": False, "msg": str(e)[:200]}))
            elif t == "logoutall":
                logout_all(); print("🔒 已讓所有裝置登出，重新啟動伺服器")
                await ws.send_str(json.dumps({"t": "bye"})); loop.call_later(0.5, os._exit, 3)
            elif t == "text": type_text(str(m.get("s", ""))[:2000])
        except Exception as e:
            print("指令錯誤:", e)
    return ws

LOGIN_HTML = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="apple-mobile-web-app-capable" content="yes"><link rel="manifest" href="/manifest.json"><title>手機遙控</title>
<style>body{background:#111;color:#eee;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{display:flex;flex-direction:column;gap:12px;width:240px}input,button{font-size:22px;padding:10px;border-radius:8px;border:0}
button{background:#2b7cff;color:#fff}p{color:#f66;min-height:1em;margin:0}</style></head><body>
<form method="post" action="/login"><h2>🔒 輸入 PIN</h2><input name="pin" type="password" inputmode="numeric" autofocus>
<button>登入</button><p>{{MSG}}</p></form></body></html>"""

APP_HTML = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="theme-color" content="#000">
<title>手機遙控</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:#000;color:#eee;font-family:sans-serif;overflow:hidden;overscroll-behavior:none}
#app{position:fixed;left:0;top:0;width:100vw;height:100dvh;display:flex}
@media (orientation:portrait){#app{width:100dvh;height:100vw;transform:rotate(90deg) translateY(-100%);transform-origin:top left}}
#wrap{flex:1;min-width:0;height:100%;overflow:hidden;display:flex;align-items:center;justify-content:center}
#wrap.zoom{overflow:auto;display:block}
#scr{max-width:100%;max-height:100%;touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;display:block}
#wrap.zoom #scr{max-width:none;max-height:none;width:220%;touch-action:pan-x pan-y}
#bar{width:58px;height:100%;display:flex;flex-direction:column;justify-content:space-evenly;align-items:center;background:#111;padding:4px 0}
#bar button,#more button{font-size:22px;width:50px;height:44px;border:0;border-radius:10px;background:#2a2a2a;color:#fff;padding:0}
#more{display:none;position:absolute;right:62px;top:50%;transform:translateY(-50%);background:#1b1b1bee;padding:8px;border-radius:12px;
 grid-template-columns:repeat(4,auto);gap:6px}
#more.show{display:grid}#more button{width:auto;min-width:60px;font-size:16px;padding:0 8px}
#more button.on,#bar button.on{background:#2b7cff}#bar #lb,#bar #rb{font-size:15px}
.tap{position:fixed;width:36px;height:36px;margin:-18px 0 0 -18px;border-radius:50%;border:3px solid #ff4;pointer-events:none;z-index:7;animation:tp .4s ease-out forwards}
.tap.r{border-color:#f4f}@keyframes tp{from{transform:scale(.4);opacity:1}to{transform:scale(1.3);opacity:0}}
#st{position:absolute;top:4px;left:6px;font-size:12px;color:#8f8;background:#0008;padding:2px 6px;border-radius:4px;pointer-events:none}
#kb{display:none;position:fixed;left:0;right:0;top:0;height:52px;background:#222;padding:8px;gap:6px;z-index:9}
body.typing #app{transform:none;width:100vw;top:52px;height:calc(var(--vh,100dvh) - 52px)}
body.typing #bar,body.typing #more{display:none}
#hint{display:none;position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:8;font-size:20px;padding:14px 22px;border:0;border-radius:30px;background:#2b7cff;color:#fff;box-shadow:0 4px 16px #0008}
body.typing #hint{display:none!important}
#kb.show{display:flex}#kb input{flex:1;font-size:18px;padding:8px;border-radius:6px;border:0;min-width:0}
#kb button{font-size:16px;padding:8px 10px;border:0;border-radius:6px;background:#2b7cff;color:#fff}
</style></head><body>
<div id="app">
 <div id="wrap"><img id="scr" alt=""></div><div id="st">連線中…</div>
 <div id="bar">
  <button id="kbBtn">⌨️</button><button data-k="playpause">⏯</button><button data-k="voldown">🔉</button>
  <button data-k="volup">🔊</button><button id="lb" class="on">左鍵</button><button id="rb">右鍵</button><button id="mo">⋯</button>
 </div>
 <div id="more">
  <button data-k="f5">F5 放映</button><button data-k="esc">Esc</button><button data-k="f">全螢幕f</button><button data-k="volmute">🔇</button>
  <button data-k="prev">⏮</button><button data-k="next">⏭</button><button data-k="space">空白</button><button data-k="alt+tab">切換</button>
  <button data-k="left">◀</button><button data-k="right">▶</button><button id="zm">🔍放大</button><button id="ql">畫質:中</button><button id="out">登出</button><button id="up" style="grid-column:span 2">⟳ 更新到最新版</button><button id="lall" style="grid-column:span 2">🔒 所有裝置登出</button>
 </div>
</div>
<div id="kb"><input id="txt" placeholder="直接打字，會即時送到電腦" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" enterkeyhint="send"><button data-k="enter">Enter</button><button data-k="backspace">⌫</button><button id="kbx">完成</button></div>
<button id="hint">⌨️ 點這裡打字</button>
<script>
const $=id=>document.getElementById(id), img=$('scr'), wrap=$('wrap'), st=$('st');
const QL=[['低',40,960],['中',60,1280],['高',80,1920]]; let batTxt='', qi=1, ws, lastUrl, rightNext=false, zoom=false, frames=0;
const rot=()=>matchMedia('(orientation:portrait)').matches && !document.body.classList.contains('typing');
function send(o){ if(ws&&ws.readyState===1) ws.send(JSON.stringify(o)); }
function req(){ send({t:'frame',q:QL[qi][1],w:QL[qi][2]}); }
function connect(){
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws'); ws.binaryType='blob';
  ws.onopen=()=>{ let u=null; try{u=sessionStorage.getItem('upd')}catch(_){} if(u){ try{sessionStorage.removeItem('upd')}catch(_){} location.reload(); return; } st.textContent='已連線'; send({t:'ping'}); req();};
  ws.onclose=()=>{st.textContent='斷線，重新連線中…'; setTimeout(connect,2000);};
  ws.onmessage=e=>{
    if(typeof e.data==='string'){const m=JSON.parse(e.data); if(m.t==='err'){st.textContent=m.msg; req();} if(m.t==='caret'){ const h=$('hint'); h.style.display='block'; clearTimeout(h._t); h._t=setTimeout(()=>h.style.display='',4000); } if(m.t==='ver'){ $('up').textContent='⟳ 更新到最新版（目前 v'+m.v+'）'; } if(m.t==='upd'){ if(m.ok){ st.textContent='已下載 v'+m.v+'，重新啟動中…'; try{sessionStorage.setItem('upd','1')}catch(_){} } else { $('up').textContent='更新失敗：'+m.msg; } } if(m.t==='bat'){batTxt=(m.ac?'🔌':'🔋')+m.p+'%'+(!m.ac&&m.p<=20?' 電量低':'');} return;}
    const u=URL.createObjectURL(e.data); img.onload=()=>{ if(lastUrl) URL.revokeObjectURL(lastUrl); lastUrl=u; frames++; setTimeout(req,30); }; img.src=u;
  };
}
setInterval(()=>{ if(ws&&ws.readyState===1) st.textContent=batTxt+'  '+frames+' fps'; frames=0; },1000);
// 直拿手機時畫面轉 90 度，座標也要跟著轉
function pos(t){const r=img.getBoundingClientRect();
  return rot()?{x:(t.clientY-r.top)/r.height, y:(r.right-t.clientX)/r.width}:{x:(t.clientX-r.left)/r.width, y:(t.clientY-r.top)/r.height};}
const along=t=>rot()?-t.clientX:t.clientY;   // 畫面上的「上下」方向
let lastTap=null, s=null, lp=null, drag=false, lastMove=0, scrollY=null;
img.addEventListener('touchstart',e=>{
  if(e.touches.length===2){ clearTimeout(lp); s=null; scrollY=(along(e.touches[0])+along(e.touches[1]))/2; if(!zoom) e.preventDefault(); return; }
  const t=e.touches[0]; s={x:t.clientX,y:t.clientY,p:pos(t),long:false}; drag=false;
  if(!zoom) e.preventDefault();
},{passive:false});
img.addEventListener('touchmove',e=>{
  if(e.touches.length===2 && scrollY!==null && !zoom){ e.preventDefault();
    const y=(along(e.touches[0])+along(e.touches[1]))/2, dy=y-scrollY;
    if(Math.abs(dy)>25){ send({t:'scroll',d:dy>0?1:-1}); scrollY=y; } return; }
  if(!s) return; const t=e.touches[0];
  if(Math.hypot(t.clientX-s.x,t.clientY-s.y)>22){ clearTimeout(lp);
    if(zoom){ s=null; return; }
    if(!drag){ drag=true; send({t:'down',...s.p}); } }
  if(drag){ e.preventDefault(); const n=Date.now(); if(n-lastMove>40){ send({t:'move',...pos(t)}); lastMove=n; } }
},{passive:false});
img.addEventListener('touchend',e=>{
  clearTimeout(lp); if(e.touches.length===0) scrollY=null;
  if(!s) return; const ch=e.changedTouches[0];
  if(drag){ send({t:'up',...pos(ch)}); }
  else {
    const now=Date.now(), dt=lastTap?now-lastTap.t:1e9, dd=lastTap?Math.hypot(s.x-lastTap.x,s.y-lastTap.y):1e9;
    if(!rightNext && dt<500 && dd<40){ send({t:'dbl',...lastTap.p}); lastTap=null; ripple(s.x,s.y,false); ripple(s.x,s.y,false); }  // 連點兩下＝雙擊（開檔案）
    else { send({t:'click',...s.p,b:rightNext?'right':'left'}); ripple(s.x,s.y,rightNext); lastTap=rightNext?null:{t:now,x:s.x,y:s.y,p:s.p}; if(rightNext) setBtn(false); }
  }
  s=null; drag=false; if(!zoom) e.preventDefault();
},{passive:false});
img.addEventListener('click',e=>{ if(!('ontouchstart' in window)) send({t:'click',...pos(e),b:'left'}); });
function flash(b){ b.style.background='#2b7cff'; setTimeout(()=>b.style.background='',200); }
document.querySelectorAll('[data-k]').forEach(b=>b.onclick=()=>{ send({t:'key',k:b.dataset.k}); flash(b); });
$('mo').onclick=()=>$('more').classList.toggle('show');
// ---- 打字模式：畫面改成直向放上面，iOS 鍵盤在下面；打的字即時送出 ----
const txt=$('txt'); let composing=false;
function fitVV(){ const h=(window.visualViewport?visualViewport.height:innerHeight); document.documentElement.style.setProperty('--vh',h+'px'); }
if(window.visualViewport){ visualViewport.addEventListener('resize',fitVV); visualViewport.addEventListener('scroll',()=>scrollTo(0,0)); }
function openKb(){ $('hint').style.display=''; document.body.classList.add('typing'); $('kb').classList.add('show'); txt.value=''; txt.focus(); fitVV(); }
function closeKb(){ document.body.classList.remove('typing'); $('kb').classList.remove('show'); txt.blur(); document.documentElement.style.removeProperty('--vh'); }
$('kbBtn').onclick=openKb; $('hint').onclick=openKb; $('kbx').onclick=closeKb;
function flush(){ if(!composing && txt.value){ send({t:'text',s:txt.value}); txt.value=''; } }
txt.addEventListener('compositionstart',()=>composing=true);
txt.addEventListener('compositionend',()=>{ composing=false; setTimeout(flush,0); });
txt.addEventListener('input',()=>setTimeout(flush,0));
txt.addEventListener('keydown',e=>{
  if(composing||e.isComposing) return;
  if(e.key==='Enter'){ e.preventDefault(); flush(); send({t:'key',k:'enter'}); }
  else if(e.key==='Backspace' && !txt.value){ e.preventDefault(); send({t:'key',k:'backspace'}); }
});
document.querySelectorAll('#kb button').forEach(b=>b.addEventListener('touchstart',e=>{ e.preventDefault(); b.click(); },{passive:false}));  // 按鈕不搶走輸入框焦點
txt.addEventListener('blur',()=>setTimeout(()=>{ if(document.activeElement!==txt) closeKb(); },300));
function setBtn(r){ rightNext=r; $('rb').classList.toggle('on',r); $('lb').classList.toggle('on',!r); }
$('lb').onclick=()=>setBtn(false); $('rb').onclick=()=>setBtn(!rightNext);   // 右鍵：點一次後自動回到左鍵
function ripple(x,y,r){ const d=document.createElement('div'); d.className='tap'+(r?' r':''); d.style.left=x+'px'; d.style.top=y+'px'; document.body.appendChild(d); setTimeout(()=>d.remove(),450); if(navigator.vibrate) navigator.vibrate(15); }
$('zm').onclick=()=>{ zoom=!zoom; wrap.classList.toggle('zoom',zoom); $('zm').classList.toggle('on',zoom); $('more').classList.remove('show'); };
$('ql').onclick=()=>{ qi=(qi+1)%3; $('ql').textContent='畫質:'+QL[qi][0]; };
$('up').onclick=()=>{ if(!confirm('要從 GitHub 下載最新版並重新啟動嗎？')) return; $('up').textContent='更新中，約 10 秒…'; send({t:'update'}); };
$('lall').onclick=()=>{ if(!confirm('所有手機（包含這支）都要重新輸入 PIN，確定嗎？')) return; send({t:'logoutall'}); setTimeout(()=>location.href='/',1500); };
$('out').onclick=()=>{ location.href='/logout'; };
connect();
</script></body></html>"""

# ---------------- 電池 ----------------
def battery():
    """回傳 (百分比, 是否插電)；讀不到回傳 (None, None)"""
    if not IS_WIN: return None, None
    class SPS(ctypes.Structure):
        _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte), ("sys", ctypes.c_ubyte),
                    ("life", wintypes.DWORD), ("full", wintypes.DWORD)]
    st = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)): return None, None
    return (None if st.pct == 255 else st.pct), st.ac == 1

def battery_guard():
    """用電池且剩 10% 以下 → 讓電腦睡眠，保護電池"""
    while True:
        time.sleep(60)
        pct, ac = battery()
        if pct is not None and not ac and pct <= 10:
            print("電池剩 %d%%，自動睡眠" % pct)
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            ctypes.windll.powrprof.SetSuspendState(0, 0, 0)

# ---------------- 外網通道（cloudflared，免安裝免管理員）----------------
import re, subprocess
URL_FILE = os.path.join(BASE, "目前手機網址.txt")

def notify_outlook(url):
    """用這台電腦的 Outlook 寄信給自己（手機收信就知道新網址）"""
    to = str(CFG.get("notify_email", "")).strip()
    ps = r"""
$o = New-Object -ComObject Outlook.Application
$to = '%s'
if (-not $to) { $to = $o.Session.Accounts.Item(1).SmtpAddress }
$m = $o.CreateItem(0); $m.To = $to
$m.Subject = '手機遙控 新網址'
$m.Body = "手機打開這個網址（輸入 PIN）：`r`n%s`r`n`r`n電腦重開或網路斷線後網址會改變，會再寄一封新的。"
$m.Send()
""" % (to.replace("'", ""), url)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60,
                           creationflags=0x08000000 if IS_WIN else 0)
        print("已用 Outlook 寄新網址給自己" if r.returncode == 0 else "Outlook 寄信失敗：" + (r.stderr or "")[:200])
    except Exception as e:
        print("Outlook 寄信失敗：", e)

def notify_login(ua):
    """有人用 PIN 登入成功就寄信通知（不是你自己的話：關掉黑色視窗、改 PIN）"""
    to = str(CFG.get("notify_email", "")).strip()
    ua = re.sub(r"[^0-9A-Za-z ._;:/()-]", "", ua)
    ps = r"""
$o = New-Object -ComObject Outlook.Application
$to = '%s'
if (-not $to) { $to = $o.Session.Accounts.Item(1).SmtpAddress }
$m = $o.CreateItem(0); $m.To = $to
$m.Subject = '手機遙控 有裝置登入'
$m.Body = "剛剛有裝置用 PIN 登入手機遙控：`r`n時間：%s`r`n裝置：%s`r`n`r`n如果不是你：馬上關掉電腦上的黑色視窗，然後改 phone_remote_config.json 的 PIN。"
$m.Send()
""" % (to.replace("'", ""), time.strftime("%Y/%m/%d %H:%M"), ua)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60,
                       creationflags=0x08000000 if IS_WIN else 0)
    except Exception as e:
        print("登入通知寄信失敗：", e)

TUNNEL = {"p": None}
import atexit
atexit.register(lambda: TUNNEL["p"] and TUNNEL["p"].poll() is None and TUNNEL["p"].kill())

def tunnel_loop():
    exe = os.path.join(BASE, "cloudflared.exe")
    if not os.path.exists(exe):
        print("（沒有 cloudflared.exe，只能本機使用）"); return
    last = None
    while True:
        print("外網通道啟動中…")
        pr = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:%d" % PORT],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        TUNNEL["p"] = pr   # 跟黑色視窗綁在一起：關視窗時 cloudflared 一起結束
        for line in pr.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and m.group(0) != last and "//api." not in m.group(0):
                last = m.group(0)
                with open(URL_FILE, "w", encoding="utf-8") as f: f.write(last + "\n")
                print("=" * 50); print(" 手機網址：" + last); print("=" * 50)
                if CFG.get("notify", "outlook") == "outlook": threading.Thread(target=notify_outlook, args=(last,), daemon=True).start()
        print("外網通道中斷，10 秒後重連"); time.sleep(10)

def do_update():
    """從 GitHub 下載最新版蓋掉自己（先檢查內容完整、語法正確，舊版留 .bak）"""
    import urllib.request
    req = urllib.request.Request(UPDATE_URL + "?t=%d" % time.time(), headers={"Cache-Control": "no-cache"})
    data = urllib.request.urlopen(req, timeout=30).read()
    if len(data) < 10000 or b"def main" not in data: raise ValueError("下載內容不完整")
    compile(data, "phone_remote.py", "exec")
    me = os.path.abspath(__file__)
    with open(me, "rb") as f: old = f.read()
    with open(me + ".bak", "wb") as f: f.write(old)
    with open(me, "wb") as f: f.write(data)
    m = re.search(rb'VERSION = "([^"]+)"', data)
    return m.group(1).decode() if m else "?"

def child_main():
    """網頁伺服器本體（更新時只重開這個，外網通道不動 → 手機網址不變）"""
    if IS_WIN: ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
    app = web.Application()
    app.add_routes([web.get("/", index), web.post("/login", login), web.get("/logout", logout), web.get("/manifest.json", manifest), web.get("/ws", ws_handler)])
    for i in range(20):   # 等舊的伺服器放開 port
        try:
            web.run_app(app, host="127.0.0.1", port=PORT, print=None); return
        except OSError:
            time.sleep(0.5)

def main():
    if "--child" in sys.argv: return child_main()
    if IS_WIN: ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
    print("=" * 50)
    print(" 手機遙控 PhoneRemote v%s 已啟動（關掉這個視窗就停止）" % VERSION)
    print(" 本機測試網址： http://127.0.0.1:%d" % PORT)
    print(" 手機網址：等一下會顯示 https://....trycloudflare.com（也會寄到你的 Outlook）")
    print(" PIN：%s   （要改 PIN：編輯 %s）" % (PIN, CFG_PATH))
    if len(PIN) < 8: print(" ⚠️ PIN 少於 8 位，建議改成 8 位以上（改完所有手機要重新登入）")
    print("=" * 50)
    threading.Thread(target=tunnel_loop, daemon=True).start()
    if IS_WIN: threading.Thread(target=battery_guard, daemon=True).start()
    while True:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--child"])
        if r.returncode == 3:
            print("已更新，重新啟動伺服器（手機網址不變）…"); continue
        print("伺服器停止（代碼 %s），5 秒後重開" % r.returncode); time.sleep(5)

if __name__ == "__main__":
    main()
