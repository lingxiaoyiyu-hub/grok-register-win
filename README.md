<div align="center">

# Grok Register Win

![Banner](docs/banner.png)

### Grok（xAI）账号自动注册面板

[![Version](https://img.shields.io/badge/version-v2.0.0-blue?style=for-the-badge)](https://github.com/lingxiaoyiyu-hub/grok-register-win/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-yellow?style=for-the-badge)](https://www.python.org/downloads/)
[![Stars](https://img.shields.io/github/stars/lingxiaoyiyu-hub/grok-register-win?style=for-the-badge&label=stars)](https://github.com/lingxiaoyiyu-hub/grok-register-win/stargazers)
[![Downloads](https://img.shields.io/github/downloads/lingxiaoyiyu-hub/grok-register-win/total?style=for-the-badge&label=downloads)](https://github.com/lingxiaoyiyu-hub/grok-register-win/releases)

<br>

**自动注册 · 多邮箱源 · SSO→CPA · NSFW · 多格式导出**

<br>

[📥 快速开始](#快速开始) ·
[✨ 功能](#功能) ·
[📦 发行包](#发行包) ·
[⚙️ 配置](#配置) ·
[❓ 常见问题](#常见问题)

</div>

---

## ⚠️ 免责声明

> 本项目仅供学习研究。自动化注册可能违反平台条款，风险自负。

---

## ✨ 功能

- **代理**：复用 Clash / mihomo 代理（常见端口 `7897`）
- **注册引擎**：Chromium 有头 / Camoufox 无头（面板可切换；服务器版仅无头）
- **邮箱**：多源下拉（CF Worker、MoeMail、LuckMail、MaliAPI、SkyMail、CloudMail、Freemail、OpenTrashMail、Laoudo 等）
- **SSO → CPA**：注册成功后自动换成 CLIProxyAPI 可用的 OAuth JSON
- **上传 SSO 续期**：上传 SSO txt 或 CPA `all.json`，整批换票；旧批次归档到 `data/archive/`
- **NSFW**：注册成功后自动设置相关偏好
- **产物下载**（同一批账号，三种格式）：
  - SSO TXT：`email----password----sso`
  - CPA ZIP：`xai-*.json`（CLIProxyAPI）
  - Sub2 ZIP：Sub2API 导入包（`grok-*.json` + `all.json`）
- **账号管理**：面板勾选删除；注册 / 上传工作区隔离

---

## 📦 发行包

在 [Releases · v2.0.0](https://github.com/lingxiaoyiyu-hub/grok-register-win/releases/tag/v2.0.0) 下载：

| 包 | 适用 | 说明 |
| :--- | :--- | :--- |
| **Source code** / 常规源码 | Windows 本地 | 使用本机 Clash 管理节点；双击 `start.bat` |
| **`grok-register-server-v2.0.0-mihomo.zip`** | Linux 服务器 | **内置 mihomo**、面板填订阅链接、**仅 Camoufox 无头**；`./start.sh` |

服务器包详情见压缩包内 `SERVER_README.md`。

---

## 📋 环境要求

### 本地版（Windows）

| 项 | 说明 |
| :--- | :--- |
| 系统 | Windows 10 / 11 |
| Python | 3.10+（勾选 *Add python.exe to PATH*） |
| 代理 | 本机 Clash（Verge / CFW / mihomo 等），在客户端选节点 |
| 浏览器 | Chrome / Edge（有头）；Camoufox 可选，首次自动下载 |

### 服务器版（Linux）

| 项 | 说明 |
| :--- | :--- |
| 系统 | Linux x86_64（无需桌面） |
| Python | 3.10+ |
| 代理 | 包内 `bin/mihomo`，面板填写订阅即可 |
| 浏览器 | 仅 Camoufox 无头 |

---

## 🚀 快速开始

### Windows 本地

1. 下载源码 ZIP 并解压  
2. 打开本机 Clash，选可用节点（建议日本）  
3. 双击 `start.bat`（首次会建 `.venv` 装依赖）  
4. 打开 http://127.0.0.1:8787  
5. 配置邮箱 → 开始注册，或上传 SSO 续期  
6. 下载 SSO / CPA / Sub2  

失败日志：`data\logs\start.log`

### Linux 服务器

```bash
unzip grok-register-server-v2.0.0-mihomo.zip
cd grok-register-server
chmod +x start.sh bin/mihomo
./start.sh
```

打开 `http://服务器IP:8787` → 配邮箱 → **订阅链接 → 更新订阅 → 选节点** → 开始注册。

---

## ⚙️ 配置

首次运行从 `config.example.json` 生成 `config.json`（勿把含密钥的配置公开上传）。

```json
{
  "proxy": "http://127.0.0.1:7897",
  "allow_proxy_fallback": false,
  "browser_engine": "chromium",
  "email_provider": "cfworker",
  "email_failover": true,
  "cfworker_api_url": "https://apimail.example.com",
  "cfworker_admin_token": "your-admin-token",
  "cfworker_domain": "mail.example.com",
  "register_count": 1,
  "round_timeout_sec": 300
}
```

| 字段 | 说明 |
| :--- | :--- |
| `proxy` | HTTP 代理地址；端口不通时启动可能自动探测 |
| `allow_proxy_fallback` | 代理失败是否直连，默认 `false` |
| `browser_engine` | `chromium` 或 `camoufox`（服务器版固定 `camoufox`） |
| `email_provider` | 邮箱源 id |
| `email_failover` | 失败时是否换源 |
| `register_count` | 单次任务注册数量 |
| `round_timeout_sec` | 单账号硬超时（秒），默认 `300` |

服务器版额外常用字段：`clash_subscription`、`clash_mixed_port`、`use_embedded_clash` 等（见 `config.example.json`）。

### 邮箱源

| 标识 | 说明 |
| :--- | :--- |
| `cfworker` | CF Worker / 自建域名（推荐） |
| `moemail` | MoeMail API |
| `maliapi` | MaliAPI / YYDS |
| `luckmail` | LuckMail |
| `skymail` / `cloudmail` | SkyMail / CloudMail |
| `freemail` | Freemail |
| `opentrashmail` | OpenTrashMail |
| `laoudo` | Laoudo 固定邮箱 |

### 环境变量（可选）

| 变量 | 含义 | 默认 |
| :--- | :--- | :--- |
| `PANEL_AUTH` | `1` 开启登录 | `0` |
| `PANEL_PASSWORD` | 登录密码 | `admin` |
| `PANEL_HOST` | 监听地址 | 本地 `127.0.0.1` / 服务器常用 `0.0.0.0` |
| `PANEL_PORT` | 端口 | `8787` |
| `GROK_PROXY` | 覆盖代理 | — |
| `ROUND_TIMEOUT_SEC` | 覆盖单轮超时 | `300` |

---

## 📂 目录结构

```
grok-register-win/
├── start.bat / start.sh      # 启动入口
├── launcher.py
├── grok_register_ttk.py      # 注册主程序
├── config.example.json
├── panel/app.py              # Web 面板
├── lib/                      # SSO→CPA、邮箱、Camoufox 等
├── bin/mihomo                # 仅服务器发行包内置
├── data/
│   ├── logs/
│   ├── cpa/
│   ├── clash/                # 服务器版 mihomo 运行时
│   └── archive/
└── accounts_*.txt
```

---

## ❓ 常见问题

<details>
<summary><b>代理端口不通</b></summary>

- 本地：确认 Clash 已启动，端口多为 `7897`  
- 服务器：在面板填写订阅并点「更新订阅」，确认混合端口与 `proxy` 一致  
</details>

<details>
<summary><b>注册失败多 / 验证码异常</b></summary>

- 多数与出口网络有关；**日本节点**通常更稳  
- 换节点后重试；单账号超时默认 5 分钟会进入下一轮  
</details>

<details>
<summary><b>收不到验证码</b></summary>

- 检查邮箱源 API / Token / 域名是否正确  
- 优先使用可正常收 xAI 邮件的自建或付费源  
</details>

<details>
<summary><b>上传 SSO 续期</b></summary>

- 支持 `email----password----sso` / `email----sso` / 纯 SSO，以及含 `sso` 的 CPA `all.json`  
- 上传会替换当前工作区，旧数据在 `data/archive/`  
</details>

<details>
<summary><b>依赖安装失败</b></summary>

```bash
# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# Linux
.venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```
</details>

---

## 📝 更新日志

版本变更、移除项、修复说明见 [Releases](https://github.com/lingxiaoyiyu-hub/grok-register-win/releases) 与 `docs/RELEASE_NOTES_*.md`。

<details>
<summary><b>展开近期版本摘要</b></summary>

### v2.0.0
- 面板 Atelier 风格重做、全中文化
- 发行附加包：服务器版（内置 mihomo、订阅、仅无头）

### v1.4.0 ~ v1.3.0
- 邮箱源调整、上传 SSO 整批续期、下载与面板账号对齐等  

完整条目见各 Release 说明。
</details>

---

## 💬 反馈与支持

| 类型 | 途径 |
| :--- | :--- |
| Bug | [Issue](https://github.com/lingxiaoyiyu-hub/grok-register-win/issues/new?template=bug_report.yml) |
| 建议 | [Issue](https://github.com/lingxiaoyiyu-hub/grok-register-win/issues/new?template=feature_request.yml) |
| 讨论 | [Discussions](https://github.com/lingxiaoyiyu-hub/grok-register-win/discussions) |

---

## 🤝 贡献

欢迎 Issue / PR。参与前请阅读 [贡献指南](CONTRIBUTING.md) 与 [行为准则](CODE_OF_CONDUCT.md)。

---

## License

[MIT License](LICENSE)

---

<div align="center">

**如果这个项目对你有帮助，欢迎 Star ⭐**

</div>
