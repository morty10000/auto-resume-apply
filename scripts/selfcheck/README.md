# 自助检查脚本

离线自检（无需启动服务）：

```powershell
runtime\python.exe scripts\selfcheck\wiring_audit.py     # 前端↔后端接线审计（API 双向核对 + 控件覆盖）
runtime\python.exe scripts\selfcheck\css_contract.py     # 样式契约检查（JS 依赖的行为类必须存在）
runtime\python.exe scripts\selfcheck\regression_suite.py # 临时数据目录离线回归：配置/数据库/任务/采集/匹配/投递
```

Edge 界面烟测（会启动临时端口与隔离数据目录，完成后自动关闭）：

```powershell
runtime\python.exe scripts\selfcheck\edge_ui_smoke.py    # 系统 Edge：9 个标签页、6 套皮肤、配置保存、响应式布局
```

以上检查都应通过。若改动前端、接口或任务链路后自检报错，说明有接线或状态机被改坏。
