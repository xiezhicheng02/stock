/* =====================================================================
   模块：首页（总览）—— 上证指数行情 + 系统运行状态统计
   上证指数随每日定时任务增量拉取，这里只读展示，不提供手动拉取按钮。
   ===================================================================== */
'use strict';

const Dashboard = (function () {
  let root = null;
  let pollTimer = null;      // 任务轮询定时器（手动触发后跟进执行状态）
  let pollJobId = null;      // 本次触发的任务 id（它跑完就整体刷新）
  let pollStart = 0;         // 本次轮询的开始时刻（ms）
  const POLL_MIN = 2000;     // 至少轮询这么久（等调度器把任务取走）
  const POLL_MAX = 60000;    // 别的任务还在跑时，最多再等这么久
  let isTradingToday;

  async function mount(el) {
    root = el;
    root.innerHTML = '<div class="muted pad">加载中…</div>';
    await load();
  }

  async function load() {
    const d = await api(API.dashboard);
    if (!root || !root.isConnected) return;   // 期间已切到别的页面，丢弃
    isTradingToday = d.is_trading_today;
    root.innerHTML = '';
    // 上证指数整行 → 数据统计整行 → 邮件与告警整行 → 定时任务整行
    root.append(
      marketCard(d.market),
      statsCard(d),
      mailCard(d.mail, d.alert),
      el('div', { id: 'dash-jobs' }, jobsCard(d)),
    );
    if (d.market && d.market.kline && d.market.kline.length) {
      charts.kline($('mk-kline'), d.market.kline);
    }
  }

  function _chgChip(label, v) {
    const cls = v == null ? '' : (v >= 0 ? 'up' : 'down');
    const txt = v == null ? '—' : (v >= 0 ? '+' : '') + fmt(v, 2) + '%';
    return el('span', { class: 'trend-chip ' + cls }, label + ' ' + txt);
  }

  // 涨跌方向 → 徽标/配色修饰类（A股习惯：红涨绿跌）
  function dirCls(v) { return v == null ? '' : (v >= 0 ? 'up' : 'down'); }

  // 行情横幅里的一格：标签 / 值 / 相对昨收的涨跌（第三层恒定存在，保证等高）
  function quoteCell(label, value, diff) {
    const sub = diff == null ? '' : (diff >= 0 ? '+' : '') + fmt(diff, 2);
    return el('div', { class: 'quote' + (diff == null ? '' : ' ' + dirCls(diff)) },
      el('span', { class: 'q-k' }, label),
      el('span', { class: 'q-v' }, value),
      el('span', { class: 'q-s' }, sub || '—'));
  }

  function marketCard(m) {
    const card = el('div', { class: 'card market-card' });
    if (!m || !m.latest || m.latest.close == null) {
      card.append(el('h3', { class: 'card-title' }, '📈 ' + ((m && m.name) || '上证指数')),
        el('div', { class: 'muted pad' }, '暂无上证指数K线，等待每日定时任务自动拉取。'));
      return card;
    }
    const lt = m.latest;
    const chg = lt.pct_chg;
    const chgCls = dirCls(chg);
    const pre = lt.preclose != null ? lt.preclose : null;
    const chgAbs = (pre != null && lt.close != null) ? lt.close - pre : null;
    // 相对昨收的差值（用于今开/最高/最低的红绿着色）
    const d = (v) => (pre == null || v == null) ? null : v - pre;

    // ---- 行情横幅：涨红跌绿浅底，与标的详情页头部风格统一 ----
    const banner = el('div', { class: 'mk-banner ' + chgCls });
    const left = el('div', { class: 'mk-banner-l' });
    left.append(el('div', { class: 'mk-title' },
      el('span', { class: 'dot ' + chgCls }),
      m.name,
      el('span', { class: 'auto-tag' }, '每日自动增量')));
    left.append(el('div', { class: 'mk-sub' },
      m.code + (lt.date ? ' · ' + lt.date : '')
      + ' · 共 ' + (m.total_rows || m.rows || 0) + ' 个交易日'));
    // 5/20/60 日涨跌紧跟在价格下方，和"今天"形成一组
    const trend = el('div', { class: 'trend-row' },
      _chgChip('5日', m.chg_5d), _chgChip('20日', m.chg_20d), _chgChip('60日', m.chg_60d));
    left.append(trend);

    const right = el('div', { class: 'mk-banner-r' });
    right.append(el('div', { class: 'mk-price ' + chgCls }, fmt(lt.close, 2)));
    const deltaRow = el('div', { class: 'mk-delta' });
    deltaRow.append(el('span', { class: 'mk-chg-pill ' + chgCls },
      (chg >= 0 ? '▲ +' : '▼ ') + fmt(chgAbs == null ? 0 : chgAbs, 2)));
    deltaRow.append(el('span', { class: 'mk-chg-pct ' + chgCls },
      chg == null ? '—' : (chg >= 0 ? '+' : '') + fmt(chg, 2) + '%'));
    right.append(deltaRow);
    if (pre != null) right.append(el('div', { class: 'mk-pre' }, '昨收 ' + fmt(pre, 2)));
    banner.append(left, right);
    card.append(banner);

    // ---- 行情快照：6 格统一网格，每格带"相对昨收"的第三层 ----
    const quotes = el('div', { class: 'quote-row' });
    quotes.append(
      quoteCell('今开', fmt(lt.open, 2), d(lt.open)),
      quoteCell('最高', fmt(lt.high, 2), d(lt.high)),
      quoteCell('最低', fmt(lt.low, 2), d(lt.low)),
      quoteCell('昨收', fmt(pre, 2), null),
      quoteCell('成交额', fmtBig(lt.amount), null),
      quoteCell('成交量', fmtBig(lt.volume), null));
    card.append(quotes);

    if (m.kline && m.kline.length) {
      const chartDiv = el('div', { class: 'chart', id: 'mk-kline', style: 'height:360px' });
      let range = '1y';
      const renderChart = async () => {
        let data = m.kline;
        if (range === '1m') data = m.kline.slice(-22);
        else if (range === 'all') {
          try {
            const full = await api(API.kline(m.code, 30));
            data = full.items || m.kline;
          } catch (e) { data = m.kline; showToast(e.message, false); }
        }
        charts.kline(chartDiv, data);
      };
      const rbar = el('div', { class: 'chart-head' },
        el('span', { class: 'card-subtitle' }, '指数走势'));
      const chips = el('div', { class: 'range-bar' });
      [['1m', '近1月'], ['1y', '近1年'], ['all', '全部']].forEach(([k, label]) => {
        chips.append(el('button', { class: 'chip' + (range === k ? ' active' : ''),
          onclick: (ev) => { range = k;
            chips.querySelectorAll('.chip').forEach((c) => c.classList.remove('active'));
            ev.target.classList.add('active');
            renderChart(); } }, label));
      });
      rbar.append(chips);
      card.append(el('div', { class: 'chart-block' }, rbar, chartDiv));
    }
    return card;
  }

  function statsCard(d) {
    const s = d.stats || {};
    const card = el('div', { class: 'card' });
    card.append(el('h3', { class: 'card-title' }, '📊 数据统计'));
    const g = el('div', { class: 'stat-grid' });
    const items = [
      ['已拉取个股', s.stocks], ['指数', s.indexes], ['组合', s.portfolios],
      ['有K线标的', s.kline_codes], ['有评分标的', s.score_codes],
    ];
    items.forEach(([k, v]) => g.append(el('div', { class: 'stat-cell' },
      el('div', { class: 'stat-val' }, v == null ? '—' : v),
      el('div', { class: 'stat-label' }, k))));
    const cal = s.trade_calendar || {};
    card.append(g);
    card.append(el('div', { class: 'muted small pad-top' },
      '交易日历：' + (cal.start || '—') + ' ~ ' + (cal.end || '—')));
    if (s.last_run_at) {
      card.append(el('div', { class: 'muted small' }, '最近跑批：' + s.last_run_at));
    }
    return card;
  }

  const KIND_LABEL = { daily: '日常', alert: '告警', manual: '手动' };

  // 跳到"指数与组合"页并打开该标的详情
  function gotoAsset(code) {
    if (typeof Targets === 'undefined' || !Targets.open) return;
    Targets.open(code);      // 先登记目标，随后的 mount 会直接定位过去
    const nav = document.querySelector('#nav .nav-item[data-module="targets"]');
    if (nav) nav.click();
  }

  // 新窗口打开某封邮件的正文（后端直接返回自包含 HTML）
  function openMail(id, bodyKey) {
    const url = bodyKey ? API.mailBody(bodyKey) : API.mailHtml(id);
    const w = window.open(url, '_blank');
    if (!w) showToast('浏览器拦截了新窗口，请允许弹出窗口', false);
  }

  // 邮件列表的一行。state: pending / sent / failed
  function mailRow(o) {
    const btn = el('button', {
      class: 'btn ghost mini',
      onclick: (ev) => { ev.stopPropagation(); openMail(o.id, o.body_key); },
    }, o.has_body ? '查看正文' : '查看说明');
    const state = o.state === 'pending'
      ? el('span', { class: 'tag run' }, '待发送')
      : o.ok ? el('span', { class: 'tag ok' }, '发送成功')
        : el('span', { class: 'tag err' }, '发送失败');
    const isAlert = o.kind === 'alert' || o.is_alert;
    // 标题 + 摘要两行：摘要里就是「沪深300 便宜 48分；…」这种告警明细，
    // 所以告警信息直接在列表里就能看到，不需要单独一块告警区域
    const titleCell = el('td', { class: 'mail-subject' },
      el('div', { class: 'ms-title', title: o.subject || '' }, o.subject || '—'),
      o.summary ? el('div', { class: 'ms-sum', title: o.summary }, o.summary) : null);
    return el('tr', {
      class: (o.state === 'pending' ? 'row-pending' : '') + (isAlert ? ' row-alert' : ''),
      title: o.state === 'pending'
        ? '已生成正文、还没发送；点击预览正文'
        : (o.has_body ? '点击查看这封邮件的正文' : '这封邮件没有正文快照，点击查看说明'),
      onclick: () => openMail(o.id, o.body_key),
    },
      el('td', { class: 'mono' }, (o.sent_at || '').slice(0, 16) || '—'),
      el('td', {}, el('span', { class: 'tag ' + (isAlert ? 'warn' : 'off') },
        isAlert ? '🔔 告警' : (KIND_LABEL[o.kind] || o.kind || '—'))),
      titleCell,
      el('td', {}, state),
      el('td', { class: 'muted small mail-recv', title: o.receivers || '' },
        o.receivers || '—'),
      el('td', { class: 'right' }, btn));
  }

  function mailCard(m, a) {
    const card = el('div', { class: 'card' });
    const t = m.today || {};
    const hist = m.history || [];
    const pend = m.pending;
    const lb = t.last_build;
    const alerts = (a && a.alert_targets) || [];

    // 标题右侧只放状态徽章：今天这一封的推送进度 + 当前告警标的数。
    // 告警不再是单独一块区域——它就是邮件的一种状态，在列表里标出来即可。
    const tags = el('span', { class: 'ct-tags' });
    tags.append(pend
      ? el('span', { class: 'tag run' }, '已构建 · 待发送')
      : !t.built
        ? el('span', { class: 'tag off' }, '今天未构建')
        : el('span', { class: 'tag ok' }, '今天已发 ' + t.sent_count + '/' + t.max_sends + ' 次'));
    tags.append(alerts.length
      ? el('span', { class: 'tag warn', title: alerts.map((x) => x.name).join('、') },
          '🔔 ' + alerts.length + ' 个告警标的')
      : el('span', { class: 'tag off' }, '无告警'));
    card.append(el('h3', { class: 'card-title' }, '✉️ 邮件记录 ', tags));

    if (!pend && !hist.length) {
      if (lb && lb.msg) {
        card.append(el('div', { class: 'mail-note ' + (lb.ok ? 'muted' : 'warn') },
          '最近一次构建（' + (lb.at || '').slice(5, 16) + '）：' + lb.msg));
      }
      card.append(el('div', { class: 'empty-note' },
        '还没有邮件记录。点下方「生成邮件正文」即可先生成一份（不发送），'
        + '生成后就会出现在这里，点一下就能预览正文。'
        + '（手动点「执行」会忽略"非交易日跳过"，休市日也能生成。）'));
      return card;
    }

    const tbl = el('table', { class: 'data clickable mail-table' });
    tbl.append(el('thead', {}, el('tr', {},
      el('th', {}, '时间'), el('th', {}, '类型'), el('th', {}, '标题 / 内容摘要'),
      el('th', {}, '状态'), el('th', {}, '收件人'), el('th', { class: 'right' }, ''))));
    const tb = el('tbody');
    if (pend) {
      tb.append(mailRow({
        id: null, body_key: pend.body_key, sent_at: pend.built_at,
        kind: pend.is_alert ? 'alert' : 'daily', is_alert: pend.is_alert,
        subject: pend.subject, summary: pend.summary, receivers: pend.receivers,
        state: 'pending', ok: null, has_body: pend.has_body,
      }));
    }
    hist.forEach((h) => tb.append(mailRow(Object.assign({ state: 'sent' }, h))));
    tbl.append(tb);
    card.append(el('div', { class: 'table-wrap scroll' }, tbl));
    return card;
  }

  function groupedJobs(jobs) {
    // 把 N 个「通知·告警重发」合并成一行，其余保持一行
    const order = { data_sync: 0, compute_indicators: 1, notify_build: 2, notify_send: 3, notify_resend: 4 };
    const out = [];
    const resends = [];
    (jobs || []).forEach((j) => {
      if (j.id.startsWith('notify_resend_')) resends.push(j);
      else out.push(j);
    });
    if (resends.length) {
      const times = resends.map((j) => ((j.trigger.match(/\d{2}:\d{2}/) || [])[0]))
        .filter(Boolean);
      const next = resends.map((j) => j.next_run).filter(Boolean).sort()[0] || null;
      const last = resends.map((j) => j.last).filter(Boolean)
        .sort((a, b) => (b.at || '').localeCompare(a.at || ''))[0] || null;
      const running = resends.some((j) => j.running);
      const runningSince = running
        ? resends.filter((j) => j.running).map((j) => j.running_since)
            .filter(Boolean).sort()[0] || null
        : null;
      out.push({
        id: 'notify_resend', name: '通知·告警重发', exec: resends[0].id,
        trigger: '每周 mon-fri ' + (times.join(' / ') || '—'),
        next_run: next, last: last, running: running, running_since: runningSince,
      });
    }
    out.sort((a, b) => (order[a.id] ?? 99) - (order[b.id] ?? 99));
    return out;
  }

  function elapsed(since) {
    if (!since) return '';
    const ms = Date.now() - new Date(since.replace(' ', 'T')).getTime();
    if (!Number.isFinite(ms) || ms < 0) return '';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return sec + ' 秒';
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return m + ' 分 ' + s + ' 秒';
    const h = Math.floor(m / 60);
    return h + ' 时 ' + (m % 60) + ' 分';
  }

  function jobName(jobs, id) {
    const j = (jobs || []).find((x) => x.id === id);
    if (j) return j.name;
    return id.startsWith('notify_resend') ? '通知·告警重发' : id;
  }

  // 单行任务的状态列：运行中 → 徽章 + 已运行时长；空闲 → 最近结果
  function jobStatusCell(j) {
    const cell = el('td', { class: 'job-status' });
    if (j.running) {
      cell.append(
        el('span', { class: 'tag running' }, '运行中'),
        el('span', { class: 'muted small' }, ' ' + (elapsed(j.running_since) || '')));
      return cell;
    }
    const last = j.last;
    if (!last) { cell.append(el('span', { class: 'muted' }, '—')); return cell; }
    const head = el('div', { class: 'js-head' },
      el('span', { class: last.ok ? 'ok-txt' : 'err-txt' }, last.ok ? '成功' : '失败'),
      el('span', { class: 'muted small' }, ' ' + (last.at || '').slice(5, 16)));
    cell.append(head);
    // 摘要可能很长（构建任务会带上补算信息），限两行 + 悬停看全文，
    // 免得把这一列撑开、把别的列挤变形
    if (last.msg) cell.append(el('div', { class: 'js-msg muted small', title: last.msg },
      last.msg));
    return cell;
  }

  function recentRunsCard(runs, jobs) {
    const box = el('div', { class: 'recent-runs' });
    box.append(el('div', { class: 'card-subtitle' }, '最近执行记录'));
    if (!runs || !runs.length) {
      box.append(el('div', { class: 'muted small' }, '暂无执行记录'));
      return box;
    }
    const tbl = el('table', { class: 'data runs-table' });
    tbl.append(el('colgroup', {},
      el('col', { style: 'width:14%' }), el('col', { style: 'width:18%' }),
      el('col', { style: 'width:10%' }), el('col', { style: 'width:58%' })));
    tbl.append(el('thead', {}, el('tr', {}, el('th', {}, '时间'), el('th', {}, '任务'),
      el('th', {}, '结果'), el('th', {}, '摘要'))));
    const tb = el('tbody');
    runs.forEach((r) => tb.append(el('tr', {},
      el('td', { class: 'mono' }, (r.at || '').slice(5, 16)),
      el('td', {}, jobName(jobs, r.job_id)),
      el('td', {}, r.ok ? el('span', { class: 'ok-txt' }, '成功')
        : el('span', { class: 'err-txt' }, '失败')),
      el('td', { class: 'muted small run-msg', title: r.msg || '' }, r.msg || ''))));
    tbl.append(tb);
    box.append(el('div', { class: 'table-wrap' }, tbl));
    return box;
  }

  function jobsCard(d) {
    const card = el('div', { class: 'card' });
    const s = d.scheduler || {};
    const tag = !s.enabled ? el('span', { class: 'tag off' }, '已关闭')
      : s.running ? el('span', { class: 'tag ok' }, '运行中')
      : el('span', { class: 'tag warn' }, '未运行');
    card.append(el('h3', { class: 'card-title' }, '⏰ 定时任务', tag));
    const tbl = el('table', { class: 'data jobs-table' });
    // 固定列宽：执行状态列给足空间，否则它下面可能很长的执行摘要
    // 会把"任务 / 触发时间 / 下次运行"挤压变形
    tbl.append(el('colgroup', {},
      el('col', { style: 'width:17%' }), el('col', { style: 'width:18%' }),
      el('col', { style: 'width:17%' }), el('col', { style: 'width:36%' }),
      el('col', { style: 'width:12%' })));
    tbl.append(el('thead', {}, el('tr', {}, el('th', {}, '任务'), el('th', {}, '触发时间'),
      el('th', {}, '下次运行'), el('th', {}, '执行状态'), el('th', { class: 'right' }, '操作'))));
    const tb = el('tbody');
    const canRun = !!(s.enabled && s.running);   // 调度器没跑时按钮点不动
    groupedJobs(d.jobs).forEach((j) => {
      const run = el('button', {
        class: 'btn ghost mini',
        title: canRun ? '立即执行一次' : '调度器未运行，无法手动触发',
        onclick: (ev) => triggerJob(j.exec || j.id, ev.target),
      }, j.running ? '运行中…' : '执行');
      if (j.running || !canRun) run.disabled = true;
      tb.append(el('tr', { class: j.running ? 'row-running' : '' },
        el('td', {}, el('span', { class: 'job-name' }, j.name)),
        el('td', { class: 'mono muted' }, j.trigger),
        el('td', { class: 'mono' }, j.next_run || '—'),
        jobStatusCell(j),
        el('td', { class: 'right' }, run)));
    });
    tbl.append(tb);
    card.append(el('div', { class: 'table-wrap' }, tbl));
    if (isTradingToday !== undefined) {
      card.append(el('div', { class: 'muted small pad-top' },
        '今天是否交易日：' + (isTradingToday === true ? '是'
          : isTradingToday === false ? '否（休市日定时构建会跳过，手动执行可强制）' : '未知')));
    }
    card.append(recentRunsCard(d.recent_runs, d.jobs));

    return card;
  }

  // ---- 手动触发 + 执行状态轮询（"触发之后接着执行"）----
  async function triggerJob(id, btn) {
    if (btn) { btn.disabled = true; btn.textContent = '触发中…'; }
    let r;
    try {
      r = await api(API.jobRun(id), { method: 'POST' });
    } catch (e) {
      showToast(e.message, false);
      if (btn) { btn.disabled = false; btn.textContent = '执行'; }
      return;
    }
    showToast(r.msg, r.ok);
    if (btn) btn.textContent = '运行中…';
    pollJobId = id;                     // 只盯这一个任务，它一停就整体刷新
    pollStart = Date.now();
    startPoll();
  }

  function startPoll() {
    if (pollTimer) return;
    pollJobs();
  }

  async function pollJobs() {
    let j;
    try { j = await api(API.scheduler); }
    catch (e) { stopPoll(); return; }
    const wrap = $('dash-jobs');
    if (wrap) wrap.replaceChildren(jobsCard({
      scheduler: j.scheduler, jobs: j.jobs, recent_runs: j.recent }));
    const jobs = j.jobs || [];
    // 分组展示的"告警重发"在 jobs_info 里仍是 notify_resend_N，所以 exec 也要匹配
    const mine = pollJobId
      ? jobs.find((x) => x.id === pollJobId || x.exec === pollJobId) : null;
    const mineRunning = !!(mine && mine.running);
    const anyRunning = jobs.some((x) => x.running);
    const waited = Date.now() - pollStart;
    // 刚触发时先至少等 POLL_MIN，避免任务还没被调度器取走就判定"已结束"；
    // 之后：目标任务一停就刷新；别的任务还在跑则最多再等 POLL_MAX
    const keep = waited < POLL_MIN || mineRunning
      || (anyRunning && waited < POLL_MAX);
    if (keep) {
      pollTimer = setTimeout(pollJobs, 1500);
    } else {
      stopPoll();
      pollJobId = null;
      load();   // 全部完成：整体刷新一次（邮件/告警/统计跟着更新）
    }
  }

  function stopPoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  return { mount, reloadList: load };
})();
