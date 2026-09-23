/* =====================================================================
   模块：指数与组合（管理）—— 合并"指数管理"与"自定义组合"
   列表（指数/组合带类型徽章 + 新建组合）、成分股增删、权重、评分、拉取数据
   ===================================================================== */
'use strict';

const Targets = (function () {
  let root = null, current = null, targetsCache = [];
  let etfAfter = null, etfDone = false, etfLoading = false;
  let idxAfter = null, idxDone = false, idxLoading = false, idxCat = '';
  let pendingCode = null;   // 首页告警行点进来时要直接打开的标的

  async function mount(el) {
    root = el;
    const want = pendingCode;   // 指定了标的就定位到它，否则默认展示第一个
    pendingCode = null;
    current = want || null;
    root.innerHTML = '<div class="layout">'
      + '<aside class="side" id="tg-list"></aside>'
      + '<section class="content" id="tg-detail"><div class="muted pad">选择左侧目标</div></section>'
      + '</div>';
    await reloadList();
    if (want) await select(want);
  }

  /** 供首页告警行调用：记录要打开的标的，随后的 mount 会直接定位过去 */
  function open(code) { pendingCode = code; }

  async function reloadList() {
    const data = await api(API.targets);
    targetsCache = (data.items || []).filter((t) => t.ktype === 'index' || t.ktype === 'portfolio');
    const box = $('tg-list');
    box.innerHTML = '';
    box.append(el('div', { class: 'side-title' }, '指数与组合'));
    targetsCache.forEach((t) => {
      const badge = t.ktype === 'index' ? '指数' : '组合';
      const item = el('div', { class: 'side-item' + (current === t.code ? ' active' : ''),
        onclick: () => select(t.code) });
      item.append(el('div', { class: 'side-name' },
        el('span', { class: 'tag ' + (t.ktype === 'index' ? 'ok' : 'warn') }, badge),
        ' ' + t.name));
      item.append(el('div', { class: 'side-code' }, t.code));
      box.append(item);
    });
    box.append(el('button', { class: 'btn block', onclick: createForm }, '＋ 新建组合'));

    // ---- 指数列表：来自 stock_basic 的 560+ 只指数元数据（baostock 文档导入），
    //      不是 kline —— 这些指数里只有"标的信息"里的少数几只有 K 线。
    //      按类别筛选 + 游标分页；点进去只浏览（详情里可「添加为标的信息」）。
    idxAfter = null; idxDone = false; idxCat = '';
    box.append(el('div', { class: 'side-title' }, '指数'));
    const idxSel = el('select', { class: 'input side-select', title: '按指数类别筛选' });
    idxSel.append(el('option', { value: '' }, '全部类别'));
    const idxHolder = el('div', { id: 'tg-idx' });
    const idxBtn = el('button', { class: 'btn block', onclick: () => loadIndexes(idxHolder, idxBtn, idxSel) },
      '加载更多指数');
    idxSel.addEventListener('change', () => {
      idxCat = idxSel.value; idxAfter = null; idxDone = false;
      idxHolder.innerHTML = '';
      loadIndexes(idxHolder, idxBtn, idxSel);
    });
    box.append(idxSel, idxHolder, idxBtn);
    loadIndexes(idxHolder, idxBtn, idxSel);   // 首屏自动带出前 60 只

    // ---- ETF 列表：每日全市场快照会落 1600+ 只，用游标分页 + 手动"加载更多"
    //      （一次渲染 1600 行会卡）。点进去复用同一套详情渲染：
    //      ETF 没有前复权 close / 没有 PE-PB，后端已让 K 线回退用 close_raw。
    etfAfter = null; etfDone = false;
    box.append(el('div', { class: 'side-title' }, 'ETF'));
    const etfHolder = el('div', { id: 'tg-etf' });
    const etfBtn = el('button', { class: 'btn block', onclick: () => loadEtfs(etfHolder, etfBtn) },
      '加载更多 ETF');
    box.append(etfHolder, etfBtn);
    loadEtfs(etfHolder, etfBtn);   // 首屏自动带出前 60 只，其余点"加载更多"（不 await，不挡页面）
    // 默认展示第一个指数/组合
    if (!current && targetsCache.length) await select(targetsCache[0].code);
  }

  function indexItem(t) {
    const item = el('div', { class: 'side-item' + (current === t.code ? ' active' : ''),
      onclick: () => select(t.code) });
    item.append(el('div', { class: 'side-name' }, t.name || t.code));
    item.append(el('div', { class: 'side-code' },
      t.code + (t.category ? ' · ' + t.category : '')));
    return item;
  }

  async function loadIndexes(holder, btn, sel) {
    if (idxLoading || idxDone) return;
    idxLoading = true;
    const old = btn.textContent;
    btn.textContent = '加载中…';
    try {
      const d = await api(API.indexes(idxAfter, 60, idxCat));
      if (sel && !sel.dataset.filled) {     // 类别下拉只填一次
        (d.categories || []).forEach((c) => sel.append(el('option', { value: c }, c)));
        sel.dataset.filled = '1';
      }
      (d.items || []).forEach((t) => holder.append(indexItem(t)));
      idxAfter = d.next_after;
      idxDone = !d.has_more;
      btn.textContent = idxDone ? '（指数已全部加载）' : '加载更多指数';
    } catch (e) {
      showToast(e.message, false);
      btn.textContent = old;
    } finally {
      idxLoading = false;
    }
  }

  function etfItem(t) {
    const item = el('div', { class: 'side-item' + (current === t.code ? ' active' : ''),
      onclick: () => select(t.code) });
    item.append(el('div', { class: 'side-name' },
      el('span', { class: 'tag off' }, 'ETF'), ' ' + (t.name || t.code)));
    item.append(el('div', { class: 'side-code' }, t.code));
    return item;
  }

  async function loadEtfs(holder, btn) {
    if (etfLoading || etfDone) return;
    etfLoading = true;
    const old = btn.textContent;
    btn.textContent = '加载中…';
    try {
      const d = await api(API.etfs(etfAfter, 60));
      (d.items || []).forEach((t) => holder.append(etfItem(t)));
      etfAfter = d.next_after;
      etfDone = !d.has_more;
      btn.textContent = etfDone ? '（ETF 已全部加载）' : '加载更多 ETF';
    } catch (e) {
      showToast(e.message, false);
      btn.textContent = old;
    } finally {
      etfLoading = false;
    }
  }

  function createForm() {
    const detail = $('tg-detail');
    if (!detail) return;
    detail.innerHTML = '';
    const card = el('div', { class: 'card' });
    card.append(el('h3', { class: 'card-title' }, '新建组合'));
    const name = el('input', { class: 'input', placeholder: '组合名称（必填）' });
    const code = el('input', { class: 'input', placeholder: '代码（可选，默认自动 pf.001）' });
    const btn = el('button', { class: 'btn', onclick: async () => {
      if (!name.value.trim()) { showToast('请填名称', false); return; }
      const body = { name: name.value.trim() };
      if (code.value.trim()) body.code = code.value.trim();
      const r = await api(API.portfolios, { method: 'POST', json: body });
      showToast('已创建', true);
      await reloadList();
      select(r.code);
    } }, '创建');
    card.append(name, code, el('div', { class: 'pad-top' }, btn));
    detail.append(card);
  }

  // 静默刷新：只重画横幅/图表（AssetView 那部分），
  // **保留成分股卡片这个 DOM 节点**，并恢复滚动位置 —— 用户不会看到页面闪一下。
  async function selectSoft(code) {
    const detail = $('tg-detail');
    if (!detail) return;
    const keep = detail.querySelector('.cons-card');   // 成分股卡片原样保留
    const y = window.scrollY;
    try {
      const [d, c, s, k, p] = await Promise.all([
        api(API.asset(code)),
        api(API.indexConstituents(code)).catch(() => ({ items: [], count: 0 })),
        api(API.score(code)).catch(() => ({ items: [] })),
        api(API.kline(code)).catch(() => ({ items: [] })),
        api(API.percentiles(code)).catch(() => ({ metrics: {} })),
      ]);
      if ($('tg-detail') !== detail) return;
      if (keep) keep.remove();          // 先摘下来，避免被 render 清空
      render(detail, code, d, c, s, k, p);
      const fresh = detail.querySelector('.cons-card');
      if (keep) {
        // 用新拉到的数据重建列表，但保持节点位置不变
        if (fresh) fresh.replaceWith(keep); else detail.append(keep);
      }
      window.scrollTo(0, y);
    } catch (e) {
      showToast('刷新详情失败：' + e.message, false);
    }
  }

  async function select(code) {
    current = code;
    document.querySelectorAll('#tg-list .side-item').forEach((n) =>
      n.classList.toggle('active', n.textContent.indexOf(code) >= 0));
    const detail = $('tg-detail');
    detail.innerHTML = '<div class="muted pad">加载中…</div>';
    try {
      if ($('tg-detail') !== detail) return;   // 已经切到别的页面，丢弃
      const [d, c, s, k, p] = await Promise.all([
        api(API.asset(code)),
        api(API.indexConstituents(code)),
        api(API.score(code)).catch(() => ({ items: [] })),
        api(API.kline(code)).catch(() => ({ items: [] })),
        api(API.percentiles(code)).catch(() => ({ metrics: {} })),
      ]);
      render(detail, code, d, c, s, k, p);
    } catch (e) {
      if ($('tg-detail') === detail) {
        detail.innerHTML = '<div class="muted pad">加载失败：' + esc(e.message) + '</div>';
      }
    }
  }

  async function syncData(code, ev) {
    const btn = ev && ev.target;
    if (btn) { btn.disabled = true; btn.textContent = '拉取中…'; }
    showToast('开始拉取 ' + code + '（首次可能较久，请稍候）', true);
    try {
      await api(API.syncTarget(code), { method: 'POST', timeoutMs: 30 * 60 * 1000 });
      showToast('拉取完成', true);
      select(code);
    } catch (e) {
      showToast(e.message, false);
      if (btn) { btn.disabled = false; btn.textContent = '拉取数据'; }
    }
  }

  async function rescore(code) {
    try {
      const r = await api(API.targetScore(code), { method: 'POST' });
      // 状态按当前配置推导（r.status 是后端算分位那一刻的快照）
      const sig = signalOf(r.score);
      showToast('评分 ' + fmt(r.score, 1) + ' · ' + sig.status
                + (sig.short && sig.short !== '—' ? ' · ' + sig.short : ''), true);
      select(code);
    } catch (e) { showToast(e.message, false); }
  }

  async function removePortfolio(code) {
    if (!confirm('确定删除组合 ' + code + ' 吗？')) return;
    await api(API.portfolio(code), { method: 'DELETE' });
    showToast('已删除', true);
    current = null;
    await reloadList();
    $('tg-detail').innerHTML = '<div class="muted pad">选择左侧目标</div>';
  }

  // 重算组合：先补成分股最新数据 → 删掉旧的组合K线/分位/评分 → 全量重算
  // 两步确认直接做在按钮上（避免浏览器弹窗打断操作）
  let rbtimer = null;
  async function rebuildPortfolio(code, btn) {
    if (btn && !btn.classList.contains('pending-del')) {
      btn.classList.add('pending-del');
      btn.textContent = '确认重算？（会删旧数据）';
      clearTimeout(rbtimer);
      rbtimer = setTimeout(() => {
        btn.classList.remove('pending-del');
        btn.textContent = '重算组合';
      }, 4000);
      return;
    }
    clearTimeout(rbtimer);
    if (btn) {
      btn.classList.remove('pending-del');
      btn.disabled = true; btn.textContent = '重算中…（先补成分股数据）';
    }
    try {
      const r = await api(API.portfolioRebuild(code),
                          { method: 'POST', timeoutMs: 30 * 60 * 1000 });
      showToast(r.msg || '重算完成', true);
      select(code);
    } catch (e) {
      showToast(e.message, false);
      if (btn) { btn.disabled = false; btn.textContent = '重算组合'; }
    }
  }

  // 与「标的信息 / 个股管理」共用同一套详情布局（AssetView）
  function render(dom, code, d, c, s, k, p) {
    // ETF 只做浏览：它不在 valuation_target 里，「拉取数据 / 立即计算评分 / 成分股」
    // 都没有意义，全部不给按钮（点错只会报错）。
    if (d.ktype === 'etf') {
      AssetView.render(dom, d, k.items || [], s.items || [], (p && p.metrics) || {}, {
        weights: 'readonly',
      });
      return;
    }
    // 普通指数（还没加入标的信息）：只浏览 + 元数据。
    // 「拉取数据 / 立即计算评分」都要求它在 valuation_target 里（否则 404），
    // 所以不给按钮；要加入就点权重卡右上角的「添加为标的信息」。
    if (d.ktype === 'index' && !d.is_target) {
      AssetView.render(dom, d, k.items || [], s.items || [], (p && p.metrics) || {}, {
        weights: 'edit', weightsCode: code,
        onWeightsSaved: () => select(code),
      });
      return;
    }
    const actions = [
      { label: '拉取数据', cls: 'btn', onClick: (ev) => syncData(code, ev) },
    ];
    if (d.ktype === 'portfolio') {
      // 组合只留一个重算按钮：它会**删掉旧的组合数据后全量重算**
      // （组合K线 → 指标 → 指标分位 → 综合评分），
      // "立即计算评分"只是它的子集，重复了所以去掉。
      actions.push({ label: '重算组合', cls: 'btn ghost',
                     onClick: (ev) => rebuildPortfolio(code, ev.target) });
      actions.push({ label: '删除组合', cls: 'btn ghost',
                     onClick: () => removePortfolio(code) });
    } else {
      actions.push({ label: '立即计算评分', cls: 'btn ghost',
                     onClick: () => rescore(code) });
    }
    AssetView.render(dom, d, k.items || [], s.items || [], (p && p.metrics) || {}, {
      weights: 'edit', weightsCode: code, actions,
      onWeightsSaved: () => select(code),
      onTargetRemoved: () => { current = null; reloadList(); },
    });
    dom.append(constituentsCard(code, c));
  }

  // 把用户粘贴的一串文本解析成股票代码：
  //   支持换行/逗号/空格/分号分隔；代码直接识别，名称走一次搜索接口解析
  async function resolveTokens(raw) {
    const toks = String(raw || "").split(/[\s,，;；\n\r\t]+/)
      .map((s) => s.trim()).filter(Boolean);
    const codes = [], bad = [];
    for (const t of toks) {
      if (/^[a-z]{2}\.\d{6}$/i.test(t)) { codes.push(t.toLowerCase()); continue; }
      try {
        const d = await api(API.search(t));
        const items = d.items || [];
        const hit = items.find((x) => x.name === t)
          || items.find((x) => String(x.code).toLowerCase() === t.toLowerCase())
          || items[0];
        if (hit) codes.push(hit.code); else bad.push(t);
      } catch (e) { bad.push(t); }
    }
    return { codes: [...new Set(codes)], bad: [...new Set(bad)] };
  }

  function constituentsCard(code, c) {
    // cons-card 用来在静默刷新时把这个节点整体保留下来
    const card = el('div', { class: 'card cons-card' });
    // 数量做成可变节点：增删后原地更新，不重渲染整页
    const countEl = el('span', {}, String(c.count));
    card.append(el('h3', { class: 'card-title' }, '成分股（', countEl, '）'));
    let count = c.count;

    // ---------- 批量加入：直接粘贴一串代码/名称 ----------
    const ta = el('textarea', { class: 'input batch-input', rows: '2',
      placeholder: '批量加入：粘贴股票代码或名称，换行/逗号/空格分隔\n'
        + '例如：sh.600000, sh.601318 平安银行\n'
        + '（系统里还没有数据的会自动去 baostock 拉取）' });
    const addBtn = el('button', { class: 'btn' }, '批量加入');
    addBtn.addEventListener('click', async () => {
      const raw = ta.value.trim();
      if (!raw) { showToast('请先粘贴代码或名称', false); return; }
      addBtn.disabled = true; addBtn.textContent = '加入中…';
      try {
        const { codes, bad } = await resolveTokens(raw);
        if (!codes.length) {
          showToast('没有识别出有效代码' + (bad.length ? '：' + bad.join('、') : ''), false);
          return;
        }
        const r = await api(API.indexConstituents(code),
                            { method: 'POST', json: { codes }, timeoutMs: 30 * 60 * 1000 });
        let msg = '已加入 ' + r.added + ' 只';
        if (r.fetched && r.fetched.length) msg += '（新拉取 ' + r.fetched.length + ' 只）';
        if (r.rebuild) msg += '，组合已重算';
        const hasFail = r.fetch_failed && r.fetch_failed.length;
        if (hasFail) msg += '；拉取失败：' + r.fetch_failed.join('、');
        showToast(msg, !hasFail);
        if (bad.length) showToast('未识别：' + bad.join('、'), false);
        ta.value = '';
        // 列表立刻更新；横幅/图表后台静默重画（保留滚动位置、不整页刷新）
        await reloadConstituents();
        selectSoft(code);
      } catch (e) {
        showToast(e.message, false);
      } finally {
        addBtn.disabled = false; addBtn.textContent = '批量加入';
      }
    });

    // ---------- 成分股表格：勾选后可批量移除 ----------
    const picked = new Set();
    const rmBtn = el('button', { class: 'btn ghost mini' }, '移除选中 (0)');
    rmBtn.disabled = true;
    const confirmBar = el('div', { class: 'cons-confirm hidden' });
    let confirming = false;
    let confirmTimer = null;

    const tbl = el('table', { class: 'data cons-table' });
    tbl.append(el('colgroup', {},
      el('col', { style: 'width:34px' }), el('col', { style: 'width:16%' }),
      el('col', { style: 'width:24%' }), el('col', { style: 'width:16%' }),
      el('col', { style: 'width:16%' }), el('col', { style: 'width:14%' })));
    const all = el('input', { type: 'checkbox', title: '全选 / 全不选' });
    tbl.append(el('thead', {}, el('tr', {}, el('th', {}, all), el('th', {}, '代码'),
      el('th', {}, '名称'), el('th', {}, '最新K线'), el('th', {}, '最新评分'),
      el('th', { class: 'right' }, '操作'))));
    const tbody = el('tbody');
    tbl.append(tbody);
    let boxes = [];

    function syncSel() {
      rmBtn.textContent = '移除选中 (' + picked.size + ')';
      rmBtn.disabled = picked.size === 0;
    }

    function setCount(n) {
      count = Math.max(0, n);
      countEl.textContent = String(count);
    }

    // 渲染一行成分股
    function renderRow(r) {
      const row = el('tr', { 'data-code': r.code });
      const cb = el('input', { type: 'checkbox' });
      cb.addEventListener('change', () => {
        if (cb.checked) picked.add(r.code); else picked.delete(r.code);
        syncSel();
      });
      boxes.push(cb);
      row.append(el('td', {}, cb));
      row.append(el('td', { class: 'mono' }, r.code));
      row.append(el('td', {}, r.name));
      row.append(el('td', { class: 'mono' }, r.latest_kline || '—'));
      row.append(el('td', { class: 'mono' }, r.latest_score || '—'));
      // 行内两步确认：第一次点变红「确认移除？」，再点才真删；3 秒无操作自动还原。
      // 比浏览器 confirm 友好：不打断操作、能一眼看清是哪一行。
      let confirmTimer = null;
      const rm = el('button', { class: 'btn ghost mini' }, '移除');
      function reset() {
        clearTimeout(confirmTimer);
        rm.textContent = '移除';
        rm.classList.remove('pending-del');
        rm.disabled = false;
      }
      rm.addEventListener('click', async () => {
        if (!rm.classList.contains('pending-del')) {
          rm.classList.add('pending-del');
          rm.textContent = '确认移除？';
          confirmTimer = setTimeout(reset, 3000);
          return;
        }
        clearTimeout(confirmTimer);
        rm.disabled = true; rm.textContent = '移除中…';
        try {
          const res = await api(API.indexConstituents(code) + '/' + encodeURIComponent(r.code),
                                { method: 'DELETE', timeoutMs: 30 * 60 * 1000 });
          // 直接从列表里删掉这一行：不重渲染整页，用户立刻看到结果
          row.remove();
          picked.delete(r.code);
          all.checked = false;
          boxes = boxes.filter((b) => b !== cb);
          setCount(count - 1);
          syncSel();
          let msg = '已移除 ' + r.code;
          if (res.rebuild) {
            const sy = res.rebuild.sync || {};
            if (sy.synced && sy.synced.length) msg += '（增量补拉 ' + sy.synced.length + ' 只）';
            msg += '，组合已重算';
          }
          showToast(msg, true);
          selectSoft(code);
        } catch (e) {
          showToast(e.message, false);
          reset();
        }
      });
      row.append(el('td', { class: 'right' }, rm));
      tbody.append(row);
    }

    function fillRows(items) {
      tbody.innerHTML = '';
      boxes = [];
      picked.clear();
      all.checked = false;
      (items || []).forEach(renderRow);
      syncSel();
    }

    // 重新拉一次成分股列表并**只更新这张表**（不重渲染整页）
    async function reloadConstituents() {
      try {
        const d = await api(API.indexConstituents(code));
        setCount(d.count);
        fillRows(d.items || []);
      } catch (e) {
        showToast('刷新成分股列表失败：' + e.message, false);
      }
    }

    // 批量移除也走行内两步确认：先亮出"将移除哪些"，再点一次才提交
    rmBtn.addEventListener('click', async () => {
      const codes = [...picked];
      if (!codes.length) return;
      if (!confirming) {
        confirming = true;
        rmBtn.classList.add('pending-del');
        rmBtn.textContent = '确认移除这 ' + codes.length + ' 只？';
        confirmBar.className = 'cons-confirm';
        confirmBar.textContent = '将移除：' + codes.join('、')
          + '（组合会删除旧K线并全量重算）';
        clearTimeout(confirmTimer);
        confirmTimer = setTimeout(cancelConfirm, 6000);
        return;
      }
      clearTimeout(confirmTimer);
      const n = codes.length;
      cancelConfirm();
      rmBtn.disabled = true; rmBtn.textContent = '移除中…';
      try {
        const r = await api(API.indexConstituentsRemove(code),
                            { method: 'POST', json: { codes }, timeoutMs: 30 * 60 * 1000 });
        // 原地把这些行删掉（不整页刷新）
        codes.forEach((cd) => {
          const tr = tbody.querySelector('tr[data-code="' + cd + '"]');
          if (tr) tr.remove();
        });
        all.checked = false;
        boxes = [...tbody.querySelectorAll('input[type=checkbox]')];
        picked.clear();
        setCount(count - (r.removed || 0));
        syncSel();
        let msg = '已移除 ' + r.removed + ' 只';
        if (r.rebuild) msg += '，组合已重算';
        showToast(msg, true);
        selectSoft(code);
      } catch (e) {
        showToast(e.message, false);
      } finally {
        syncSel();
      }
    });

    function cancelConfirm() {
      confirming = false;
      rmBtn.classList.remove('pending-del');
      confirmBar.className = 'cons-confirm hidden';
      confirmBar.textContent = '';
      syncSel();
    }

    all.addEventListener('change', () => {
      boxes.forEach((cb) => {
        const cd = cb.closest('tr').getAttribute('data-code');
        cb.checked = all.checked;
        if (all.checked) picked.add(cd); else picked.delete(cd);
      });
      syncSel();
    });

    fillRows(c.items || []);
    card.append(el('div', { class: 'batch-add' }, ta,
        el('div', { class: 'btnbar' }, addBtn)),
      el('div', { class: 'btnbar cons-bar' }, confirmBar, rmBtn),
      el('div', { class: 'table-wrap' }, tbl));
    return card;
  }

  return { mount, reloadList, select, open };
})();
