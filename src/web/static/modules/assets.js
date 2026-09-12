/* =====================================================================
   模块：标的信息（只读展示）—— 目标列表 + 详情图表 + 建议 + 权重展示
   ===================================================================== */
'use strict';

const Assets = (function () {
  let root = null, current = null;
  // 列表按邮件顺序平铺，类型只能标在每一行上
  const KIND_LABEL = { index: '指数', portfolio: '组合', stock: '个股' };

  async function mount(el) {
    root = el;
    current = null;          // 每次从导航进来都默认展示第一个标的
    root.innerHTML = '<div class="layout">'
      + '<aside class="side" id="asset-list"></aside>'
      + '<section class="content" id="asset-detail"><div class="muted pad">选择左侧标的查看详情</div></section>'
      + '</div>';
    await reloadList();
  }

  // 按 sort_order 排好的完整顺序（跨类型），调序时用它算新位置
  async function moveTarget(code, delta) {
    const data = await api(API.targets);
    const codes = (data.items || []).map((t) => t.code);
    const i = codes.indexOf(code);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= codes.length) return;
    [codes[i], codes[j]] = [codes[j], codes[i]];
    try {
      await api(API.targetOrder, { method: 'PUT', json: { codes } });
      showToast('顺序已更新（邮件顺序同步）', true);
      await reloadList();
    } catch (e) {
      showToast(e.message, false);
    }
  }

  async function reloadList() {
    const data = await api(API.targets);
    const targets = data.items || [];
    const box = $('asset-list');
    box.innerHTML = '';
    box.append(el('div', { class: 'side-hint muted' },
      '顺序即「标的信息」与邮件的展示顺序，用 ▲▼ 调整'));
    // 平铺展示（不再按类型分组）：列表顺序**就是**邮件顺序，
    // 所以点一下 ▲▼ 立刻能看到位置变化，不会出现"顺序变了但界面没动"。
    // 类型改用每行一个小标签标出来。
    targets.forEach((t, idx) => {
      const item = el('div', { class: 'side-item' + (current === t.code ? ' active' : ''),
        onclick: () => select(t.code) });
      const up = el('button', {
        class: 'ord-btn', title: '上移', disabled: idx === 0,
        onclick: (ev) => { ev.stopPropagation(); moveTarget(t.code, -1); },
      }, '▲');
      const down = el('button', {
        class: 'ord-btn', title: '下移', disabled: idx === targets.length - 1,
        onclick: (ev) => { ev.stopPropagation(); moveTarget(t.code, 1); },
      }, '▼');
      item.append(el('div', { class: 'side-ord' }, up, down));
      const body = el('div', { class: 'side-body' });
      body.append(el('div', { class: 'side-name' }, t.name,
        el('span', { class: 'tag side-kind k-' + t.ktype },
          KIND_LABEL[t.ktype] || t.ktype)));
      body.append(el('div', { class: 'side-code' }, t.code
        + (t.latest_score ? ' · ' + fmt(t.latest_score) : '')));
      item.append(body);
      box.append(item);
    });
    if (!targets.length) box.append(el('div', { class: 'muted pad' }, '暂无估值目标'));
    // 默认展示第一个标的
    if (!current && targets.length) await select(targets[0].code);
  }

  async function select(code) {
    current = code;
    document.querySelectorAll('#asset-list .side-item').forEach((n) =>
      n.classList.toggle('active', n.textContent.indexOf(code) >= 0));
    const detail = $('asset-detail');
    if (!detail) return;
    detail.innerHTML = '<div class="muted pad">加载中…</div>';
    try {
      if ($('asset-detail') !== detail) return;   // 已经切到别的页面，丢弃
      const [d, k, p, s] = await Promise.all([
        api(API.asset(code)), api(API.kline(code)),
        api(API.percentiles(code)), api(API.score(code)),
      ]);
      // 标的信息页也允许调整权重/移出（"添加为标的信息"在个股/指数与组合页）
      AssetView.render(detail, d, k.items, s.items, p.metrics, {
        weights: 'edit', weightsCode: code,
        onWeightsSaved: () => select(code),
        onTargetRemoved: () => reloadList(),
      });
    } catch (e) {
      if ($('asset-detail') === detail) {
        detail.innerHTML = '<div class="muted pad">加载失败：' + esc(e.message) + '</div>';
      }
    }
  }

  return { mount, reloadList, select };
})();
