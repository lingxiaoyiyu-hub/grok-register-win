# Grok Register Win（本地版）

Windows 双击运行的 Grok 注册机面板：

- **代理**：使用你本机已安装的 Clash（不内置 mihomo、不内置订阅）
- **注册**：浏览器自动化注册
- **自动转 CPA**：注册成功后后台把 web SSO 换成 CLIProxyAPI 可用的 OAuth JSON
- **下载**：
  - SSO TXT：`email----password----sso`
  - CPA ZIP：`xai-*.json`（`auth_kind=oauth`）

> 仅供学习研究。自动化注册可能违反平台条款，风险自负。

---

## 环境要求

1. **Windows 10/11**
2. **Python 3.10+**（安装时勾选 Add python.exe to PATH）
3. **本机 Clash**（Clash Verge / CFW / mihomo 客户端均可）
   - HTTP 或 mixed 端口默认 **7890**
   - 订阅、节点切换请在 **Clash 客户端** 里完成
4. **Chrome 或 Edge**

---

## 快速开始

1. 下载本仓库 ZIP 并解压（路径尽量不要有奇怪权限限制）  
2. **先打开 Clash**，更新订阅，选一个能用的节点  
3. 双击 **`start.bat`**（推荐；`启动.bat` 也会转调它）  
   - 首次会自动创建 `.venv` 并安装依赖，窗口会保留，不要关  
   - 若失败，看 `data\logs\start.log`  
4. 浏览器自动打开：http://127.0.0.1:8787  
5. 默认密码：`admin`  
6. 点 **开始注册**  
7. 下载：
   - **下载 SSO (TXT)**
   - **下载 CPA (JSON)**

> 若双击窗口一闪就没：请用 `start.bat`，不要只依赖中文文件名；并确认已装 Python 3.10+。

---

## 配置

首次运行会从 `config.example.json` 生成 `config.json`。

常用字段：

```json
{
  "proxy": "http://127.0.0.1:7890",
  "email_provider": "tempmailer",
  "register_count": 1
}
```

若 Clash 端口不是 7890，改 `proxy` 即可。

可选环境变量（高级）：

| 变量 | 含义 | 默认 |
|------|------|------|
| `PANEL_PASSWORD` | 面板密码 | `admin` |
| `PANEL_PORT` | 面板端口 | `8787` |
| `GROK_PROXY` | 覆盖代理 | 读 config.json |
| `CLASH_API` | 本机 Clash 控制器 | `http://127.0.0.1:9090` |
| `ENABLE_CLASH_UI` | 是否尝试读节点列表 | `1` |

说明：即使没有开 Clash 的 external-controller（9090），只要 **7890 代理可用** 也能注册。  
节点请在 Clash 客户端里自己选。

---

## 目录结构

```
grok-register-win/
  启动.bat              # 双击启动
  setup.bat             # 仅安装依赖
  launcher.py           # 启动器
  grok_register_ttk.py  # 注册机
  config.example.json
  panel/app.py          # Web 面板
  lib/sso2cpa_core.py   # SSO → CPA 转换核心
  data/
    logs/               # 运行日志
    cpa/                # 已转换的 CPA JSON
  accounts_*.txt        # 注册成功产出的 SSO
```

---

## 常见问题

### 代理端口不通
- 先开 Clash  
- 确认 mixed-port / HTTP 端口是 7890  
- 或改 `config.json` 的 `proxy`

### 注册页打不开 / 找不到「使用邮箱注册」
- 多半是 **节点失效**  
- 在 Clash 里换节点、更新订阅后再试  

### CPA zip 是空的
- 先有成功注册（有 `accounts_*.txt`）  
- 转换需要代理能访问 xAI OAuth  
- 可点面板「补转未转换 CPA」  

### 依赖安装失败
- 用管理员或换国内 pip 源：
  ```bat
  .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  ```

---

## 安全提示

- 默认只监听 `127.0.0.1`，不暴露到公网  
- 不要把 `config.json`、账号文件、CPA JSON 提交到公开仓库  
- 面板密码请自行修改（环境变量 `PANEL_PASSWORD`）

---

## License

MIT（若上游组件另有协议，以对应文件为准）
