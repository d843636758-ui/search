# search
通用浏览器 MCP v0.2

这一版把原来的“美团浏览器 MCP”改成了 通用远程浏览器 MCP。

现在不只美团

前端可以直接打开任意普通公网 http/https 网站，并保留同一个 Chromium 登录状态。
美团、淘宝、京东、小红书只是快捷入口，不是限制。

MCP 主要工具：

• browser_status
• browser_open(url)
• browser_goto(url)
• browser_read_page
• browser_list_controls
• browser_click_text
• browser_fill
• browser_press
• browser_back
• browser_reload

前端

访问部署后的根地址：

```text
https://你的域名/
```

输入 DASHBOARD_TOKEN 后，可以：

• 在地址栏打开任意网址
• 直接点击远程浏览器画面
• 滚动、后退、刷新
• 手动输入手机号、验证码、密码等
• 自己完成购买、付款等最终确认

MCP 安全边界

AI 自动导航允许公网 http/https 网站，但默认禁止：

• localhost
• .local
• 私网 / 回环 / 链路本地等直接 IP

如果想限制 AI 只能访问某些站点：

```text
BROWSER_ALLOWED_HOSTS=meituan.com,taobao.com,jd.com
```

如果希望允许任意公网网站：

```text
BROWSER_ALLOWED_HOSTS=*
```

MCP 自动操作还会阻止常见的：

• 验证码、密码、2FA
• 银行卡、CVV、支付字段
• 提交订单、付款
• 转账、充值、提现
• 购买、订阅
• 删除 / 注销账号

这些动作仍可由你本人从前端手动完成。

Zeabur 环境变量

```text
DASHBOARD_TOKEN=<至少32位随机字符串>
MCP_PATH_TOKEN=<另一串随机字符串>
PORT=8080
BROWSER_PROFILE_DIR=/data/browser-profile
HEADLESS=true
BROWSER_START_URL=about:blank
BROWSER_ALLOWED_HOSTS=*
```

请给 /data 挂持久卷，不然容器重建后浏览器登录态会丢。

MCP 地址

```text
https://你的域名/mcp/<MCP_PATH_TOKEN>/
```

现实限制

“浏览器能开”不等于所有网站都一定能稳定自动化。以下情况可能需要人工接管或单站适配：

• 强验证码 / 人机验证
• Headless 检测
• Passkey / WebAuthn
• DRM
• 必须拉起手机原生 App 的支付或登录
• 同一台手机无法扫描远程网页里的二维码

遇到这些时不绕验证，直接在前端接管即可。
