import asyncio
import ipaddress
import os
import re
import secrets
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

START_URL = os.getenv(
    "BROWSER_START_URL",
    "about:blank",
)

PROFILE_DIR = Path(
    os.getenv(
        "BROWSER_PROFILE_DIR",
        "/data/browser-profile",
    )
)

DASHBOARD_TOKEN = os.getenv(
    "DASHBOARD_TOKEN",
    "",
).strip()

MCP_PATH_TOKEN = (
    os.getenv(
        "MCP_PATH_TOKEN",
        "",
    ).strip()
    or "change-me"
)

HEADLESS = (
    os.getenv(
        "HEADLESS",
        "true",
    ).lower()
    not in {
        "0",
        "false",
        "no",
    }
)

TIMEOUT = int(
    os.getenv(
        "BROWSER_TIMEOUT_MS",
        "15000",
    )
)


# =========================
# 手机模式
# =========================

MOBILE_W = int(
    os.getenv(
        "VIEWPORT_WIDTH",
        "430",
    )
)

MOBILE_H = int(
    os.getenv(
        "VIEWPORT_HEIGHT",
        "932",
    )
)

MOBILE_DEVICE = (
    os.getenv(
        "BROWSER_MOBILE_DEVICE",
        "Pixel 7",
    ).strip()
    or "Pixel 7"
)


# =========================
# 桌面模式
# =========================

DESKTOP_W = int(
    os.getenv(
        "DESKTOP_VIEWPORT_WIDTH",
        "1440",
    )
)

DESKTOP_H = int(
    os.getenv(
        "DESKTOP_VIEWPORT_HEIGHT",
        "1000",
    )
)


# =========================
# 默认模式
# =========================

DEFAULT_MODE = os.getenv(
    "BROWSER_DEFAULT_MODE",
    "",
).strip().lower()

if DEFAULT_MODE not in {
    "mobile",
    "desktop",
}:

    # 兼容之前已经配置过的
    # BROWSER_MOBILE=true / false
    old_mobile = (
        os.getenv(
            "BROWSER_MOBILE",
            "true",
        ).lower()
        not in {
            "0",
            "false",
            "no",
        }
    )

    DEFAULT_MODE = (
        "mobile"
        if old_mobile
        else "desktop"
    )


# =========================
# MCP 自动导航域名限制
# =========================

ALLOWED_HOSTS = [
    x.strip().lower()
    for x in os.getenv(
        "BROWSER_ALLOWED_HOSTS",
        "*",
    ).split(",")
    if x.strip()
]


# =========================
# 高风险操作保护
# =========================

BLOCK_CLICK = re.compile(
    r"("
    r"提交订单|"
    r"确认下单|"
    r"确认订单|"
    r"立即下单|"
    r"去支付|"
    r"立即支付|"
    r"确认支付|"
    r"付款|"
    r"确认付款|"
    r"转账|"
    r"充值|"
    r"提现|"
    r"确认购买|"
    r"立即购买|"
    r"购买|"
    r"订阅|"
    r"删除账号|"
    r"注销账号|"
    r"place\s*order|"
    r"confirm\s*purchase|"
    r"pay\s*now|"
    r"transfer|"
    r"withdraw|"
    r"delete\s*account"
    r")",
    re.I,
)


BLOCK_INPUT = re.compile(
    r"("
    r"验证码|"
    r"密码|"
    r"口令|"
    r"支付|"
    r"银行卡|"
    r"身份证|"
    r"手机号|"
    r"手机号码|"
    r"card|"
    r"password|"
    r"otp|"
    r"sms|"
    r"cvv|"
    r"security\s*code|"
    r"2fa|"
    r"two[- ]factor"
    r")",
    re.I,
)


# ==========================================================
# Browser Manager
# ==========================================================

class Browser:

    def __init__(self):

        self.pw = None
        self.ctx = None
        self.page = None

        self.lock = asyncio.Lock()

        self.mode = DEFAULT_MODE


    def viewport(
        self,
    ) -> dict[str, int]:

        if self.mode == "desktop":

            return {
                "width": DESKTOP_W,
                "height": DESKTOP_H,
            }

        return {
            "width": MOBILE_W,
            "height": MOBILE_H,
        }


    def launch_kwargs(
        self,
    ) -> dict[str, Any]:

        vp = self.viewport()

        kwargs: dict[str, Any] = {

            "user_data_dir":
                str(PROFILE_DIR),

            "headless":
                HEADLESS,

            "viewport":
                vp,

            "screen":
                vp,

            "locale":
                "zh-CN",

            "timezone_id":
                "Asia/Shanghai",

        }


        # =========================
        # 手机浏览器模拟
        # =========================

        if self.mode == "mobile":

            device = None

            if self.pw:

                device = (
                    self.pw.devices.get(
                        MOBILE_DEVICE
                    )
                    or
                    self.pw.devices.get(
                        "Pixel 7"
                    )
                )

            if device:

                for key in (
                    "user_agent",
                    "device_scale_factor",
                    "is_mobile",
                    "has_touch",
                ):

                    if key in device:
                        kwargs[key] = (
                            device[key]
                        )

                # 使用我们自己的固定尺寸，
                # 保证截图与点击坐标一致。

                kwargs["viewport"] = vp
                kwargs["screen"] = vp

            else:

                kwargs["is_mobile"] = True
                kwargs["has_touch"] = True


        # =========================
        # 桌面浏览器模拟
        # =========================

        else:

            kwargs["is_mobile"] = False
            kwargs["has_touch"] = False
            kwargs[
                "device_scale_factor"
            ] = 1


        # =========================
        # 可选地理位置
        # =========================

        lat = os.getenv(
            "BROWSER_LAT",
            "",
        ).strip()

        lng = os.getenv(
            "BROWSER_LNG",
            "",
        ).strip()

        if lat and lng:

            try:

                kwargs[
                    "geolocation"
                ] = {

                    "latitude":
                        float(lat),

                    "longitude":
                        float(lng),

                }

                kwargs[
                    "permissions"
                ] = [
                    "geolocation"
                ]

            except ValueError:
                pass


        return kwargs


    async def _start_unlocked(
        self,
    ):

        PROFILE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )


        if self.pw is None:

            self.pw = (
                await
                async_playwright().start()
            )


        self.ctx = (
            await
            self.pw.chromium
            .launch_persistent_context(
                **self.launch_kwargs()
            )
        )


        self.ctx.set_default_timeout(
            TIMEOUT
        )


        self.page = (
            self.ctx.pages[0]
            if self.ctx.pages
            else
            await self.ctx.new_page()
        )


        if (
            START_URL
            not in {
                "",
                "about:blank",
            }
            and
            self.page.url
            in {
                "",
                "about:blank",
            }
        ):

            await self.page.goto(
                START_URL,
                wait_until=
                    "domcontentloaded",
            )


        return self.page


    async def start(
        self,
    ):

        async with self.lock:

            if (
                self.page
                and
                not self.page.is_closed()
            ):

                return self.page


            return (
                await
                self._start_unlocked()
            )


    async def get(
        self,
    ):

        if (
            not self.page
            or
            self.page.is_closed()
        ):

            return await self.start()


        return self.page


    async def stop(
        self,
    ):

        async with self.lock:

            if self.ctx:

                try:

                    await self.ctx.close()

                except Exception:
                    pass


            if self.pw:

                try:

                    await self.pw.stop()

                except Exception:
                    pass


            self.ctx = None
            self.page = None
            self.pw = None


    # ======================================================
    # 手机 / 桌面模式切换
    # ======================================================

    async def set_mode(
        self,
        mode: str,
        keep_url: bool = True,
    ):

        mode = (
            mode
            or ""
        ).strip().lower()


        if mode not in {
            "mobile",
            "desktop",
        }:

            return {
                "ok": False,
                "reason":
                    "mode 只能是 mobile 或 desktop",
            }


        async with self.lock:

            old_url = (
                "about:blank"
            )


            if (
                self.page
                and
                not self.page.is_closed()
            ):

                old_url = (
                    self.page.url
                    or
                    "about:blank"
                )


            # 已经是这个模式，
            # 不重复重启 Chromium。

            if (
                mode == self.mode
                and
                self.page
                and
                not self.page.is_closed()
            ):

                return {

                    "ok":
                        True,

                    "changed":
                        False,

                    "mode":
                        self.mode,

                    "viewport":
                        self.viewport(),

                    "page":
                        await self.summary(
                            2200
                        ),

                }


            self.mode = mode


            # persistent profile
            # 不能被两个 Chromium context
            # 同时占用。
            #
            # 所以先关闭旧 context，
            # 再用相同 profile 重新启动。

            if self.ctx:

                try:

                    await self.ctx.close()

                except Exception:
                    pass


            self.ctx = None
            self.page = None


            page = (
                await
                self._start_unlocked()
            )


            # 尽量恢复切换前的页面。

            if (
                keep_url
                and
                old_url.startswith(
                    (
                        "http://",
                        "https://",
                    )
                )
            ):

                try:

                    await page.goto(
                        old_url,
                        wait_until=
                            "domcontentloaded",
                    )

                except Exception:
                    pass


            return {

                "ok":
                    True,

                "changed":
                    True,

                "mode":
                    self.mode,

                "viewport":
                    self.viewport(),

                "page":
                    await self.summary(
                        2500
                    ),

            }


    async def status(
        self,
    ):

        p = await self.get()


        try:

            title = await p.title()

        except Exception:

            title = ""


        return {

            "ok":
                True,

            "alive":
                not p.is_closed(),

            "url":
                p.url,

            "title":
                title,

            "mode":
                self.mode,

            "viewport":
                self.viewport(),

            "headless":
                HEADLESS,

            "mobile_device":
                (
                    MOBILE_DEVICE
                    if
                    self.mode
                    == "mobile"
                    else
                    None
                ),

        }


    async def summary(
        self,
        limit: int = 7000,
    ):

        p = await self.get()


        try:

            text = (
                await
                p.locator(
                    "body"
                )
                .inner_text(
                    timeout=5000
                )
            )

        except Exception:

            text = ""


        text = re.sub(
            r"\n{3,}",
            "\n\n",
            text,
        ).strip()


        if len(text) > limit:

            text = (
                text[:limit]
                +
                "\n…（已截断）"
            )


        try:

            title = await p.title()

        except Exception:

            title = ""


        return {

            "url":
                p.url,

            "title":
                title,

            "text":
                text,

            "mode":
                self.mode,

            "viewport":
                self.viewport(),

        }


    # ======================================================
    # URL 安全检查
    # ======================================================

    async def goto(
        self,
        url: str,
        mcp_safe: bool = False,
    ):

        if mcp_safe:

            u = urlparse(
                url
            )

            host = (
                u.hostname
                or ""
            ).strip().lower()


            if (
                u.scheme
                not in {
                    "http",
                    "https",
                }
                or
                not host
            ):

                raise ValueError(
                    "MCP 自动导航只允许有效的 http/https URL。"
                )


            if (
                host
                in {
                    "localhost",
                    "localhost.localdomain",
                }
                or
                host.endswith(
                    ".local"
                )
            ):

                raise ValueError(
                    "MCP 自动导航默认禁止 localhost / .local 地址。"
                )


            try:

                ip = (
                    ipaddress
                    .ip_address(
                        host
                    )
                )


                if (
                    ip.is_private
                    or
                    ip.is_loopback
                    or
                    ip.is_link_local
                    or
                    ip.is_reserved
                    or
                    ip.is_multicast
                    or
                    ip.is_unspecified
                ):

                    raise ValueError(
                        "MCP 自动导航默认禁止私网、回环、链路本地等地址。"
                    )


            except ValueError as e:

                # 普通域名不是 IP，
                # ip_address 会报 ValueError。
                #
                # 如果是我们主动阻止的私网，
                # 就继续抛出。

                if (
                    "默认禁止"
                    in str(e)
                ):

                    raise


            if (
                ALLOWED_HOSTS
                != [
                    "*"
                ]
            ):

                if not any(

                    host == x
                    or
                    host.endswith(
                        "."
                        + x
                    )

                    for x
                    in ALLOWED_HOSTS

                ):

                    raise ValueError(
                        "该域名不在 BROWSER_ALLOWED_HOSTS 白名单中。"
                    )


        p = await self.get()


        await p.goto(
            url,
            wait_until=
                "domcontentloaded",
        )


        return (
            await
            self.summary(
                3500
            )
        )


    async def screenshot(
        self,
    ) -> bytes:

        p = await self.get()

        return (
            await
            p.screenshot(
                type="png",
                full_page=False,
            )
        )


    # ======================================================
    # 点击坐标自动适应当前 viewport
    # ======================================================

    async def click_normalized(
        self,
        x: float,
        y: float,
    ):

        p = await self.get()

        vp = self.viewport()


        px = max(
            0,
            min(
                vp["width"] - 1,
                x
                * vp["width"],
            ),
        )


        py = max(
            0,
            min(
                vp["height"] - 1,
                y
                * vp["height"],
            ),
        )


        await p.mouse.click(
            px,
            py,
        )


browser = Browser()


# ==========================================================
# MCP
# ==========================================================

mcp = FastMCP(
    APP_NAME
)


@mcp.tool
async def browser_status() -> dict:

    """
    查看浏览器状态、
    当前 mobile / desktop 模式
    和 viewport。
    """

    return (
        await
        browser.status()
    )


@mcp.tool
async def browser_set_mode(
    mode: str,
) -> dict:

    """
    切换浏览器形态。

    mode:
    - mobile
    - desktop

    会继续复用同一个持久化 profile。
    """

    return (
        await
        browser.set_mode(
            mode,
            keep_url=True,
        )
    )


@mcp.tool
async def browser_open(
    url: str,
) -> dict:

    """
    打开一个公网 http/https 网页。
    """

    try:

        return {

            "ok":
                True,

            "page":
                await browser.goto(
                    url,
                    mcp_safe=True,
                ),

        }

    except ValueError as e:

        return {

            "ok":
                False,

            "reason":
                str(e),

        }


@mcp.tool
async def browser_goto(
    url: str,
) -> dict:

    """
    导航到一个公网 http/https 网页。
    """

    try:

        return {

            "ok":
                True,

            "page":
                await browser.goto(
                    url,
                    mcp_safe=True,
                ),

        }

    except ValueError as e:

        return {

            "ok":
                False,

            "reason":
                str(e),

        }


@mcp.tool
async def browser_read_page(
    max_chars: int = 7000,
) -> dict:

    """
    读取当前页面标题、
    URL 和可见文本。
    """

    return (
        await
        browser.summary(

            max(
                1000,
                min(
                    max_chars,
                    12000,
                ),
            )

        )
    )


@mcp.tool
async def browser_list_controls(
    limit: int = 80,
) -> list[dict[str, str]]:

    """
    列出当前页面可见的按钮、链接和输入框。

    额外返回：
    - href：链接真实地址
    - role：ARIA role
    - type：input/button 类型
    """

    p = await browser.get()

    js = """
    (limit) => [
      ...document.querySelectorAll(
        'button,a,input,textarea,[role="button"],[contenteditable="true"]'
      )
    ]
    .filter(el => {

      const r =
        el.getBoundingClientRect();

      const s =
        getComputedStyle(el);

      return (
        r.width > 2 &&
        r.height > 2 &&
        s.visibility !== 'hidden' &&
        s.display !== 'none'
      );

    })
    .slice(
      0,
      limit
    )
    .map((el, i) => ({

      index:
        String(i),

      tag:
        el.tagName
        .toLowerCase(),

      text:
        (
          el.innerText ||
          el.value ||
          el.getAttribute('aria-label') ||
          ''
        )
        .trim()
        .slice(
          0,
          160
        ),

      placeholder:
        (
          el.getAttribute('placeholder') ||
          ''
        )
        .trim()
        .slice(
          0,
          120
        ),

      aria:
        (
          el.getAttribute('aria-label') ||
          ''
        )
        .trim()
        .slice(
          0,
          120
        ),

      href:
        (
          el.href ||
          el.getAttribute('href') ||
          ''
        )
        .trim()
        .slice(
          0,
          800
        ),

      role:
        (
          el.getAttribute('role') ||
          ''
        )
        .trim()
        .slice(
          0,
          80
        ),

      type:
        (
          el.getAttribute('type') ||
          ''
        )
        .trim()
        .slice(
          0,
          80
        )

    }))
    """

    safe_limit = max(
        10,
        min(
            limit,
            120,
        ),
    )

    return await p.evaluate(
        js,
        safe_limit,
    )
@mcp.tool
async def browser_click_text(
    text: str,
    exact: bool = False,
) -> dict:

    """
    按可见文字点击。

    购买、下单、支付、
    转账等最终动作会被拦截。
    """

    target = (
        text
        or ""
    ).strip()


    if not target:

        return {

            "ok":
                False,

            "reason":
                "text 不能为空",

        }


    if BLOCK_CLICK.search(
        target
    ):

        return {

            "ok":
                False,

            "blocked":
                True,

            "reason":
                "下单/支付等最终动作必须由用户在前端手动点击。",

        }


    p = await browser.get()


    loc = p.get_by_text(
        target,
        exact=exact,
    )


    count = await loc.count()


    if count == 0:

        return {

            "ok":
                False,

            "reason":
                "没有找到匹配文字",

            "page":
                await browser.summary(
                    2200
                ),

        }


    chosen = None


    for i in range(
        min(
            count,
            12,
        )
    ):

        item = loc.nth(
            i
        )

        try:

            if (
                await
                item.is_visible()
            ):

                chosen = item
                break

        except Exception:
            pass


    if chosen is None:

        return {

            "ok":
                False,

            "reason":
                "匹配项不可见",

        }


    try:

        real_text = (
            await
            chosen.inner_text()
        ).strip()

    except Exception:

        real_text = target


    if BLOCK_CLICK.search(
        real_text
    ):

        return {

            "ok":
                False,

            "blocked":
                True,

            "reason":
                "目标实际是购买/支付等最终动作，请用户手动点击。",

        }


    await chosen.click()


    await p.wait_for_timeout(
        600
    )


    return {

        "ok":
            True,

        "page":
            await browser.summary(
                3000
            ),

    }


@mcp.tool
async def browser_fill(
    value: str,
    placeholder: str = "",
    aria_label: str = "",
) -> dict:

    """
    填写普通输入框。

    验证码、手机号、
    密码和支付信息
    必须由用户手动输入。
    """

    marker = (
        f"{placeholder} "
        f"{aria_label}"
    )


    if BLOCK_INPUT.search(
        marker
    ):

        return {

            "ok":
                False,

            "blocked":
                True,

            "reason":
                "敏感登录/支付字段必须由用户手动输入。",

        }


    p = await browser.get()


    if placeholder:

        loc = (
            p.get_by_placeholder(
                placeholder,
                exact=False,
            )
        )

    elif aria_label:

        loc = (
            p.get_by_label(
                aria_label,
                exact=False,
            )
        )

    else:

        return {

            "ok":
                False,

            "reason":
                "请提供 placeholder 或 aria_label",

        }


    if (
        await loc.count()
        == 0
    ):

        return {

            "ok":
                False,

            "reason":
                "没有找到输入框",

        }


    field = loc.first


    ph = (
        await
        field.get_attribute(
            "placeholder"
        )
        or ""
    )


    aria = (
        await
        field.get_attribute(
            "aria-label"
        )
        or ""
    )


    if BLOCK_INPUT.search(
        ph
        + " "
        + aria
    ):

        return {

            "ok":
                False,

            "blocked":
                True,

            "reason":
                "该字段属于敏感登录/支付输入。",

        }


    await field.fill(
        value
    )


    return {

        "ok":
            True,

        "page":
            await browser.summary(
                2500
            ),

    }


@mcp.tool
async def browser_press(
    key: str,
) -> dict:

    """
    发送普通按键。
    """

    allowed = {

        "Enter",
        "Escape",
        "Tab",

        "ArrowDown",
        "ArrowUp",
        "ArrowLeft",
        "ArrowRight",

        "PageDown",
        "PageUp",

        "Home",
        "End",

        "Control+A",
        "Meta+A",

        "Backspace",
        "Delete",

    }


    if key not in allowed:

        return {

            "ok":
                False,

            "reason":
                "不允许的按键",

        }


    p = await browser.get()


    await p.keyboard.press(
        key
    )


    await p.wait_for_timeout(
        350
    )


    return {

        "ok":
            True,

        "page":
            await browser.summary(
                2500
            ),

    }


@mcp.tool
async def browser_back() -> dict:

    p = await browser.get()

    await p.go_back(
        wait_until=
            "domcontentloaded"
    )

    return {

        "ok":
            True,

        "page":
            await browser.summary(
                2500
            ),

    }


@mcp.tool
async def browser_reload() -> dict:

    p = await browser.get()

    await p.reload(
        wait_until=
            "domcontentloaded"
    )

    return {

        "ok":
            True,

        "page":
            await browser.summary(
                2500
            ),

    }


# ==========================================================
# FastMCP + FastAPI
# ==========================================================

mcp_app = mcp.http_app(
    path="/"
)


def guard(
    token: str | None,
):

    if not DASHBOARD_TOKEN:

        raise HTTPException(
            503,
            "未配置 DASHBOARD_TOKEN",
        )


    if (
        not token
        or
        not secrets.compare_digest(
            token,
            DASHBOARD_TOKEN,
        )
    ):

        raise HTTPException(
            401,
            "控制台口令不正确",
        )


class ClickBody(
    BaseModel
):

    x: float = Field(
        ge=0,
        le=1,
    )

    y: float = Field(
        ge=0,
        le=1,
    )


class TypeBody(
    BaseModel
):

    text: str = Field(
        min_length=1,
        max_length=1000,
    )


class KeyBody(
    BaseModel
):

    key: str = Field(
        min_length=1,
        max_length=64,
    )


class ScrollBody(
    BaseModel
):

    delta_y: int = Field(
        ge=-5000,
        le=5000,
    )


class GotoBody(
    BaseModel
):

    url: str = Field(
        min_length=1,
        max_length=2048,
    )


class ModeBody(
    BaseModel
):

    mode: str = Field(
        min_length=1,
        max_length=20,
    )


@asynccontextmanager
async def web_lifespan(
    app: FastAPI,
):

    yield

    await browser.stop()


app = FastAPI(

    title=
        APP_NAME,

    lifespan=
        combine_lifespans(
            web_lifespan,
            mcp_app.lifespan,
        ),

)


MCP_MOUNT = (
    f"/mcp/"
    f"{MCP_PATH_TOKEN}"
)


app.mount(
    MCP_MOUNT,
    mcp_app,
)


@app.get(
    "/",
    response_class=
        HTMLResponse,
)
async def home():

    return HTML.replace(
        "__MCP_PATH__",
        MCP_MOUNT,
    )


@app.get(
    "/health"
)
async def health():

    return {

        "ok":
            True,

        "dashboard_token":
            bool(
                DASHBOARD_TOKEN
            ),

        "mcp_secret":
            (
                MCP_PATH_TOKEN
                !=
                "change-me"
            ),

        "mode":
            browser.mode,

        "viewport":
            browser.viewport(),

    }


@app.get(
    "/api/browser/status"
)
async def api_status(

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )

    return (
        await
        browser.status()
    )


@app.post(
    "/api/browser/mode"
)
async def api_mode(

    body:
        ModeBody,

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )

    return (
        await
        browser.set_mode(
            body.mode,
            keep_url=True,
        )
    )


@app.post(
    "/api/browser/goto"
)
async def api_goto(

    body:
        GotoBody,

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )

    return (
        await
        browser.goto(
            body.url
        )
    )


@app.get(
    "/api/browser/screenshot"
)
async def api_screenshot(

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )


    return Response(

        await
        browser.screenshot(),

        media_type=
            "image/png",

        headers={
            "Cache-Control":
                "no-store",
        },

    )


@app.post(
    "/api/browser/click"
)
async def api_click(

    body:
        ClickBody,

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )


    await browser.click_normalized(
        body.x,
        body.y,
    )


    return {
        "ok":
            True
    }


@app.post(
    "/api/browser/scroll"
)
async def api_scroll(

    body:
        ScrollBody,

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )


    p = await browser.get()


    await p.mouse.wheel(
        0,
        body.delta_y,
    )


    return {
        "ok":
            True
    }


@app.post(
    "/api/browser/type"
)
async def api_type(

    body:
        TypeBody,

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )


    p = await browser.get()


    # 输入内容不写入应用日志，
    # 直接发送给当前网页焦点。

    await p.keyboard.type(
        body.text,
        delay=25,
    )


    return {
        "ok":
            True
    }


@app.post(
    "/api/browser/key"
)
async def api_key(

    body:
        KeyBody,

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )


    p = await browser.get()


    await p.keyboard.press(
        body.key
    )


    return {
        "ok":
            True
    }


@app.post(
    "/api/browser/back"
)
async def api_back(

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )


    p = await browser.get()


    await p.go_back(
        wait_until=
            "domcontentloaded"
    )


    return {
        "ok":
            True
    }


@app.post(
    "/api/browser/reload"
)
async def api_reload(

    x_dashboard_token:
        str | None
        =
        Header(
            default=None
        ),

):

    guard(
        x_dashboard_token
    )


    p = await browser.get()


    await p.reload(
        wait_until=
            "domcontentloaded"
    )


    return {
        "ok":
            True
    }


# ==========================================================
# Frontend
# ==========================================================

HTML = r'''
<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1,viewport-fit=cover"
>

<title>
通用浏览器 MCP
</title>


<style>

:root{
    --bg:#0b0d10;
    --card:#151922;
    --text:#f5f7fa;
    --muted:#98a2b1;
    --line:#29303a;
    --yellow:#ffd100;
    --ok:#43d39b;
}


*{
    box-sizing:border-box;
}


body{
    margin:0;

    background:
        linear-gradient(
            #0b0d10,
            #111722,
            #0b0d10
        );

    color:
        var(--text);

    font:
        15px/1.5
        -apple-system,
        BlinkMacSystemFont,
        "PingFang SC",
        sans-serif;
}


.wrap{

    width:
        min(
            1180px,
            100%
        );

    margin:auto;

    padding:
        14px;

}


.top{

    display:flex;

    justify-content:
        space-between;

    align-items:
        center;

    gap:
        10px;

    margin-bottom:
        14px;

}


.brand{

    font-size:
        20px;

    font-weight:
        900;

    display:flex;

    align-items:
        center;

    gap:
        10px;

}


.logo{

    width:
        38px;

    height:
        38px;

    border-radius:
        13px;

    background:
        var(--yellow);

    color:
        #111;

    display:grid;

    place-items:
        center;

}


.grid{

    display:grid;

    grid-template-columns:
        minmax(
            0,
            1fr
        )
        330px;

    gap:
        14px;

}


.card{

    background:
        #151922;

    border:
        1px solid
        var(--line);

    border-radius:
        20px;

    overflow:
        hidden;

}


.browser{
    padding:
        12px;
}


.bar{

    display:flex;

    gap:
        8px;

    flex-wrap:
        wrap;

    margin-bottom:
        10px;

}


button,
input{
    font:
        inherit;
}


button{

    border:
        1px solid
        var(--line);

    background:
        #222a35;

    color:
        var(--text);

    padding:
        10px
        12px;

    border-radius:
        12px;

    cursor:
        pointer;

}


button.primary{

    background:
        var(--yellow);

    color:
        #111;

    border-color:
        var(--yellow);

    font-weight:
        800;

}


button.active{

    outline:
        2px solid
        var(--yellow);

    outline-offset:
        1px;

}


.screen{

    position:
        relative;

    background:
        #050607;

    border-radius:
        16px;

    overflow:
        auto;

    min-height:
        520px;

    display:grid;

    place-items:
        start center;

}


.screen img{

    width:
        100%;

    height:
        auto;

    display:block;

    object-fit:
        contain;

    touch-action:
        manipulation;

}


.side{
    padding:
        15px;
}


.group{

    padding:
        14px
        0;

    border-top:
        1px solid
        var(--line);

}


.group:first-child{

    border-top:
        0;

    padding-top:
        0;

}


.small,
label{

    font-size:
        12px;

    color:
        var(--muted);

}


input{

    width:
        100%;

    background:
        #0f141c;

    color:
        #fff;

    border:
        1px solid
        var(--line);

    padding:
        12px;

    border-radius:
        12px;

    outline:
        none;

}


.row{

    display:flex;

    gap:
        8px;

    margin-top:
        8px;

}


.row > *{
    flex:
        1;
}


.note{

    font-size:
        13px;

    color:
        #cbd3dd;

    background:
        #0f141c;

    border:
        1px solid
        var(--line);

    border-radius:
        14px;

    padding:
        12px;

}


.note b{
    color:
        var(--yellow);
}


.dot{

    display:
        inline-block;

    width:
        8px;

    height:
        8px;

    border-radius:
        50%;

    background:
        #68717e;

    margin-right:
        6px;

}


.dot.ok{
    background:
        var(--ok);
}


#overlay{

    position:
        fixed;

    inset:
        0;

    background:
        rgba(
            3,
            5,
            8,
            .94
        );

    display:grid;

    place-items:
        center;

    padding:
        20px;

    z-index:
        10;

}


.login{

    width:
        min(
            420px,
            100%
        );

    background:
        #151922;

    border:
        1px solid
        var(--line);

    border-radius:
        20px;

    padding:
        22px;

}


.hidden{
    display:
        none!important;
}


.tap{

    position:
        absolute;

    width:
        18px;

    height:
        18px;

    border:
        2px solid
        var(--yellow);

    border-radius:
        50%;

    transform:
        translate(
            -50%,
            -50%
        );

    pointer-events:
        none;

}


@media(
    max-width:
        840px
){

    .grid{

        grid-template-columns:
            1fr;

    }


    .side{
        order:
            -1;
    }


    .screen{
        min-height:
            420px;
    }


    .wrap{
        padding:
            9px;
    }

}

</style>

</head>


<body>


<div id="overlay">

    <div class="login">

        <h2>
        控制台口令
        </h2>

        <p class="small">
        口令只保存在当前浏览器 sessionStorage。
        </p>

        <input
            id="token"
            type="password"
            placeholder="DASHBOARD_TOKEN"
        >

        <div
            style="
                height:8px
            "
        ></div>

        <button
            class="primary"
            style="
                width:100%
            "
            onclick="login()"
        >
            进入
        </button>

        <p
            id="err"
            class="small"
        ></p>

    </div>

</div>


<div class="wrap">


    <div class="top">

        <div class="brand">

            <div class="logo">
                网
            </div>

            通用浏览器 MCP

        </div>


        <div class="small">

            手机 / 桌面双模式 ·
            MCP: __MCP_PATH__/

        </div>

    </div>


    <div class="grid">


        <section
            class="card browser"
        >


            <div class="bar">


                <button
                    class="primary"
                    onclick="
                        quickGoto(
                            'https://h5.waimai.meituan.com/waimai/mindex/home',
                            'mobile'
                        )
                    "
                >
                    美团外卖 📱
                </button>


                <button
                    onclick="
                        quickGoto(
                            'https://www.taobao.com/',
                            'mobile'
                        )
                    "
                >
                    淘宝 📱
                </button>


                <button
                    onclick="
                        quickGoto(
                            'https://www.jd.com/',
                            'desktop'
                        )
                    "
                >
                    京东 🖥️
                </button>


                <button
                    onclick="
                        quickGoto(
                            'https://www.xiaohongshu.com/',
                            'desktop'
                        )
                    "
                >
                    小红书 🖥️
                </button>


                <button
                    onclick="
                        post(
                            '/api/browser/back'
                        )
                    "
                >
                    ← 后退
                </button>


                <button
                    onclick="
                        post(
                            '/api/browser/reload'
                        )
                    "
                >
                    刷新
                </button>


                <button
                    onclick="
                        scrollRemote(
                            -650
                        )
                    "
                >
                    ↑
                </button>


                <button
                    onclick="
                        scrollRemote(
                            650
                        )
                    "
                >
                    ↓
                </button>


                <button
                    id="pause"
                    onclick="
                        toggleRefresh()
                    "
                >
                    暂停画面
                </button>


            </div>


            <div
                class="screen"
                id="shell"
            >

                <img
                    id="img"
                    alt="远端浏览器"
                >

            </div>


        </section>


        <aside
            class="card side"
        >


            <div class="group">

                <div>

                    <span
                        id="dot"
                        class="dot"
                    ></span>

                    <b>
                        浏览器状态
                    </b>

                </div>

                <div
                    id="status"
                    class="small"
                >
                    等待连接…
                </div>

            </div>


            <div class="group">

                <label>
                    浏览器模式
                </label>


                <div class="row">


                    <button
                        id="mobileBtn"
                        onclick="
                            setMode(
                                'mobile'
                            )
                        "
                    >
                        📱 手机
                    </button>


                    <button
                        id="desktopBtn"
                        onclick="
                            setMode(
                                'desktop'
                            )
                        "
                    >
                        🖥️ 桌面
                    </button>


                </div>


                <div
                    id="modeHelp"
                    class="small"
                    style="
                        margin-top:8px
                    "
                >

                    切换会重启远端 Chromium，
                    但继续用同一个 profile。

                </div>

            </div>


            <div class="group">

                <label>
                    打开任意网址
                </label>


                <input
                    id="url"
                    type="text"
                    placeholder="https://example.com"
                >


                <div class="row">


                    <button
                        class="primary"
                        onclick="
                            manualGoto()
                        "
                    >
                        打开网址
                    </button>


                    <button
                        onclick="
                            quickGoto(
                                'https://www.baidu.com/',
                                'mobile'
                            )
                        "
                    >
                        百度
                    </button>


                </div>

            </div>


            <div class="group">

                <label>
                    手动输入到当前焦点
                </label>


                <input
                    id="text"
                    type="password"
                    placeholder="验证码 / 登录信息 / 其他文字"
                >


                <div class="row">


                    <button
                        class="primary"
                        onclick="
                            typeText()
                        "
                    >
                        输入并清空
                    </button>


                    <button
                        id="mask"
                        onclick="
                            toggleMask()
                        "
                    >
                        显示文字
                    </button>


                </div>


                <div class="row">


                    <button
                        onclick="
                            sendKey(
                                'Enter'
                            )
                        "
                    >
                        Enter
                    </button>


                    <button
                        onclick="
                            sendKey(
                                'Tab'
                            )
                        "
                    >
                        Tab
                    </button>


                    <button
                        onclick="
                            sendKey(
                                'Escape'
                            )
                        "
                    >
                        Esc
                    </button>


                </div>


                <div class="row">


                    <button
                        onclick="
                            sendKey(
                                'Control+A'
                            )
                        "
                    >
                        全选
                    </button>


                    <button
                        onclick="
                            sendKey(
                                'Backspace'
                            )
                        "
                    >
                        删除
                    </button>


                </div>

            </div>


            <div class="group">


                <div class="note">

                    <b>
                        现在可以随时切手机 / 桌面。
                    </b>

                    <br>

                    美团这类 H5 默认走手机；

                    小红书这种移动网页喜欢把内容
                    赶去 App 的，就先切桌面再试。

                    <br><br>

                    AI 仍会拦截验证码、密码、
                    2FA、付款、转账、购买等最终动作，

                    这些由你本人从前端接管。

                </div>


            </div>


        </aside>


    </div>


</div>


<script>


let token =
    sessionStorage.getItem(
        'browser_token'
    )
    || '';


let refreshing =
    true;


let currentMode =
    'mobile';


function headers(){

    return {

        'X-Dashboard-Token':
            token,

        'Content-Type':
            'application/json'

    };

}


async function req(
    url,
    opts={}
){

    opts.headers = {

        ...(
            opts.headers
            || {}
        ),

        ...headers()

    };


    const r =
        await fetch(
            url,
            opts
        );


    if(
        r.status
        ===
        401
    ){

        sessionStorage.removeItem(
            'browser_token'
        );

        overlay.classList.remove(
            'hidden'
        );

        throw Error(
            '口令不正确'
        );

    }


    if(
        !r.ok
    ){

        let msg =
            '请求失败 '
            +
            r.status;


        try{

            const j =
                await r.json();


            if(
                j.detail
            ){

                msg =
                    String(
                        j.detail
                    );

            }

        }catch{}


        throw Error(
            msg
        );

    }


    return r;

}


async function login(){

    token =
        document
        .getElementById(
            'token'
        )
        .value
        .trim();


    try{

        await req(
            '/api/browser/status'
        );


        sessionStorage.setItem(

            'browser_token',

            token

        );


        overlay.classList.add(
            'hidden'
        );


        refreshStatus();

        refreshScreen();


    }catch(e){

        err.textContent =
            e.message;

    }

}


async function post(
    url,
    body={}
){

    const r =
        await req(

            url,

            {

                method:
                    'POST',

                body:
                    JSON.stringify(
                        body
                    )

            }

        );


    let data =
        null;


    try{

        data =
            await r.json();

    }catch{}


    setTimeout(

        ()=>{

            refreshStatus();

            refreshScreen();

        },

        220

    );


    return data;

}


function paintMode(
    mode
){

    currentMode =
        mode
        || 'mobile';


    mobileBtn.classList.toggle(

        'active',

        currentMode
        ===
        'mobile'

    );


    desktopBtn.classList.toggle(

        'active',

        currentMode
        ===
        'desktop'

    );

}


async function refreshStatus(){

    try{

        const j =
            await (
                await req(
                    '/api/browser/status'
                )
            ).json();


        const vp =
            j.viewport
            || {};


        dot.classList.toggle(

            'ok',

            !!j.alive

        );


        paintMode(
            j.mode
        );


        status.innerHTML =

            '<b>'
            +
            (
                j.title
                ||
                '无标题'
            )
            +
            '</b><br>'
            +
            (
                j.url
                ||
                ''
            )
            +
            '<br>mode='
            +
            (
                j.mode
                ||
                ''
            )
            +
            ' · '
            +
            (
                vp.width
                ||
                '?'
            )
            +
            '×'
            +
            (
                vp.height
                ||
                '?'
            );


    }catch{

        dot.classList.remove(
            'ok'
        );

    }

}


async function refreshScreen(){

    if(
        !refreshing
    ){
        return;
    }


    try{

        const r =
            await req(

                '/api/browser/screenshot?t='
                +
                Date.now()

            );


        const blob =
            await r.blob();


        const old =
            img.src;


        img.src =
            URL.createObjectURL(
                blob
            );


        img.onload =
            ()=>{

                if(
                    old.startsWith(
                        'blob:'
                    )
                ){

                    URL.revokeObjectURL(
                        old
                    );

                }

            };


    }catch{}

}


img.addEventListener(

    'click',

    async e=>{


        const r =
            img.getBoundingClientRect();


        const x =
            (
                e.clientX
                -
                r.left
            )
            /
            r.width;


        const y =
            (
                e.clientY
                -
                r.top
            )
            /
            r.height;


        const d =
            document.createElement(
                'div'
            );


        d.className =
            'tap';


        d.style.left =
            (
                x
                *
                100
            )
            +
            '%';


        d.style.top =
            (
                y
                *
                100
            )
            +
            '%';


        shell.appendChild(
            d
        );


        setTimeout(

            ()=>d.remove(),

            350

        );


        await post(

            '/api/browser/click',

            {
                x,
                y
            }

        );

    }

);


async function setMode(
    mode
){

    paintMode(
        mode
    );


    modeHelp.textContent =

        '正在切换到 '

        +

        (
            mode
            ===
            'desktop'

            ?

            '桌面'

            :

            '手机'
        )

        +

        ' 模式…';


    try{


        const data =
            await post(

                '/api/browser/mode',

                {
                    mode
                }

            );


        if(
            data
            &&
            data.ok
            ===
            false
        ){

            throw Error(

                data.reason
                ||
                '切换失败'

            );

        }


        modeHelp.textContent =

            '已切换到 '

            +

            (
                mode
                ===
                'desktop'

                ?

                '桌面'

                :

                '手机'
            )

            +

            ' 模式；继续使用同一个 profile。';


        await refreshStatus();

        await refreshScreen();


    }catch(e){


        modeHelp.textContent =

            '切换失败：'

            +

            e.message;


    }

}


async function quickGoto(
    url,
    mode
){

    document
    .getElementById(
        'url'
    )
    .value =
        url;


    if(
        mode
        &&
        mode
        !==
        currentMode
    ){

        await setMode(
            mode
        );

    }


    await post(

        '/api/browser/goto',

        {
            url
        }

    );

}


async function manualGoto(){

    let url =
        document
        .getElementById(
            'url'
        )
        .value
        .trim();


    if(
        !url
    ){
        return;
    }


    if(
        !/^https?:\/\//i.test(
            url
        )
    ){

        url =
            'https://'
            +
            url;

    }


    await post(

        '/api/browser/goto',

        {
            url
        }

    );

}


async function scrollRemote(
    delta_y
){

    await post(

        '/api/browser/scroll',

        {
            delta_y
        }

    );

}


async function typeText(){

    const el =
        document
        .getElementById(
            'text'
        );


    if(
        !el.value
    ){
        return;
    }


    await post(

        '/api/browser/type',

        {
            text:
                el.value
        }

    );


    el.value =
        '';

}


async function sendKey(
    key
){

    await post(

        '/api/browser/key',

        {
            key
        }

    );

}


function toggleMask(){

    const el =
        document
        .getElementById(
            'text'
        );


    el.type =

        el.type
        ===
        'password'

        ?

        'text'

        :

        'password';


    mask.textContent =

        el.type
        ===
        'password'

        ?

        '显示文字'

        :

        '隐藏文字';

}


function toggleRefresh(){

    refreshing =
        !refreshing;


    pause.textContent =

        refreshing

        ?

        '暂停画面'

        :

        '继续画面';


    if(
        refreshing
    ){

        refreshScreen();

    }

}


if(
    token
){

    overlay.classList.add(
        'hidden'
    );

    refreshStatus();

    refreshScreen();

}


setInterval(
    refreshScreen,
    900
);


setInterval(
    refreshStatus,
    4500
);


</script>


</body>

</html>
'''
