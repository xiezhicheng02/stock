/* =====================================================================
   共享的"标的详情视图"（个股 / 指数与组合 / 标的信息 三页统一使用）：
     头部横幅（评分/涨跌）→ 基本信息 → 评分权重 → 指标卡 → K线/评分/分位走势图
   权重：'edit' 可编辑（个股、指数与组合页） / 'readonly' 只读（标的信息页）。
   没有配置权重时不展示权重比例与综合评分。
   ===================================================================== */
'use strict';

const AssetView = (function () {
  let chartInst = {};

  // 未配置权重时，"配置权重"用的默认比例（与后端 COMPOSITE_WEIGHTS 默认一致）
  const DEFAULT_WEIGHTS = { pe: 25, pb: 20, ps: 25, pcf: 15, dividend: 15 };

  function disposeCharts() {
    Object.values(chartInst).forEach((i) => { try { i.dispose(); } catch (e) {} });
    chartInst = {};
  }

  function ktypeLabel(kt) {
    return kt === 'index' ? '指数' : kt === 'portfolio' ? '组合' : '个股';
  }

  // 卡片标题 + 右侧"当前值"小标签（走势图标题里带最新值用）
  function ctitle(text, chips) {
    const h = el('h3', { class: 'card-title' }, text);
    const list = (chips || []).filter(Boolean);
    if (list.length) {
      const box = el('span', { class: 'ct-vals' });
      list.forEach((c) => box.append(c));
      h.append(box);
    }
    return h;
  }

  function valChip(text, cls, color) {
    const attrs = { class: 'ct-chip' + (cls ? ' ' + cls : '') };
    if (color) attrs.style = 'color:' + color;
    return el('span', attrs, text);
  }

  // 状态胶囊：图标取自 ACTION_ICON，状态名+短动作取自配置。
  // 颜色用 CSS 变量传（而不是直接写 background/color），这样横幅里可以换成
  // "白底 + 状态色文字"，才不会和横幅的评分底色打架（见 style.css）。
  function statusPill(sig, opts) {
    opts = opts || {};
    const node = el('span', {
      class: 'status-pill' + (opts.big ? ' big' : '') + (sig.alert ? ' alert' : ''),
      style: '--pill-bg:' + sig.color + ';--pill-fg:' + sig.text,
      title: (sig.action || '') + (sig.alert ? '（告警状态）' : ''),
    });
    if (sig.icon) node.append(el('span', { class: 'sp-icon' }, sig.icon));
    node.append(el('span', { class: 'sp-name' }, sig.status));
    if (opts.withAction && sig.short) {
      node.append(el('span', { class: 'sp-act' }, '· ' + sig.short));
    }
    if (sig.alert && opts.mark) {
      node.append(el('span', { class: 'sp-mark' }, opts.mark));
    }
    return node;
  }

  function _gapDays(a, b) {
    try { return Math.round((new Date(b) - new Date(a)) / 86400000); }
    catch (e) { return null; }
  }

  // 综合评分较前一交易日的变化（用已取到的评分序列算，不再多请求）
  // 方向用 up/down/flat 三个类带出去，由 CSS 决定颜色：
  // 评分越高越贵 → 上升用红（--dear）、下降用绿（--cheap），与全站涨跌一致
  function scoreDeltaEl(score, light) {
    if (!score || score.length < 2) return null;
    const cur = score[score.length - 1];
    const prev = score[score.length - 2];
    if (cur.score == null || prev.score == null) return null;
    const d = cur.score - prev.score;
    const gap = _gapDays(prev.date, cur.date);
    const lbl = gap != null && gap > 4 ? ('较 ' + String(prev.date).slice(5)) : '较前日';
    const flat = Math.abs(d) < 0.05;
    const dir = flat ? 'flat' : (d > 0 ? 'up' : 'down');
    const body = flat ? '─ 0.0' : (d > 0 ? '▲ +' : '▼ ') + fmt(d, 1);
    return el('div', {
      class: 'asset-delta ' + dir + (light ? ' light' : ''),
      title: '综合评分' + (flat ? '基本持平' : (d > 0 ? '上升 ' : '下降 ')
                             + fmt(Math.abs(d), 1) + ' 分（越高越贵）'),
    }, body, el('span', { class: 'asset-delta-lbl' }, lbl));
  }

  /* opts: { weights: 'edit'|'readonly', weightsCode, actions:[...], onWeightsSaved } */
  function render(dom, d, kline, score, pcts, opts) {
    opts = opts || {};
    pcts = pcts || {};
    disposeCharts();
    const sc = d.score || {};
    const scored = sc.score != null;
    const color = scoreColor(scored ? sc.score : 50);

    // ---- 头部横幅 ----
    // 左侧：名称 +（紧跟其后的）状态与操作建议胶囊，下面是代码/类型等副信息
    // 右侧：综合评分 + 较前日变化
    const sig = scored ? signalOf(sc.score) : null;
    const head = el('div', { class: 'asset-head', style: 'background:' + color });
    const title = el('div', { class: 'asset-title' }, d.name);
    if (sig) {
      // 状态/动作一律按当前配置实时推导（库里的是算分位时的快照）
      title.append(statusPill(sig, { withAction: true, mark: META.alertMark }));
    }
    head.append(el('div', { class: 'asset-head-l' },
      title,
      el('div', { class: 'asset-sub' },
        d.code + ' · ' + ktypeLabel(d.ktype)
        + (d.constituent_count != null ? ' · ' + d.constituent_count + ' 只成分股' : '')
        + (d.industry ? ' · ' + d.industry : '')
        + (d.listed_date ? ' · 上市 ' + d.listed_date : ''))));
    const right = el('div', { class: 'asset-head-r' });
    if (scored) {
      right.append(el('div', { class: 'asset-score' }, fmt(sc.score, 1) + '%'));
      const delta = scoreDeltaEl(score);
      if (delta) right.append(delta);
    } else {
      // 没配置权重 → 不算综合评分，这里也不显示
      right.append(el('div', { class: 'asset-noscore' }, '未配置评分权重'));
      right.append(el('div', { class: 'asset-status' }, '仅展示估值分位'));
    }
    head.append(right);

    dom.innerHTML = '';
    dom.append(head);

    // ---- 基本信息（操作按钮放卡片右上角）----
    if (opts.info !== false) dom.append(basicInfoCard(d, opts));

    // ---- 指标与权重（合并卡片：权重堆叠条 + 五指标卡）----
    if (opts.weights) dom.append(metricsWeightsCard(d, pcts, opts));

    // ---- K线（标题带最新价 / 涨跌 / 日期）----
    const kLast = kline.length ? kline[kline.length - 1] : null;
    const kChips = [];
    if (kLast) {
      const cls = kLast.pct_chg == null ? '' : (kLast.pct_chg >= 0 ? 'up' : 'down');
      kChips.push(valChip(fmt(kLast.close, 2), cls));
      if (kLast.pct_chg != null) {
        kChips.push(valChip((kLast.pct_chg >= 0 ? '+' : '')
          + fmt(kLast.pct_chg, 2) + '%', cls));
      }
      if (kLast.date) kChips.push(valChip(kLast.date, 'muted'));
    }
    dom.append(el('div', { class: 'card' },
      ctitle('K线走势', kChips),
      el('div', { class: 'chart', id: 'av-kline', style: 'height:360px' })));
    if (kline.length) chartInst.kline = charts.kline($('av-kline'), kline);

    // ---- 综合评分走势（仅"有评分"的标的；标题带最新评分）----
    const scoredRows = (score || []).filter((r) => r.score != null);
    if (scoredRows.length) {
      const sLast = scoredRows[scoredRows.length - 1];
      const sSig = signalOf(sLast.score);
      const sDiv = divergenceOf(sLast.score, sLast.score5);
      const sChips = [
        valChip(fmt(sLast.score, 1) + '%', '', sSig.color),
        sLast.score5 != null ? valChip('5年 ' + fmt(sLast.score5, 1) + '%', 'muted') : null,
      ];
      // 短期显著偏离长期（DIVERGENCE_THRESHOLD）时给一个醒目标记
      if (sDiv && sDiv.over) {
        sChips.push(el('span', { class: 'ct-chip diverge' },
          '偏离 ' + fmt(sDiv.diff, 1)));
      }
      sChips.push(statusPill(sSig));
      sChips.push(valChip(sLast.date, 'muted'));
      dom.append(el('div', { class: 'card' },
        ctitle('综合评分走势', sChips),
        el('div', { class: 'chart', id: 'av-score', style: 'height:300px' })));
      chartInst.score = charts.scoreTrend($('av-score'), scoredRows);
    }

    // ---- 5 个指标分位走势 ----
    Object.assign(chartInst, metricPctCharts(dom, pcts));
  }

  // 基本信息卡片：左标题 + 右上角操作按钮 + **固定两行** 键值网格（每行 5 格）
  //   第一行 = 标的属性：代码 · 类型 · 市场 · 行业 · 上市日期
  //   第二行 = 最新数据：最新收盘 · 综合评分 · 评分权重 · 分位偏离 · K线区间
  // 每一格都是「标签 / 数值 / 补充说明」三层，缺值填空占位（—），这样两行永远对齐、
  // 不会因为某只标的没有行业或成分股就塌掉一格。配色见下方 tone / iv-sub 类。
  function basicInfoCard(d, opts) {
    const card = el('div', { class: 'card' });
    const head = el('div', { class: 'section-head' });
    head.append(el('h3', { class: 'card-title', style: 'margin:0' }, '基本信息'));
    if (opts.actions && opts.actions.length) {
      const acts = el('div', { class: 'section-actions' });
      opts.actions.forEach((a) => acts.append(
        el('button', { class: a.cls || 'btn', onclick: a.onClick }, a.label)));
      head.append(acts);
    }
    card.append(head);

    const k = d.kline || {};
    const sc = d.score || {};
    const sig = sc.score != null ? signalOf(sc.score) : null;
    const div = divergenceOf(sc.score, sc.score5);
    const empty = (v) => v === null || v === undefined || v === '';
    const sumW = d.weights
      ? Math.round(WEIGHT_KEYS.reduce((a, x) => a + (d.weights[x] || 0), 0) * 100) : null;

    const cells = [
      // ---------- 第一行：标的属性 ----------
      { label: '代码', value: d.code },
      { label: '类型', value: ktypeLabel(d.ktype),
        sub: d.constituent_count != null
          ? el('span', { class: 'iv-sub muted' }, d.constituent_count + ' 只成分股') : null },
      { label: '市场', value: d.market_label || d.market },
      { label: '行业', value: d.industry },
      { label: '上市日期', value: d.listed_date },

      // ---------- 第二行：最新数据 ----------
      { label: '最新收盘', value: k.close != null ? fmt(k.close, 2) : null,
        sub: k.pct_chg != null
          ? el('span', { class: 'iv-sub ' + (k.pct_chg >= 0 ? 'up' : 'down') },
               (k.pct_chg >= 0 ? '+' : '') + fmt(k.pct_chg, 2) + '%')
          : null },
      { label: '综合评分',
        value: sc.score != null
          ? el('span', { style: 'color:' + (sig ? sig.color : 'inherit') },
               fmt(sc.score, 1) + '%')
          : null,
        sub: sc.score5 != null
          ? el('span', { class: 'iv-sub muted' }, '5年 ' + fmt(sc.score5, 1) + '%') : null },
      { label: '评分权重', value: d.is_target ? '已配置' : '未配置',
        tone: d.is_target ? 'ok' : 'warn',
        sub: el('span', { class: 'iv-sub muted' },
          sumW > 0 ? '合计 ' + sumW + '%' : '未设置比例') },
      { label: '分位偏离',
        value: div
          ? el('span', { class: div.over ? 'iv-alert' : '' },
               (div.diff > 0 ? '+' : '') + fmt(div.diff, 1))
          : null,
        tone: div && div.over ? 'warn' : null,
        sub: el('span', { class: 'iv-sub muted' }, '阈值 ' + META.divergence) },
      { label: 'K线区间',
        // 只显示到月（如 2022-01 ~ 2026-09），完整起止日期放 title，
        // 这样窄屏也不会换行、两行高度始终一致
        value: (empty(k.first_date) && empty(k.latest_date)) ? null
          : (k.first_date || '—').slice(0, 7) + ' ~ ' + (k.latest_date || '—').slice(0, 7),
        title: (k.first_date || '—') + ' ~ ' + (k.latest_date || '—'),
        sub: k.rows != null ? el('span', { class: 'iv-sub muted' }, k.rows + ' 行') : null },
    ];

    const grid = el('div', { class: 'info-grid' });
    cells.forEach((c) => {
      const blank = empty(c.value);
      const attrs = {
        class: 'info-cell' + (c.tone ? ' tone-' + c.tone : '') + (blank ? ' is-blank' : ''),
      };
      if (c.title) attrs.title = c.title;
      grid.append(el('div', attrs,
        el('span', { class: 'ik' }, c.label),
        el('span', { class: 'iv' + (blank ? ' empty' : '') }, blank ? '—' : c.value),
        // 补充说明层恒定存在（没有内容就留空），这样两行里的每格都是
        // 「标签/数值/说明」三层，行高完全一致，网格不会参差不齐
        c.sub || el('span', { class: 'iv-sub muted' })));
    });
    card.append(grid);
    return card;
  }

  // ---- 权重：单条堆叠条 + 图例（比多行进度条紧凑）----
  function weightsStack(w) {
    const box = el('div', { class: 'weight-stack' });
    WEIGHT_KEYS.forEach((k) => {
      const v = w[k] || 0;
      if (v <= 0) return;
      const meta = METRIC_META[WEIGHT_META[k].field] || {};
      box.append(el('span', { class: 'ws-seg',
        style: 'width:' + (v * 100) + '%;background:' + (meta.color || '#75839a'),
        title: WEIGHT_META[k].label + ' ' + Math.round(v * 100) + '%' }));
    });
    return box;
  }

  function weightsLegend(w) {
    const box = el('div', { class: 'weight-legend' });
    WEIGHT_KEYS.forEach((k) => {
      const v = w[k] || 0;
      if (v <= 0) return;
      const meta = METRIC_META[WEIGHT_META[k].field] || {};
      box.append(el('span', { class: 'wl-item' },
        el('i', { style: 'background:' + (meta.color || '#75839a') }),
        WEIGHT_META[k].label + ' ' + Math.round(v * 100) + '%'));
    });
    return box;
  }

  /* =====================================================================
     「指标与权重」卡片：把原先分开的「评分权重」和「五指标卡」合并成一张卡
     结构：标题(+合计/操作) → 编辑框(可选) → 权重堆叠条+图例 → 5 张指标卡
     ===================================================================== */
  const FIELD_KEY = {};            // pe_ttm → pe
  Object.keys(WEIGHT_META).forEach((k) => { FIELD_KEY[WEIGHT_META[k].field] = k; });

  // 某指标最新一天的 10 年分位
  function pctLatest(pcts, field) {
    const s = (pcts || {})[field] || [];
    for (let i = s.length - 1; i >= 0; i--) {
      if (s[i].pct != null) return s[i].pct;
    }
    return null;
  }

  // 五张指标卡：彩色标签 + 权重占比 + 当前值 + 10年分位
  function metricCards(d, pcts, weights) {
    const grid = el('div', { class: 'metric-grid' });
    Object.entries(METRIC_META).forEach(([f, m]) => {
      const val = d.kline && d.kline.metrics ? d.kline.metrics[f] : null;
      const w = (weights || {})[FIELD_KEY[f]] || 0;
      const p = pctLatest(pcts, f);
      const label = el('div', { class: 'metric-label' },
        el('span', { style: 'color:' + m.color }, m.label),
        w > 0 ? el('span', { class: 'metric-w' }, Math.round(w * 100) + '%') : null);
      grid.append(el('div', { class: 'metric-card' },
        label,
        el('div', { class: 'metric-val' }, fmt(val, 2)),
        el('div', { class: 'metric-sub' },
          p != null
            ? el('span', {}, '10年分位 ',
                el('b', { style: 'color:' + charts.bandColor(p) }, fmt(p, 0) + '%'))
            : el('span', { class: 'muted' }, '暂无分位'))));
    });
    return grid;
  }

  // opts.weights: 'edit' | 'readonly' | false
  // opts.pcts:    分位序列（算指标卡的当前分位）
  // opts.onWeightsSaved / opts.onTargetRemoved
  function metricsWeightsCard(d, pcts, opts) {
    opts = opts || {};
    const code = opts.weightsCode || d.code;
    const w = d.weights || {};
    const configured = !!d.is_target && WEIGHT_KEYS.some((k) => (w[k] || 0) > 0);

    const card = el('div', { class: 'card mw-card' });
    const acts = el('div', { class: 'section-actions' });
    const sumEl = el('span', { class: 'muted small' });
    card.append(el('div', { class: 'section-head' },
      el('h3', { class: 'card-title', style: 'margin:0' }, '指标与权重'), acts));

    // 编辑态的输入框只创建一次（反复重建会丢焦点）
    const inputs = {};
    const editor = el('div', { class: 'weight-editor', style: 'display:none' });
    WEIGHT_KEYS.forEach((k) => {
      const meta = METRIC_META[WEIGHT_META[k].field] || {};
      const inp = el('input', { type: 'number', min: '0', step: '1', max: '100',
        value: configured ? Math.round((w[k] || 0) * 100) : DEFAULT_WEIGHTS[k] });
      inputs[k] = inp;
      inp.addEventListener('input', refresh);
      editor.append(el('label', { class: 'we-cell' },
        el('span', { class: 'we-label',
          style: 'color:' + (meta.color || '#75839a') }, WEIGHT_META[k].label),
        inp, el('span', { class: 'muted small' }, '%')));
    });

    const body = el('div');
    const gridHolder = el('div');
    card.append(editor, body, gridHolder);

    const saveBtn = el('button', { class: 'btn mini', onclick: save }, '保存权重');
    const editBtn = el('button', { class: 'btn ghost mini', onclick: reveal },
      configured ? '修改权重' : '添加为标的信息');
    const outBtn = configured
      ? el('button', { class: 'btn ghost mini', onclick: removeTarget }, '移出标的信息')
      : null;

    function cur() {
      const out = {};
      WEIGHT_KEYS.forEach((k) => { out[k] = (Number(inputs[k].value) || 0) / 100; });
      return out;
    }

    function refresh() {
      const cw = cur();
      const sum = WEIGHT_KEYS.reduce((a, k) => a + cw[k], 0);
      sumEl.textContent = sum > 0 ? '合计 ' + Math.round(sum * 100) + '%' : '';
      body.innerHTML = '';
      gridHolder.innerHTML = '';
      if (sum > 0) {
        body.append(weightsStack(cw), weightsLegend(cw));
      } else {
        body.append(el('div', { class: 'muted small' },
          '未配置评分权重：不计算综合评分，只展示估值分位'));
      }
      // 指标卡用编辑中的权重，边调边看占比
      gridHolder.append(metricCards(d, pcts, cw));
    }

    function setActs(editing) {
      acts.innerHTML = '';
      acts.append(sumEl);
      if (editing) {
        acts.append(saveBtn);
      } else {
        acts.append(editBtn);
        if (outBtn) acts.append(outBtn);
      }
    }

    function reveal() {
      WEIGHT_KEYS.forEach((k) => {
        inputs[k].value = configured ? Math.round((w[k] || 0) * 100) : DEFAULT_WEIGHTS[k];
      });
      editor.style.display = '';
      setActs(true);
      refresh();
    }

    async function save() {
      saveBtn.disabled = true;
      try {
        const b = {};
        WEIGHT_KEYS.forEach((k) => { b[k] = Number(inputs[k].value) || 0; });
        const r = await api(API.weights(code), { method: 'PUT', json: b });
        showToast((configured ? '权重已保存' : '已添加为标的信息')
          + (r.scored ? '，评分已重算' : ''), true);
        if (opts.onWeightsSaved) opts.onWeightsSaved();
      } catch (e) {
        showToast(e.message, false);
      } finally {
        saveBtn.disabled = false;
      }
    }

    // 移出标的信息：**两步内联确认**（比浏览器 confirm 友好，也不打断页面）
    //   点「移出标的信息」→ 就地展开提示 + [确认移出][取消]
    function removeTarget() {
      const wasEditing = editor.style.display !== 'none';
      editor.style.display = 'none';
      acts.innerHTML = '';
      const warn = el('span', { class: 'rm-warn' },
        '移出后不再计算综合评分，也不会出现在邮件里：');
      const yes = el('button', { class: 'btn mini danger' }, '确认移出');
      const no = el('button', { class: 'btn ghost mini' }, '取消');
      yes.addEventListener('click', async () => {
        yes.disabled = true; yes.textContent = '移出中…';
        try {
          await api(API.targetDelete(code), { method: 'DELETE' });
          showToast('已把 ' + (d.name || code) + ' 移出标的信息', true);
          if (opts.onTargetRemoved) opts.onTargetRemoved();
        } catch (e) {
          showToast(e.message, false);
          yes.disabled = false; yes.textContent = '确认移出';
        }
      });
      no.addEventListener('click', () => {
        setActs(false);
        if (wasEditing) { editor.style.display = ''; setActs(true); }
      });
      acts.append(warn, yes, no);
    }

    if (opts.weights === 'edit') {
      setActs(false);
      // 初始态：已配置就展示真实权重；未配置只给提示，
      // 不能把"默认权重"当成已配置画出来（会误导）
      if (configured) {
        sumEl.textContent = '合计 '
          + Math.round(WEIGHT_KEYS.reduce((a, k) => a + (w[k] || 0), 0) * 100) + '%';
        body.append(weightsStack(w), weightsLegend(w));
      } else {
        body.append(el('div', { class: 'muted small' },
          '未配置评分权重：不计算综合评分，只展示估值分位。'
          + '点右上角「添加为标的信息」即可配置五指标权重。'));
      }
      gridHolder.append(metricCards(d, pcts, configured ? w : {}));
    } else {
      // 只读：不显示操作按钮
      sumEl.textContent = configured
        ? '合计 ' + Math.round(WEIGHT_KEYS.reduce((a, k) => a + (w[k] || 0), 0) * 100) + '%'
        : '';
      acts.append(sumEl);
      body.innerHTML = '';
      if (configured) {
        body.append(weightsStack(w), weightsLegend(w));
      } else {
        body.append(el('div', { class: 'muted small' },
          '未配置评分权重：本页只展示估值分位，不计算综合评分'));
      }
      gridHolder.append(metricCards(d, pcts, w));
    }
    return card;
  }

  // 渲染 5 个指标的分位走势图（pcts: {指标: [{date,pct}]}）
  function metricPctCharts(container, pcts) {
    const insts = {};
    Object.entries(PCT_FIELDS).forEach(([metric]) => {
      const meta = METRIC_META[metric] || { label: metric, color: '#666' };
      const series = (pcts && pcts[metric]) || [];
      const last = series.length ? series[series.length - 1].pct : null;
      const card = el('div', { class: 'card' });
      card.append(ctitle(meta.label + ' 估值分位', [
        last != null ? valChip(fmt(last, 1) + '%', '', charts.bandColor(last)) : null,
      ]));
      const wrap = el('div', { class: 'chart', id: 'avpct-' + metric, style: 'height:220px' });
      card.append(wrap);
      container.append(card);
      insts[metric] = charts.pctChart(wrap, series, 'pct', meta.label);
    });
    return insts;
  }

  return { render, metricPctCharts, ctitle, valChip, statusPill, scoreDeltaEl };
})();
