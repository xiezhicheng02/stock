/* =====================================================================
   模块：设置 —— 整个系统的配置管理
   * 配置节（分组）：{title, keys}，一组一个「保存」按钮
   * 支持手动新增/删除配置节，节内新增/删除配置项
   * 复杂配置（区间、操作、告警、收件人、权重等）用结构化编辑器
   ===================================================================== */
'use strict';

const Settings = (function () {
  let root = null, sections = [], itemsByKey = {};

  // =====================================================================
  // 「估值区间与信号」+「推荐操作与配色」互相适配
  // ---------------------------------------------------------------------
  // SIGNAL_BANDS 是状态名的唯一来源；STATUS_STYLE / ACTION_SHORT / ACTION_ICON /
  // ALERT_STATUSES 都以"状态名"为键。这里让后四者跟着 SIGNAL_BANDS 走：
  //   · 键名输入框挂 datalist（候选 = 当前区间里的状态名）
  //   · ALERT_STATUSES 的勾选项由状态名实时生成，不再写死
  //   · 每个配置项下方做一次"适配体检"，缺哪个状态直接点出来
  // =====================================================================
  const liveGetters = {};          // 配置键 → 取编辑器当前值
  const statusWatchers = [];       // 状态列表变化时要刷新的回调
  const diagEls = [];              // {key, el} 适配体检输出位

  function liveValue(key) {
    const g = liveGetters[key];
    if (g) { try { return g(); } catch (e) { /* 编辑器未就绪，退回已保存值 */ } }
    return (itemsByKey[key] || {}).value;
  }

  // 当前 SIGNAL_BANDS 里的状态名（去重、保序）
  function liveStatuses() {
    const rows = liveValue('SIGNAL_BANDS') || [];
    const out = [];
    rows.forEach((r) => {
      const s = String((r && r[2]) || '').trim();
      if (s && out.indexOf(s) < 0) out.push(s);
    });
    return out;
  }

  function refreshDiagnostics() {
    const list = liveStatuses();
    ensureDatalist(list);
    statusWatchers.forEach((fn) => { try { fn(list); } catch (e) { /* ignore */ } });
    diagEls.forEach(({ key, el: node }) => {
      const rep = diagText(key);
      node.className = 'cfg-diag ' + (rep.ok ? 'ok' : 'warn');
      node.textContent = rep.text;
    });
  }

  // 状态名候选表：给 STATUS_STYLE / ACTION_SHORT / ACTION_ICON 的"键"输入框做下拉
  let statusDatalist = null;
  function ensureDatalist(list) {
    if (!statusDatalist) {
      statusDatalist = el('datalist', { id: 'dl-status-name' });
      document.body.append(statusDatalist);
    }
    statusDatalist.innerHTML = '';
    list.forEach((s) => statusDatalist.append(el('option', { value: s })));
  }

  // 单个配置项的"适配体检"：只报与它自己相关的问题
  function diagText(key) {
    const statuses = liveStatuses();
    const style = liveValue('STATUS_STYLE') || {};
    const short = liveValue('ACTION_SHORT') || {};
    const icon = liveValue('ACTION_ICON') || {};
    const alerts = liveValue('ALERT_STATUSES') || [];
    const miss = (obj) => statuses.filter((s) => !obj[s]);
    const msgs = [];

    if (key === 'SIGNAL_BANDS') {
      const rows = liveValue('SIGNAL_BANDS') || [];
      const names = rows.map((r) => String((r && r[2]) || '').trim());
      const dup = names.filter((s, i) => s && names.indexOf(s) !== i);
      if (dup.length) msgs.push('状态名重复：' + dup.join('、'));
      const blank = names.filter((s) => !s).length;
      if (blank) msgs.push(blank + ' 档没填状态名');
      // 连续性：按区间排序后检查首档从 0 起、末档到 100、中间不留缝
      const sorted = rows.filter((r) => r && r[1] > r[0])
        .map((r) => [Number(r[0]), Number(r[1])]).sort((a, b) => a[0] - b[0]);
      for (let i = 1; i < sorted.length; i++) {
        if (sorted[i][0] > sorted[i - 1][1]) {
          msgs.push('区间留缝 ' + sorted[i - 1][1] + '~' + sorted[i][0]);
        } else if (sorted[i][0] < sorted[i - 1][1]) {
          msgs.push('区间重叠 ' + sorted[i][0] + '~' + sorted[i - 1][1]);
        }
      }
      if (sorted.length && sorted[0][0] > 0) msgs.push('最低档没从 0 开始');
      if (sorted.length && sorted[sorted.length - 1][1] < 100) msgs.push('最高档没覆盖到 100');
    } else if (key === 'STATUS_STYLE') {
      miss(style).forEach((s) => msgs.push('缺少「' + s + '」的配色'));
      const extra = Object.keys(style).filter((k) => k !== '未知' && statuses.indexOf(k) < 0);
      if (extra.length) msgs.push('多余「' + extra.join('、') + '」（区间里已无此状态）');
    } else if (key === 'ACTION_SHORT') {
      miss(short).forEach((s) => msgs.push('缺少「' + s + '」的短动作'));
    } else if (key === 'ACTION_ICON') {
      miss(icon).forEach((s) => msgs.push('缺少「' + s + '」的图标'));
    } else if (key === 'ALERT_STATUSES') {
      const orphan = (alerts || []).filter((s) => statuses.indexOf(s) < 0);
      if (orphan.length) msgs.push('引用了不存在的状态「' + orphan.join('、') + '」');
      if (!alerts || !alerts.length) msgs.push('为空 → 不会触发任何告警');
    }
    return {
      ok: !msgs.length,
      text: msgs.length
        ? ('⚠ ' + msgs.join('；'))
        : ('✔ 与 SIGNAL_BANDS 的 ' + statuses.length + ' 个状态对齐'),
    };
  }

  const HIDDEN = new Set(['LAST_RUN_AT', 'LAST_MAIL_AT', 'LAST_MAIL_SUBJECT',
    'LAST_ALERT_SIG', 'LAST_ALERT_DATE',
    'SCHEDULER_LAST_RUNS', 'SETTING_GROUPS']);

  async function mount(el) {
    root = el;
    root.innerHTML = '<div class="muted pad">加载中…</div>';
    await load();
  }

  async function load() {
    const d = await api(API.settingsEditable);
    sections = d.sections || [];
    itemsByKey = d.items || {};
    Object.values(itemsByKey).forEach((it) => {
      if (it.val_type === 'json' && typeof it.value === 'string') {
        try { it.value = JSON.parse(it.value); } catch (e) { it.value = null; }
      }
    });
    render();
  }

  function render() {
    root.innerHTML = '';
    // 每次重绘都会重建编辑器，联动注册表必须一并清空，否则会累积旧节点
    diagEls.length = 0;
    statusWatchers.length = 0;
    Object.keys(liveGetters).forEach((k) => { delete liveGetters[k]; });
    root.append(headerBar());

    const used = new Set();
    sections.forEach((sec) => {
      const keys = (sec.keys || []).filter((k) => itemsByKey[k] && !HIDDEN.has(k));
      keys.forEach((k) => used.add(k));
      root.append(sectionCard(sec, keys));
    });
    // 未归入任何节的配置项 → 其它
    const others = Object.keys(itemsByKey).filter((k) => !HIDDEN.has(k) && !used.has(k));
    if (others.length) {
      root.append(sectionCard({ title: '📦 其它', keys: others }, others));
    }
    // 所有编辑器就绪后再做一次区间/信号联动体检
    refreshDiagnostics();
  }

  /* ---------------- 顶部：新增分组 ---------------- */
  function headerBar() {
    const bar = el('div', { class: 'card' });
    const head = el('div', { class: 'section-head' });
    head.append(el('h3', { class: 'card-title' }, '🧩 配置分组'));
    const addBtn = el('button', { class: 'btn', onclick: () => {
      form.classList.toggle('hidden'); gTitle.focus();
    } }, '＋ 新增分组');
    head.append(addBtn);
    bar.append(head);
    const form = el('div', { class: 'add-form hidden' });
    const gTitle = el('input', { class: 'input', placeholder: '分组标题（如 我的分组）' });
    const ok = el('button', { class: 'btn', onclick: async () => {
      if (!gTitle.value.trim()) { showToast('标题不能为空', false); return; }
      try {
        await api(API.settingsSections, { method: 'POST', json: { title: gTitle.value.trim() } });
        showToast('分组已添加', true); await load();
      } catch (e) { showToast(e.message, false); }
    } }, '确定');
    form.append(gTitle, ok);
    bar.append(form);
    return bar;
  }

  /* ---------------- 分组卡片 ---------------- */
  function sectionCard(sec, keys) {
    const card = el('div', { class: 'card' });
    const saveBtn = el('button', { class: 'btn', onclick: async () => {
      saveBtn.disabled = true; saveBtn.textContent = '保存中…';
      let n = 0, err = null, schedMsg = '';
      for (const { it, ed } of editors) {
        try {
          const v = ed.getValue();
          if (v === undefined) continue;
          const r = await api(API.settingsPut(it.key), { method: 'PUT', json: { value: v } });
          n++;
          // 定时任务相关配置：后端会立刻让调度器生效，把结果提示出来
          if (r && r.scheduler && r.scheduler.msg) schedMsg = r.scheduler.msg;
        } catch (e) { err = err || e; }
      }
      if (err) showToast('部分保存失败：' + err.message, false);
      else {
        showToast('已保存「' + sec.title + '」' + (n ? '（' + n + ' 项）' : '')
                  + (schedMsg ? ' · ' + schedMsg : ''), true);
      }
      saveBtn.disabled = false; saveBtn.textContent = '保存';
      await load();
    } }, '保存');
    const delBtn = el('button', { class: 'btn ghost mini', onclick: async () => {
      if (!confirm('删除分组「' + sec.title + '」？其配置项会移到「其它」。')) return;
      try {
        await api(API.settingsSections + '/' + encodeURIComponent(sec.title), { method: 'DELETE' });
        showToast('分组已删除', true); await load();
      } catch (e) { showToast(e.message, false); }
    } }, '删除分组');
    const head = el('div', { class: 'section-head' });
    head.append(el('h3', { class: 'card-title' }, sec.title));
    head.append(el('div', { class: 'section-actions' }, saveBtn, delBtn));
    card.append(head);

    const list = el('div', { class: 'setting-list' });
    const editors = keys.map((k) => ({ it: itemsByKey[k], ed: renderEditor(itemsByKey[k]) }));
    editors.forEach(({ it, ed }) => {
      const row = el('div', { class: 'setting-row' });
      row.append(el('div', { class: 'setting-info' },
        el('div', { class: 'setting-key' }, it.key),
        it.remark ? el('div', { class: 'muted small' }, it.remark) : null,
        el('button', { class: 'link-del', onclick: async () => {
          if (!confirm('删除配置项 ' + it.key + '？')) return;
          try { await api(API.settingsDelete(it.key), { method: 'DELETE' });
            showToast('已删除', true); await load(); }
          catch (e) { showToast(e.message, false); }
        } }, '删除')));
      row.append(el('div', { class: 'setting-edit' }, ed.dom));
      list.append(row);
    });

    // 新增配置项（可折叠）
    const addWrap = el('div', { class: 'add-wrap' });
    const toggleBtn = el('button', { class: 'btn ghost mini', onclick: () => {
      form.classList.toggle('hidden'); if (!form.classList.contains('hidden')) keyIn.focus();
    } }, '＋ 新增配置项');
    const form = el('div', { class: 'add-form hidden muted-border' });
    const keyIn = el('input', { class: 'input', placeholder: '键名（如 MY_KEY）' });
    const typeSel = el('select', { class: 'input' },
      el('option', { value: 'str' }, '字符串'),
      el('option', { value: 'number' }, '数字'),
      el('option', { value: 'bool' }, '布尔'),
      el('option', { value: 'json' }, 'JSON'));
    const valIn = el('input', { class: 'input', placeholder: '值' });
    const remarkIn = el('input', { class: 'input', placeholder: '备注（可选）' });
    const ok = el('button', { class: 'btn mini', onclick: async () => {
      if (!keyIn.value.trim()) { showToast('键名不能为空', false); return; }
      const type = typeSel.value;
      let value;
      try {
        if (type === 'number') value = valIn.value === '' ? '' : Number(valIn.value);
        else if (type === 'bool') value = ['true', '1', 'yes', 'on'].includes(String(valIn.value).toLowerCase());
        else if (type === 'json') value = valIn.value === '' ? null : JSON.parse(valIn.value);
        else value = valIn.value;
      } catch (e) { showToast('值格式错误：' + e.message, false); return; }
      try {
        await api(API.settingsCreate, { method: 'POST',
          json: { key: keyIn.value.trim(), value, section: sec.title, remark: remarkIn.value.trim() } });
        showToast('已新增 ' + keyIn.value.trim(), true); await load();
      } catch (e) { showToast(e.message, false); }
    } }, '确定');
    form.append(keyIn, typeSel, valIn, remarkIn, ok);
    addWrap.append(toggleBtn, form);
    list.append(addWrap);
    card.append(list);

    return card;
  }

  /* ---------------- 编辑器（返回 {dom, getValue}） ---------------- */
  function renderEditor(it) {
    switch (it.key) {
      case 'SIGNAL_BANDS': return bandsEditor(it);
      case 'ACTION_SHORT': return kvEditor(it, 'text');
      case 'ACTION_ICON': return kvEditor(it, 'text');
      case 'STATUS_STYLE': return kvEditor(it, 'colors');
      case 'ALERT_STATUSES': return alertStatusesEditor(it);
      case 'MAIL_TO': return listEditor(it);
      case 'ALERT_RUN_HOURS': return hoursEditor(it);
      case 'COMPOSITE_WEIGHTS': return weightsEditor(it);
      case 'SYNC_RUN_TIME':
      case 'NOTIFY_BUILD_TIME':
      case 'NOTIFY_SEND_TIME': return timeEditor(it);
      case 'NOTIFY_RESEND_TIMES': return timesListEditor(it);
      default: return genericEditor(it);
    }
  }

  function genericEditor(it) {
    let input;
    if (it.val_type === 'bool') {
      input = el('input', { type: 'checkbox' });
      input.checked = it.value === true || it.value === '1' || it.value === 'true';
      return { dom: input, getValue: () => input.checked };
    }
    if (it.val_type === 'int' || it.val_type === 'float') {
      input = el('input', { class: 'input', type: 'number',
        step: it.val_type === 'float' ? '0.01' : '1', value: it.value ?? '' });
      return { dom: input, getValue: () => (input.value === '' ? undefined : Number(input.value)) };
    }
    input = el('input', { class: 'input', type: 'text', value: it.value ?? '' });
    return { dom: input, getValue: () => input.value };
  }

  function bandsEditor(it) {
    const rows = (it.value || []).map((r) => r.slice());
    const box = el('div', { class: 'struct' });
    const tbl = el('table', { class: 'data' });
    tbl.append(el('thead', {}, el('tr', {},
      ['下限%', '上限%', '状态', '图标', '动作建议', ''].map((h) =>
        el('th', { class: h === '' ? 'right' : '' }, h)))));
    const tb = el('tbody');
    function addRow(r) {
      r = r || [0, 20, '', '', ''];
      const tr = el('tr');
      const inputs = [r[0], r[1], r[2], r[3], r[4]].map((v) =>
        el('input', { class: 'input mini', value: v ?? '' }));
      // 状态名改了要立刻广播给 STATUS_STYLE / ACTION_* / ALERT_STATUSES
      inputs[2].setAttribute('list', 'dl-status-name');
      [2, 3, 4].forEach((i) => inputs[i].addEventListener('input', refreshDiagnostics));
      const del = el('button', { class: 'btn ghost mini',
        onclick: () => { tr.remove(); refreshDiagnostics(); } }, '删');
      tr.append(...inputs.map((i) => el('td', {}, i)), el('td', { class: 'right' }, del));
      tb.append(tr);
    }
    rows.forEach(addRow);
    const add = el('button', { class: 'btn ghost mini',
      onclick: () => { addRow(); refreshDiagnostics(); } }, '＋ 添加区间');
    tbl.append(tb);
    const diag = el('div', { class: 'cfg-diag' });
    box.append(tbl, el('div', { class: 'btnbar pad-top' }, add), diag);

    const getValue = () => [...tb.querySelectorAll('tr')].map((tr) => {
      const c = tr.querySelectorAll('input');
      return [Number(c[0].value) || 0, Number(c[1].value) || 0,
              c[2].value.trim(), c[3].value.trim(), c[4].value.trim()];
    });
    liveGetters.SIGNAL_BANDS = getValue;
    diagEls.push({ key: 'SIGNAL_BANDS', el: diag });
    // 任何"状态名"输入都触发一次体检；用事件委托省掉逐行绑定
    tb.addEventListener('input', refreshDiagnostics);
    return { dom: box, getValue };
  }

  function kvEditor(it, valueType) {
    const obj = it.value || {};
    const isColors = valueType === 'colors';
    const box = el('div', { class: 'struct' });
    const tbl = el('table', { class: 'data' });
    tbl.append(el('thead', {}, el('tr', {},
      ['键', isColors ? '背景色' : '值', isColors ? '文字色' : null, '']
        .filter(Boolean).map((h) => el('th', { class: h === '' ? 'right' : '' }, h)))));
    const tb = el('tbody');
    function addRow(k, v) {
      const tr = el('tr');
      const keyIn = el('input', { class: 'input mini', value: k || '' });
      // 键名候选 = 当前 SIGNAL_BANDS 里的状态名（编辑区间时自动同步）
      keyIn.setAttribute('list', 'dl-status-name');
      keyIn.addEventListener('input', refreshDiagnostics);
      let vIn, vIn2;
      if (isColors) {
        vIn = el('input', { class: 'color', type: 'color', value: (Array.isArray(v) ? v[0] : '#999999') || '#999999' });
        vIn2 = el('input', { class: 'color', type: 'color', value: (Array.isArray(v) ? v[1] : '#ffffff') || '#ffffff' });
      } else {
        vIn = el('input', { class: 'input grow', value: (v == null ? '' : v) });
      }
      const del = el('button', { class: 'btn ghost mini',
        onclick: () => { tr.remove(); refreshDiagnostics(); } }, '删');
      const cells = [el('td', {}, keyIn), el('td', {}, vIn)];
      if (isColors) cells.push(el('td', {}, vIn2));
      cells.push(el('td', { class: 'right' }, del));
      tr.append(...cells);
      tb.append(tr);
    }
    Object.entries(obj).forEach(([k, v]) => addRow(k, v));
    const add = el('button', { class: 'btn ghost mini',
      onclick: () => { addRow('', ''); refreshDiagnostics(); } }, '＋ 添加');
    tbl.append(tb);
    const diag = el('div', { class: 'cfg-diag' });
    box.append(tbl, el('div', { class: 'btnbar pad-top' }, add), diag);

    const getValue = () => {
      const out = {};
      tb.querySelectorAll('tr').forEach((tr) => {
        const k = tr.querySelectorAll('input')[0].value.trim();
        if (!k) return;
        if (isColors) {
          const cs = tr.querySelectorAll('input[type=color]');
          out[k] = [cs[0].value, cs[1].value];
        } else out[k] = tr.querySelectorAll('input')[1].value;
      });
      return out;
    };
    liveGetters[it.key] = getValue;
    diagEls.push({ key: it.key, el: diag });
    return { dom: box, getValue };
  }

  function alertStatusesEditor(it) {
    let selected = (it.value || []).slice();
    const box = el('div', { class: 'struct' });
    const grid = el('div', { class: 'check-grid' });
    let cbs = {};
    box.append(grid);
    const customHint = el('div', { class: 'muted small' },
      '勾选项来自当前「估值区间与信号」里的状态名；未在区间里出现的状态会被标为失效。');

    // 从当前 DOM 收一次勾选结果（重建列表前用，避免丢用户的勾选）
    function syncFromDom() {
      const picked = Object.keys(cbs).filter((s) => cbs[s].checked);
      const orphan = selected.filter((s) => Object.keys(cbs).indexOf(s) < 0);
      selected = picked.concat(orphan);
    }

    // 勾选项由 SIGNAL_BANDS 的状态名实时生成，不再写死五个状态
    function rebuild(list) {
      syncFromDom();
      grid.innerHTML = '';
      cbs = {};
      const names = list.slice();
      // 已勾选但区间里已不存在的状态也列出来（标红），方便用户手动取消
      selected.forEach((s) => { if (names.indexOf(s) < 0) names.push(s); });
      if (!names.length) {
        grid.append(el('div', { class: 'muted small' }, '（SIGNAL_BANDS 里还没有状态名）'));
        return;
      }
      names.forEach((s) => {
        const orphan = list.indexOf(s) < 0;
        const label = el('label', { class: 'check-item' + (orphan ? ' orphan' : '') });
        const cb = el('input', { type: 'checkbox' });
        cb.checked = selected.indexOf(s) >= 0;
        cbs[s] = cb;
        label.append(cb, el('span', {}, s + (orphan ? '（已失效）' : '')));
        grid.append(label);
      });
    }
    statusWatchers.push(rebuild);

    const getValue = () => {
      syncFromDom();
      return selected.slice();
    };
    liveGetters.ALERT_STATUSES = getValue;
    box.append(customHint);
    const diag = el('div', { class: 'cfg-diag' });
    box.append(diag);
    diagEls.push({ key: 'ALERT_STATUSES', el: diag });
    grid.addEventListener('change', refreshDiagnostics);
    return { dom: box, getValue };
  }

  function listEditor(it) {
    const ta = el('textarea', { class: 'input', rows: '4' });
    ta.value = (Array.isArray(it.value) ? it.value : []).join('\n');
    return { dom: ta, getValue: () => ta.value.split('\n').map((s) => s.trim()).filter(Boolean) };
  }

  // 单个时间 HH:MM（用原生 time 选择器）
  function timeEditor(it) {
    const input = el('input', { class: 'input', type: 'time', value: it.value || '' });
    return { dom: input, getValue: () => input.value };
  }

  // 重发时点列表（每行一个 HH:MM）
  function timesListEditor(it) {
    const ta = el('textarea', { class: 'input', rows: '3' });
    ta.value = (Array.isArray(it.value) ? it.value : []).join('\n');
    return { dom: ta, getValue: () => ta.value.split('\n').map((s) => s.trim()).filter(Boolean) };
  }

  function hoursEditor(it) {
    const selected = it.value || [];
    const box = el('div', { class: 'struct' });
    const grid = el('div', { class: 'check-grid' });
    const cbs = {};
    for (let h = 0; h < 24; h++) {
      const label = el('label', { class: 'check-item' });
      const cb = el('input', { type: 'checkbox' });
      cb.checked = selected.includes(h); cbs[h] = cb;
      label.append(cb, el('span', {}, String(h).padStart(2, '0')));
      grid.append(label);
    }
    box.append(grid);
    return { dom: box, getValue: () => { const o = []; for (let h = 0; h < 24; h++) if (cbs[h].checked) o.push(h); return o; } };
  }

  function weightsEditor(it) {
    const w = it.value || {};
    const box = el('div', { class: 'struct' });
    const grid = el('div', { class: 'weight-grid' });
    const inputs = {};
    WEIGHT_KEYS.forEach((k) => {
      const row = el('label', { class: 'weight-row' });
      row.append(el('span', { class: 'weight-label' }, WEIGHT_META[k].label));
      const inp = el('input', { class: 'input mini', type: 'number', min: '0', step: '1',
        value: Math.round((w[k] || 0) * 100) });
      inputs[k] = inp; row.append(inp); row.append(el('span', { class: 'muted' }, '%'));
      grid.append(row);
    });
    box.append(grid);
    return {
      dom: box,
      getValue: () => {
        const raw = {};
        WEIGHT_KEYS.forEach((k) => raw[k] = Number(inputs[k].value) || 0);
        const sum = WEIGHT_KEYS.reduce((a, k) => a + raw[k], 0);
        if (sum <= 0) throw new Error('权重合计不能为 0');
        const out = {};
        WEIGHT_KEYS.forEach((k) => out[k] = Math.round(raw[k] / sum * 1000) / 1000);
        return out;
      },
    };
  }

  return { mount };
})();
