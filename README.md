# Grok Register Win（本地版）

Windows 双击运行的 Grok 注册机面板：

- **代理**：使用你本机已安装的 Clash（不内置 mihomo、不内置订阅）
- **自动探测代理端口**（常见 `7897` / `7890` 等，Clash Verge 多为 7897）
- **注册**：浏览器自动化注册
- **邮箱**：面板可选 Tempmailer / 自定义自建 API（InboxKitten 已移除：xAI 拒绝该域名）
- **自动转 CPA**：注册成功后后台把 web SSO 换成 CLIProxyAPI 可用的 OAuth JSON
- **下载**：
  - SSO TXT：`email----password----sso`
  - CPA ZIP：`xai-*.json`（`auth_kind=oauth`）
- **账号文件**：可勾选删除，避免重复下载

> 仅供学习研究。自动化注册可能违反平台条款，风险自负。

---

## 环境要求

1. **Windows 10/11**
2. **Python 3.10+**（安装时勾选 Add python.exe to PATH）
3. **本机 Clash**（Clash Verge / CFW / mihomo 客户端均可）
   - Clash Verge 默认 mixed 端口常见为 **7897**（不一定是 7890）
   - 订阅、节点切换请在 **Clash 客户端** 里完成
4. **Chrome 或 Edge**

---

## 快速开始

1. 下载本仓库 ZIP 并解压  
2. **先打开 Clash**，更新订阅，选一个能用的节点  
3. 双击 **`start.bat`**（推荐；`启动.bat` 也会转调它）  
   - 首次会自动创建 `.venv` 并安装依赖，窗口会保留，不要关  
   - 若失败，看 `data\logs\start.log`  
4. 浏览器自动打开：http://127.0.0.1:8787  
5. 默认密码：`admin`  
6. 在「邮箱服务」里选 Tempmailer 或自定义并保存  
7. 点 **开始注册**  
8. 下载 SSO / CPA；不需要的账号文件可勾选 **删除选中**

> 若双击窗口一闪就没：请用 `start.bat`，并确认已装 Python 3.10+。

---

## 配置

首次运行会从 `config.example.json` 生成 `config.json`。

常用字段：

```json
{
  "proxy": "http://127.0.0.1:7897",
  "allow_proxy_fallback": false,
  "email_provider": "tempmailer",
  "email_failover": true,
  "register_count": 1
}
```

- `allow_proxy_fallback`：代理失败是否回退直连（默认 `false`，建议保持关闭）  
- 启动时若配置的端口不通，会自动探测本机常见 Clash 端口并写回 `config.json`

### 邮箱

| 选项 | 说明 |
|------|------|
| Tempmailer | 内置免 key（默认 `bluenode.cc`） |
| 自定义 | 自建临时邮 API（兼容 cloudflare_temp_email）：API 根地址 / Key / 域名 / 路径 |

> **InboxKitten 已移除**：`inboxkitten.com` 域名会被 xAI 直接拒绝注册，请使用 Tempmailer 或自有域名。

自定义需服务支持「创建地址」和「收信读验证码」。

可选环境变量（高级）：

| 变量 | 含义 | 默认 |
|------|------|------|
| `PANEL_PASSWORD` | 面板密码 | `admin` |
| `PANEL_PORT` | 面板端口 | `8787` |
| `GROK_PROXY` | 覆盖代理 | 读 config.json |

面板默认只监听 `127.0.0.1`。

---

## 目录结构

```
grok-register-win/
  start.bat / 启动.bat  # 双击启动
  setup.bat             # 仅安装依赖
  launcher.py           # 启动器（代理探测）
  grok_register_ttk.py  # 注册机
  config.example.json
  panel/app.py          # Web 面板
  lib/sso2cpa_core.py   # SSO → CPA
  data/logs/            # 运行日志
  data/cpa/             # 已转换 CPA JSON
  accounts_*.txt        # 注册产出（可勾选删除）
```

---

## 常见问题

### 代理端口不通 / WinError 10061
- 先开 Clash  
- Clash Verge 常见端口 **7897**，不是 7890  
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

MIT（若上游组件另有协议，以对应文件为准）
