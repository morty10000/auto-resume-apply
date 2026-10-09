# 全自动投递简历系统

> 本地运行的四平台求职自动化工具：**采集岗位 → 简历匹配 → 自动投递**，全程可视化，只用系统 Edge，数据不出本机。

支持平台：**Boss直聘 · 智联招聘 · 51job · 猎聘**　｜　系统要求：**Windows 10 / 11（64 位）**

---

## ✨ 功能特性

- **岗位采集**：四平台并行；原生标签页 + 页面内请求（非爬虫协议），按平台差异化的人工节奏防风控
- **简历解析**：支持 PDF / Word / 图片版 PDF（自动 OCR），提取技能 / 学历 / 经验
- **智能匹配**：技能覆盖 40% + 职位名相似 30% + 薪资区间 15% + HR活跃/福利/企业加分 15%；
  硬过滤（城市 / 学历 / 经验 / 标题黑名单），阈值可调
- **自动投递**：按分数从高到低，平台独立每日限额与投递节奏；遇验证码自动跳过（人工处理后 可续投）
- **运行监控**：实时阶段 / 当前动作 / 进度 / 日志；随时暂停、停止，已处理的数据全部保留；
  任务由服务端自驱接力（采集 → 匹配 → 投递），**刷新 / 关闭页面都不影响运行**
- **6 套界面皮肤**：新拟物 / 新野兽派 / 编辑杂志 / 便当盒 / 暗黑模式 / 企业简洁，标签栏一键切换
- **数据全本地**：登录态、简历、采集与投递记录只保存在本机 `data/` 目录

## 📦 下载与安装

### 方式一：便携版（推荐，免安装）

到 **Releases** 下载便携版压缩包（`auto-resume-apply-<版本>-portable.zip`）→ 解压到任意目录 → 双击 `start.bat`。
包内自带完整 Python 运行环境，**无需安装任何东西**；只要求系统自带 Microsoft Edge。

> zip 约 200MB（内含完整运行环境），解压后约 550MB。

### 方式二：从源码运行（开发者）

需要 Python 3.10 环境（或直接使用便携包里的 `runtime/` 目录）：

```powershell
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

或双击 `start.bat`（存在 `runtime\` 时优先使用，否则回退系统 Python）。然后浏览器打开
http://127.0.0.1:8000 。（源码仓库不含 `runtime/` 与 `data/`，均按需生成 / 从 Releases 获取。）

## 🚀 快速上手

1. 双击 `start.bat` —— 自动启动服务并打开「专用 Edge 窗口」显示操作界面
2. 主页四个平台卡片逐个点「去登录」（在专用窗口内完成，一次登录长期有效）
3. 「简历」页上传简历 →「采集设置」页填写岗位关键词与城市
4. 主页点「⚡ 一键全流程」→「运行监控」页实时查看进度与日志

完整说明见 **[使用说明.md](使用说明.md)**（含防风控建议 / 数据位置 / FAQ）。

## 🔒 数据与隐私

- 所有数据（登录态 / 简历 / 岗位 / 投递记录 / 配置）只保存在本机 `data/` 目录，**不上传任何第三方**
- 本仓库通过 `.gitignore` 排除了 `data/`、`runtime/`、`dist/` 与全部本机开发脚本，
  仓库内**不含任何使用者的个人数据**
- 想彻底重置：退出程序后删除 `data/` 目录即可（下次启动自动重建）

## 🗂 项目结构

```
backend/    FastAPI 服务端（api 路由 / platforms 四平台适配器 / services 浏览器与匹配 / db 数据层）
frontend/   原生 JS 单页界面（index.html + app.js + style.css + themes/ 六套皮肤）
scripts/    启动辅助（open_ui.py）与打包脚本（package.ps1）
start.bat   一键启动（自动拉起服务并打开专用 Edge 窗口）
runtime/    便携 Python 3.10 运行环境（仅便携包包含；源码仓库没有）
data/       运行时数据（自动生成；不进仓库）
```

## 🧱 技术栈

FastAPI · SQLAlchemy（SQLite / WAL）· 原生 JavaScript（零前端框架）·
Microsoft Edge DevTools Protocol（CDP，自动化经本地调试端口完成，主流程不挂接页面，最大化降低自动化特征）

## ⚠️ 免责声明

本工具仅供个人求职效率用途。请遵守各招聘平台的服务条款与相关法律法规；
使用本工具产生的一切后果由使用者自行承担。

## 🛠 发布（维护者）

- 打便携包：`powershell -ExecutionPolicy Bypass -File scripts\package.ps1`
  → 生成 `dist\全自动投简历-便携版.zip`（内含完整 runtime）。
  打包脚本自带**安全校验**：缺少必需文件、或混入个人数据 / 开发产物会直接失败，
  并在打包前做「内容级扫描」（连 exe 内嵌的本机路径都会检出）。
- 仓库保持「仅源码」：`.gitignore` 已排除 `data/`、`runtime/`、`dist/` 与全部开发脚本；
  便携包 zip 作为 **Releases 附件**上传（不要提交进仓库）。
- GitHub 会清理附件名中的非 ASCII 字符——上传前先把 zip 改名为英文
  （如 `auto-resume-apply-<版本>-portable.zip`）再上传。
- 离线自检：`runtime\python.exe scripts\selfcheck\wiring_audit.py`
  （前端↔后端接线）与 `runtime\python.exe scripts\selfcheck\css_contract.py`（样式契约）。
