/* =====================================================================
   模块：个股管理 —— 左侧分页列表（瀑布流加载）+ 详情（基本信息 + 图表 + 操作）
   ===================================================================== */
'use strict';

const Stocks = (function () {
  const PAGE = 60;                 // 每页条数
  let root = null, current = null;
  let after = null, hasMore = true, loading = false, loaded = 0;
  let gen = 0;                     // 每次进入页面 +1，用来作废上一次遗留的请求

  async function mount(el) {
    root = el;
    current = null;          // 每次从导航进来都默认展示第一只
    gen += 1;                // 作废上一次遗留的请求/渲染
    after = null; hasMore = true; loading = false; loaded = 0;
    root.innerHTML = '<div class="layout">'
      + '<aside class="side side-flex">'
      + '  <div class="side-fixed" id="stock-search"></div>'
      + '  <div class="side-scroll" id="stock-list"></div>'
      + '</aside>'
      + '<section class="content" id="stock-detail"><div class="muted pad">搜索或选择个股</div></section>'
      + '</div>';
    buildSearch();
    const items = await loadMore(true);
    // 默认展示第一只个股
    if (!current && items && items.length) await select(items[0].code);
  }

  function buildSearch() {
    const box = $('stock-search');
    box.innerHTML = '';
    const search = el('input', { class: 'input', placeholder: '搜索代码/名称…' });
    const results = el('div', { class: 'search-results' });
    let timer = null;
    search.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = search.value.trim();
        if (!q) { results.innerHTML = ''; return; }
        const d = await api(API.search(q));
        results.innerHTML = '';
        (d.items || []).forEach((s) => {
          const row = el('div', { class: 'search-row', onclick: () => {
            search.value = ''; results.innerHTML = ''; select(s.code);
          } });
          row.append(el('span', { class: 'search-code' }, s.code));
          row.append(el('span', { class: 'muted' }, s.name || ''));
          results.append(row);
        });
      }, 250);
    });
    box.append(el('div', { class: 'addbar' }, search, results));
    box.append(el('div', { class: 'side-title', id: 'stock-count' }, '加载中…'));

    // 滚到底部附近 → 自动加载下一页（瀑布流）
    const list = $('stock-list');
    list.addEventListener('scroll', () => {
      if (loading || !hasMore) return;
      if (list.scrollTop + list.clientHeight >= list.scrollHeight - 140) {
        loadMore(false);
      }
    });
  }

  async function loadMore(reset) {
    if (loading && !reset) return null;      // 非重置时防并发
    if (!reset && !hasMore) return null;
    const my = gen;
    loading = true;
    if (reset) { after = null; hasMore = true; loaded = 0; }
    try {
      const d = await api(API.stocks(after, PAGE));
      if (my !== gen) return null;           // 期间切走又切回 → 丢弃本次结果
      const box = $('stock-list');
      if (!box) return null;
      if (reset) box.innerHTML = '';
      (d.items || []).forEach((s) => box.append(sideItem(s)));
      loaded += (d.items || []).length;
      after = d.next_after;
      hasMore = !!d.has_more;
      const cnt = $('stock-count');
      if (cnt) {
        cnt.textContent = '已有数据个股（已加载 ' + loaded + (hasMore ? '+' : '') + '）';
      }
      return d.items || [];
    } catch (e) {
      showToast(e.message, false);
      return null;
    } finally {
      if (my === gen) loading = false;
    }
  }

  function sideItem(s) {
    const item = el('div', {
      class: 'side-item' + (current === s.code ? ' active' : ''),
      onclick: () => select(s.code) });
    item.append(el('div', { class: 'side-name' }, s.name));
    item.append(el('div', { class: 'side-code' }, s.code
      + (s.latest_kline ? ' · ' + s.latest_kline : '')));
    return item;
  }

  async function reloadList() {
    current = current;                   // 保留选中
    await loadMore(true);
  }

  async function select(code) {
    current = code;
    document.querySelectorAll('#stock-list .side-item').forEach((n) =>
      n.classList.toggle('active', n.textContent.indexOf(code) >= 0));
    const detail = $('stock-detail');
    if (!detail) return;
    detail.innerHTML = '<div class="muted pad">加载中…</div>';
    try {
      const [d, k, p, s] = await Promise.all([
        api(API.asset(code)), api(API.kline(code)),
        api(API.percentiles(code)), api(API.score(code)),
      ]);
      if ($('stock-detail') !== detail) return;   // 已经切到别的页面，丢弃
      AssetView.render(detail, d, k.items, s.items, p.metrics, {
        weights: 'edit',
        weightsCode: code,
        actions: [{ label: '拉取数据', cls: 'btn', onClick: (ev) => sync(code, ev) }],
        onWeightsSaved: () => select(code),
        onTargetRemoved: () => select(code),
      });
    } catch (e) {
      if ($('stock-detail') === detail) {
        detail.innerHTML = '<div class="muted pad">加载失败：' + esc(e.message) + '</div>';
      }
    }
  }

  async function sync(code, ev) {
    const btn = ev && ev.target;
    if (btn) { btn.disabled = true; btn.textContent = '拉取中…'; }
    showToast('开始拉取 ' + code + '（首次可能较久，请稍候）', true);
    try {
      const r = await api(API.syncStock(code), { method: 'POST', timeoutMs: 30 * 60 * 1000 });
      showToast('拉取完成：K线 ' + (r.sync && r.sync.kline) + ' 行', true);
      select(code);
    } catch (e) {
      showToast(e.message, false);
      if (btn) { btn.disabled = false; btn.textContent = '拉取数据'; }
    }
  }

  return { mount, reloadList, select };
})();
