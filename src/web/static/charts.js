/* =====================================================================
   ECharts 封装：K线蜡烛图 / 多指标折线 / 评分走势 / 分位柱状
   图表容器通过 resize 自适应；每次 init 前先 dispose 旧实例避免泄漏。
   ===================================================================== */
'use strict';

const charts = (function () {
  function _init(dom) {
    const inst = echarts.getInstanceByDom(dom);
    if (inst) inst.dispose();
    return echarts.init(dom, null, { renderer: 'canvas' });
  }

  const BASE_TEXT = { color: '#8b93a5', fontSize: 11 };
  const AXIS = {
    axisLine: { lineStyle: { color: '#e6e8ef' } },
    axisTick: { show: false },
    axisLabel: BASE_TEXT,
    splitLine: { lineStyle: { color: '#f0f1f5' } },
  };

  function _dateAxis(data, gridIndex) {
    return {
      type: 'category', data: data, boundaryGap: true, gridIndex: gridIndex || 0,
      axisLine: { lineStyle: { color: '#e6e8ef' } }, axisTick: { show: false },
      axisLabel: Object.assign({}, BASE_TEXT, { hideOverlap: true }),
    };
  }

  function kline(dom, items) {
    const inst = _init(dom);
    const dates = items.map((r) => r.date);
    const ohlc = items.map((r) => [r.open, r.close, r.low, r.high]);  // ECharts 顺序
    const vol = items.map((r) => ({ value: r.volume, itemStyle: {
      color: r.close >= r.open ? '#c94f4f' : '#1e8e5a' } }));

    inst.setOption({
      animation: false,
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'cross' },
        backgroundColor: 'rgba(34,41,58,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 11 },
        formatter: (ps) => {
          const p = ps.find((x) => x.seriesType === 'candlestick');
          if (!p) return '';
          const i = p.dataIndex; const r = items[i];
          return [r.date,
            '开 ' + fmt(r.open, 2) + '  收 ' + fmt(r.close, 2),
            '低 ' + fmt(r.low, 2) + '  高 ' + fmt(r.high, 2),
            '涨跌 ' + fmt(r.pct_chg, 2) + '%  换手 ' + fmt(r.turn, 2) + '%',
            '量 ' + fmt(r.volume, 0)].join('<br>');
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 58, right: 16, top: 30, height: '58%' },
        { left: 58, right: 16, top: '76%', height: '16%' },
      ],
      xAxis: [_dateAxis(dates, 0), _dateAxis(dates, 1)],
      yAxis: [
        Object.assign({ scale: true, gridIndex: 0 }, AXIS),
        Object.assign({ gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } }),
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 16, start: 60, end: 100 },
      ],
      series: [
        { name: 'K线', type: 'candlestick', data: ohlc,
          itemStyle: { color: '#c94f4f', color0: '#1e8e5a', borderColor: '#c94f4f', borderColor0: '#1e8e5a' },
          // 最新收盘价画一条横线，并在 y 轴一侧标出具体数值
          markLine: _closeMarkLine(items) },
        { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: vol },
      ],
    });
    return inst;
  }

  function multiLine(dom, items, fields, opts) {
    const inst = _init(dom);
    opts = opts || {};
    const dates = items.map((r) => r.date);
    const series = fields.map((f) => {
      const m = METRIC_META[f] || { label: f, color: '#666' };
      return { name: m.label, type: 'line', showSymbol: false, smooth: true,
               lineStyle: { width: 1.5, color: m.color },
               itemStyle: { color: m.color },
               connectNulls: true,
               data: items.map((r) => r[f]) };
    });
    inst.setOption({
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(34,41,58,.92)',
                 borderWidth: 0, textStyle: { color: '#fff', fontSize: 11 } },
      legend: { top: 2, textStyle: BASE_TEXT, itemWidth: 14, itemHeight: 8 },
      grid: { left: 58, right: 16, top: 34, bottom: 26 },
      xAxis: _dateAxis(dates),
      yAxis: Object.assign({ scale: true }, AXIS),
      dataZoom: [{ type: 'inside', start: 0, end: 100 }],
      series: series,
    });
    return inst;
  }

  function _bandMarkLine() {
    // 阈值与配色来自后端 SIGNAL_BANDS / STATUS_STYLE（经 /api/health meta 下发），
    // 不再写死。每档只画它的上界（最后一档的上界是开区间，不画）。
    const data = BANDS.slice(0, -1).map((b) => ({
      yAxis: b[1], name: b[2] + ' ' + b[1],
      lineStyle: { color: b[3], type: 'dashed', opacity: 0.55 },
    }));
    return { silent: true, symbol: 'none',
      label: { formatter: '{b}', fontSize: 9, position: 'insideEndTop' },
      data };
  }

  function _bandColor(v) {
    // 数值落在哪个估值区间 → 该区间配色（与 BANDS/标线一致）
    if (v === null || v === undefined || v !== v) return '#8b93a5';
    if (!BANDS.length) return '#8b93a5';
    for (const b of BANDS) if (v >= b[0] && v < b[1]) return b[3];
    return v < BANDS[0][0] ? BANDS[0][3] : BANDS[BANDS.length - 1][3];
  }

  function _endLabel(values) {
    // 折线末端标注最新值（跳过末尾的 null）
    let last = null;
    for (let i = values.length - 1; i >= 0; i--) {
      if (values[i] !== null && values[i] !== undefined) { last = values[i]; break; }
    }
    if (last === null) return { show: false };
    const c = _bandColor(last);
    return {
      show: true, distance: 6, offset: [4, 0], align: 'left',
      formatter: () => fmt(last, 1),
      color: '#fff', fontSize: 11, fontWeight: 700,
      backgroundColor: c, padding: [2, 5], borderRadius: 4,
    };
  }

  function pctChart(dom, items, field, label) {
    const inst = _init(dom);
    const dates = items.map((r) => r.date);
    const values = items.map((r) => r[field]);
    inst.setOption({
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(34,41,58,.92)',
                 borderWidth: 0, textStyle: { color: '#fff', fontSize: 11 },
                 formatter: (ps) => {
                   if (!ps || !ps.length) return '';
                   const i = ps[0].dataIndex; const r = items[i];
                   return [r.date, label + ' 分位 ' + fmt(r[field], 1) + '%'].join('<br>');
                 } },
      grid: { left: 46, right: 52, top: 24, bottom: 24 },
      xAxis: _dateAxis(dates),
      yAxis: Object.assign({ min: 0, max: 100 }, AXIS),
      visualMap: { show: false, seriesIndex: 0,
        pieces: BANDS.map((b) => ({ gt: b[0], lte: b[1], color: b[3] })) },
      dataZoom: [{ type: 'inside', start: 0, end: 100 }],
      series: [{ name: label, type: 'line', showSymbol: false, connectNulls: true,
                 sampling: 'lttb', data: values, markLine: _bandMarkLine(),
                 endLabel: _endLabel(values) }],
    });
    return inst;
  }

  // 最新一根 K 线收盘价处的横线：虚线 + 在 y 轴一侧显示具体数值。
  // ECharts 的 markLine 标签只能贴线，所以放在线的左端（紧邻 y 轴），
  // 视觉上就读作"y 轴标注了这一档价格"。
  function _closeMarkLine(items) {
    let last = null;
    for (let i = items.length - 1; i >= 0; i--) {
      if (items[i] && items[i].close != null) { last = items[i]; break; }
    }
    if (!last) return undefined;
    const up = last.open == null || last.close >= last.open;
    const c = up ? '#c94f4f' : '#1e8e5a';
    return {
      silent: true, symbol: 'none', animation: false,
      lineStyle: { color: c, type: 'dashed', width: 1, opacity: .9 },
      data: [{
        yAxis: last.close,
        label: {
          show: true, position: 'insideStartTop', distance: 2,
          formatter: () => fmt(last.close, 2),
          color: '#fff', fontSize: 10, fontWeight: 700, padding: [2, 4],
          borderRadius: 3, backgroundColor: c,
        },
      }],
    };
  }

  function scoreTrend(dom, items) {
    const inst = _init(dom);
    const dates = items.map((r) => r.date);
    inst.setOption({
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(34,41,58,.92)',
                 borderWidth: 0, textStyle: { color: '#fff', fontSize: 11 },
                 formatter: (ps) => {
                   if (!ps || !ps.length) return '';
                   const i = ps[0].dataIndex; const r = items[i];
                   // 状态按当前配置实时推导（库里的 r.status 是旧快照）
                   const sig = signalOf(r.score);
                   const div = divergenceOf(r.score, r.score5);
                   const lines = [r.date,
                     '评分10年 ' + fmt(r.score, 1) + '%',
                     '评分5年 ' + fmt(r.score5, 1) + '%'];
                   if (div && div.over) {
                     lines.push('短期偏离 ' + (div.diff > 0 ? '+' : '') + fmt(div.diff, 1)
                                + '（阈值 ' + META.divergence + '）');
                   }
                   if (sig.status !== '未知') {
                     lines.push(sig.icon + ' ' + sig.status + ' · ' + sig.short);
                   }
                   return lines.join('<br>');
                 } },
      legend: { top: 2, textStyle: BASE_TEXT, itemWidth: 14, itemHeight: 8 },
      grid: { left: 46, right: 52, top: 34, bottom: 26 },
      xAxis: _dateAxis(dates),
      yAxis: Object.assign({ min: 0, max: 100 }, AXIS),
      visualMap: { show: false, seriesIndex: 0,
        pieces: BANDS.map((b) => ({ gt: b[0], lte: b[1], color: b[3] })) },
      dataZoom: [{ type: 'inside', start: 0, end: 100 }],
      series: [
        { name: '评分10年', type: 'line', showSymbol: false,
          sampling: 'lttb', data: items.map((r) => r.score),
          lineStyle: { width: 2 }, areaStyle: { opacity: 0.08 },
          markLine: _bandMarkLine(),
          endLabel: _endLabel(items.map((r) => r.score)) },
        { name: '评分5年', type: 'line', showSymbol: false,
          sampling: 'lttb', data: items.map((r) => r.score5),
          lineStyle: { width: 1.3, type: 'dashed', color: '#3a7bd5' },
          itemStyle: { color: '#3a7bd5' } },
      ],
    });
    return inst;
  }

  return { kline, multiLine, pctChart, scoreTrend, bandColor: _bandColor,
           init: _init };
})();

window.addEventListener('resize', () => {
  document.querySelectorAll('.chart').forEach((dom) => {
    const inst = echarts.getInstanceByDom(dom);
    if (inst) inst.resize();
  });
});
