# Grok Register Win v1.3.1

## 修复

### 下载数量 = 面板当前账号数

- **下载 CPA / Sub2** 只打包当前面板账号列表中的凭证
- 删除 `accounts_*.txt` 时同步清理无主 CPA JSON
- 避免「面板已删号，下载仍全量打包」

## 升级

1. 下载本 Release 的 Source code (zip)，或 `git pull` / 检出 `v1.3.1`
2. 双击 `start.bat`
3. 浏览器 **Ctrl+F5** 强制刷新面板

## 相对 v1.3.0

- v1.3.0：上传 SSO / all.json 整批续期、工作区隔离
- v1.3.1：下载与删除严格对齐当前面板账号
