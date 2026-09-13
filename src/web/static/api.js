/* =====================================================================
   通用工具：fetch 封装（鉴权/CSRF/超时）、DOM 助手、格式化、toast
   无任何第三方依赖；与 app.js / modules/* 共享全局 API。
   ===================================================================== */
'use strict';

const API = {
  targets: '/api/targets',
  // 调整标的信息顺序（同时决定邮件里各标的的先后）
  targetOrder: '/api/target-order',
  asset: (c) => '/api/asset/' + encodeURIComponent(c),
  kline: (c, y) => '/api/asset/' + encodeURIComponent(c) + '/kline?years=' + (y || META.chartYears),
  valuation: (c, y) => '/api/asset/' + encodeURIComponent(c) + '/valuation?years=' + (y || META.chartYears),
  score: (c, y) => '/api/asset/' + encodeURIComponent(c) + '/score?years=' + (y || META.mainYears),
  percentiles: (c) => '/api/asset/' + encodeURIComponent(c) + '/percentiles',
  weights: (c) => '/api/asset/' + encodeURIComponent(c) + '/weights',
  // 把标的移出标的信息（删除 valuation_target 记录）
  targetDelete: (c) => '/api/target/' + encodeURIComponent(c),
  search: (q) => '/api/stock/search?q=' + encodeURIComponent(q),
  indexConstituents: (c) => '/api/index/' + encodeURIComponent(c) + '/constituents',
  // 批量移除成分股（勾选多只后一次提交，后端只重算一次）
  indexConstituentsRemove: (c) => '/api/index/' + encodeURIComponent(c)
    + '/constituents/remove',
  indexWeights: (c) => '/api/index/' + encodeURIComponent(c) + '/weights',
  portfolios: '/api/portfolio',
  portfolio: (c) => '/api/portfolio/' + encodeURIComponent(c),
  portfolioConstituents: (c) => '/api/portfolio/' + encodeURIComponent(c) + '/constituents',
  portfolioScore: (c) => '/api/portfolio/' + encodeURIComponent(c) + '/score',
  // 组合整条重算：组合K线（成分股等权合成）→ 历史评分 → 当日评分
  portfolioRebuild: (c) => '/api/portfolio/' + encodeURIComponent(c) + '/rebuild',
  settingsEditable: '/api/settings/editable',
  dashboard: '/api/dashboard',
  marketSync: '/api/market/sync',
  stocks: (after, limit) => '/api/stocks?limit=' + (limit || 60)
    + (after ? '&after=' + encodeURIComponent(after) : ''),
  syncStock: (c) => '/api/stock/' + encodeURIComponent(c) + '/sync',
  syncTarget: (c) => '/api/target/' + encodeURIComponent(c) + '/sync',
  targetScore: (c) => '/api/target/' + encodeURIComponent(c) + '/score',
  settingsPut: (k) => '/api/settings/' + encodeURIComponent(k),
  settingsCreate: '/api/settings',
  settingsDelete: (k) => '/api/settings/' + encodeURIComponent(k),
  settingsSections: '/api/settings/sections',
  health: '/api/health',
  // 已发送邮件的正文（后端直接返回自包含 HTML，新窗口打开）。
  // window.open 带不了请求头，所以令牌走 ?token= 查询参数。
  mailHtml: (id) => '/api/mail/' + encodeURIComponent(id) + '/html'
    + (authToken() ? '?token=' + encodeURIComponent(authToken()) : ''),
  // 按正文快照 key（构建日期 / manual-时间戳）取正文：首页"待发送"那封还没发送，
  // 在 mail_log 里没有 id，只能按 key 取。
  mailBody: (key) => '/api/mail/body/' + encodeURIComponent(key) + '/html'
    + (authToken() ? '?token=' + encodeURIComponent(authToken()) : ''),
  scheduler: '/api/scheduler/jobs',
  jobRun: (id) => '/api/scheduler/jobs/' + encodeURIComponent(id) + '/run',
  preview: '/api/report/preview',
  previews: '/api/report/previews',
  send: '/api/report/send',
  recount: '/api/report/run',
  rebuildPercentiles: '/api/report/rebuild-percentiles',
};

/* ---------------- 鉴权 / fetch ---------------- */
function authToken() {
  try { return window.localStorage.getItem('dsh_token') || ''; } catch (e) { return ''; }
}
function setAuthToken(t) {
  try {
    if (t) window.localStorage.setItem('dsh_token', t);
    else window.localStorage.removeItem('dsh_token');
  } catch (e) { /* 忽略 */ }
}

async function api(path, options) {
  const headers = {
    'Accept': 'application/json',
    'X-Requested-With': 'fetch',   // CSRF 防护（服务端要求）
  };
  const token = authToken();
  if (token) headers['X-Auth-Token'] = token;

  const opt = Object.assign({ headers: headers }, options || {});
  if (opt.json !== undefined) {
    opt.body = JSON.stringify(opt.json);
    opt.headers['Content-Type'] = 'application/json';
    delete opt.json;
  }
  const timeoutMs = opt.timeoutMs || (10 * 60 * 1000);   // 拉取数据等长任务可覆盖
  delete opt.timeoutMs;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  opt.signal = ctrl.signal;

  let res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    throw new Error(e.name === 'AbortError' ? '请求超时（10 分钟）' : ('无法连接服务：' + e.message));
  } finally {
    clearTimeout(timer);
  }

  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) {
      throw new Error('返回内容不是 JSON（HTTP ' + res.status + '）');
    }
  }
  if (res.status === 401) {
    const t = window.prompt('该操作需要访问令牌（设置 → 系统的 WEB_AUTH_TOKEN）：', authToken());
    if (t) { setAuthToken(t); throw new Error('已保存令牌，请重试'); }
    throw new Error('未授权：访问令牌不正确');
  }
  if (!res.ok) {
    const detail = data && (data.detail || data.message);
    throw new Error(typeof detail === 'string' ? detail : ('HTTP ' + res.status));
  }
  return data;
}

/* ---------------- DOM 助手 ---------------- */
function $(id) { return document.getElementById(id); }

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k === 'html') n.innerHTML = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      // 布尔属性要特殊处理：setAttribute('disabled', false) 会写成
      // disabled="false"，而属性只要存在就生效 —— 按钮照样点不动。
      else if (v === true) n.setAttribute(k, '');
      else if (v === false || v === null || v === undefined) n.removeAttribute(k);
      else n.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    n.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return n;
}

function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmt(v, nd) {
  if (v === null || v === undefined || v === '' || v !== v) return '—';
  const n = Number(v);
  if (nd !== undefined && Number.isFinite(n)) return n.toFixed(nd);
  return String(v);
}

function pct(v, nd) { return v === null || v === undefined ? '—' : fmt(v, nd === undefined ? 1 : nd) + '%'; }

function fmtBig(v) {
  // 大数格式化：>=1亿 显示 X.XX亿；>=1万 显示 X.XX万；否则原样
  if (v === null || v === undefined || v === '' || v !== v) return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const a = Math.abs(n);
  if (a >= 1e8) return (n / 1e8).toFixed(2) + '亿';
  if (a >= 1e4) return (n / 1e4).toFixed(2) + '万';
  return String(n);
}

function showToast(msg, ok) {
  const t = $('toast');
  if (!t) return;      // 没有 toast 容器时别抛异常：抛了会把调用方的后续步骤（如刷新列表）一起带走
  t.textContent = (ok ? '✔ ' : '⚠ ') + msg;
  t.className = 'toast ' + (ok ? 'ok' : 'err');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), 3000);
}

function showBanner(msg) {
  const b = $('banner');
  b.textContent = '⚠ ' + msg;
  b.classList.remove('hidden');
}

// 常驻提示：切换页面会调 clearBanner()，但"后端没返回展示配置"这类问题需要
// 一直看得见，否则用户只会看到状态/配色悄悄退回默认值而不知道原因。
let stickyWarn = '';
function setStickyWarn(msg) {
  stickyWarn = msg || '';
  if (stickyWarn) showBanner(stickyWarn);
  else clearBanner();
}
function clearBanner() {
  if (stickyWarn) { showBanner(stickyWarn); return; }
  $('banner').classList.add('hidden');
}

function scoreColor(s) {
  // 与后端邮件配色一致的连续色阶：0 绿 → 50 中性 → 100 红
  const x = Math.max(0, Math.min(100, s)) / 100;
  const edge = Math.abs(x - 0.5) * 2;
  const hue = (145 - 145 * x);
  const light = 28 + 15 * edge;
  const sat = 55 + 25 * edge;
  const [r, g, b] = hsl2rgb(hue, sat, light);
  return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('');
}
function hsl2rgb(h, s, l) {
  s /= 100; l /= 100;
  const k = (n) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)];
}

const METRIC_META = {
  pe_ttm: { label: 'PE-TTM', color: '#3a7bd5' },
  pb_mrq: { label: 'PB', color: '#1e8e5a' },
  ps_ttm: { label: 'PS-TTM', color: '#a85a00' },
  pcf_ncf_ttm: { label: 'PCF', color: '#7d5bbe' },
  div_yield: { label: '股息率%', color: '#17a2b8' },
};
const WEIGHT_KEYS = ['pe', 'pb', 'ps', 'pcf', 'dividend'];

// 指标字段 → 评分表里的分位字段（用于分位走势图）
const PCT_FIELDS = {
  pe_ttm: 'pct_pe', pb_mrq: 'pct_pb', ps_ttm: 'pct_ps',
  pcf_ncf_ttm: 'pct_pcf', div_yield: 'pct_dividend',
};

// =====================================================================
// 展示类配置：由 /api/health 的 meta 下发（设置页改完刷新页面即生效）
// ---------------------------------------------------------------------
// 下面的 DEFAULT_* 是"与后端 config/defaults.py 一致"的出厂兜底值。
// 它们有两个作用：
//   ① /api/health 没带回 meta 时（例如后端还是改动前的旧进程、或 health 超时），
//      页面仍按默认档位显示状态，而不是全部变成"❓ 未知"；
//   ② 正常情况下 applyMeta() 会用数据库里的配置整体覆盖掉。
// =====================================================================
const DEFAULT_BAND_INFO = [
  { lo: 0, hi: 20, name: '低估', icon: '🟢', action: '大额定投（2-3倍）', color: '#1e8e5a', text: '#ffffff' },
  { lo: 20, hi: 40, name: '偏低', icon: '🔵', action: '正常定投', color: '#2f6fc1', text: '#ffffff' },
  { lo: 40, hi: 70, name: '正常', icon: '⚪', action: '小额定投', color: '#75839a', text: '#ffffff' },
  { lo: 70, hi: 85, name: '偏高', icon: '🟠', action: '停止定投', color: '#a85a00', text: '#ffffff' },
  { lo: 85, hi: 101, name: '高估', icon: '🔴', action: '分批卖出（每涨5%分位卖1/3）', color: '#c94f4f', text: '#ffffff' },
];
const DEFAULT_ACTION_SHORT = { 低估: '大额买入', 偏低: '正常定投', 正常: '小额/持有', 偏高: '停止定投', 高估: '分批卖出' };
const DEFAULT_ACTION_ICON = { 低估: '💰', 偏低: '💵', 正常: '🤏', 偏高: '⏸️', 高估: '📤' };
const DEFAULT_ALERT_STATUSES = ['低估', '高估'];

const META = {
  loaded: false,       // /api/health 是否成功带回 meta（false = 用的出厂兜底值）
  chartYears: 5,       // HISTORY_YEARS_CHART：K 线/分位走势窗口
  mainYears: 10,       // HISTORY_YEARS_10Y：评分走势窗口
  healthRefresh: 300,  // WEB_HEALTH_REFRESH_SEC：顶栏健康检查间隔(秒)，0=只查一次
  actionShort: DEFAULT_ACTION_SHORT,    // ACTION_SHORT：状态 → 短动作词
  actionIcon: DEFAULT_ACTION_ICON,      // ACTION_ICON：状态 → 图标
  alertStatuses: DEFAULT_ALERT_STATUSES, // ALERT_STATUSES：进入告警态的状态
  alertMark: '⚡',      // ALERT_MARK：告警标记
  alertPrefix: '',     // ALERT_PREFIX：告警标题前缀
  divergence: 15,      // DIVERGENCE_THRESHOLD：5年/10年分位"显著偏离"阈值
};

// 估值区间：[下限, 上限, 名称, 颜色]（charts.js 的下标访问依赖这个形状）
let BANDS = [];
// 区间明细（含文字色/图标/长动作），与 BANDS 同源；signalOf() 用它取配色与文案。
// 先用出厂默认值填好，meta 到了再整体替换。
let BAND_INFO = [];

function _setBands(list) {
  BAND_INFO = list;
  BANDS = list.map((b) => [b.lo, b.hi, b.name, b.color]);
}
_setBands(DEFAULT_BAND_INFO.map((b) => Object.assign({}, b)));

// 用后端配置重建 BANDS/META（在 refreshHeader 里调用）
function applyMeta(meta) {
  if (!meta) return false;
  const w = meta.windows || {};
  if (w.chart > 0) META.chartYears = w.chart;
  if (w.main > 0) META.mainYears = w.main;
  // 兼容旧后端字段名 page_refresh（缓存里的旧响应），取不到就用 300
  const hr = meta.health_refresh != null ? meta.health_refresh : meta.page_refresh;
  META.healthRefresh = hr != null ? hr : 300;
  META.actionShort = meta.action_short || {};
  META.actionIcon = meta.action_icon || {};
  META.alertStatuses = meta.alert_statuses || [];
  if (meta.alert_mark) META.alertMark = meta.alert_mark;
  if (meta.alert_prefix != null) META.alertPrefix = meta.alert_prefix;
  if (meta.divergence_threshold != null) META.divergence = meta.divergence_threshold;
  const bands = (meta.bands || []).filter((b) => b && b.hi > b.lo);
  if (bands.length) _setBands(bands);
  META.loaded = true;
  return true;
}

// =====================================================================
// 信号判定：全前端唯一的"评分 → 状态/动作/配色"入口
// ---------------------------------------------------------------------
// 后端的 valuation_score.status/action 是**算分位那一刻的快照**，改了
// SIGNAL_BANDS 之后不会自动更新。所以展示一律按当前配置实时推导，这样在
// 设置页改完「估值区间与信号」「推荐操作与配色」刷新页面就生效。
// 与后端 config.signal_of() 的口径保持一致。
// =====================================================================
const UNKNOWN_SIGNAL = {
  status: '未知', icon: '❓', emoji: '❓', action: '无', short: '—',
  color: '#9aa3b2', text: '#ffffff', alert: false,
};

function bandOf(v) {
  if (v === null || v === undefined || v !== v) return null;
  if (!BAND_INFO.length) return null;
  for (const b of BAND_INFO) if (v >= b.lo && v < b.hi) return b;
  return v < BAND_INFO[0].lo ? BAND_INFO[0] : BAND_INFO[BAND_INFO.length - 1];
}

function signalOf(v) {
  const b = bandOf(v);
  if (!b) return Object.assign({}, UNKNOWN_SIGNAL);
  return {
    status: b.name,
    icon: META.actionIcon[b.name] || b.icon || '',
    emoji: b.icon || '',
    action: b.action || '',
    short: META.actionShort[b.name] || b.action || '—',
    // 配色来自 STATUS_STYLE（后端已合并进 bands 的 color/text）
    color: b.color || '#75839a',
    text: b.text || '#ffffff',
    alert: META.alertStatuses.indexOf(b.name) >= 0,
  };
}

// 短期（5年口径）相对长期（10年口径）的偏离是否超过阈值
function divergenceOf(score, score5) {
  if (score == null || score5 == null) return null;
  const d = score5 - score;
  return { diff: d, over: Math.abs(d) >= META.divergence };
}


const WEIGHT_META = {
  pe: { label: 'PE-TTM', field: 'pe_ttm' },
  pb: { label: 'PB', field: 'pb_mrq' },
  ps: { label: 'PS-TTM', field: 'ps_ttm' },
  pcf: { label: 'PCF', field: 'pcf_ncf_ttm' },
  dividend: { label: '股息率', field: 'div_yield' },
};
