# Grok Register Win（本地版）

Windows 双击运行的 Grok 注册机面板：

- **代理**：使用本机已安装的 Clash，自动探测代理端口（Clash Verge 常见 `7897`）
- **注册**：浏览器自动化注册，支持 Chromium 有头 / Camoufox 无头反检测
- **邮箱**：内置 Tempmailer，或自定义自建临时邮 API
- **自动转 CPA**：注册成功后后台把 web SSO 换成 CLIProxyAPI 可用的 OAuth JSON
- **NSFW 自动开启**：注册成功后自动设置 ToS、生日、NSFW 偏好
- **下载**：
  - SSO TXT：`email----password----sso`
  - CPA ZIP：`xai-*.json`（`auth_kind=oauth`）
- **账号文件**：可勾选删除，避免重复下载

> 仅供学习研究。自动化注册可能违反平台条款，风险自负。

---

## 更新日志

### v1.0.5（2026-07-16）

**修复：**
- **CPA 转换 404 "Server action not found"**：consent 页面已改为标准 HTML 表单 POST 到 `auth.x.ai/oauth2/authorize`，不再使用 Next.js Server Action。重写 consent 提交逻辑为 `application/x-www-form-urlencoded` 表单提交，从 302 重定向 `Location` header 提取 OAuth code。
- **NSFW 开启 GBK 编码崩溃**：Windows 控制台默认 GBK 编码遇到 Unicode 字符（`\ufffd`）时 `print()` 崩溃。在 `launcher.py`、`grok_register_ttk.py`、`panel/app.py` 三个入口文件强制 `stdout/stderr` 使用 UTF-8 + `errors="replace"`。
- **子进程 stdout GBK 解码崩溃**：面板读取注册子进程输出时 `UnicodeDecodeError`。改为 `encoding="utf-8", errors="replace"`。
- **Playwright 驱动崩溃（coreBundle.js）**：Camoufox/Firefox 页面产生未捕获 JS 错误且 `pageError.location` 为 `undefined` 时，Playwright Node.js 驱动进程崩溃，导致浏览器在 SSO 提取阶段死亡。新增 `lib/patch_playwright.py` 自动修补 `pageError.location?.url` 和 `tString` 验证器默认值。

**改进：**
- **提交按钮检测扩展**：注册流程中 "You are signing into" 确认页面找不到提交按钮。选择器新增 `<a href>` 标签，文本模式新增 `continue`/`signin`/`proceed`/`confirm`/`next`/`agree`/`authorize`/`allow`/`继续`，排除 `go back`/`返回`。
- **浏览器崩溃快速失败**：Camoufox 后端检测到 `connection closed`/`browser closed`/`pipe closed` 等致命错误后立即标记死亡，后续操作快速失败而非反复超时等待。
- **CPA 调试支持**：consent 页面提取失败时自动 dump HTML 到 `%TEMP%\sso2cpa_consent_debug.html`，错误消息包含 `action_id` 和 `redirect_url` 便于诊断。

### v1.0.4（2026-07-16）

**新功能：**
- **Camoufox 无头浏览器引擎**：面板可切换「Camoufox 无头」引擎，基于 Firefox 反检测浏览器，无头运行不占用桌面
  - 自动 GeoIP 对齐：根据出口 IP 自动匹配浏览器时区、语言、地理位置
  - 首次使用自动下载 Firefox 二进制和 GeoLite2 数据库
  - 与原有 Chromium 有头引擎二选一，面板下拉框切换

**修复：**
- 新增 `lib/patch_playwright.py`：启动时自动修补 Playwright `coreBundle.js` 的 `pageError.location` 崩溃
- `start.bat` 和 `launcher.py` 在依赖安装后自动执行 patch（幂等，已修补不会重复）
- 扩展提交按钮检测逻辑（`<a>` 标签 + 更多文本模式）
- Camoufox 浏览器崩溃快速失败机制
- `.gitignore` 新增 `mail_credentials.txt`

---

## 环境要求

1. **Windows 10/11**
2. **Python 3.10+**（安装时勾选 Add python.exe to PATH）
3. **本机 Clash**（Clash Verge / CFW / mihomo 客户端均可）
   - Clash Verge 默认 mixed 端口常见为 **7897**
   - 订阅、节点切换请在 **Clash 客户端** 里完成
4. **Chrome 或 Edge**（默认 Chromium 有头引擎）
5. **可选 Camoufox 无头**：面板可切换「Camoufox 无头」；首次使用会自动下载 Firefox 二进制

---

## 快速开始

1. 下载本仓库 ZIP 并解压  
2. **先打开 Clash**，更新订阅，选一个能用的节点  
3. 双击 **`start.bat`**（推荐；`启动.bat` 也会转调它）  
   - 首次会自动创建 `.venv` 并安装依赖，窗口会保留，不要关  
   - 若失败，看 `data\logs\start.log`  
4. 浏览器自动打开：http://127.0.0.1:8787（**无需登录密码**，直接进面板）  
5. 在「邮箱服务」里选 Tempmailer 或自定义并保存  
6. 点 **开始注册**  
7. 下载 SSO / CPA；不需要的账号文件可勾选 **删除选中**

> 若双击窗口一闪就没：请用 `start.bat`，并确认已装 Python 3.10+。

---

## 配置

首次运行会从 `config.example.json` 生成 `config.json`。

常用字段：

```json
{
  "proxy": "http://127.0.0.1:7897",
  "allow_proxy_fallback": false,
  "browser_engine": "chromium",
  "email_provider": "tempmailer",
  "email_failover": true,
  "register_count": 1
}
```

- `allow_proxy_fallback`：代理失败是否回退直连（默认 `false`，建议保持关闭）  
- `browser_engine`：`chromium`（有头，默认）或 `camoufox`（无头反检测 Firefox）  
- 面板「启动注册」里可用下拉框二选一；启动任务前会写回 `config.json`  
- Camoufox 首次运行会自动执行 `python -m camoufox fetch` 下载浏览器（体积较大）  
- 启动时若配置的端口不通，会自动探测本机常见 Clash 端口并写回 `config.json`

### 邮箱

| 选项 | 说明 |
|------|------|
| Tempmailer | 内置免 key（默认 `bluenode.cc`） |
| 自定义 | 自建临时邮 API（兼容 cloudflare_temp_email）：API 根地址 / Key / 域名 / 路径 |

自定义需服务支持「创建地址」和「收信读验证码」。

可选环境变量（高级）：

| 变量 | 含义 | 默认 |
|------|------|------|
| `PANEL_AUTH` | 是否开启登录（`1` 开启） | `0`（关闭，免密） |
| `PANEL_PASSWORD` | 登录密码（仅 `PANEL_AUTH=1` 时有效） | `admin` |
| `PANEL_PORT` | 面板端口 | `8787` |
| `GROK_PROXY` | 覆盖代理 | 读 config.json |

面板默认只监听 `127.0.0.1`，且**默认不需要密码**。若要加密码：

```powershell
$env:PANEL_AUTH="1"
$env:PANEL_PASSWORD="你的密码"
.\start.bat
```

---

## 目录结构

```
grok-register-win/
  start.bat / 启动.bat  # 双击启动
  setup.bat             # 仅安装依赖
  launcher.py           # 启动器（代理探测、Playwright 修补）
  grok_register_ttk.py  # 注册机
  config.example.json
  panel/app.py          # Web 面板
  lib/sso2cpa_core.py   # SSO → CPA
  lib/camoufox_backend.py  # Camoufox 无头适配层
  lib/patch_playwright.py  # Playwright 驱动崩溃自动修补
  data/logs/            # 运行日志
  data/cpa/             # 已转换 CPA JSON
  accounts_*.txt        # 注册产出（可勾选删除）
```

---

## 常见问题

### 代理端口不通 / WinError 10061
- 先开 Clash  
- Clash Verge 默认端口 **7897**  
- 或改 `config.json` 的 `proxy`，重启 `start.bat`（也会自动探测）

### 卡在 Cookie / 拿不到 SSO
- 已会尝试自动点「接受所有 Cookie」  
- 仍失败时换节点重试  

### 依赖安装失败
```bat
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## License

本项目基于 [MIT License](LICENSE) 发布。若上游组件另有协议，以对应文件为准。
