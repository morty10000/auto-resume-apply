"""样式契约检查：JS 依赖的每个行为类 / 状态选择器必须存在于 style.css。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
css = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")

required = [
    # 显隐与核心状态
    '.hidden', '.tab-panel', '.tab-panel.active', '.tab', '.tab.active',
    '.platform-card.selected', '.chip.selected', '.seg-btn.active',
    '.drop-zone.dragover', '.drop-zone.busy',
    '.pace-row.off', '.plat-limit-row.off',
    '.platform-card.selected .check::after',
    '.stat-card.ok .stat-num', '.stat-card.err .stat-num',
    '.flow-step.active .fs-dot', '.flow-step.done .fs-dot', '.flow-step.err .fs-dot',
    '.ms-live-beat.on', '.ms-live-fresh.stale',
    '.dot-on', '.dot-off', '.dot-waiting',
    # 日志过滤
    '.log-box.only-important .log-INFO',
    '.log-box.only-important .log-SYSTEM',
    '.log-OK', '.log-WARN', '.log-ERROR',
    # 提示条
    '.toast.show', '.toast-success', '.toast-error', '.toast-warn',
    # 状态胶囊 / 分数
    '.status-pill', '.status-applied', '.status-failed', '.status-matched',
    '.status-collected', '.status-skipped', '.status-rejected',
    '.score-pill', '.sp-hi', '.sp-lo', '.score-high', '.score-low',
    # 数字 / 按钮 / 主流程
    '.stat-num', '.progress-fill',
    '.btn-primary', '.btn-ghost', '.btn-warn', '.btn-danger-ghost', '.btn-sm',
    '.resume-item', '.md-grid', '.md-line', '.match-detail',
    '.skill', '.skill-kw', '.rs-badge', '.rs-full',
    # 顶栏 / 胶囊状态
    '.pill-idle', '.pill-running', '.pill-paused', '.pill-done', '.pill-stopped',
    # 标签输入
    '.tag-input', '.tag', '.tag-x', '.preset', '.opt input',
]

missing = [s for s in required if s not in css]
print(f"检查 {len(required)} 个行为选择器 …")
if missing:
    print("缺失：")
    for m in missing:
        print("  ✗", m)
else:
    print("全部存在 ✓")

print("大括号配平:", "OK" if css.count('{') == css.count('}') else
      f"FAIL ({css.count('{')} vs {css.count('}')})")

raise SystemExit(1 if (missing or css.count('{') != css.count('}')) else 0)
