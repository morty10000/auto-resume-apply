'use strict';

/* ============================================================
   全自动投递简历系统 — 前端交互
   真实模式：采集 / 匹配 / 投递全部直连后端（real* 函数族）。
   ============================================================ */

const PLATFORMS = [
  { name: 'boss',    display: 'Boss直聘' },
  { name: 'zhilian', display: '智联招聘' },
  { name: 'job51',   display: '51job' },
  { name: 'liepin',  display: '猎聘' },
];
const PLATFORM_DISPLAY = Object.fromEntries(PLATFORMS.map(p => [p.name, p.display]));

const CITY_PRESETS = ['北京', '上海', '广州', '深圳', '杭州', '成都', '武汉', '南京', '苏州', '西安'];
const EXPERIENCE_OPTIONS = ['应届生', '1年以内', '1-3年', '3-5年', '5-10年', '10年以上'];
const EDUCATION_OPTIONS = ['大专', '本科', '硕士', '博士'];
const CONFIG_KEY = 'autoapply.config.v1';

/* ===================== 全局状态 ===================== */

const state = {
  selectedPlatforms: new Set(),
  platformStatus: {},          // name -> { status, message, updated_at }
  resume: null,                // { filename, parsed }
  run: {
    status: 'idle',            // idle | running | paused | done | stopped
    stats: { collected: 0, matched: 0, applied: 0, failed: 0 },
  },
  jobs: [],                    // 投递记录行
  lastMatched: [],             // 最近一次匹配通过的岗位（供「仅投递」使用）
  realJobs: [],                // 真实采集记录（来自数据库）
  matches: [],                 // 真实匹配记录（来自数据库）
};

/* ===================== 工具函数 ===================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fmtClock(d = new Date()) {
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  $('#toastBox').appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 300);
  }, 3200);
}

/* ===================== 任务实时监控（服务端状态轮询） =====================
   运行中每 1.5 秒拉一次 /api/task/status：即使刷新页面、重开标签页，
   也能恢复「正在做什么、做到哪一步」，并与服务端任务锁保持一致。 */

const taskPoll = {
  timer: null,
  lastLogSeq: 0,        // 已渲染的服务端日志游标
  wasActive: false,     // 上一次快照里任务是否在跑（用于检测「后台完成」）
  inFetch: false,       // 本页是否正 await 着任务 API（本地流程接管时轮询只管横幅）
  doneHandled: false,   // 本次任务的「完成」是否已处理过（防重复提示）
  lastStatus: null,
  verifyAckAt: 0,       // 用户点「继续运行」的时间（短暂忽略仍残留的 verify_wait）
  lastVerifyWait: false,// 上一轮快照是否有「等待验证」标记（用于检测解除）
};

const mapLevel = lv => (lv === 'ERR' ? 'ERROR' : (lv === 'OK' || lv === 'WARN' ? lv : 'INFO'));

async function fetchTaskStatus() {
  const r = await fetch('/api/task/status');
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function startTaskPoll() {
  stopTaskPoll();
  taskPoll.timer = setInterval(() => { void pollTaskOnce(); }, 1500);
  void pollTaskOnce();
}

function stopTaskPoll() {
  if (taskPoll.timer) clearInterval(taskPoll.timer);
  taskPoll.timer = null;
}

async function pollTaskOnce() {
  try {
    const s = await fetchTaskStatus();
    applyTaskSnapshot(s);
  } catch { /* 服务暂时不可达时静默，下次再试 */ }
}

/* 启动新任务前调用：把日志游标对齐到服务端当前值，避免重放上一任务的日志 */
async function bootstrapPollCursor() {
  try {
    const s = await fetchTaskStatus();
    (s.logs || []).forEach(l => { taskPoll.lastLogSeq = Math.max(taskPoll.lastLogSeq, l.seq); });
    taskPoll.lastStatus = s;
  } catch { /* 忽略 */ }
}

/* ===================== 验证续跑（处理验证后继续该平台） ===================== */

function renderResumeBar(items) {
  const bar = document.getElementById('resumeBar');
  if (!bar) return;
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    bar.classList.add('hidden');
    bar.innerHTML = '';
    return;
  }
  bar.innerHTML = list.map((it, idx) => {
    const n = (it.remaining_keywords || []).length;
    return `<div class="resume-item">
      <span>⏳【${esc(it.display || it.platform)}】检测到验证码已跳过：剩余 <b>${n}</b> 个关键词未采集 —— 请先在浏览器完成验证，然后</span>
      <button type="button" class="btn btn-warn btn-sm" data-resume="${idx}">继续采集该平台</button>
    </div>`;
  }).join('');
  bar.classList.remove('hidden');
  $$('button[data-resume]', bar).forEach(btn => {
    btn.addEventListener('click', () => {
      const it = list[Number(btn.dataset.resume)];
      if (it) resumePlatform(it);
    });
  });
}

function resumePlatform(item) {
  const cfg = collectConfig();
  cfg.platforms = [item.platform];
  cfg.keywords = (item.remaining_keywords || []).slice();
  if (item.cities && item.cities.length) cfg.cities = item.cities.slice();
  if (!cfg.keywords.length) return;
  // 验证后的续跑自动放慢 1.5 倍翻页节奏（更保守，降低再次触发概率）
  const pace = cfg.pace_by_platform && cfg.pace_by_platform[item.platform];
  if (pace && Array.isArray(pace.page_delay)) {
    pace.page_delay = [
      Math.min(600, Math.round(pace.page_delay[0] * 1.5)),
      Math.min(600, Math.round(pace.page_delay[1] * 1.5)),
    ];
  }
  renderResumeBar([]);
  // 走标准启动流程（beginRun）：进入监控、锁定按钮、实时轮询、忙预检——运行中随时可停止
  void beginRun(cfg, `验证续跑·${item.display || item.platform}（节奏放慢 1.5 倍）`, 'collect');
}

function applyTaskSnapshot(s) {
  taskPoll.lastStatus = s;
  renderTaskLive(s);
  renderResumeBar(s.resume_pending || []);
  if (s.stats) { Object.assign(state.run.stats, s.stats); renderStats(); }
  if (typeof s.percent === 'number') setProgress(s.percent);
  (s.logs || []).forEach(l => {
    if (l.seq > taskPoll.lastLogSeq) {
      taskPoll.lastLogSeq = l.seq;
      appendLog(mapLevel(l.level), l.message, l.t);
    }
  });
  const busy = !!(s.active || s.busy);

  // 「等待安全验证」状态联动：显示横幅 + 暂停态；解除后自动恢复
  const ackFresh = Date.now() - (taskPoll.verifyAckAt || 0) < 12000;
  const hadWait = taskPoll.lastVerifyWait;
  taskPoll.lastVerifyWait = !!s.verify_wait;
  if (busy && s.verify_wait && !ackFresh) {
    const label = PLATFORM_DISPLAY[s.verify_wait.platform] || s.verify_wait.platform;
    showVerifyBanner(true, `⚠️【${label}】触发安全验证，已切换到该页面。请完成验证，系统会自动继续（也可点右侧按钮立即继续）。`);
    if (state.run.status !== 'paused' && state.run.status !== 'stopped') setRunStatus('paused');
  } else if (busy && !s.verify_wait && hadWait) {
    showVerifyBanner(false);
    if (state.run.status === 'paused' && !s.paused) setRunStatus('running');
    logOk('安全验证已处理，任务继续运行');
  }

  // 暂停状态联动（服务端真暂停：冻结 → UI 暂停；恢复 → UI 恢复；本地暂停请求 4 秒防抖）
  if (busy && s.paused && state.run.status !== 'paused' && state.run.status !== 'stopped') {
    setRunStatus('paused');
  } else if (
    busy && !s.paused && state.run.status === 'paused' && !s.verify_wait &&
    Date.now() - (taskPoll.pauseAckAt || 0) > 4000
  ) {
    setRunStatus('running');
  }

  if (busy) {
    taskPoll.wasActive = true;
    if (!taskPoll.inFetch) {
      // 本页没有流程在跑（刷新恢复 / 其他标签页启动）：UI 同步为运行态
      if (state.run.status !== 'running' && !s.verify_wait && !s.paused) setRunStatus('running');
      restoreFlowFromSnapshot(s);
    } else if (s.stage) {
      restoreFlowFromSnapshot(s);   // 本地流程也跟随服务端阶段，防止进度条滞后
    }
  } else if (taskPoll.wasActive && !taskPoll.inFetch) {
    finishFromPoll(s);   // 后台任务已结束，而本页没有在等它 → 兜底收尾
  }
}

/* 根据服务端阶段恢复四步流程条（刷新后 / 轮询同步） */
function restoreFlowFromSnapshot(s) {
  const stageOrder = ['collect', 'match', 'apply'];
  const busy = !!(s.active || s.busy);
  const stage = s.stage;
  if (busy) { setFlowState('auth', 'done'); setFlowSub('auth', '已检查'); }
  if (!stage) return;
  const idx = stageOrder.indexOf(stage);
  stageOrder.forEach((st, i) => {
    if (!busy) {
      setFlowState(st, (s.ok === false && i === idx) ? 'err' : 'done');
    } else if (i < idx) setFlowState(st, 'done');
    else if (i === idx) setFlowState(st, 'active');
    else setFlowState(st, 'pending');
  });
}

/* 后台任务结束、而本页没有在等待时：提示 + 刷新数据 */
function finishFromPoll(s) {
  if (taskPoll.doneHandled) return;
  taskPoll.doneHandled = true;
  stopTaskPoll();
  const ok = s.ok !== false;
  const stoppedByUser = state.run.status === 'stopped';
  const summary = s.summary || (ok ? '任务已结束' : '任务异常结束');
  if (ok) {
    logOk(`任务完成：${summary}`);
    setRunStatus('done');
    toast('任务完成', 'success');
    restoreFlowFromSnapshot(s);
  } else if (stoppedByUser) {
    logWarn(`任务已停止：${summary}`);
    setRunStatus('stopped');
    resetFlow();
  } else {
    logErr(`任务结束：${summary}`);
    setRunStatus('idle');
    toast(`任务未完成：${summary}`, 'error');
    restoreFlowFromSnapshot(s);
  }
  void loadRealJobs();
  void loadMatches();
  void loadApplications();
  void loadTodayStats();
}

/* 监控页实时横幅渲染 */
function renderTaskLive(s) {
  const dot = $('#msLiveDot');
  const detail = $('#msLiveDetail');
  const timer = $('#msLiveTimer');
  const fresh = $('#msLiveFresh');
  if (!dot || !detail) return;
  const busy = !!(s.active || s.busy);
  if (busy) {
    dot.classList.add('on');
    detail.textContent = s.detail || `${s.stage_label || '任务'}进行中…`;
    timer.textContent = s.elapsed ? `运行时长 ${fmtDur(s.elapsed)}` : '';
    const fs = typeof s.fresh_seconds === 'number' ? s.fresh_seconds : null;
    if (fs === null) {
      fresh.textContent = '';
      fresh.className = 'ms-live-fresh';
    } else {
      fresh.textContent = fs < 4 ? '刚刚有更新' : `最后更新于 ${Math.round(fs)} 秒前`;
      fresh.className = 'ms-live-fresh' + (fs > 90 ? ' stale' : '');
    }
  } else {
    dot.classList.remove('on');
    if (s.status === 'done' || s.status === 'error') {
      detail.textContent = s.summary || (s.ok === false ? '任务失败结束' : '任务已结束');
      timer.textContent = s.elapsed ? `总用时 ${fmtDur(s.elapsed)}` : '';
      fresh.textContent = s.ok === false ? '未成功' : '已完成';
      fresh.className = 'ms-live-fresh';
    } else {
      detail.textContent = '暂无运行中的任务';
      timer.textContent = '';
      fresh.textContent = '';
      fresh.className = 'ms-live-fresh';
    }
  }
}

function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return `${m} 分 ${String(s).padStart(2, '0')} 秒`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

/* 页面加载时恢复任务状态：后台有任务在跑 → 直接接入实时监控 */
async function restoreTaskState() {
  let s;
  try { s = await fetchTaskStatus(); } catch { return; }
  taskPoll.lastStatus = s;
  const busy = !!(s.active || s.busy);
  const doneFresh = !busy && s.finished_at && (s.server_time - s.finished_at) < 600;
  if (busy || doneFresh) {
    // 运行中的任务 / 刚结束（10 分钟内）的任务：重放日志，恢复现场
    (s.logs || []).forEach(l => {
      if (l.seq > taskPoll.lastLogSeq) {
        taskPoll.lastLogSeq = l.seq;
        appendLog(mapLevel(l.level), l.message, l.t);
      }
    });
  } else {
    // 没有任务 / 太久以前的任务：不重放旧日志，只把游标对齐
    (s.logs || []).forEach(l => { taskPoll.lastLogSeq = Math.max(taskPoll.lastLogSeq, l.seq); });
  }
  if (busy) {
    taskPoll.wasActive = true;
    taskPoll.doneHandled = false;
    Object.assign(state.run.stats, s.stats || {});
    renderStats();
    if (typeof s.percent === 'number') setProgress(s.percent);
    setRunStatus('running');
    restoreFlowFromSnapshot(s);
    renderTaskLive(s);
    startTaskPoll();
    if (s.verify_wait) {
      const label = PLATFORM_DISPLAY[s.verify_wait.platform] || s.verify_wait.platform;
      showVerifyBanner(true, `⚠️【${label}】触发安全验证，已切换到该页面。请完成验证，系统会自动继续（也可点右侧按钮立即继续）。`);
      setRunStatus('paused');
      taskPoll.lastVerifyWait = true;
      logWarn('后台任务正在等待安全验证 —— 完成后自动继续');
      toast('任务在等待安全验证，请在浏览器页面完成', 'warn');
    } else {
      logWarn('检测到后台任务正在运行 —— 上方横幅实时显示当前进度');
      toast('后台有任务在运行，已接入实时监控', 'warn');
    }
  } else if (doneFresh) {
    renderTaskLive(s);
    if (typeof s.percent === 'number') setProgress(s.percent);
    logInfo(`最近一次任务：${s.summary || s.status}`);
  } else {
    renderTaskLive({ active: false, status: 'idle', logs: [] });
  }
}

/* ===================== 标签页 ===================== */

function switchTab(name) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $$('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));
  if (name === 'results') void loadApplications();
  if (name === 'collect') void loadRealJobs();
  if (name === 'matchlog') void loadMatches();
  if (name === 'home') void loadTodayStats();
}

/* ===================== 平台卡片 ===================== */

// 对外只展示两个状态：未登录 / 已登录（waiting 等中间态一律按「未登录」显示）
const LOGIN_TEXT = { logged_in: '已登录' };

function renderPlatforms() {
  const box = $('#platformList');
  box.innerHTML = PLATFORMS.map(p => {
    const sel = state.selectedPlatforms.has(p.name);
    const info = state.platformStatus[p.name] || {};
    const st = info.status || 'unknown';
    const logged = st === 'logged_in';
    const dotCls = logged ? 'on' : 'off';
    const foot = logged
      ? `<span class="login-ok" title="${esc(info.updated_at || '')}">✓ 已登录</span>`
      : `<button type="button" class="btn btn-ghost btn-sm" data-login="${p.name}">去登录</button>`;
    return `
      <div class="platform-card ${sel ? 'selected' : ''}" data-name="${p.name}">
        <div class="pc-head"><span class="check"></span><span class="pc-name">${p.display}</span></div>
        <div class="pc-foot">
          <span class="status-dot dot-${dotCls}"></span>
          <span>${logged ? '已登录' : '未登录'}</span>
          ${foot}
        </div>
      </div>`;
  }).join('');
  syncPlatformLimitState();
}

/* ===================== 每平台数量设置（采集上限 / 每日投递上限） ===================== */

const LIMIT_FIELDS = [
  { box: 'maxJobsByPlatform',    prefix: 'maxJobs',    dft: 20, lo: 1, hi: 1000 },
  { box: 'dailyLimitByPlatform', prefix: 'dailyLimit', dft: 20, lo: 1, hi: 200 },
];

function renderPlatformLimits() {
  LIMIT_FIELDS.forEach(f => {
    const el = document.getElementById(f.box);
    if (!el) return;
    el.innerHTML = PLATFORMS.map(p => `
      <label class="plat-limit-row" data-platform="${p.name}">
        <span class="pl-name">${p.display}</span>
        <input id="${f.prefix}-${p.name}" type="number" min="${f.lo}" max="${f.hi}" value="${f.dft}">
      </label>`).join('');
  });
  syncPlatformLimitState();
}

/* 未勾选的平台 → 该平台的输入框置灰不可编辑（值保留，重新勾选即可用） */
function syncPlatformLimitState() {
  LIMIT_FIELDS.forEach(f => {
    PLATFORMS.forEach(p => {
      const row = document.querySelector(`.plat-limit-row[data-platform="${p.name}"]`);
      if (!row) return;
      const on = state.selectedPlatforms.has(p.name);
      row.classList.toggle('off', !on);
      const input = row.querySelector('input');
      if (input) input.disabled = !on;
    });
  });
  syncPaceState();
}

/* ===================== 防风控方案（按平台差异化节奏） ===================== */

const PACE_PLATFORMS = ['boss', 'zhilian', 'job51', 'liepin'];
const PACE_DEFAULTS = {
  boss:    { pages: 5, page: [6, 12], apply: [20, 45] },
  zhilian: { pages: 5, page: [5, 10], apply: [15, 35] },
  job51:   { pages: 3, page: [5, 10], apply: [15, 35] },
  liepin:  { pages: 4, page: [4, 8],  apply: [12, 30] },
};

function renderPaceGrid() {
  const el = document.getElementById('paceByPlatform');
  if (!el) return;
  el.innerHTML = PACE_PLATFORMS.map(name => {
    const d = PACE_DEFAULTS[name];
    return `
      <div class="pace-row" data-platform="${name}">
        <span class="pl-name">${PLATFORM_DISPLAY[name] || name}</span>
        <label>页数 ≤<input id="pace-pages-${name}" type="number" min="1" max="50" value="${d.pages}"></label>
        <label>翻页 <input id="pace-page-min-${name}" type="number" min="1" max="600" value="${d.page[0]}">—<input id="pace-page-max-${name}" type="number" min="1" max="600" value="${d.page[1]}"> 秒</label>
        <label>投递 <input id="pace-apply-min-${name}" type="number" min="1" max="600" value="${d.apply[0]}">—<input id="pace-apply-max-${name}" type="number" min="1" max="600" value="${d.apply[1]}"> 秒</label>
        <label class="opt"><input type="checkbox" id="pace-body-${name}"> 正文校验</label>
      </div>`;
  }).join('');
  syncPaceState();
}

function syncPaceState() {
  PACE_PLATFORMS.forEach(name => {
    const row = document.querySelector(`.pace-row[data-platform="${name}"]`);
    if (row) row.classList.toggle('off', !state.selectedPlatforms.has(name));
  });
}

function _paceSet(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function _paceGet(id, lo, hi, dft) {
  const el = document.getElementById(id);
  return clampInt(el ? el.value : dft, lo, hi, dft);
}

function readPaceConfig() {
  const pace = {};
  const pages = {};
  const body = {};
  PACE_PLATFORMS.forEach(name => {
    const d = PACE_DEFAULTS[name];
    pages[name] = _paceGet(`pace-pages-${name}`, 1, 50, d.pages);
    const bodyEl = document.getElementById(`pace-body-${name}`);
    body[name] = !!(bodyEl && bodyEl.checked);
    pace[name] = {
      page_delay: [
        _paceGet(`pace-page-min-${name}`, 1, 600, d.page[0]),
        _paceGet(`pace-page-max-${name}`, 1, 600, d.page[1]),
      ],
      apply_delay: [
        _paceGet(`pace-apply-min-${name}`, 1, 600, d.apply[0]),
        _paceGet(`pace-apply-max-${name}`, 1, 600, d.apply[1]),
      ],
    };
  });
  return { pace, pages, body };
}

function writePaceConfig(cfg, pagesCfg, bodyCfg) {
  PACE_PLATFORMS.forEach(name => {
    const d = PACE_DEFAULTS[name];
    const v = (cfg && cfg[name]) || {};
    const pd = Array.isArray(v.page_delay) ? v.page_delay : d.page;
    const ad = Array.isArray(v.apply_delay) ? v.apply_delay : d.apply;
    _paceSet(`pace-pages-${name}`, (pagesCfg && pagesCfg[name]) ?? d.pages);
    _paceSet(`pace-page-min-${name}`, pd[0]);
    _paceSet(`pace-page-max-${name}`, pd[1]);
    _paceSet(`pace-apply-min-${name}`, ad[0]);
    _paceSet(`pace-apply-max-${name}`, ad[1]);
    const bodyEl = document.getElementById(`pace-body-${name}`);
    if (bodyEl) bodyEl.checked = !!((bodyCfg && bodyCfg[name]) || false);
  });
}

function fillPacePreset() {
  PACE_PLATFORMS.forEach(name => {
    const d = PACE_DEFAULTS[name];
    _paceSet(`pace-pages-${name}`, d.pages);
    _paceSet(`pace-page-min-${name}`, d.page[0]);
    _paceSet(`pace-page-max-${name}`, d.page[1]);
    _paceSet(`pace-apply-min-${name}`, d.apply[0]);
    _paceSet(`pace-apply-max-${name}`, d.apply[1]);
  });
  toast('已填入推荐方案（Boss 保守 · 其余平台适中）', 'success');
}

function readPlatformLimits(prefix, dft, lo, hi) {
  const out = {};
  PLATFORMS.forEach(p => {
    const el = document.getElementById(`${prefix}-${p.name}`);
    out[p.name] = clampInt(el ? el.value : dft, lo, hi, dft);
  });
  return out;
}

/* ---- 今日数据（各平台：采集 / 投递数量） ---- */
async function loadTodayStats() {
  const box = $('#todayStats');
  if (!box) return;
  try {
    const d = await (await fetch('/api/stats/today')).json();
    const by = d.platforms || {};
    const dateEl = $('#todayStatsDate');
    if (dateEl && d.date) dateEl.textContent = `（${String(d.date).slice(5)}）`;
    box.innerHTML = PLATFORMS.map(p => {
      const st = by[p.name] || {};
      return `
        <div class="today-stat">
          <div class="ts-name">${p.display}</div>
          <div class="ts-nums">
            <span class="ts-collect">采集<b>${st.collected ?? 0}</b></span>
            <span class="ts-apply">投递<b>${st.applied ?? 0}</b></span>
          </div>
        </div>`;
    }).join('');
  } catch { /* 后端未启动时静默 */ }
}

function writePlatformLimits(prefix, byPlatform, fallback, lo, hi) {
  PLATFORMS.forEach(p => {
    const el = document.getElementById(`${prefix}-${p.name}`);
    if (el) el.value = clampInt(byPlatform[p.name] ?? fallback, lo, hi, fallback);
  });
}

let loginPollTimer = null;

function anyWaiting() {
  return Object.values(state.platformStatus).some(s => (s || {}).status === 'waiting');
}

async function refreshPlatformStatus() {
  try {
    const list = await (await fetch('/api/platforms', { signal: AbortSignal.timeout(8000) })).json();
    list.forEach(p => {
      state.platformStatus[p.name] = {
        status: p.status,
        message: p.message,
        updated_at: p.updated_at,
      };
    });
    renderPlatforms();
  } catch {
    /* 后端未启动时静默（例如直接以文件方式打开页面） */
  }
}

function startLoginPolling() {
  if (loginPollTimer) return;
  loginPollTimer = setInterval(async () => {
    await refreshPlatformStatus();
    if (!anyWaiting()) {
      clearInterval(loginPollTimer);
      loginPollTimer = null;
      toast('登录状态已更新', 'success');
    }
  }, 2500);
}

async function handlePlatformLogin(name) {
  try {
    const r = await fetch(`/api/platforms/${name}/login`, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      toast(d.detail || '发起登录失败', 'error');
      return;
    }
    if (d.status === 'logged_in') {
      toast(`${PLATFORM_DISPLAY[name]} 已处于登录状态`, 'success');
      await refreshPlatformStatus();
      return;
    }
    toast(d.message || '已在专用窗口新标签页打开登录页，请完成登录', 'success');
    state.platformStatus[name] = { status: 'waiting', message: d.message || '' };
    renderPlatforms();
    startLoginPolling();
  } catch (e) {
    toast('连接后端失败：' + (e.message || e), 'error');
  }
}

function initPlatformEvents() {
  $('#platformList').addEventListener('click', e => {
    const loginBtn = e.target.closest('[data-login]');
    if (loginBtn) {
      e.stopPropagation();
      handlePlatformLogin(loginBtn.dataset.login);
      return;
    }
    const card = e.target.closest('.platform-card');
    if (!card) return;
    const name = card.dataset.name;
    if (state.selectedPlatforms.has(name)) state.selectedPlatforms.delete(name);
    else state.selectedPlatforms.add(name);
    renderPlatforms();
  });
}

/* ===================== 简历上传 ===================== */

function fmtExp(p) {
  if (p.years_experience === 0) return p.experience_note || '应届 / 暂无正式经历';
  if (p.years_experience == null) return p.experience_note || '未识别';
  return `${p.years_experience} 年经验`;
}

function renderResumeResult(resume) {
  const p = resume.parsed || {};
  const box = $('#resumeResult');
  box.classList.remove('hidden');
  const engineLabel = resume.engine === 'pdf_ocr' ? '图片版 · OCR 识别'
    : resume.engine === 'pdf_text' ? 'PDF 文本层'
    : resume.engine === 'docx' ? 'Word 文档' : '';
  const eduLine = [p.education, p.school, p.major, p.graduation ? `${p.graduation} 毕业` : '']
    .filter(Boolean).join(' · ') || '未识别';
  const contact = [p.phone, p.email].filter(Boolean).join(' / ') || '未识别';
  const advChips = (p.advantages || []).map(s => `<span class="skill">${esc(s)}</span>`).join('');
  const skillChips = (p.skills || []).map(s => `<span class="skill">${esc(s)}</span>`).join('');
  const kwChips = (p.suggested_keywords || []).map(k =>
    `<span class="skill skill-kw" data-kw="${esc(k)}" title="点击复制">${esc(k)}</span>`).join('');
  box.innerHTML = `
    <div class="rs-item"><div class="rs-label">文件名</div>
      <div class="rs-value">${esc(resume.filename)} ${engineLabel ? `<span class="rs-badge">${engineLabel}</span>` : ''}</div></div>
    ${resume.uploaded_at ? `<div class="rs-item"><div class="rs-label">上传时间</div><div class="rs-value">${esc(resume.uploaded_at)}</div></div>` : ''}
    <div class="rs-item"><div class="rs-label">姓名</div><div class="rs-value">${esc(p.name || '未识别')}</div></div>
    <div class="rs-item"><div class="rs-label">求职意向</div><div class="rs-value">${esc(p.expected_position || '未识别')}</div></div>
    <div class="rs-item"><div class="rs-label">联系方式</div><div class="rs-value">${esc(contact)}</div></div>
    <div class="rs-item"><div class="rs-label">经验</div><div class="rs-value">${esc(fmtExp(p))}</div></div>
    <div class="rs-item rs-full"><div class="rs-label">学历 / 学校 / 专业</div><div class="rs-value">${esc(eduLine)}</div></div>
    ${advChips ? `<div class="rs-item rs-full"><div class="rs-label">核心优势</div><div class="rs-value">${advChips}</div></div>` : ''}
    ${skillChips ? `<div class="rs-item rs-full"><div class="rs-label">技能（词表命中）</div><div class="rs-value">${skillChips}</div></div>` : ''}
    ${kwChips ? `<div class="rs-item rs-full"><div class="rs-label">建议搜索关键词（点击复制）</div><div class="rs-value">${kwChips}</div></div>` : ''}
    <div class="rs-item rs-full"><div class="rs-label">个人简介</div><div class="rs-value">${esc(p.summary || '未识别')}</div></div>`;
}

function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); } catch { toast('复制失败，请手动选择', 'warn'); }
  ta.remove();
}

document.addEventListener('click', e => {
  const k = e.target.closest('.skill-kw');
  if (!k) return;
  const kw = k.dataset.kw || k.textContent.trim();
  const done = () => toast(`已复制关键词：${kw}`, 'success');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(kw).then(done, () => fallbackCopy(kw, done));
  } else {
    fallbackCopy(kw, done);
  }
});

/* 真实简历上传：调用后端解析（图片版 PDF 自动 OCR） */
async function realUploadResume(file) {
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/api/resume/upload', { method: 'POST', body: fd });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

async function uploadResumeFile(file) {
  if (!file) return;
  const okExt = /\.(pdf|docx)$/i.test(file.name);
  if (!okExt) { toast('仅支持 .pdf / .docx 文件', 'error'); return; }
  if (file.size > 10 * 1024 * 1024) { toast('文件不能超过 10MB', 'error'); return; }
  const zone = $('#dropZone');
  const busy = $('#resumeBusy');
  zone.classList.add('busy');
  busy.classList.remove('hidden');
  busy.textContent = '⏳ 正在上传并解析…（图片版 PDF 会自动 OCR，首次约 15~40 秒）';
  toast('正在上传并解析简历…');
  try {
    const res = await realUploadResume(file);
    state.resume = {
      filename: res.filename || file.name,
      parsed: res.parsed || {},
      engine: res.engine,
      uploaded_at: res.uploaded_at,
    };
    renderResumeResult(state.resume);
    const p = res.parsed || {};
    const engineNote = res.engine === 'pdf_ocr' ? `OCR ${res.ocr_seconds}s` : '文本解析';
    toast(`解析完成：${p.name || '未识别姓名'} · ${(p.skills || []).length} 项技能（${engineNote}）`, 'success');
    appendLog('OK', `简历解析完成：${res.filename}（${engineNote}，${res.text_chars} 字）`);
  } catch (err) {
    toast(`解析失败：${err.message || err}`, 'error');
  } finally {
    zone.classList.remove('busy');
    busy.classList.add('hidden');
  }
}

/* 页面加载时恢复已上传的简历档案 */
async function loadCurrentResume() {
  try {
    const d = await (await fetch('/api/resume/current')).json();
    if (d && d.resume && d.resume.parsed) {
      state.resume = {
        filename: d.resume.filename,
        parsed: d.resume.parsed,
        engine: d.resume.engine,
        uploaded_at: d.resume.uploaded_at,
      };
      renderResumeResult(state.resume);
    }
  } catch { /* 服务未启动时静默 */ }
}

function initDropZone() {
  const zone = $('#dropZone');
  const input = $('#fileInput');
  zone.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    uploadResumeFile(input.files[0]);
    input.value = '';
  });
  ['dragenter', 'dragover'].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove('dragover'); }));
  zone.addEventListener('drop', e => uploadResumeFile(e.dataTransfer.files[0]));
}

/* ===================== 标签输入组件 ===================== */

function createTagInput(container, input, onChange, opts = {}) {
  const splitter = opts.splitter || /[,，、;；\n]+/;
  const tags = [];

  function render() {
    $$('.tag', container).forEach(el => el.remove());
    tags.forEach((t, i) => {
      const chip = document.createElement('span');
      chip.className = 'tag';
      chip.innerHTML = `<span>${esc(t)}</span><button type="button" class="tag-x" data-i="${i}">×</button>`;
      container.insertBefore(chip, input);
    });
  }

  function add(v) {
    v = String(v).trim().replace(/[,，、;；]$/, '');
    if (!v || tags.includes(v)) return false;
    tags.push(v);
    render();
    onChange && onChange();
    return true;
  }
  function addMany(text) {
    // 支持一次粘贴/输入多个：逗号、顿号、分号、换行 分隔
    const parts = String(text).split(splitter);
    let added = 0;
    parts.forEach(p => { if (add(p)) added++; });
    return added;
  }
  function remove(i) {
    tags.splice(i, 1);
    render();
    onChange && onChange();
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ',' || e.key === '，') {
      e.preventDefault();
      if (addMany(input.value)) input.value = '';
    } else if (e.key === 'Backspace' && !input.value && tags.length) {
      remove(tags.length - 1);
    }
  });
  input.addEventListener('paste', e => {
    const text = (e.clipboardData || window.clipboardData)?.getData('text') || '';
    if (/[,，、;；\n]/.test(text)) {
      e.preventDefault();
      addMany(text);
      input.value = '';
    }
  });
  input.addEventListener('blur', () => {
    if (input.value.trim() && addMany(input.value)) input.value = '';
  });
  container.addEventListener('click', e => {
    const x = e.target.closest('.tag-x');
    if (x) { remove(+x.dataset.i); return; }
    if (e.target === container) input.focus();
  });

  return {
    add,
    addMany,
    get: () => [...tags],
    set: arr => { tags.length = 0; arr.forEach(v => { if (!tags.includes(v)) tags.push(v); }); render(); },
  };
}

let kwTags, cityTags, blTags;

/* ---- 搜索方式（逐个 / 组合） ---- */

let keywordMode = 'each';

function renderKeywordMode() {
  $$('#keywordMode .seg-btn').forEach(b => b.classList.toggle('active', b.dataset.value === keywordMode));
  const el = $('#keywordModeHint');
  if (!el) return;
  const kws = kwTags ? kwTags.get() : [];
  if (keywordMode === 'combined') {
    el.textContent = kws.length > 1
      ? `合并为一次搜索：「${kws.join(' ')}」`
      : '多个关键词合并为一次搜索（当前不足 2 个，按单个搜索）';
  } else {
    el.textContent = kws.length > 1
      ? `${kws.length} 个关键词逐个搜索（共 ${kws.length} 组结果）`
      : '每个关键词单独搜索';
  }
}

function initKeywordMode() {
  $('#keywordMode').addEventListener('click', e => {
    const b = e.target.closest('.seg-btn');
    if (!b) return;
    keywordMode = b.dataset.value;
    renderKeywordMode();
  });
  renderKeywordMode();
}

/* ---- 多选 chips（经验 / 学历） ---- */

const expSelected = new Set();
const eduSelected = new Set();

function renderChipGroup(containerId, options, selected) {
  $(containerId).innerHTML = options.map(opt =>
    `<button type="button" class="chip ${selected.has(opt) ? 'selected' : ''}" data-value="${esc(opt)}">${esc(opt)}</button>`
  ).join('');
}

function initChipGroup(containerId, options, selected) {
  renderChipGroup(containerId, options, selected);
  $(containerId).addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    const v = chip.dataset.value;
    if (selected.has(v)) selected.delete(v);
    else selected.add(v);
    chip.classList.toggle('selected');
  });
}

/* ===================== 配置收集 / 持久化 ===================== */

const numOrNull = v => {
  const n = parseInt(v, 10);
  return Number.isNaN(n) ? null : n;
};

function clampInt(v, lo, hi, dft) {
  const n = parseInt(v, 10);
  if (Number.isNaN(n)) return dft;
  return Math.min(hi, Math.max(lo, n));
}

function collectConfig() {
  const maxJobsByPlatform = readPlatformLimits('maxJobs', 20, 1, 1000);
  const dailyLimitByPlatform = readPlatformLimits('dailyLimit', 20, 1, 200);
  const paceCfg = readPaceConfig();
  return {
    platforms: [...state.selectedPlatforms],
    keywords: kwTags.get(),
    cities: cityTags.get(),
    salary_min: numOrNull($('#salaryMin').value),
    salary_max: numOrNull($('#salaryMax').value),
    experience: [...expSelected],
    education: [...eduSelected],
    hr_active_days: numOrNull($('#hrActive').value),
    max_pages: clampInt($('#maxPages').value, 1, 50, 5),
    max_jobs_by_platform: maxJobsByPlatform,
    max_pages_by_platform: paceCfg.pages,
    verify_body_by_platform: paceCfg.body,
    pace_by_platform: paceCfg.pace,
    max_jobs: Math.max(...Object.values(maxJobsByPlatform)),   // 兼容旧字段（真实限制看 by_platform）
    keyword_mode: keywordMode,
    kw_delay_min: clampInt($('#kwDelayMin').value, 1, 600, 8),
    kw_delay_max: clampInt($('#kwDelayMax').value, 1, 600, 20),
    page_delay_min: clampInt($('#pageDelayMin').value, 1, 600, 3),
    page_delay_max: clampInt($('#pageDelayMax').value, 1, 600, 8),
    shuffle_keywords: $('#shuffleKeywords').checked,
    humanize_scroll: $('#humanizeScroll').checked,
    daily_limit_by_platform: dailyLimitByPlatform,
    daily_limit: Math.max(...Object.values(dailyLimitByPlatform)),   // 兼容旧字段（真实限制看 by_platform）
    delay_min: clampInt($('#delayMin').value, 1, 600, 5),
    delay_max: clampInt($('#delayMax').value, 1, 600, 12),
    threshold: clampInt($('#thresholdNum').value, 0, 100, 60),
    greeting: $('#greeting').value.trim(),
    blacklist: blTags.get(),
    resume: state.resume ? state.resume.filename : null,
  };
}

function applyConfig(cfg) {
  state.selectedPlatforms = new Set(cfg.platforms || []);
  kwTags.set(cfg.keywords || []);
  cityTags.set(cfg.cities || []);
  $('#salaryMin').value = cfg.salary_min ?? '';
  $('#salaryMax').value = cfg.salary_max ?? '';
  expSelected.clear();
  (Array.isArray(cfg.experience) ? cfg.experience : cfg.experience ? [cfg.experience] : []).forEach(v => expSelected.add(v));
  renderChipGroup('#experienceChips', EXPERIENCE_OPTIONS, expSelected);
  eduSelected.clear();
  (Array.isArray(cfg.education) ? cfg.education : cfg.education ? [cfg.education] : []).forEach(v => eduSelected.add(v));
  renderChipGroup('#educationChips', EDUCATION_OPTIONS, eduSelected);
  $('#hrActive').value = cfg.hr_active_days == null ? '' : String(cfg.hr_active_days);
  $('#maxPages').value = cfg.max_pages ?? 5;
  writePlatformLimits('maxJobs', cfg.max_jobs_by_platform || {}, cfg.max_jobs ?? 20, 1, 1000);
  writePaceConfig(cfg.pace_by_platform, cfg.max_pages_by_platform, cfg.verify_body_by_platform);
  keywordMode = cfg.keyword_mode === 'combined' ? 'combined' : 'each';
  $('#kwDelayMin').value = cfg.kw_delay_min ?? 8;
  $('#kwDelayMax').value = cfg.kw_delay_max ?? 20;
  $('#pageDelayMin').value = cfg.page_delay_min ?? 3;
  $('#pageDelayMax').value = cfg.page_delay_max ?? 8;
  $('#shuffleKeywords').checked = cfg.shuffle_keywords !== false;
  $('#humanizeScroll').checked = cfg.humanize_scroll !== false;
  renderKeywordMode();
  writePlatformLimits('dailyLimit', cfg.daily_limit_by_platform || {}, cfg.daily_limit ?? 30, 1, 200);
  $('#delayMin').value = cfg.delay_min ?? 5;
  $('#delayMax').value = cfg.delay_max ?? 12;
  const th = clampInt(cfg.threshold ?? 60, 0, 100, 60);
  $('#threshold').value = th;
  $('#thresholdNum').value = th;
  blTags.set(cfg.blacklist || []);
  $('#greeting').value = cfg.greeting || '';
  renderPlatforms();
}

function saveConfig(silent = false) {
  const cfg = collectConfig();
  localStorage.setItem(CONFIG_KEY, JSON.stringify(cfg));
  // 同步写入服务端文本文件（data/user_config.json）：关闭 / 重启项目后配置持续保存
  fetch('/api/userconfig', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config: cfg }),
  }).catch(() => {});
  if (!silent) toast('配置已保存（本地 + 文件同步）', 'success');
  return cfg;
}

function loadConfig() {
  try {
    const raw = localStorage.getItem(CONFIG_KEY);
    if (raw) applyConfig(JSON.parse(raw));
    else renderPlatforms();
  } catch {
    renderPlatforms();
  }
  void syncConfigFromServer();   // 以服务端文本文件为准（关闭 / 重启项目后从这里恢复配置）
}

async function syncConfigFromServer() {
  try {
    const d = await (await fetch('/api/userconfig')).json();
    if (d && d.ok && d.config && Object.keys(d.config).length) {
      applyConfig(d.config);
      localStorage.setItem(CONFIG_KEY, JSON.stringify(d.config));
    }
  } catch { /* 忽略：无文件时用本地草稿 / 默认值 */ }
  saveConfig(true);   // 打开即对账：把当前生效配置写入文件（首次使用即生成 data/user_config.json）
}

/* ---- 草稿自动暂存（表单改动即存，刷新不丢；无需手动保存） ---- */

let draftTimer = null;

function scheduleDraftSave() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => saveConfig(true), 400);
}

function bindDraftAutosave() {
  const panel = document.querySelector('main');
  panel.addEventListener('input', scheduleDraftSave);
  panel.addEventListener('change', scheduleDraftSave);
  panel.addEventListener('click', scheduleDraftSave);
}

function validateForStart(cfg, kind = 'all') {
  if (kind === 'collect') {
    if (cfg.platforms.length === 0) return '请至少选择一个投递平台';
    if (cfg.keywords.length === 0) return '请至少添加一个岗位关键词';
    if (cfg.cities.length === 0) return '请至少添加一个城市';
    return null;
  }
  if (kind === 'match' || kind === 'apply') {
    if (!state.resume) return '请先在「简历」页上传简历';
    return null;
  }
  if (cfg.platforms.length === 0) return '请至少选择一个投递平台';
  if (!state.resume) return '请先在「简历」页上传简历';
  if (cfg.keywords.length === 0) return '请至少添加一个岗位关键词';
  if (cfg.cities.length === 0) return '请至少添加一个城市';
  return null;
}

/* ===================== 运行状态 / 统计 / 日志 ===================== */

const PILL_TEXT = { idle: '空闲', running: '运行中', paused: '已暂停', done: '已完成', stopped: '已停止' };

const MONITOR_STATUS_UI = {
  idle:    ['💤', '空闲中', '回「主页」点「开始采集 / 开始匹配 / 开始投递 / 一键全流程」即可启动任务'],
  running: ['🔄', '任务运行中', '正在浏览器中执行，请留意下方日志'],
  paused:  ['⏸️', '已暂停', '按提示处理完成后，点「▶ 继续」恢复运行'],
  done:    ['✅', '已完成', '想看结果可去「采集记录 / 匹配记录 / 投递记录」页'],
  stopped: ['⏹️', '已停止', '可以直接启动新任务'],
};

function setRunStatus(status) {
  state.run.status = status;
  const pill = $('#taskStatePill');
  pill.className = `pill pill-${status}`;
  pill.textContent = PILL_TEXT[status] || status;

  const ui = MONITOR_STATUS_UI[status] || MONITOR_STATUS_UI.idle;
  const msIcon = $('#msIcon');
  if (msIcon) msIcon.textContent = ui[0];
  const msTitle = $('#msTitle');
  if (msTitle) msTitle.textContent = ui[1];
  const msSub = $('#msSub');
  if (msSub) msSub.textContent = ui[2];

  const busy = status === 'running' || status === 'paused';
  $('#btnStart').disabled = busy;
  $('#btnStart').textContent = busy ? '运行中…' : '⚡ 一键全流程';
  ['btnRunCollect', 'btnRunMatch', 'btnRunApply'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.disabled = busy;
  });
  $('#btnStop').disabled = !busy;
  $('#btnPause').disabled = !busy;
  $('#btnPause').textContent = status === 'paused' ? '▶ 继续' : '⏸ 暂停';
  if (status === 'done') setProgress(100);
  if (status === 'idle') setProgress(0);
}

function setProgress(percent) {
  const p = Math.min(100, Math.max(0, percent));
  $('#progressBar').style.width = `${p}%`;
  const el = $('#progressPercent');
  if (el) el.textContent = `${Math.round(p)}%`;
}

/* ---- 运行流程可视化 ---- */

const FLOW_STEPS = ['auth', 'collect', 'match', 'apply'];

const STAGE_LABEL = { auth: '登录检查', collect: '采集岗位', match: '匹配打分', apply: '投递沟通' };
const STAGE_ACTIVE_TEXT = {
  auth: '正在检查登录状态…',
  collect: '正在采集岗位…（按拟人节奏翻页，耗时取决于配置）',
  match: '正在匹配打分…（用简历逐条对比岗位）',
  apply: '正在投递沟通…（低频间隔发送，请耐心等待）',
};

function setFlowState(step, status) {
  const el = document.querySelector(`.flow-step[data-step="${step}"]`);
  if (!el) return;
  el.classList.remove('pending', 'active', 'done', 'err');
  el.classList.add(status);
  const stage = document.getElementById('msStage');
  if (stage) {
    const label = STAGE_LABEL[step] || step;
    if (status === 'active') stage.textContent = `当前阶段：${STAGE_ACTIVE_TEXT[step] || label}`;
    else if (status === 'done') stage.textContent = `当前阶段：${label}已完成 ✔`;
    else if (status === 'err') stage.textContent = `当前阶段：${label}出错，请查看日志`;
  }
}

function setFlowSub(step, text) {
  const el = document.querySelector(`.flow-step[data-step="${step}"] .fs-sub`);
  if (el) el.textContent = text || '';
}

function resetFlow() {
  FLOW_STEPS.forEach(s => { setFlowState(s, 'pending'); setFlowSub(s, ''); });
  const stage = document.getElementById('msStage');
  if (stage) stage.textContent = '当前阶段：等待任务启动';
}

function renderStats() {
  const s = state.run.stats;
  $('#statCollected').textContent = s.collected;
  $('#statMatched').textContent = s.matched;
  $('#statApplied').textContent = s.applied;
  $('#statFailed').textContent = s.failed;
  setFlowSub('collect', s.collected ? `${s.collected} 个` : '');
  setFlowSub('match', s.matched ? `${s.matched} 个` : '');
  setFlowSub('apply', s.applied ? `${s.applied} 个` : '');
}

const LOG_ICON = { SYSTEM: '·', INFO: '·', OK: '✅', WARN: '⚠️', ERROR: '❌' };

function appendLog(level, message, timeText) {
  const box = $('#logBox');
  const el = document.createElement('div');
  el.className = `log-line log-${level}`;
  el.innerHTML = `<span class="log-t">${timeText || fmtClock()}</span><span class="log-i">${LOG_ICON[level] || '·'}</span><span class="log-m">${esc(message)}</span>`;
  box.appendChild(el);
  while (box.children.length > 400) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

const logInfo = m => appendLog('INFO', m);
const logOk = m => appendLog('OK', m);
const logWarn = m => appendLog('WARN', m);
const logErr = m => appendLog('ERROR', m);

function showVerifyBanner(show, text) {
  $('#verifyBanner').classList.toggle('hidden', !show);
  if (show && text) {
    const el = $('#verifyText');
    if (el) el.textContent = text;
  }
}

/* ===================== 任务控制 ===================== */

/* 真实采集：调用后端执行 Boss 真实采集（原生标签 + 页面内请求，低频防风控） */
async function realCollect(cfg, opts = {}) {
  const r = await fetch('/api/collect/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      platforms: cfg.platforms,
      keywords: cfg.keywords,
      cities: cfg.cities,
      max_jobs: cfg.max_jobs,
      max_jobs_by_platform: cfg.max_jobs_by_platform,
      max_pages_by_platform: cfg.max_pages_by_platform,
      verify_body_by_platform: cfg.verify_body_by_platform,
      pace_by_platform: cfg.pace_by_platform,
      max_pages: cfg.max_pages,
      keyword_mode: cfg.keyword_mode,
      salary_min: cfg.salary_min,
      salary_max: cfg.salary_max,
      experience: cfg.experience,
      education: cfg.education,
      hr_active_days: cfg.hr_active_days,
      kw_delay_min: cfg.kw_delay_min,
      kw_delay_max: cfg.kw_delay_max,
      page_delay_min: cfg.page_delay_min,
      page_delay_max: cfg.page_delay_max,
      shuffle_keywords: cfg.shuffle_keywords,
      humanize_scroll: cfg.humanize_scroll,
      flow_label: state.run.label || '',
      ...opts,
    }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

/* ===================== 运行入口（当前表单配置；改动自动同步到本地文件） ===================== */

const RUN_KINDS = {
  all:     { stages: ['collect', 'match', 'apply'], text: '一键全做' },
  collect: { stages: ['collect'], text: '仅采集' },
  match:   { stages: ['match'],   text: '仅匹配' },
  apply:   { stages: ['apply'],   text: '仅投递' },
};

function startRun(kind = 'all') {
  const cfg = saveConfig(true);
  const err = validateForStart(cfg, kind);
  if (err) { toast(err, 'warn'); return; }
  void beginRun(cfg, '当前表单', kind);
}

async function beginRun(cfg, label, kind = 'all') {
  state.run.stats = { collected: 0, matched: 0, applied: 0, failed: 0 };
  state.run.label = label;
  renderStats();
  resetFlow();
  setFlowState('auth', 'active');
  setFlowSub('auth', '检查中…');
  setProgress(0);
  setRunStatus('running');
  showVerifyBanner(false);
  switchTab('monitor');
  appendLog('SYSTEM', `──────────────── 任务启动（${new Date().toLocaleString()}）────────────────`);
  const kindInfo = RUN_KINDS[kind] || RUN_KINDS.all;
  const modeText = cfg.keyword_mode === 'combined' ? '组合搜索' : '逐个搜索';
  logInfo(`来源：${label} ｜ 模式：${kindInfo.text} ｜ 平台：${cfg.platforms.map(n => PLATFORM_DISPLAY[n]).join('、')} ｜ 关键词：${cfg.keywords.join('、')}（${modeText}） ｜ 城市：${cfg.cities.join('、')}`);

  // 启动前预检：服务端已有任务在跑 → 直接接入实时监控，不重复启动
  try {
    const s = await fetchTaskStatus();
    if (s.active || s.busy) {
      taskPoll.inFetch = false;
      taskPoll.wasActive = true;
      taskPoll.doneHandled = false;
      applyTaskSnapshot(s);
      startTaskPoll();
      logWarn('检测到后台已有任务在运行 —— 已接入实时监控，无需重复启动');
      toast('已有任务在运行，已接入实时监控', 'warn');
      return;
    }
  } catch { /* 预检失败不阻断启动 */ }

  taskPoll.inFetch = true;
  taskPoll.wasActive = false;
  taskPoll.doneHandled = false;
  await bootstrapPollCursor();
  startTaskPoll();

  runPipeline(cfg, kindInfo.stages);
}

function onPauseClick() {
  if (state.run.status === 'running') {
    taskPoll.pauseAckAt = Date.now();
    setRunStatus('paused');
    logWarn('已发送暂停请求：当前操作完成后冻结（点「继续」恢复）');
    fetch('/api/task/pause', { method: 'POST' }).catch(() => {});
  } else if (state.run.status === 'paused') {
    resumeRun();
  }
}

function resumeRun() {
  const bannerVisible = !$('#verifyBanner').classList.contains('hidden');
  if (state.run.status !== 'paused' && !bannerVisible) return;
  taskPoll.verifyAckAt = Date.now();
  fetch('/api/task/verify-resolved', { method: 'POST' }).catch(() => {});
  setRunStatus('running');
  showVerifyBanner(false);
  logInfo('已确认验证处理完成，正在继续任务…');
  fetch('/api/task/resume', { method: 'POST' }).catch(() => {});
}

function stopRun() {
  if (state.run.status !== 'running' && state.run.status !== 'paused') return;
  setRunStatus('stopped');
  resetFlow();
  showVerifyBanner(false);
  // 通知服务端真正停止：协作式取消，在下一个检查点安全退出，已处理数据保留
  fetch('/api/task/stop', { method: 'POST' }).catch(() => {});
  logWarn('已发送停止请求：当前操作完成后立即停止（已采集 / 已投递的数据会保留）');
}

/* ============ 任务运行引擎（真实调用：采集 / 匹配 / 投递） ============ */

/* 阶段 0：登录检查 —— 真实查询各平台登录态（只读 cookie，零页面接触）
   查询失败不阻断：后端执行采集时仍会逐平台检查并跳过未登录平台 */
async function checkLoginStage(platforms) {
  const selected = Array.isArray(platforms) ? platforms : [];
  setFlowState('auth', 'active');
  setFlowSub('auth', '检查中…');
  if (!selected.length) {
    setFlowState('auth', 'done');
    setFlowSub('auth', '未选择平台');
    return true;
  }
  try {
    const resp = await fetch('/api/platforms', { signal: AbortSignal.timeout(8000) });
    const list = await resp.json();
    const by = new Map((Array.isArray(list) ? list : []).map(p => [p.name, p]));
    const missing = selected.filter(n => (by.get(n) || {}).status !== 'logged_in');
    const okCount = selected.length - missing.length;
    if (missing.length) {
      const names = missing.map(n => PLATFORM_DISPLAY[n] || n).join('、');
      setFlowSub('auth', `已登录 ${okCount}/${selected.length} · 未登录：${names}`);
      logWarn(`登录检查：${names} 未登录 —— 执行到该平台时会自动跳过（可到主页平台卡片点「去登录」）`);
      if (okCount === 0) toast('所选平台均未登录，运行时会全部跳过 —— 请先点「去登录」', 'warn');
    } else {
      setFlowSub('auth', `已登录 ${okCount}/${selected.length}`);
      logOk('登录检查：所选平台全部已登录');
    }
    setFlowState('auth', 'done');
    return missing.length === 0;
  } catch {
    setFlowState('auth', 'done');
    setFlowSub('auth', '状态查询失败（执行时后端仍会逐平台检查）');
    return true;
  }
}

async function runPipeline(cfg, stages) {
  const startedAt = Date.now();
  const flowMode = stages.length > 1 ? 'all' : stages[0];   // 服务端任务标记：all / collect / match / apply
  // 采集 / 匹配阶段是真实调用：进度按阶段占位，投递阶段从 applyBase 起算
  const progressBase = stages.includes('collect') ? (stages.length > 1 ? 12 : 100) : 0;
  const applyBase = stages.includes('match') ? Math.max(progressBase, 60) : progressBase;

  try {
    /* 阶段 0：登录检查（真实查询各平台登录态；查询失败不阻断） */
    await checkLoginStage(cfg.platforms);

    /* 采集（真实采集：通过 Edge 访问 Boss，低频防风控） */
    if (stages.includes('collect')) {
      setFlowState('collect', 'active');
      const collectLimitDesc = cfg.platforms
        .map(n => `${PLATFORM_DISPLAY[n]} ${cfg.max_jobs_by_platform[n] ?? cfg.max_jobs}`)
        .join(' · ');
      logInfo(`阶段：开始真实采集（关键词 ${cfg.keywords.join('、')} · ${cfg.cities.join('、')} · 上限 ${collectLimitDesc}）…`);
      let d;
      try {
        d = await realCollect(cfg, { flow: flowMode });
      } catch (e) {
        logErr(`采集失败：${e.message || e}`);
        setFlowState('collect', 'err');
        throw new Error('__abort__');
      }
      state.run.stats.collected = d.collected;
      renderStats();
      renderResumeBar((d && d.resume) || []);
      if (d.stopped) {
        logWarn(`采集已手动停止：保留已采到的 ${d.collected} 个（新入库 ${d.new} 个）`);
        void loadRealJobs();
        void loadTodayStats();
        throw 'stopped';
      }
      if (d.by_platform) {
        Object.entries(d.by_platform).forEach(([name, v]) => {
          const label = PLATFORM_DISPLAY[name] || name;
          if (v.ok) logInfo(`${label}：采集 ${v.collected} 个（扫描 ${v.scanned} · 过滤 ${v.filtered}${v.body_dropped ? ` · 正文弃 ${v.body_dropped}` : ''}${v.body_rescued ? ` · 正文救回 ${v.body_rescued}` : ''}）`);
          else logWarn(`${label}：已跳过（${v.reason || '未知原因'}）`);
        });
      }
      (d.samples || []).forEach(j =>
        logOk(`采集到岗位：${j.title} @ ${j.company}（${j.salary || '-'} · ${j.city || '-'}）`)
      );
      logOk(
        `采集完成：共 ${d.collected} 个` +
        (d.capped ? '（部分平台已达各自上限，提前结束）' : '（结果已翻完）') +
        ` ｜ 扫描 ${d.scanned} · 过滤 ${d.filtered} · 新入库 ${d.new}`
      );
      setFlowState('collect', 'done');
      setProgress(progressBase);
      void loadRealJobs();   // 采集完成即刷新采集记录（无论当前在哪个页）
    }

    /* 匹配（真实：简历档案 × 数据库岗位，打分落库） */
    if (stages.includes('match')) {
      setFlowState('match', 'active');
      logInfo(`阶段：开始真实匹配打分（阈值 ${cfg.threshold} 分 ｜ 硬过滤：城市/学历/经验/标题黑名单）…`);
      let d;
      try {
        d = await realMatch(cfg, { flow: flowMode });
      } catch (e) {
        logErr(`匹配失败：${e.message || e}`);
        setFlowState('match', 'err');
        throw new Error('__abort__');
      }
      state.run.stats.matched = d.matched;
      state.lastMatched = d.top || [];
      renderStats();
      if (d.stopped) {
        logWarn(`匹配已手动停止（已完成 ${d.scanned} 个）`);
        void loadMatches();
        throw 'stopped';
      }
      (d.top || []).slice(0, 5).forEach(j =>
        logOk(`匹配通过：${j.title} @ ${j.company}（${j.score} 分）`)
      );
      if (!d.scanned) {
        logWarn('数据库里还没有岗位 —— 先回「主页」跑一次「开始采集」，再来匹配');
      } else {
        logOk(`匹配完成：扫描 ${d.scanned} 个 ｜ 通过 ${d.matched} 个 ｜ 未通过 ${d.rejected} 个`);
      }
      setFlowState('match', 'done');
      if (stages.includes('apply')) setProgress(applyBase);
      void loadMatches();
    }

    /* 投递（真实：对匹配通过的岗位发起沟通，服务器内带节奏与每日限额） */
    if (stages.includes('apply')) {
      setFlowState('apply', 'active');
      const dailyLimitDesc = cfg.platforms
        .map(n => `${PLATFORM_DISPLAY[n]} ${cfg.daily_limit_by_platform[n] ?? cfg.daily_limit}`)
        .join(' · ');
      logInfo(`阶段：开始真实投递（间隔 ${cfg.delay_min}-${cfg.delay_max} 秒 ｜ 每日上限 ${dailyLimitDesc}｜可能持续几分钟，期间请勿关闭页面）…`);
      let d;
      try {
        d = await realApply(cfg, { flow: flowMode });
      } catch (e) {
        logErr(`投递失败：${e.message || e}`);
        setFlowState('apply', 'err');
        throw new Error('__abort__');
      }
      (d.results || []).forEach(r => {
        const label = PLATFORM_DISPLAY[r.platform] || r.platform || '';
        if (r.success) logOk(`投递成功：${label} ${r.company} · ${r.title}（${r.message}）`);
        else logErr(`投递失败：${label} ${r.title}（${r.message}）`);
      });
      state.run.stats.applied = d.applied;
      state.run.stats.failed = d.failed;
      renderStats();
      if (d.stopped) {
        logWarn(`投递已手动停止：成功 ${d.applied} · 失败 ${d.failed}（已投数据已入库）`);
        void loadMatches();
        void loadApplications();
        void loadTodayStats();
        throw 'stopped';
      }
      if (d.queued === 0) {
        logWarn(d.message || '没有待投递的岗位');
        toast('没有待投递的岗位 —— 先跑一次「匹配」，或全部已投递', 'warn');
      } else {
        logOk(`投递完成：成功 ${d.applied} 个 ｜ 失败 ${d.failed} 个 ｜ 今日各平台合计已投 ${d.daily_used} 个${d.elapsed != null ? `（用时 ${d.elapsed} 秒）` : ''}`);
        toast('投递完成', 'success');
      }
      if (d.need_verify) {
        logWarn('有平台需要安全验证（验证码），已跳过该平台；处理验证后重新点「开始投递」可继续该平台剩余岗位');
        toast('有平台需要验证码，已跳过（其余平台继续）', 'warn');
      }
      setFlowState('apply', 'done');
      setProgress(100);
      void loadMatches();
      void loadApplications();
    }

    taskPoll.doneHandled = true;   // 本地已处理完成，避免轮询重复提示
    setRunStatus('done');
    if (!stages.includes('match') && !stages.includes('apply')) {
      logOk(`任务完成：本次真实采集 ${state.run.stats.collected} 个岗位，已写入本地数据库（用时 ${Math.round((Date.now() - startedAt) / 1000)} 秒）`);
    } else {
      logOk(`任务完成：采集 ${state.run.stats.collected} 个 ｜ 匹配通过 ${state.run.stats.matched} 个 ｜ 投递成功 ${state.run.stats.applied} 个，失败 ${state.run.stats.failed} 个（用时 ${Math.round((Date.now() - startedAt) / 1000)} 秒）`);
    }
    toast('任务完成', 'success');
  } catch (e) {
    if (e === 'stopped') {
      logWarn('任务已手动停止');
    } else if (e && e.message === '__abort__') {
      setRunStatus('idle');
    } else {
      logErr(`任务异常：${e.message || e}`);
      const act = document.querySelector('.flow-step.active');
      if (act) { act.classList.remove('active'); act.classList.add('err'); }
      setRunStatus('idle');
    }
  }
  // 实时监控收尾：本地流程结束 → 拉一次最终状态渲染横幅；正常完成则停轮询
  taskPoll.inFetch = false;
  void loadTodayStats();   // 任务结束刷新「今日数据」
  if (state.run.status === 'done') {
    void pollTaskOnce().finally(() => stopTaskPoll());
  }
}

/* ===================== 结果表格 / 导出 ===================== */

const STATUS_TEXT = { applied: '已投递', failed: '失败', matched: '待投递', rejected: '已跳过' };

function renderJobs() {
  const st = $('#filterStatus').value;
  const q = $('#searchBox').value.trim().toLowerCase();
  const rows = state.jobs.filter(j =>
    (!st || j.status === st) &&
    (!q || (j.title || '').toLowerCase().includes(q) || (j.company || '').toLowerCase().includes(q))
  );
  const tbody = $('#jobsTbody');
  tbody.innerHTML = rows.map(j => `
    <tr>
      <td>${esc(PLATFORM_DISPLAY[j.platform] || j.platform)}</td>
      <td>${esc(j.title || '-')}</td>
      <td>${esc(j.company || '-')}</td>
      <td>${esc(j.salary || '-')}</td>
      <td>${esc(j.city || '-')}</td>
      <td><span class="score ${j.score >= 80 ? 'score-high' : j.score >= 60 ? '' : 'score-low'}">${j.score ?? '-'}</span></td>
      <td><span class="status-pill status-${j.status}">${STATUS_TEXT[j.status] || j.status}</span></td>
      <td>${esc(j.time)}</td>
      <td>${j.status === 'failed' ? `<button class="btn btn-ghost btn-sm" data-retry="${j.job_id}">重试</button>` : j.status === 'matched' ? `<button class="btn btn-ghost btn-sm" data-apply="${j.job_id}">投递</button>` : ''}</td>
    </tr>`).join('');
  $('#tableEmpty').classList.toggle('hidden', rows.length > 0);
  $('#jobsTable').classList.toggle('hidden', rows.length === 0);
}

/* ---- 采集记录（真实数据，来自数据库） ---- */

const COLLECT_STATUS = {
  collected: '已采集',
  matched: '已匹配',
  rejected: '未过筛',
  skipped: '未过筛',
  applied: '已投递',
  failed: '投递失败',
};

async function loadRealJobs() {
  try {
    const rows = await (await fetch('/api/jobs')).json();
    if (Array.isArray(rows)) state.realJobs = rows;
  } catch { /* 后端异常时保留现有数据 */ }
  renderCollect();
}

function renderCollect() {
  const rows = state.realJobs;
  $('#collectTbody').innerHTML = rows.map(r => `
    <tr>
      <td>${esc(PLATFORM_DISPLAY[r.platform] || r.platform)}</td>
      <td>${esc(r.title || '-')}</td>
      <td>${esc(r.company || '-')}</td>
      <td>${esc(r.salary || '-')}</td>
      <td>${esc(r.city || '-')}</td>
      <td>${esc(r.hr_active_desc ? r.hr_active_desc : (r.hr_active_days != null ? `${r.hr_active_days} 天` : '—'))}</td>
      <td><span class="status-pill status-${r.status}">${COLLECT_STATUS[r.status] || r.status}</span></td>
      <td>${esc(String(r.collected_at || '').slice(5, 16))}</td>
    </tr>`).join('');
  $('#collectEmpty').classList.toggle('hidden', rows.length > 0);
  $('#collectTable').classList.toggle('hidden', rows.length === 0);
  $('#collectCount').textContent = rows.length ? `共 ${rows.length} 条` : '';
}

/* ---- 匹配记录（真实数据，来自数据库） ---- */

const MATCH_STATUS = {
  matched: '匹配通过', rejected: '未通过',
  applied: '已投递', failed: '投递失败', collected: '已采集', skipped: '已跳过',
};

async function realMatch(cfg, opts = {}) {
  const r = await fetch('/api/match/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      threshold: cfg.threshold,
      salary_min: cfg.salary_min,
      salary_max: cfg.salary_max,
      cities: cfg.cities,
      blacklist: cfg.blacklist,
      flow_label: state.run.label || '',
      ...opts,
    }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

async function loadMatches() {
  try {
    const d = await (await fetch('/api/matches')).json();
    if (d && Array.isArray(d.rows)) state.matches = d.rows;
  } catch { /* 后端异常时保留现有数据 */ }
  renderMatchLog();
}

/* ---- 投递（真实：对匹配通过的岗位发起沟通） ---- */

async function realApply(cfg, opts = {}) {
  const r = await fetch('/api/apply/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      daily_limit: cfg.daily_limit,
      daily_limit_by_platform: cfg.daily_limit_by_platform,
      pace_by_platform: cfg.pace_by_platform,
      delay_min: cfg.delay_min,
      delay_max: cfg.delay_max,
      greeting: cfg.greeting,
      ...opts,
    }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

/* ---- 投递记录（真实数据，来自数据库） ---- */

async function loadApplications() {
  try {
    const d = await (await fetch('/api/applications')).json();
    if (d && Array.isArray(d.rows)) {
      state.jobs = d.rows.map(r => ({
        id: r.id, job_id: r.job_id, kind: r.kind,
        platform: r.platform, title: r.title, company: r.company,
        salary: r.salary, city: r.city, score: r.score,
        status: r.status, message: r.message,
        time: String(r.time || '').slice(5, 16),
      }));
    }
  } catch { /* 后端异常时保留现有数据 */ }
  renderJobs();
}

const fmtPct = v => (v == null ? '-' : `${Math.round(v * 100)}%`);

function renderMatchLog() {
  const filter = ($('#matchFilter') || {}).value || 'all';
  const rows = state.matches.filter(r => filter === 'all' || r.status === filter);
  $('#matchTbody').innerHTML = rows.map(r => {
    const det = r.detail || {};
    const jn = (det.job_skills || []).length;
    const hit = det.hit_skills || [];
    const hitTxt = jn ? `${hit.length}/${jn}` : '—';
    const hitTip = jn
      ? `命中：${hit.join('、') || '无'}\n缺少：${(det.missing_skills || []).join('、') || '无'}`
      : '岗位未标注技能（按中性值计分）';
    return `
    <tr class="match-row" data-id="${r.id}">
      <td><span class="score-pill ${r.status === 'matched' ? 'sp-hi' : 'sp-lo'}">${r.match_score == null ? '-' : Number(r.match_score).toFixed(1)}</span></td>
      <td>${esc(r.title || '-')}</td>
      <td>${esc(r.company || '-')}</td>
      <td>${esc(r.salary || '-')}</td>
      <td>${esc(r.city || '-')}</td>
      <td title="${esc(hitTip)}">${hitTxt}</td>
      <td><span class="status-pill status-${r.status}">${MATCH_STATUS[r.status] || r.status}</span></td>
      <td>${esc(String(r.matched_at || '').slice(5, 16))}</td>
    </tr>`;
  }).join('');
  $('#matchEmpty').classList.toggle('hidden', rows.length > 0);
  $('#matchTable').classList.toggle('hidden', rows.length === 0);
  $('#matchCount').textContent = rows.length ? `共 ${rows.length} 条` : '';
}

function initMatchEvents() {
  $('#matchTbody').addEventListener('click', e => {
    const tr = e.target.closest('tr.match-row');
    if (!tr) return;
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('match-detail')) { next.remove(); return; }
    const r = state.matches.find(x => String(x.id) === tr.dataset.id);
    if (!r) return;
    const det = r.detail || {};
    const el = document.createElement('tr');
    el.className = 'match-detail';
    const parts = [
      `技能覆盖 <b>${fmtPct(det.skill_coverage)}</b>`,
      `名称相似 <b>${fmtPct(det.title_sim)}</b>`,
      `薪资匹配 <b>${fmtPct(det.salary_fit)}</b>`,
      `加分 <b>${det.bonus ?? 0}/15</b>`,
      `阈值 <b>${det.threshold ?? '-'}</b>`,
    ];
    el.innerHTML = `<td colspan="8">
      <div class="md-grid">${parts.map(p => `<span>${p}</span>`).join('')}</div>
      <div class="md-line">命中技能：${esc((det.hit_skills || []).join('、') || '无')} ｜ 岗位要求：${esc((det.job_skills || []).join('、') || '未标注')} ｜ 缺失：${esc((det.missing_skills || []).join('、') || '无')}</div>
      <div class="md-line">岗位要求：${esc(det.job_experience || '经验不限')} · ${esc(det.job_degree || '学历不限')}${det.reason ? ` ｜ 判定：${esc(det.reason)}` : ''}</div>
      ${(det.bonus_notes || []).length ? `<div class="md-line">加分明细：${esc(det.bonus_notes.join(' ｜ '))}</div>` : ''}
    </td>`;
    tr.after(el);
  });
  $('#btnRefreshMatch').addEventListener('click', async () => {
    await loadMatches();
    toast('匹配记录已刷新', 'success');
  });
  $('#matchFilter').addEventListener('change', renderMatchLog);
}

function exportCsv() {
  if (!state.jobs.length) { toast('暂无记录可导出', 'warn'); return; }
  const header = ['平台', '岗位', '公司', '薪资', '城市', '匹配分', '状态', '时间'];
  const lines = [header.join(',')];
  for (const j of state.jobs) {
    const cells = [
      PLATFORM_DISPLAY[j.platform] || j.platform, j.title, j.company,
      j.salary || '', j.city || '', j.score ?? '', STATUS_TEXT[j.status] || j.status, j.time,
    ];
    lines.push(cells.map(c => `"${String(c).replace(/"/g, '""')}"`).join(','));
  }
  const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  const d = new Date();
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}_${String(d.getHours()).padStart(2, '0')}${String(d.getMinutes()).padStart(2, '0')}`;
  a.download = `投递记录_${stamp}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast('已导出 CSV', 'success');
}

function initResultsEvents() {
  $('#jobsTbody').addEventListener('click', async e => {
    const retryBtn = e.target.closest('[data-retry]');
    const applyBtn = e.target.closest('[data-apply]');
    if (!retryBtn && !applyBtn) return;
    const btn = retryBtn || applyBtn;
    const jobId = parseInt(btn.dataset.retry || btn.dataset.apply, 10);
    const row = state.jobs.find(r => r.job_id === jobId);
    if (!row || Number.isNaN(jobId)) return;
    if (applyBtn && !window.confirm(`确认向「${row.company} · ${row.title}」发起沟通？`)) return;
    btn.disabled = true;
    btn.textContent = '投递中…';
    try {
      const cfg = saveConfig(true);
      const d = await realApply(cfg, { job_ids: [jobId], limit: 1 });
      const res = (d.results || [])[0];
      if (res && res.success) {
        toast('已投递', 'success');
        appendLog('OK', `${applyBtn ? '手动投递' : '手动重试'}成功：${row.company} · ${row.title}`);
      } else {
        toast(`投递失败：${(res && res.message) || d.message || '未知原因'}`, 'error');
      }
    } catch (err) {
      toast(`投递失败：${err.message || err}`, 'error');
    }
    await loadApplications();
    void loadMatches();
  });
  $('#filterStatus').addEventListener('change', renderJobs);
  $('#searchBox').addEventListener('input', renderJobs);
  $('#btnExport').addEventListener('click', exportCsv);
}

/* ===================== 初始化 ===================== */

function init() {
  $('#realBadge').classList.remove('hidden');
  initThemePicker();

  $$('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));

  initPlatformEvents();
  initDropZone();
  initResultsEvents();
  initMatchEvents();

  kwTags = createTagInput($('#keywordTags'), $('#keywordInput'), () => {
    renderKeywordMode();
    scheduleDraftSave();
  });
  cityTags = createTagInput($('#cityTags'), $('#cityInput'), () => scheduleDraftSave());
  blTags = createTagInput($('#blacklistTags'), $('#blacklistInput'), () => scheduleDraftSave(), { splitter: /[,，、;；\s]+/ });

  initKeywordMode();
  initChipGroup('#experienceChips', EXPERIENCE_OPTIONS, expSelected);
  initChipGroup('#educationChips', EDUCATION_OPTIONS, eduSelected);

  $('#btnPacePreset').addEventListener('click', fillPacePreset);

  $('#cityPresets').innerHTML = CITY_PRESETS.map(c =>
    `<button type="button" class="preset" data-city="${c}">+ ${c}</button>`).join('');
  $('#cityPresets').addEventListener('click', e => {
    const b = e.target.closest('[data-city]');
    if (b && cityTags.add(b.dataset.city)) b.remove();
  });

  $('#threshold').addEventListener('input', e => {
    $('#thresholdNum').value = e.target.value;
  });
  $('#thresholdNum').addEventListener('input', e => {
    const n = parseInt(e.target.value, 10);
    if (!Number.isNaN(n)) $('#threshold').value = Math.min(100, Math.max(0, n));
  });
  $('#thresholdNum').addEventListener('change', () => {
    const v = clampInt($('#thresholdNum').value, 0, 100, clampInt($('#threshold').value, 0, 100, 60));
    $('#threshold').value = v;
    $('#thresholdNum').value = v;
  });
  bindDraftAutosave();
  $('#btnStart').addEventListener('click', () => startRun('all'));
  $('#btnRunCollect').addEventListener('click', () => startRun('collect'));
  $('#btnRunMatch').addEventListener('click', () => startRun('match'));
  $('#btnRunApply').addEventListener('click', () => startRun('apply'));
  $('#btnPause').addEventListener('click', onPauseClick);
  $('#btnStop').addEventListener('click', stopRun);
  $('#btnResume').addEventListener('click', resumeRun);
  $('#btnClearLog').addEventListener('click', () => {
    $('#logBox').innerHTML = '';
    appendLog('SYSTEM', '日志已清空');
  });
  $('#btnLogFilter').addEventListener('click', () => {
    const on = $('#logBox').classList.toggle('only-important');
    $('#btnLogFilter').textContent = on ? '显示全部' : '只看重要';
  });
  $('#btnRefreshCollect').addEventListener('click', async () => {
    await loadRealJobs();
    toast('采集记录已刷新', 'success');
  });
  $('#btnGoResume').addEventListener('click', () => switchTab('resume'));

renderPlatformLimits();   // 每平台 采集/投递 数量输入框（必须在 loadConfig 之前渲染）
renderPaceGrid();         // 防风控方案（按平台差异化节奏）
  loadConfig();
  refreshPlatformStatus();
  void loadTodayStats();
  void loadCurrentResume();
  renderStats();
  setRunStatus('idle');
  appendLog('SYSTEM', '等待任务启动…先在「配置」页完成平台选择、简历上传与岗位条件设置。');
  void restoreTaskState();   // 页面加载 / 刷新后：若后台有任务在跑，直接接入实时监控
}

document.addEventListener('DOMContentLoaded', init);

/* ===================== 界面风格切换（多皮肤） ===================== */

const THEMES = [
  { slug: 'neumorphism', name: '新拟物',   desc: '双光源软塑 · 默认' },
  { slug: 'brutalist',   name: '新野兽派', desc: '黑框硬影 · 冲击力' },
  { slug: 'editorial',   name: '编辑杂志', desc: '衬线单色 · 大留白' },
  { slug: 'bento',       name: '便当盒',   desc: '圆角卡片 · 轻浮起' },
  { slug: 'dark',        name: '暗黑模式', desc: '深蓝夜色 · 高亮青蓝' },
  { slug: 'corporate',   name: '企业简洁', desc: '白卡蓝钮 · 专业利落' },
];
const THEME_KEY = 'ui_theme';

function currentTheme() {
  const t = localStorage.getItem(THEME_KEY);
  return THEMES.some(x => x.slug === t) ? t : 'neumorphism';
}

function applyTheme(slug, opts = {}) {
  if (!THEMES.some(x => x.slug === slug)) slug = 'neumorphism';
  localStorage.setItem(THEME_KEY, slug);
  document.documentElement.setAttribute('data-theme', slug);
  $$('link[data-theme-css]').forEach(l => { l.disabled = (l.dataset.themeCss !== slug); });
  const meta = THEMES.find(x => x.slug === slug);
  const nameEl = $('#themeName');
  if (nameEl) nameEl.textContent = meta.name;
  $$('#themeMenu .theme-opt').forEach(b => b.classList.toggle('selected', b.dataset.slug === slug));
  if (!opts.silent) toast(`界面风格已切换：${meta.name}`, 'success');
}

function initThemePicker() {
  const menu = $('#themeMenu'), btn = $('#btnTheme');
  if (!menu || !btn) return;
  menu.innerHTML = THEMES.map(t =>
    `<button type="button" class="theme-opt" data-slug="${t.slug}" role="option">` +
    `<span class="to-name">${t.name}</span><span class="to-desc">${t.desc}</span>` +
    `<span class="to-check">✓</span></button>`).join('');
  const close = () => { menu.classList.add('hidden'); btn.setAttribute('aria-expanded', 'false'); };
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const nowHidden = menu.classList.toggle('hidden');
    btn.setAttribute('aria-expanded', String(!nowHidden));
  });
  menu.addEventListener('click', e => {
    const opt = e.target.closest('.theme-opt');
    if (!opt) return;
    applyTheme(opt.dataset.slug);
    close();
  });
  document.addEventListener('click', e => {
    if (!menu.classList.contains('hidden') && !e.target.closest('.theme-picker')) close();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  // 同步初始状态（首屏样式已由 <head> 内联脚本应用）
  applyTheme(currentTheme(), { silent: true });
}

window.applyTheme = applyTheme;
