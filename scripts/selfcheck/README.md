# 自助检查脚本

离线自检（无需启动服务）：

```powershell
runtime\python.exe scripts\selfcheck\wiring_audit.py     # 前端↔后端接线审计（API 双向核对 + 控件覆盖）
runtime\python.exe scripts\selfcheck\css_contract.py     # 样式契约检查（JS 依赖的行为类必须存在）
```

两项都应输出「全部通过 ✓ / 全部存在 ✓」。若改动前端或接口后自检报错，说明有接线被改坏。
