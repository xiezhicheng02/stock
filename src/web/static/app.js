/* =====================================================================
   入口：导航路由 + 模块挂载 + 顶栏状态
   ===================================================================== */
'use strict';

const MODULES = {
  dashboard: Dashboard,
  assets: Assets,
  targets: Targets,
  stocks: Stocks,
  settings: Settings,
};

let activeModule = 'dashboard';
let moduleSeq = 0;              // 每次切页 +1，用于作废上一次模块的异步回调

function switchModule(name) {
  activeModule = name;
  const my = ++moduleSeq;
  document.querySelectorAll('#nav .nav-item').forEach((n) =>
    n.classList.toggle('active', n.getAttribute('data-module') === name));
  const main = $('main');
  main.innerHTML = '';
  clearBanner();
  const mod = MODULES[name];
  if (!mod || typeof mod.mount !== 'function') {
    main.innerHTML = '<div class="muted pad">模块「' + esc(name)
      + '」未加载——通常是浏览器缓存了旧脚本，请强制刷新（Ctrl+Shift+R / Cmd+Shift+R）。</div>';
    return;
  }
  // 每个模块挂到自己的容器里：切走后旧模块的"慢异步回调"只会写进这个已被摘除的
  // 容器，不会把新页面冲掉（否则从首页切到个股页时，首页慢查询回来会覆盖个股页）。
  const box = el('div', { class: 'module-root' });
  main.append(box);
  mod.mount(box).catch((e) => {
    if (my !== moduleSeq) return;            // 已经切到别的页面，不要再覆盖
    box.innerHTML = '<div class="muted pad">加载失败：' + esc(e.message) + '</div>';
  });
}

async function refreshHeader() {
  try {
    const h = await api(API.health);
    applyMeta(h.meta);                       // 用库里的配置重建区间/配色/窗口/刷新间隔
    $('app-sub').textContent = 'v' + h.version + ' · ' + (h.ok ? '服务正常' : '存在异常');
    $('foot').textContent = '最后刷新 ' + new Date().toLocaleTimeString();
    // 后端没返回 meta（多半是服务进程还是改动前的旧代码，静态文件却是新的）：
    // 状态/配色会退回出厂默认值，必须明确告诉用户，否则会被误当成"状态全变未知"
    setStickyWarn(META.loaded ? '' : ('后端未返回展示配置（/api/health 无 meta 字段）：'
      + '估值区间与配色暂按出厂默认值显示。请重启服务后刷新页面。'));
    scheduleHeaderRefresh(META.pageRefresh);
  } catch (e) {
    showBanner(e.message);
  }
}

// 顶栏刷新定时器：间隔由 WEB_PAGE_REFRESH_SEC 决定，0 = 不自动刷新。
// 每次刷新后按最新配置重排，所以改完设置不用重启服务。
let headerTimer = null;
function scheduleHeaderRefresh(sec) {
  if (headerTimer) { clearInterval(headerTimer); headerTimer = null; }
  if (sec > 0) headerTimer = setInterval(refreshHeader, sec * 1000);
}

function bind() {
  document.querySelectorAll('#nav .nav-item').forEach((n) =>
    n.addEventListener('click', () => switchModule(n.getAttribute('data-module'))));
}

bind();
// 先取回展示类配置再挂首页，否则首屏会用到兜底的图表窗口（3 年）。
refreshHeader().finally(() => switchModule('dashboard'));
