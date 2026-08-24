import asyncio
import os
import re
import secrets
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastmcp import FastMCP
from fastmcp.utilities.lifespan import combine_lifespans
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

APP_NAME = "通用浏览器 MCP"
START_URL = os.getenv("BROWSER_START_URL", "about:blank")
PROFILE_DIR = Path(os.getenv("BROWSER_PROFILE_DIR", "/data/browser-profile"))
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
MCP_PATH_TOKEN = os.getenv("MCP_PATH_TOKEN", "").strip() or "change-me"
HEADLESS = os.getenv("HEADLESS", "true").lower() not in {"0", "false", "no"}
VW = int(os.getenv("VIEWPORT_WIDTH", "430"))
VH = int(os.getenv("VIEWPORT_HEIGHT", "932"))
TIMEOUT = int(os.getenv("BROWSER_TIMEOUT_MS", "15000"))

ALLOWED_HOSTS = [x.strip().lower() for x in os.getenv("BROWSER_ALLOWED_HOSTS", "*").split(",") if x.strip()]
BLOCK_CLICK = re.compile(r"(提交订单|确认下单|确认订单|立即下单|去支付|立即支付|确认支付|付款|确认付款|转账|充值|提现|确认购买|立即购买|购买|订阅|删除账号|注销账号|place\\s*order|confirm\\s*purchase|pay\\s*now|transfer|withdraw|delete\\s*account)", re.I)
BLOCK_INPUT = re.compile(r"(验证码|密码|口令|支付|银行卡|身份证|手机号|手机号码|card|password|otp|sms|cvv|security\\s*code|2fa|two[- ]factor)", re.I)


class Browser:
    def __init__(self):
        self.pw = None
        self.ctx = None
        self.page = None
        self.lock = asyncio.Lock()

    async def start(self):
        async with self.lock:
            if self.page and not self.page.is_closed():
                return self.page
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self.pw = await async_playwright().start()
            kwargs: dict[str, Any] = {
                "user_data_dir": str(PROFILE_DIR),
                "headless": HEADLESS,
                "viewport": {"width": VW, "height": VH},
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
            lat, lng = os.getenv("BROWSER_LAT", "").strip(), os.getenv("BROWSER_LNG", "").strip()
            if lat and lng:
                try:
                    kwargs["geolocation"] = {"latitude": float(lat), "longitude": float(lng)}
                    kwargs["permissions"] = ["geolocation"]
                except ValueError:
                    pass
            self.ctx = await self.pw.chromium.launch_persistent_context(**kwargs)
            self.ctx.set_default_timeout(TIMEOUT)
            self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
            if START_URL not in {"about:blank", ""} and self.page.url in {"about:blank", ""}:
                await self.page.goto(START_URL, wait_until="domcontentloaded")
            return self.page

    async def get(self):
        return await self.start() if not self.page or self.page.is_closed() else self.page

    async def stop(self):
        async with self.lock:
            if self.ctx:
                await self.ctx.close()
            if self.pw:
                await self.pw.stop()
            self.pw = self.ctx = self.page = None

    async def status(self):
        p = await self.get()
        try:
            title = await p.title()
        except Exception:
            title = ""
        return {"ok": True, "alive": not p.is_closed(), "url": p.url, "title": title,
                "viewport": {"width": VW, "height": VH}, "headless": HEADLESS}

    async def summary(self, limit=7000):
        p = await self.get()
        try:
            text = await p.locator("body").inner_text(timeout=5000)
        except Exception:
            text = ""
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > limit:
            text = text[:limit] + "\n…（已截断）"
        return {"url": p.url, "title": await p.title(), "text": text}

    async def goto(self, url: str, mcp_safe=False):
        if mcp_safe:
            u = urlparse(url)
            host = (u.hostname or "").strip().lower()
            if u.scheme not in {"http", "https"} or not host:
                raise ValueError("MCP 自动导航只允许有效的 http/https URL。")
            if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
                raise ValueError("MCP 自动导航默认禁止 localhost / .local 地址。")
            try:
                ip = ipaddress.ip_address(host)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                    raise ValueError("MCP 自动导航默认禁止私网、回环、链路本地等地址。")
            except ValueError as e:
                # host 不是 IP 时属于正常域名；真正的策略错误需要继续抛出。
                if "默认禁止" in str(e):
                    raise
            if ALLOWED_HOSTS != ["*"]:
                if not any(host == s or host.endswith("." + s) for s in ALLOWED_HOSTS):
                    raise ValueError("该域名不在 BROWSER_ALLOWED_HOSTS 白名单中。")
        p = await self.get()
        await p.goto(url, wait_until="domcontentloaded")
        return await self.summary(3500)


browser = Browser()
mcp = FastMCP(APP_NAME)


@mcp.tool
async def browser_status() -> dict:
    """查看持久化远程浏览器状态。"""
    return await browser.status()


@mcp.tool
async def browser_open(url: str) -> dict:
    """打开一个公网 http/https 网页；默认禁止 localhost / 私网地址。"""
    try:
        return {"ok": True, "page": await browser.goto(url, mcp_safe=True)}
    except ValueError as e:
        return {"ok": False, "reason": str(e)}


@mcp.tool
async def browser_read_page(max_chars: int = 7000) -> dict:
    """读取当前页面标题、URL 和可见文本。"""
    return await browser.summary(max(1000, min(max_chars, 12000)))


@mcp.tool
async def browser_list_controls(limit: int = 80) -> list[dict[str, str]]:
    """列出当前页面可见按钮、链接和输入框。"""
    p = await browser.get()
    js = """
    (limit) => [...document.querySelectorAll('button,a,input,textarea,[role="button"],[contenteditable="true"]')]
      .filter(el => { const r=el.getBoundingClientRect(), s=getComputedStyle(el); return r.width>2&&r.height>2&&s.visibility!=='hidden'&&s.display!=='none'; })
      .slice(0,limit).map((el,i)=>({index:String(i),tag:el.tagName.toLowerCase(),text:(el.innerText||el.value||el.getAttribute('aria-label')||'').trim().slice(0,160),placeholder:(el.getAttribute('placeholder')||'').trim().slice(0,120),aria:(el.getAttribute('aria-label')||'').trim().slice(0,120)}))
    """
    return await p.evaluate(js, max(10, min(limit, 120)))


@mcp.tool
async def browser_click_text(text: str, exact: bool = False) -> dict:
    """按文字点击。提交订单/付款等最终动作会被拦截，必须用户手动完成。"""
    target = (text or "").strip()
    if not target:
        return {"ok": False, "reason": "text 不能为空"}
    if BLOCK_CLICK.search(target):
        return {"ok": False, "blocked": True, "reason": "下单/支付动作必须由用户在前端手动点击。"}
    p = await browser.get()
    loc = p.get_by_text(target, exact=exact)
    count = await loc.count()
    if count == 0:
        return {"ok": False, "reason": "没有找到匹配文字", "page": await browser.summary(2200)}
    chosen = None
    for i in range(min(count, 12)):
        item = loc.nth(i)
        try:
            if await item.is_visible():
                chosen = item
                break
        except Exception:
            pass
    if not chosen:
        return {"ok": False, "reason": "匹配项不可见"}
    try:
        real = (await chosen.inner_text()).strip()
    except Exception:
        real = target
    if BLOCK_CLICK.search(real):
        return {"ok": False, "blocked": True, "reason": "目标实际是下单/支付动作，请用户手动点击。"}
    await chosen.click()
    await p.wait_for_timeout(600)
    return {"ok": True, "page": await browser.summary(3000)}


@mcp.tool
async def browser_fill(value: str, placeholder: str = "", aria_label: str = "") -> dict:
    """填写普通输入框；验证码、手机号、密码和支付信息必须用户手动输入。"""
    marker = f"{placeholder} {aria_label}"
    if BLOCK_INPUT.search(marker):
        return {"ok": False, "blocked": True, "reason": "敏感登录/支付字段必须由用户手动输入。"}
    p = await browser.get()
    if placeholder:
        loc = p.get_by_placeholder(placeholder, exact=False)
    elif aria_label:
        loc = p.get_by_label(aria_label, exact=False)
    else:
        return {"ok": False, "reason": "请提供 placeholder 或 aria_label"}
    if await loc.count() == 0:
        return {"ok": False, "reason": "没有找到输入框"}
    field = loc.first
    ph = (await field.get_attribute("placeholder") or "")
    aria = (await field.get_attribute("aria-label") or "")
    if BLOCK_INPUT.search(ph + " " + aria):
        return {"ok": False, "blocked": True, "reason": "该字段属于敏感登录/支付输入。"}
    await field.fill(value)
    return {"ok": True, "page": await browser.summary(2500)}


@mcp.tool
async def browser_press(key: str) -> dict:
    """发送普通按键，如 Enter / Tab / ArrowDown。"""
    allowed = {"Enter","Escape","Tab","ArrowDown","ArrowUp","ArrowLeft","ArrowRight","PageDown","PageUp","Home","End"}
    if key not in allowed:
        return {"ok": False, "reason": "不允许的按键"}
    p = await browser.get()
    await p.keyboard.press(key)
    await p.wait_for_timeout(350)
    return {"ok": True, "page": await browser.summary(2500)}


@mcp.tool
async def browser_back() -> dict:
    p = await browser.get(); await p.go_back(wait_until="domcontentloaded")
    return {"ok": True, "page": await browser.summary(2500)}


@mcp.tool
async def browser_reload() -> dict:
    p = await browser.get(); await p.reload(wait_until="domcontentloaded")
    return {"ok": True, "page": await browser.summary(2500)}


@mcp.tool
async def browser_goto(url: str) -> dict:
    """导航到一个公网 http/https 网页。"""
    try:
        return {"ok": True, "page": await browser.goto(url, mcp_safe=True)}
    except ValueError as e:
        return {"ok": False, "reason": str(e)}


mcp_app = mcp.http_app(path="/")


def guard(token: str | None):
    if not DASHBOARD_TOKEN:
        raise HTTPException(503, "未配置 DASHBOARD_TOKEN")
    if not token or not secrets.compare_digest(token, DASHBOARD_TOKEN):
        raise HTTPException(401, "控制台口令不正确")


class ClickBody(BaseModel):
    x: float = Field(ge=0, le=1); y: float = Field(ge=0, le=1)
class TypeBody(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
class KeyBody(BaseModel):
    key: str = Field(min_length=1, max_length=64)
class ScrollBody(BaseModel):
    delta_y: int = Field(ge=-5000, le=5000)
class GotoBody(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


@asynccontextmanager
async def web_lifespan(app: FastAPI):
    yield
    await browser.stop()


app = FastAPI(title=APP_NAME, lifespan=combine_lifespans(web_lifespan, mcp_app.lifespan))
MCP_MOUNT = f"/mcp/{MCP_PATH_TOKEN}"
app.mount(MCP_MOUNT, mcp_app)


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML.replace("__MCP_PATH__", MCP_MOUNT)


@app.get("/health")
async def health():
    return {"ok": True, "dashboard_token": bool(DASHBOARD_TOKEN), "mcp_secret": MCP_PATH_TOKEN != "change-me"}


@app.get("/api/browser/status")
async def api_status(x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token); return await browser.status()


@app.post("/api/browser/open-start")
async def api_open(x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token)
    if START_URL in {"about:blank", ""}:
        return await browser.summary(2200)
    return await browser.goto(START_URL)


@app.post("/api/browser/goto")
async def api_goto(body: GotoBody, x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token); return await browser.goto(body.url)


@app.get("/api/browser/screenshot")
async def api_screen(x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token); p = await browser.get(); data = await p.screenshot(type="png", full_page=False)
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/browser/click")
async def api_click(body: ClickBody, x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token); p = await browser.get(); await p.mouse.click(body.x * VW, body.y * VH); return {"ok": True}


@app.post("/api/browser/scroll")
async def api_scroll(body: ScrollBody, x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token); p = await browser.get(); await p.mouse.wheel(0, body.delta_y); return {"ok": True}


@app.post("/api/browser/type")
async def api_type(body: TypeBody, x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token); p = await browser.get(); await p.keyboard.type(body.text, delay=25); return {"ok": True}


@app.post("/api/browser/key")
async def api_key(body: KeyBody, x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token); p = await browser.get(); await p.keyboard.press(body.key); return {"ok": True}


@app.post("/api/browser/back")
async def api_back(x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token); p = await browser.get(); await p.go_back(wait_until="domcontentloaded"); return {"ok": True}


@app.post("/api/browser/reload")
async def api_reload(x_dashboard_token: str | None = Header(default=None)):
    guard(x_dashboard_token); p = await browser.get(); await p.reload(wait_until="domcontentloaded"); return {"ok": True}


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>通用浏览器 MCP</title><style>
:root{--bg:#0b0d10;--card:#151922;--text:#f5f7fa;--muted:#98a2b1;--line:#29303a;--yellow:#ffd100;--ok:#43d39b}*{box-sizing:border-box}body{margin:0;background:linear-gradient(#0b0d10,#111722,#0b0d10);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}.wrap{width:min(1120px,100%);margin:auto;padding:14px}.top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px}.brand{font-size:20px;font-weight:900;display:flex;align-items:center;gap:10px}.logo{width:38px;height:38px;border-radius:13px;background:var(--yellow);color:#111;display:grid;place-items:center}.grid{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:14px}.card{background:#151922;border:1px solid var(--line);border-radius:20px;overflow:hidden}.browser{padding:12px}.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}button,input{font:inherit}button{border:1px solid var(--line);background:#222a35;color:var(--text);padding:10px 12px;border-radius:12px}button.primary{background:var(--yellow);color:#111;border-color:var(--yellow);font-weight:800}.screen{position:relative;background:#050607;border-radius:16px;overflow:hidden;min-height:520px;display:grid;place-items:center}.screen img{width:100%;display:block;touch-action:manipulation}.side{padding:15px}.group{padding:14px 0;border-top:1px solid var(--line)}.group:first-child{border-top:0;padding-top:0}.small,label{font-size:12px;color:var(--muted)}input{width:100%;background:#0f141c;color:#fff;border:1px solid var(--line);padding:12px;border-radius:12px;outline:none}.row{display:flex;gap:8px;margin-top:8px}.row>*{flex:1}.note{font-size:13px;color:#cbd3dd;background:#0f141c;border:1px solid var(--line);border-radius:14px;padding:12px}.note b{color:var(--yellow)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#68717e;margin-right:6px}.dot.ok{background:var(--ok)}#overlay{position:fixed;inset:0;background:rgba(3,5,8,.94);display:grid;place-items:center;padding:20px;z-index:10}.login{width:min(420px,100%);background:#151922;border:1px solid var(--line);border-radius:20px;padding:22px}.hidden{display:none!important}.tap{position:absolute;width:18px;height:18px;border:2px solid var(--yellow);border-radius:50%;transform:translate(-50%,-50%);pointer-events:none}@media(max-width:840px){.grid{grid-template-columns:1fr}.side{order:-1}.screen{min-height:420px}.wrap{padding:9px}}</style></head><body>
<div id="overlay"><div class="login"><h2>控制台口令</h2><p class="small">口令只保存在当前浏览器 sessionStorage。</p><input id="token" type="password" placeholder="DASHBOARD_TOKEN"><div style="height:8px"></div><button class="primary" style="width:100%" onclick="login()">进入</button><p id="err" class="small"></p></div></div>
<div class="wrap"><div class="top"><div class="brand"><div class="logo">网</div>通用浏览器 MCP</div><div class="small">MCP: __MCP_PATH__/</div></div><div class="grid"><section class="card browser"><div class="bar"><button class="primary" onclick="quickGoto('https://h5.waimai.meituan.com/waimai/mindex/home')">美团外卖</button><button onclick="quickGoto('https://www.taobao.com/')">淘宝</button><button onclick="quickGoto('https://www.jd.com/')">京东</button><button onclick="quickGoto('https://www.xiaohongshu.com/')">小红书</button><button onclick="post('/api/browser/back')">← 后退</button><button onclick="post('/api/browser/reload')">刷新</button><button onclick="scrollByRemote(-650)">↑</button><button onclick="scrollByRemote(650)">↓</button><button id="pause" onclick="toggle()">暂停画面</button></div><div class="screen" id="shell"><img id="img" alt="远端浏览器"></div></section><aside class="card side"><div class="group"><div><span id="dot" class="dot"></span><b>浏览器状态</b></div><div id="status" class="small">等待连接…</div></div><div class="group"><label>打开任意网址</label><input id="url" type="text" placeholder="https://example.com"><div class="row"><button class="primary" onclick="manualGoto()">打开网址</button><button onclick="quickGoto('https://www.baidu.com/')">百度</button></div></div><div class="group"><label>手动输入到当前焦点</label><input id="text" type="password" placeholder="验证码 / 登录信息 / 其他文字"><div class="row"><button class="primary" onclick="typeText()">输入并清空</button><button id="mask" onclick="mask()">显示文字</button></div><div class="row"><button onclick="key('Enter')">Enter</button><button onclick="key('Tab')">Tab</button><button onclick="key('Escape')">Esc</button></div><div class="row"><button onclick="key('Control+A')">全选</button><button onclick="key('Backspace')">删除</button></div></div><div class="group"><div class="note"><b>这是通用浏览器，不只给美团用。</b><br>AI 可以逛普通公网网页，但验证码、密码、2FA、付款、转账、购买等高风险最终动作会被 MCP 拦截；这些由你本人直接点左边画面完成。某些网站可能因为人机验证、Passkey、DRM 或必须拉起原生 App 而需要单独适配。</div></div></aside></div></div>
<script>let token=sessionStorage.getItem('browser_token')||'',refreshing=true;function H(){return {'X-Dashboard-Token':token,'Content-Type':'application/json'}}async function req(u,o={}){o.headers={...(o.headers||{}),...H()};let r=await fetch(u,o);if(r.status===401){sessionStorage.removeItem('browser_token');overlay.classList.remove('hidden');throw Error('口令不正确')}if(!r.ok)throw Error('请求失败 '+r.status);return r}async function login(){token=document.getElementById('token').value.trim();try{await req('/api/browser/status');sessionStorage.setItem('browser_token',token);overlay.classList.add('hidden');statusNow();screen();}catch(e){err.textContent=e.message}}async function post(u,b={}){await req(u,{method:'POST',body:JSON.stringify(b)});setTimeout(()=>{statusNow();screen()},200)}async function statusNow(){try{let j=await (await req('/api/browser/status')).json();dot.classList.toggle('ok',j.alive);status.innerHTML='<b>'+(j.title||'无标题')+'</b><br>'+(j.url||'')}catch{dot.classList.remove('ok')}}async function screen(){if(!refreshing)return;try{let r=await req('/api/browser/screenshot?t='+Date.now()),b=await r.blob(),old=img.src;img.src=URL.createObjectURL(b);img.onload=()=>{if(old.startsWith('blob:'))URL.revokeObjectURL(old)}}catch{}}img.addEventListener('click',async e=>{let r=img.getBoundingClientRect(),x=(e.clientX-r.left)/r.width,y=(e.clientY-r.top)/r.height,d=document.createElement('div');d.className='tap';d.style.left=(x*100)+'%';d.style.top=(y*100)+'%';shell.appendChild(d);setTimeout(()=>d.remove(),350);await post('/api/browser/click',{x,y})});async function quickGoto(url){document.getElementById('url').value=url;await post('/api/browser/goto',{url})}async function manualGoto(){let url=document.getElementById('url').value.trim();if(!url)return;if(!/^https?:\/\//i.test(url))url='https://'+url;await post('/api/browser/goto',{url})}async function scrollByRemote(delta_y){await post('/api/browser/scroll',{delta_y})}async function typeText(){let el=document.getElementById('text');if(!el.value)return;await post('/api/browser/type',{text:el.value});el.value=''}async function key(k){await post('/api/browser/key',{key:k})}function mask(){let el=document.getElementById('text');el.type=el.type==='password'?'text':'password';document.getElementById('mask').textContent=el.type==='password'?'显示文字':'隐藏文字'}function toggle(){refreshing=!refreshing;pause.textContent=refreshing?'暂停画面':'继续画面';if(refreshing)screen()}if(token){overlay.classList.add('hidden');statusNow();screen()}setInterval(screen,900);setInterval(statusNow,4500);</script></body></html>'''
