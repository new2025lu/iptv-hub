const state = {
  meta: null,
  stats: null,
  sources: [],
  groups: [],
  page: 1,
  pageSize: 30,
  total: 0,
  view: 'overview',
  channelSeq: 0,
  // v49：「仅看全部线路判死」只读清单开关。true 时 loadChannels() 改打
  // /api/dead-channels（纯查询接口），分页状态与目录共用 state.page / pageSize。
  deadOnly: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const number = (value) => new Intl.NumberFormat('zh-CN').format(Number(value || 0));
const formatTime = (value) => value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value)) : '尚无记录';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function statusInfo(value) {
  const map = {
    ok: ['正常', 'status-ok'],
    not_modified: ['无变化', 'status-ok'],
    never: ['待同步', 'status-neutral'],
    error: ['异常', 'status-bad'],
    succeeded: ['完成', 'status-ok'],
    partial: ['部分完成', 'status-warn'],
    failed: ['失败', 'status-bad'],
    running: ['运行中', 'status-warn'],
      blocked: ['跳过', 'status-neutral'],
      purged: ['已停用', 'status-bad'],
      unsupported: ['不支持', 'status-neutral'],
      // v51：不是后端的 source.status（后端只有 never/ok/not_modified/error），而是前端
      // 给「数据源已停用」造的虚拟状态，复用同一套 pill 样式，不新增 CSS。
      disabled: ['已停用', 'status-neutral'],
  };
  return map[value] || [value || '未知', 'status-neutral'];
}

function pill(value) {
  const [label, className] = statusInfo(value);
  return el('span', `status-pill ${className}`, label);
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove('show'), 2300);
}

const ADMIN_TOKEN_KEY = 'iptv_admin_token';

// 管理令牌只存在**本机浏览器**（localStorage），不写进任何请求之外的地方。
// 后端 IPTV_ADMIN_TOKEN 为空时不会校验，这里带了也无副作用。
function adminToken() {
  try { return localStorage.getItem(ADMIN_TOKEN_KEY) || ''; } catch (_) { return ''; }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = adminToken();
  // 需要鉴权的接口（如 /api/sources）靠这个头；播放列表等公开接口不看它
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) throw new Error('没有权限：请点右上角状态处填写管理令牌');
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    // v62：把状态码与原始 payload 一起抛出去 —— 调用方要能区分 409「地址已存在」这种
    // 目标状态已达成 的结果，而不是把它当失败展示（手动添加源重复时的观感就是「加不进去」）。
    let payload = null;
    try {
      payload = await response.json();
      detail = typeof payload.detail === 'string' ? payload.detail : payload.detail?.message || detail;
    } catch (_) {}
    const error = new Error(detail);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return response.json();
}

function showError(container, message) {
  container.replaceChildren(el('div', 'error-box', message));
}

// 读取失败时的**可见**提示（v42 空白页修复）。
// 实测故障：服务端启用了管理令牌（IPTV_ADMIN_TOKEN）而浏览器里没填 → /api/sources 返回 401
// → refreshAll 的 Promise.all 整批失败 → renderSources 根本没执行。错误原先只写进
// #sourceSummary，而那个容器在「概览」视图里 —— 用户停在「数据源」视图时看不到任何提示，
// 页面就是"标题在、计数还是 —、下面两张骨架卡"的整片空白。
// 这里把同一条错误也写进调用方指定的可见容器，并给一个「重试」按钮（填完令牌直接点）。
function showRetryError(container, message) {
  if (!container) return;
  const box = el('div', 'error-box');
  box.append(el('span', '', message));
  const retry = el('button', 'text-button', '重试');
  retry.addEventListener('click', () => refreshAll());
  box.append(retry);
  container.replaceChildren(box);
}

function updateMeta(meta) {
  state.meta = meta;
  const secured = meta.auth_required;
  const hasToken = !!adminToken();
  // 状态即入口：点它填写/清除管理令牌（后端 IPTV_ADMIN_TOKEN 为空时这里显示"可信局域网模式"）。
  // 不再像旧代码那样在未启用鉴权时清掉本地令牌 —— 服务器只是"不校验"，令牌留着，
  // 以后在 .env 里开了鉴权就不用重新输入。
  $('#securityState span').textContent = secured
    ? (hasToken ? '管理令牌已启用' : '需要管理令牌')
    : '可信局域网模式';
  $('#syncMiniTitle').textContent = meta.syncing ? '正在同步' : '每日自动同步';
  $('#syncMiniText').textContent = meta.syncing ? '请稍候，源站串行读取中' : `${meta.schedule} · ${meta.timezone}`;
  $('#syncDot').style.background = meta.syncing ? 'var(--warn)' : 'var(--good)';
  $('#syncButton').disabled = meta.syncing;
  $('#syncButton span').textContent = meta.syncing ? '同步中' : '立即同步';
  renderMainExcludeHint(meta);
}

// 主订阅默认排除项（MAIN_EXCLUDE_GROUP）的文案：面板说明必须与**服务端实际生效值**一致，
// 否则用户会照着页面上的字去判断订阅里有没有那一组。三个状态分别对应"已生效/配了没生效/没配"。
function renderMainExcludeHint(meta) {
  const box = $('#mainExcludeHint');
  if (!box) return;
  const effective = meta.main_exclude_group || '';
  const configured = meta.main_exclude_group_configured || '';
  if (effective) {
    box.textContent = `主订阅已默认排除「${effective}」这一组：/iptv、/playlist.m3u、/txt 与静态 data/live.m3u|txt 都不含它`
      + `（服务端 MAIN_EXCLUDE_GROUP=${effective}，地址不用改）。想临时取回全量：在地址后加 ?exclude_group= 或 ?group=。`;
  } else if (configured) {
    box.textContent = `服务端配置了 MAIN_EXCLUDE_GROUP=${configured}，但库里没有这个分组，已自动忽略`
      + `（主订阅当前仍包含全部线路）—— 请检查分组名是否写错。`;
  } else {
    box.textContent = '主订阅当前包含全部分组（服务端未配置 MAIN_EXCLUDE_GROUP）。'
      + '想默认排除某一组，在 .env 里设置 MAIN_EXCLUDE_GROUP=分组名 后重建容器。';
  }
}

// 源级失效告警：上游挂了/被反爬/换地址时，界面上必须一眼看到 —— 否则会以为"源还在，
// 只是线路变少了"。判据在后端（/api/stats 的 source_alerts）：状态 error 或超过 N 天没成功同步。
// v40：status='never' 的新源不再计入异常（它们只是还没轮到首次同步），后端另给
// pending_first_sync 计数 —— 没有真异常但有它时显示灰色普通提示，不再用红字 showError 误报。
function renderSourceAlerts(alerts) {
  const box = $('#sourceAlerts');
  if (!box) return;
  if (!alerts || !alerts.count) {
    const pending = (alerts && alerts.pending_first_sync) || 0;
    if (pending) {
      box.replaceChildren(el('div', 'notice-box',
        `⏳ 有 ${number(pending)} 个新源等待首次同步（下次同步后自动核对）。`));
    } else {
      box.replaceChildren();
    }
    return;
  }
  const items = alerts.items || [];
  const names = items.slice(0, 5).map((s) => s.name || s.slug).join('、');
  const more = alerts.count > Math.min(items.length, 5) ? ` 等 ${alerts.count} 个` : '';
  const detail = items.find((s) => s.reason)?.reason || '';
  showError(
    box,
    `⚠ ${alerts.count} 个数据源异常：${names}${more}`
    + `（判据：状态失败或超过 ${alerts.stale_days} 天没有成功同步）。`
    + `到「数据源」页看每行的状态与错误信息。`
    + (detail ? ` 例：${detail}` : ''),
  );
}

function renderStats(data) {
  renderSourceAlerts(data.source_alerts);
  $('#statChannels').textContent = number(data.channels);
  $('#statStreams').textContent = number(data.streams);
  $('#statEntries').textContent = number(data.raw_entries);
  $('#statFavorites').textContent = number(data.favorites);
  $('#statDomestic').textContent = number(data.domestic_channels);
  $('#statInternational').textContent = number(data.international_channels);
  $('#statTested').textContent = number(data.tested_streams);
  $('#statDead').textContent = number(data.dead_streams);
  $('#statFlagged').textContent = number(data.flagged_streams);
  $('#statPruned').textContent = number(data.pruned_entries);
  const purgedEl = $('#statPurged');
  if (purgedEl) purgedEl.textContent = number(data.purged_streams ?? 0);
  // 测速欠账：从没测过的 + 用旧算法测的，按当前每日预算还要多久清完
  if (data.speed_backlog) {
    const b = data.speed_backlog;
    $('#statBacklog').textContent = number(b.pending);
    // 别一律显示「约 N 天」：days_to_clear 是 ceil(欠账/日预算)，欠账 70、预算 15704 也会显示成
    // 「约 1 天清完」，看着像卡住（用户实际就报了这个）。按量级给真实口径。
    const budget = number(b.daily_budget);
    $('#statBacklogHint').textContent = !b.pending
      ? `已全部用 ${b.method} 测过`
      : (b.pending <= (b.daily_budget || 0)
          ? `下一轮即可测完（单轮预算 ${budget} 条）`
          : `约 ${b.days_to_clear} 天清完（每日 ${budget} 条）`);
  }
  $('#visualCount').textContent = number(data.streams);
  const container = $('#lastRun');
  container.replaceChildren();
  if (!data.latest_sync) {
    container.className = 'empty-state';
    container.textContent = '首次启动后将自动同步四个数据源';
    return;
  }
  const run = data.latest_sync;
  container.className = 'source-summary run-summary';
  let auto = null;
  try {
    auto = run.auto_speed_json ? JSON.parse(run.auto_speed_json) : null;
  } catch (_) {
    auto = null;
  }
  const dot = (color) => {
    const icon = el('i');
    icon.style.background = color;
    return icon;
  };
  const rows = [
    {
      name: triggerLabel(run.trigger),
      hint: formatTime(run.started_at),
      badge: pill(run.status),
      dot: run.status === 'failed' ? 'var(--bad)' : run.status === 'partial' ? 'var(--warn, var(--muted))' : 'var(--good)',
    },
    {
      name: '数据源结果',
      hint: `成功 ${run.sources_ok} · 未变更 ${run.sources_not_modified} · 失败 ${run.sources_failed}`,
      dot: run.sources_failed ? 'var(--bad)' : 'var(--good)',
    },
    {
      name: '条目变化',
      hint: `抓取 ${number(run.items_seen)} · 新增 ${number(run.items_added)} · 移除 ${number(run.items_removed)}`,
      dot: 'var(--muted)',
    },
  ];
  if (auto) {
    rows.push({
      name: '自动测速',
      hint: `${number(auto.done)} 条 · 可用 ${number(auto.ok)} · 失效 ${number(auto.failed)}${auto.blocked ? ` · 跳过 ${number(auto.blocked)}` : ''}`,
      dot: auto.failed ? 'var(--bad)' : 'var(--good)',
    });
    rows.push({
      name: '失效处理',
      hint: `标记 ${number(auto.failed)} · 剔除 ${number(auto.pruned)}${auto.revived ? ` · 复活 ${number(auto.revived)}` : ''}`,
      dot: auto.pruned ? 'var(--bad)' : 'var(--muted)',
    });
  }
  rows.forEach((row) => {
    const line = el('div', 'source-line');
    const name = el('div', 'source-name');
    const label = el('div');
    label.append(el('strong', '', row.name), el('small', '', row.hint));
    name.append(dot(row.dot), label);
    line.append(name, el('span', 'source-count', ''), row.badge || el('span', 'source-count', ''));
    container.append(line);
  });
}

function triggerLabel(trigger) {
  if (trigger === 'scheduled') return '每日计划同步';
  if (trigger === 'initial') return '首次启动同步';
  if (trigger === 'region-policy') return '区域策略变更同步';
  if (trigger === 'manual') return '手动同步';
  return trigger || '同步';
}

function renderSourceSummary(sources) {
  const container = $('#sourceSummary');
  container.className = 'source-summary';
  container.replaceChildren();
  sources.forEach((source) => {
    // v51：停用的源在总览条里也要能看出来，否则「条目数一直不变」会被当成同步故障
    const disabled = Number(source.enabled) === 0;
    const row = el('div', 'source-line');
    const name = el('div', 'source-name');
    const dot = el('i');
    dot.style.background = disabled ? 'var(--muted)'
      : source.status === 'error' ? 'var(--bad)' : source.status === 'never' ? 'var(--muted)' : 'var(--good)';
    const label = el('div'); label.append(el('strong', '', source.name), el('small', '', formatTime(source.last_success_at)));
    name.append(dot, label);
    row.append(name,
               el('span', 'source-count', disabled ? '已停用' : `${number(source.item_count)} 条`),
               disabled ? pill('disabled') : pill(source.status));
    container.append(row);
  });
}

// ── 发现候选 ─────────────────────────────────────────────────────────────
// 只展示与「采用/忽略」。采用等价于用该地址走一遍添加源流程（校验 → 放行主机 → 入库 → 排队），
// 所以界面上不需要另写一套校验提示：失败时后端的 detail 会原样显示出来。
// 空列表时用到的两个跨渲染状态：未检测区是否展开、上一轮渲染的数据指纹
let discoverPendingOpen = false;
let discoverRenderKey = '';

async function loadDiscovered() {
  const panel = $('#discoverPanel');
  const list = $('#discoverList');
  if (!panel || !list) return;
  try {
    const data = await api('/api/discovered');
    const items = data.items || [];
    // 数据没变就别重建 DOM：refreshAll() 会周期性调用这里，重建一次就闪一次，
    // 而且会把用户正在操作的行（比如刚点开的折叠区）状态冲掉。
    // v42：url 现在是**打码后**的展示串（后端不再回显带凭据的原文），只差凭据的两个候选
    // 打码后可能一模一样，所以指纹里带上 ref（不透明句柄），否则数据变了也不重建。
    const key = JSON.stringify(items.map((x) => [x.ref, x.url, x.state, x.check_status, x.check_items, x.hits]));
    if (key === discoverRenderKey && list.childElementCount) { panel.hidden = false; return; }
    discoverRenderKey = key;
    const counts = data.counts || {};
    const valid = items.filter((x) => x.state === 'new' && x.check_status === 'ok');
    const pending = items.filter((x) => x.state === 'new' && x.check_status !== 'ok');
    const adopted = items.filter((x) => x.state === 'accepted');
    $('#discoverCount').textContent = number(valid.length);
    panel.hidden = false;
    // 一行摘要：这里只该显示「要你决定的」，已忽略/已判死的根本不出现
    const summary = el('div', 'discover-summary');
    summary.append(
      el('span', valid.length ? 'discover-chip ok' : 'discover-chip', `检测有效待入库 ${valid.length}`),
      el('span', 'discover-chip', `未检测 ${pending.length}`),
      el('span', 'discover-chip', `已采用 ${adopted.length}（不再显示）`),
    );
    list.replaceChildren(summary);

    // 有效候选：短行 + 直接给「采用 / 忽略」
    if (valid.length) {
      list.append(el('h4', 'discover-group', '检测有效，可直接入库'));
      valid.forEach((item) => {
        const row = el('div', 'discover-line');
        const url = el('code', 'discover-url', shortUrl(item.url));
        url.title = item.url;
        const actions = el('div', 'discover-actions');
        const accept = el('button', 'primary-button tiny', '采用');
        // v42：操作一律带 ref（不透明句柄）而不是 url —— 列表里的 url 已打码，
        // 拿它当主键会查不到候选；真实地址由后端按 ref 还原（见 main._candidate_target）。
        accept.addEventListener('click', () => act('/api/discovered/accept', item.ref, '已采用，正在校验并排队同步'));
        const skip = el('button', 'secondary-button tiny', '忽略');
        skip.addEventListener('click', () => act('/api/discovered/ignore', item.ref, '已忽略'));
        actions.append(accept, skip);
        row.append(url, el('span', 'discover-tag ok', `${item.check_items} 条`), actions);
        list.append(row);
      });
    }

    // 未检测的收进折叠区：它们只是待办，不该铺满整页
    if (pending.length) {
      const details = el('details', 'discover-pending-wrap');
      // 展开状态要跨重渲染保留：列表每隔几秒会被 refreshAll() 整体重建，
      // 新建的 <details> 默认收起 —— 不记住状态的话，点开就被收回去了。
      details.open = discoverPendingOpen;
      details.addEventListener('toggle', () => { discoverPendingOpen = details.open; });
      details.append(el('summary', '', `未检测的候选 ${pending.length} 条（点上方「检测未验证的」批量验，无效的会自动删掉）`));
      pending.slice(0, 200).forEach((item) => {
        const row = el('div', 'discover-line');
        const url = el('code', 'discover-url', shortUrl(item.url));
        url.title = item.url;
        const actions = el('div', 'discover-actions');
        const check = el('button', 'secondary-button tiny', '检测');
        check.addEventListener('click', () => checkOne(item.ref, check));
        actions.append(check);
        row.append(url, el('span', 'discover-tag', item.hits > 1 ? `见过 ${item.hits} 次` : '新'), actions);
        details.append(row);
      });
      if (pending.length > 200) details.append(el('p', 'muted', `还有 ${pending.length - 200} 条未展示`));
      list.append(details);
    }

    if (!valid.length && !pending.length && !adopted.length) {
      list.append(el('p', 'muted', '还没有候选。每天 05:00 会自动抓一轮，也可以点「立即发现」。'));
    }
  } catch (error) {
    panel.hidden = false;
    // v42（审计⑦）：失败时要把渲染指纹清掉。原先只靠 list.childElementCount 早退，
    // 一次接口抖动（比如刚重启、令牌没带上）就会把红色错误框留在页面上，
    // 之后即使接口恢复、数据也变了，也会因为"指纹没变"而一直不重建。
    discoverRenderKey = '';
    showError($('#discoverList'), error.message || '读取候选失败');
  }
}

// 候选地址在列表里去掉协议头、中段省略：一屏能看十几条，鼠标悬停看完整地址
function shortUrl(url) {
  const text = String(url).replace(/^https?:\/\//, '');
  return text.length > 72 ? `${text.slice(0, 44)}…${text.slice(-24)}` : text;
}

// 批量入库：把所有「检测有效」的候选一次收进来（后台串行，每条间隔 1 秒），这里只轮询进度
async function adoptBatch(button) {
  button.disabled = true;
  const old = button.textContent;
  try {
    const start = await api('/api/discovered/adopt-batch?limit=20', { method: 'POST' });
    if (!start.started) {
      toast(start.message || '已有入库在进行');
      return;
    }
    button.textContent = `入库 0/${start.total}`;
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      const state = await api('/api/discovered/adopt-status');
      button.textContent = `入库 ${state.done}/${state.total}`;
      await loadDiscovered();
      if (!state.running) {
        toast(`批量入库完成：成功 ${state.ok} 条`
          + (state.already ? `，已在库中 ${state.already} 条` : '')
          + (state.failed ? `，跳过 ${state.failed} 条` : ''));
        break;
      }
    }
  } catch (error) {
    toast(error.message || '批量入库失败');
  } finally {
    button.disabled = false;
    button.textContent = old;
    await loadDiscovered();
  }
}

async function checkOne(ref, button) {
  button.disabled = true;
  const old = button.textContent;
  button.textContent = '检测中…';
  try {
    // v42：改用 ref（打码后的 url 不能当主键）
    const r = await api(`/api/discovered/check?ref=${encodeURIComponent(ref)}`, { method: 'POST' });
    toast(r.status === 'ok' ? `有效：解析出 ${r.items} 条`
      : `无效，已从候选列表删除：${r.message}`);
    await loadDiscovered();
  } catch (error) {
    toast(error.message || '检测失败');
  } finally {
    button.disabled = false;
    button.textContent = old;
  }
}

// 批量检测：后台串行跑（每条间隔 1 秒），这里只轮询进度
async function checkBatch(button) {
  button.disabled = true;
  const old = button.textContent;
  try {
    const start = await api('/api/discovered/check-batch?limit=30', { method: 'POST' });
    if (!start.started) {
      toast(start.message || '已有检测在进行');
      return;
    }
    button.textContent = `检测 0/${start.total}`;
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      const state = await api('/api/discovered/check-status');
      button.textContent = `检测 ${state.done}/${state.total}`;
      await loadDiscovered();
      if (!state.running) {
        toast(`检测完成：有效 ${state.ok} / ${state.total}`
          + (state.removed ? `，已清掉无效 ${state.removed} 条` : ''));
        break;
      }
    }
  } catch (error) {
    toast(error.message || '批量检测失败');
  } finally {
    button.disabled = false;
    button.textContent = old;
    await loadDiscovered();
  }
}

async function act(path, ref, okText) {
  try {
    // v42：采用/忽略改用 ref（不透明句柄）—— 列表里的 url 已经是打码后的展示串
    const result = await api(`${path}?ref=${encodeURIComponent(ref)}`, { method: 'POST' });
    if (result && result.sync_queued === false && result.run_id === '') {
      toast('同步队列已满，请稍后再试');
    } else {
      toast(okText);
    }
    await loadDiscovered();
  } catch (error) {
    toast(error.message || '操作失败');
  }
}

function setupDiscover() {
  const button = $('#discoverRefresh');
  const batch = $('#discoverCheck');
  if (batch) batch.addEventListener('click', () => checkBatch(batch));
  const adopt = $('#discoverAdopt');
  if (adopt) adopt.addEventListener('click', () => adoptBatch(adopt));
  if (!button) return;
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = '发现中…';
    try {
      const data = await api('/api/discovered/refresh', { method: 'POST' });
      // v42（审计 P1-3 单飞）：已有一轮在跑时后端直接回 started=false，
      // 这时不该显示"发现完成：候选 0 条"这种误导性提示。
      if (data.started === false) {
        toast(data.message || '已有一轮发现正在跑');
        return;
      }
      const stats = data.stats || {};
      toast(`发现完成：候选 ${stats.found || 0} 条（新增 ${stats.new || 0}），失败站点 ${stats.failed || 0} 个`);
      await loadDiscovered();
    } catch (error) {
      toast(error.message || '发现失败');
    } finally {
      button.disabled = false;
      button.textContent = '立即发现';
    }
  });
}

// 数据源分组（v40）：source_group 非空的源（如「发现源」）各自单独一块，
// 空分组的内置源与手动添加源归入默认区块。这里只按字符串分区，不写死具体分组名 ——
// 后端 DISCOVERED_SOURCE_GROUP 的值换了，前端不用跟着改。
const DEFAULT_SOURCE_GROUP = '内置与手动添加';

// 单张数据源卡片。抽成函数是为了让「默认区块」与各分组区块共用同一段渲染逻辑，
// 避免两处布局各写一遍后走样（同步此源 / 移除等操作也只在这一处维护）。
function sourceCard(source) {
  const card = el('article', 'panel source-card');
  // 卡片带上 slug：地址重复时前端要滚动定位到「已有的那条源」（v62）
  card.dataset.slug = source.slug;
  // v51：enabled=0 的源**仍然出现在列表里**（源行保留是这一机制的设计前提），但不再抓取、
  // 不再测速 —— 卡片必须一眼看出「它是被主动停用的」而不是「它挂了」。
  const disabled = Number(source.enabled) === 0;
  const top = el('div', 'source-card-top');
  const title = el('div'); title.append(el('p', 'eyebrow', source.format.toUpperCase()), el('h3', '', source.name));
  top.append(title, disabled ? pill('disabled') : pill(source.status));
  const url = el('div', 'source-url', source.url);
  url.title = source.url;
  const grid = el('div', 'source-card-stats');
  // 停用后条目数不再更新，显示旧数字会让人以为它还在同步 —— 显示「—」
  [[disabled ? '—' : number(source.item_count), '当前条目'], [source.last_http_status || '—', 'HTTP'], [source.etag ? '已记录' : '无', 'ETag']].forEach(([value, label]) => {
    const item = el('div'); item.append(el('span', '', label), el('strong', '', String(value))); grid.append(item);
  });
  const foot = el('div', 'source-card-foot');
  const note = disabled
    ? '已停用：不再抓取、不再测速（条目与历史保留，可随时启用）'
    : (source.error_message || `上次成功：${formatTime(source.last_success_at)}`);
  const actions = el('div', 'source-card-actions');
  const button = el('button', 'secondary-button', '同步此源');
  button.disabled = disabled;   // 后端也会跳过并打日志，这里直接从交互上挡掉
  button.addEventListener('click', () => startSync(source.slug));
  actions.append(button);
  // 停用/启用对内置源同样开放（与「移除」不同：内置源删了会被种子重建，所以只给停用）
  const toggle = el('button', disabled ? 'secondary-button' : 'text-button danger-text',
                    disabled ? '启用此源' : '停用此源');
  toggle.addEventListener('click', () => toggleSource(source, disabled ? 1 : 0));
  actions.append(toggle);
  // 只给手动添加的源提供删除入口：内置源每次启动都会被 SOURCE_SEEDS 重新写入，删了也会回来
  // 手动添加(manual::)与粘贴导入(inline::)都是用户自己建的，都该能删；内置源不能删
  if (/^(manual|inline)::/.test(String(source.slug))) {
    const del = el('button', 'text-button danger-text', '移除');
    del.addEventListener('click', () => removeSource(source));
    actions.append(del);
  }
  foot.append(el('small', '', note), actions);
  card.append(top, url, grid, foot);
  return card;
}

function renderSources(sources) {
  // v62：把源列表缓存到 state —— 手动添加源提交前要在本地查重（避免同一个地址重复提交、
  // 重复尝试抓取）。refreshAll 与 refreshSources 都走这个函数，所以这里是唯一入口。
  state.sources = Array.isArray(sources) ? sources : [];
  renderSourceSummary(sources);
  const countBadge = $('#sourceCount');
  if (countBadge) countBadge.textContent = sources.length;
  const select = $('#sourceFilter');
  const selected = select.value;
  select.replaceChildren(new Option('全部来源', ''));
  sources.forEach((source) => select.add(new Option(source.name, source.slug)));
  select.value = selected;
  const container = $('#sourceCards');
  container.replaceChildren();
  // 按 source_group 分区：分组内保持后端给的 priority 顺序，组间顺序固定
  // （默认区块 → 其余分组按名称），这样 refreshAll 反复渲染时区块不会跳位置。
  const grouped = new Map();
  sources.forEach((source) => {
    const name = String(source.source_group || '').trim() || DEFAULT_SOURCE_GROUP;
    if (!grouped.has(name)) grouped.set(name, []);
    grouped.get(name).push(source);
  });
  const names = Array.from(grouped.keys()).sort((a, b) => {
    if (a === DEFAULT_SOURCE_GROUP) return -1;
    if (b === DEFAULT_SOURCE_GROUP) return 1;
    return a.localeCompare(b, 'zh-Hans-CN');
  });
  names.forEach((name) => {
    const list = grouped.get(name);
    const section = el('section', 'source-group-block');
    const head = el('div', 'source-group-head');
    head.append(el('h3', 'source-group-title', name), el('span', 'source-group-count', `${list.length} 个源`));
    const grid = el('div', 'source-card-grid');
    list.forEach((source) => grid.append(sourceCard(source)));
    section.append(head, grid);
    container.append(section);
  });
}

// v51：停用 / 启用数据源。停用是**可逆**的：后端只置 sources.enabled，条目与测速历史一行不删，
// 所以这里用「停用」而不是删除 —— 内置源也允许停用（它删不掉，删了会被种子重建）。
async function toggleSource(source, value) {
  const verb = value ? '启用' : '停用';
  if (!value) {
    const ok = window.confirm(`停用数据源「${source.name}」？\n\n停用后不再抓取、不再测速；条目与测速历史全部保留，随时可以启用回来。\n（与「移除」不同：移除会连带删除该源已抓取的线路。）`);
    if (!ok) return;
  }
  try {
    const result = await api(`/api/sources/${encodeURIComponent(source.slug)}/enable?value=${value}`, { method: 'POST' });
    toast(result.changed ? `已${verb}「${source.name}」` : `「${source.name}」已经是${verb}状态`);
    await refreshSources();
  } catch (error) {
    toast(error.message || `${verb}失败`);
  }
}

async function removeSource(source) {
  const ok = window.confirm(`移除数据源「${source.name}」？\n\n该源已抓取的线路会连同健康记录一起删除，并从订阅中移除。\n（已放行的主机名保留，重新添加同一地址即可恢复。）`);
  if (!ok) return;
  try {
    const result = await api(`/api/sources/${encodeURIComponent(source.slug)}`, { method: 'DELETE' });
    toast(`已移除「${source.name}」（删除 ${number(result.removed_entries)} 条线路）`);
    await refreshSources();
  } catch (error) {
    toast(error.message || '移除失败');
  }
}

async function refreshSources() {
  const sources = await api('/api/sources');
  renderSources(sources);
  return sources;
}

function setMsg(selector, text, kind = 'info') {
  const box = $(selector);
  if (!box) return;
  box.textContent = text;
  box.classList.remove('msg-info', 'msg-error', 'msg-success');
  box.classList.add(`msg-${kind}`);
  box.hidden = !text;
}

function setAddSourceMsg(text, kind = 'info') {
  setMsg('#addSourceMsg', text, kind);
}

async function submitPasteSource(event) {
  event.preventDefault();
  const content = $('#pasteSourceContent').value.trim();
  const name = $('#pasteSourceName').value.trim();
  if (!content || !name) return;
  const submit = $('#pasteSourceSubmit');
  submit.disabled = true;
  submit.textContent = '导入中…';
  setMsg('#pasteSourceMsg', '正在解析清单…', 'info');
  try {
    const query = new URLSearchParams({ name });
    const result = await api(`/api/sources/inline?${query}`, { method: 'POST', body: content });
    setMsg('#pasteSourceMsg',
      `${result.updated ? '已更新' : '已导入'}「${result.name}」：解析出 ${number(result.entries)} 条；`
      + (result.sync_queued ? '已排队同步，完成后自动测速并并入总源。' : '同步队列已满，请稍后在卡片上点「同步此源」。'),
      'success');
    $('#pasteSourceForm').reset();
    await refreshSources();
  } catch (error) {
    setMsg('#pasteSourceMsg', `导入失败：${error.message || '未知错误'}`, 'error');
  } finally {
    submit.disabled = false;
    submit.textContent = '导入清单';
  }
}

// v62：本地查重 —— 用已加载的源列表判断这个地址是不是已经添加过。
// 与服务端同一口径：trim 后**完全相等**才认为重复（服务端 sources.url 的 UNIQUE 就是按
// 原始串比；这里放宽会出现「本地说重复、服务端却当新源」的分歧，比漏判更糟）。
// 注意：/api/sources 返回的 url 是**脱敏后**的展示串（长路径段与查询串会被打码），
// 所以本地这步是"能省一次往返就省"的优化；权威判定仍是服务端 409（下一段兜底）。
function findSourceByUrl(url) {
  const key = String(url || '').trim();
  if (!key) return null;
  return (state.sources || []).find((s) => String(s.url || '').trim() === key) || null;
}

// 地址重复时把视线带到「已有的那条源」卡片上（v62）：原先只有一句「这个地址已经添加过了」，
// 用户在几十个源里找不到它。滚动到卡片 + 短暂高亮，是这条 409 最需要的「指路」。
function revealSourceCard(slug) {
  const key = String(slug || '');
  const sel = (window.CSS && CSS.escape) ? CSS.escape(key) : key;
  const card = document.querySelector(`.source-card[data-slug="${sel}"]`);
  if (!card) return false;
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.classList.add('source-card-flash');
  setTimeout(() => card.classList.remove('source-card-flash'), 2600);
  return true;
}

async function submitAddSource(event) {
  event.preventDefault();
  const url = $('#addSourceUrl').value.trim();
  const name = $('#addSourceName').value.trim();
  const format = $('#addSourceFormat').value;
  if (!url) return;
  // v62：提交前先本地查重 —— 同一个地址第二次提交时**不发请求、也不再尝试抓取**，
  // 直接提示「已经在数据源里了」并定位到它的卡片（用户要求：避免重复尝试）。
  const known = findSourceByUrl(url);
  if (known) {
    setAddSourceMsg(
      `这个地址已经在数据源里了：「${known.name}」（状态 ${known.status || '未知'}，`
      + `${number(known.item_count)} 条），已为你定位到它的卡片；本次未再重复尝试添加。`,
      'info',
    );
    $('#addSourceForm').reset();
    revealSourceCard(known.slug);
    return;
  }
  const submit = $('#addSourceSubmit');
  submit.disabled = true;
  submit.textContent = '检测中…';
  setAddSourceMsg('正在抓取源并校验有效性（不写任何数据）…', 'info');
  try {
    const query = new URLSearchParams({ url, name, format });
    const result = await api(`/api/sources?${query}`, { method: 'POST' });
    const detail = result.format === 'tvbox'
      ? `是 TVBox 配置，含 ${number(result.sub_sources)} 个直播列表`
      : `解析出 ${number(result.entries)} 条线路`;
    setAddSourceMsg(
      `已添加「${result.name}」：${detail}；已放行主机 ${result.hosts_approved.join('、')}。`
      + (result.sync_queued
        ? '已排队同步，完成后会自动测速并并入总源，可在「同步记录」查看进度。'
        : '同步队列已满，请稍后在卡片上点「同步此源」。'),
      'success',
    );
    $('#addSourceForm').reset();
    await refreshSources();
  } catch (error) {
    // v62：409 = 这个地址已经在数据源里，用户想要的结果已经达成 —— 不该说「添加失败」。
    // 改成中性提示并带上已有源的名字/状态/条数，再滚动定位到它的卡片（revealSourceCard）。
    const existing = error.status === 409 ? (error.payload && error.payload.detail) || null : null;
    if (existing && existing.slug) {
      const state = existing.enabled ? `状态 ${existing.status || '未知'}` : '当前是「已停用」状态';
      setAddSourceMsg(
        `这个地址已经在数据源里了：「${existing.name}」（${state}，${number(existing.item_count)} 条），`
        + '已为你定位到它的卡片。'
        + (existing.enabled ? '' : '需要的话在卡片上点「启用此源」。'),
        'info',
      );
      $('#addSourceForm').reset();
      await refreshSources();
      revealSourceCard(existing.slug);
    } else {
      setAddSourceMsg(`添加失败：${error.message || '未知错误'}`, 'error');
    }
  } finally {
    submit.disabled = false;
    submit.textContent = '添加并检测';
  }
}

function renderGroups(groups) {
  const select = $('#groupFilter');
  const selected = select.value;
  select.replaceChildren(new Option('全部分组', ''));
  groups.forEach((group) => select.add(new Option(`${group.name} (${number(group.count)})`, group.name)));
  select.value = selected;
}

function channelLogo(item) {
  return el('span', 'channel-logo', (item.name || '?').trim().slice(0, 1).toUpperCase());
}

function renderChannels(data) {
  state.total = data.total;
  // 页码收敛：剔除/清理后总数变少，旧页码可能已越界（返回空列表却显示「第 196 / 194 页」）
  const lastPage = Math.max(1, Math.ceil(data.total / state.pageSize));
  if (state.page > lastPage) {
    state.page = lastPage;
    loadChannels();
    return;
  }
  const container = $('#channelRows');
  container.replaceChildren();
  $('#channelCount').textContent = `共 ${number(data.total)} 个频道`;
  $('#pageInfo').textContent = `第 ${state.page} / ${Math.max(1, Math.ceil(data.total / state.pageSize))} 页`;
  $('#prevPage').disabled = state.page <= 1;
  $('#nextPage').disabled = state.page * state.pageSize >= data.total;
  if (!data.items.length) {
    const empty = el('div', 'empty-state', '没有符合当前筛选条件的频道');
    container.append(empty);
    return;
  }
  data.items.forEach((item) => {
    const row = el('div', 'channel-row');
    row.tabIndex = 0;
    row.dataset.key = item.channel_key;
    const title = el('div', 'channel-title');
    const text = el('div'); text.append(el('strong', '', item.name), el('small', '', item.tvg_id || item.country?.toUpperCase() || '未提供频道 ID'));
    title.append(channelLogo(item), text);
    const group = el('span', 'cell-muted', item.group_title || '未分类');
    const tags = el('div', 'source-tags');
    String(item.sources || item.source_slug).split(',').slice(0, 3).forEach((value) => tags.append(el('span', 'source-tag', value)));
    const count = el('span', 'cell-muted', String(item.stream_count));
    const speed = el('span', 'cell-muted', item.best_speed ? `${Number(item.best_speed).toFixed(0)} KB/s` : '—');
    if (item.best_speed) speed.classList.add('speed-cell-ok');
    const actions = el('div', 'row-actions');
    const regionTag = el('span', `source-tag ${item.region === 'domestic' ? 'region-domestic' : 'region-international'}`, item.region === 'domestic' ? '国内' : '国外');
    actions.append(regionTag);
    const favorite = el('button', `row-button ${item.favorite ? 'active' : ''}`, item.favorite ? '已收藏' : '收藏');
    favorite.addEventListener('click', (event) => { event.stopPropagation(); toggleFavorite(item); });
    actions.append(favorite);
    row.append(title, group, tags, count, speed, actions);
    row.addEventListener('click', () => openChannel(item.channel_key));
    // keydown 会冒泡：焦点在行内按钮（收藏）上按 Enter 时，浏览器随后还会对按钮合成
    // click —— 不挡的话「打开抽屉」和「切换收藏」会同时触发。只响应行自身的 Enter。
    row.addEventListener('keydown', (event) => { if (event.key === 'Enter' && event.target === row) openChannel(item.channel_key); });
    container.append(row);
  });
}

// ---------------------------------------------------------------------------
// v49：只读「全部线路判死」清单（频道目录页的第二个数据源）
// ---------------------------------------------------------------------------
// 判据由服务端单独实现（见 app/main.py 的 _ALIVE_ENTRY_SQL）：该频道所有活跃线路都
// 判死（error/unsupported 或没有健康行）；只要有一条 ok **或未测**就不算 —— 没测 ≠ 坏。
// 前端只负责展示：**行上没有任何破坏性操作**（没有删除、没有清理按钮），
// 只给「复制 channel_key」与「详情（复用 openChannel）」两个入口。

// 表头随模式切换：目录看「流数/速度」，判死清单看「判死线路/最近检查」。
// 两种模式共用同一套 .channel-row 六列栅格，所以只换文案、不新增样式。
function renderChannelHead(deadOnly) {
  const head = $('#channelHead');
  if (!head) return;
  const labels = deadOnly
    ? ['频道（channel_key 可复制）', '分组', '来源', '判死 / 线路', '最近检查', '操作']
    : ['频道', '分组', '来源', '流数', '速度', '操作'];
  head.replaceChildren(...labels.map((text) => el('span', '', text)));
}

// 判死清单只吃 page / page_size（接口没有别的筛选参数）。与既有的 params() 完全分开，
// 目录查询的参数拼装一个字符都不动。
function deadParams() {
  return new URLSearchParams({ page: state.page, page_size: state.pageSize });
}

// 判死清单接口不支持 q/source/group/region/sort/favorite —— 勾选期间把无关控件禁用，
// 免得用户以为「搜了却没结果」。取消勾选原样恢复，已填的筛选值本身不动。
function applyDeadMode() {
  ['searchInput', 'sourceFilter', 'groupFilter', 'regionFilter', 'favoriteFilter', 'sortFilter']
    .forEach((id) => { const node = $(`#${id}`); if (node) node.disabled = state.deadOnly; });
}

function renderDeadChannels(data) {
  state.total = data.count;
  // 页码收敛：与目录同样的处理，避免停在越界页看到空列表
  const lastPage = Math.max(1, Math.ceil(data.count / state.pageSize));
  if (state.page > lastPage) {
    state.page = lastPage;
    loadChannels();
    return;
  }
  const container = $('#channelRows');
  container.replaceChildren();
  $('#channelCount').textContent = `全部线路判死 ${number(data.count)} 个频道（只读清单）`;
  $('#pageInfo').textContent = `第 ${state.page} / ${lastPage} 页`;
  $('#prevPage').disabled = state.page <= 1;
  $('#nextPage').disabled = state.page * state.pageSize >= data.count;
  if (!data.items.length) {
    container.append(el('div', 'empty-state', '没有「全部线路判死」的频道'));
    return;
  }
  data.items.forEach((item) => {
    const row = el('div', 'channel-row');
    row.tabIndex = 0;
    row.dataset.key = item.channel_key;
    const title = el('div', 'channel-title');
    const text = el('div');
    // channel_key 直接显示在副标题里（形如 id:1983年春晚 / name:bd230c…），配合「复制键」便于排查
    text.append(el('strong', '', item.name),
                el('small', '', `${item.channel_key} · ${item.region === 'domestic' ? '国内' : '国外'}`));
    title.append(channelLogo(item), text);
    const group = el('span', 'cell-muted', item.group_title || '未分类');
    const tags = el('div', 'source-tags');
    const names = Array.isArray(item.sources) ? item.sources : [];
    names.forEach((name) => tags.append(el('span', 'source-tag', name)));
    if (!names.length) tags.append(el('span', 'source-tag', '来源未知'));
    // 接口只返回前 3 个源名，多出来的用 +N 提示（source_count 是真实总数）
    if (item.source_count > names.length) {
      tags.append(el('span', 'source-tag', `+${item.source_count - names.length}`));
    }
    const lines = el('span', 'cell-muted', `${number(item.dead_streams)} / ${number(item.stream_count)}`);
    const checked = el('span', 'cell-muted', item.last_checked_at ? formatTime(item.last_checked_at) : '—');
    const actions = el('div', 'row-actions');
    const copyKey = el('button', 'row-button', '复制键');
    copyKey.title = '复制 channel_key（排查用）';
    copyKey.addEventListener('click', (event) => {
      event.stopPropagation();
      copyText(item.channel_key, `已复制频道键：${item.channel_key}`);
    });
    const detail = el('button', 'row-button', '详情');
    detail.title = '打开频道详情（复用目录的抽屉）';
    detail.addEventListener('click', (event) => { event.stopPropagation(); openChannel(item.channel_key); });
    actions.append(copyKey, detail);
    row.append(title, group, tags, lines, checked, actions);
    row.addEventListener('click', () => openChannel(item.channel_key));
    row.addEventListener('keydown', (event) => { if (event.key === 'Enter' && event.target === row) openChannel(item.channel_key); });
    container.append(row);
  });
}

function params() {
  const query = new URLSearchParams({ page: state.page, page_size: state.pageSize });
  const q = $('#searchInput').value.trim();
  const source = $('#sourceFilter').value;
  const group = $('#groupFilter').value;
  const sort = $('#sortFilter')?.value;
  if (q) query.set('q', q);
  if (source) query.set('source', source);
  if (group) query.set('group', group);
  if (sort) query.set('sort', sort);
  const region = $('#regionFilter')?.value;
  if (region) query.set('region', region);
  if ($('#favoriteFilter').checked) query.set('favorite', 'true');
  return query;
}

async function loadChannels() {
  // 请求序号：筛选/翻页会并发发请求，慢响应后到会把新条件的结果覆盖掉
  const seq = (state.channelSeq = (state.channelSeq || 0) + 1);
  const container = $('#channelRows');
  container.innerHTML = '<div class="table-loading"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>';
  try {
    // v49：判死清单走另一条只读接口；其余情况与改动前逐字节一致
    const data = state.deadOnly
      ? await api(`/api/dead-channels?${deadParams()}`)
      : await api(`/api/channels?${params()}`);
    if (seq !== state.channelSeq) return;   // 只渲染最新一次请求的结果
    if (state.deadOnly) renderDeadChannels(data);
    else renderChannels(data);
  } catch (error) {
    if (seq === state.channelSeq) showError(container, error.message);
  }
}

async function openChannel(key) {
  const drawer = $('#channelDrawer');
  const backdrop = $('#drawerBackdrop');
  $('#drawerContent').replaceChildren(el('div', 'skeleton'));
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  backdrop.hidden = false;
  try {
    const data = await api(`/api/channels/${encodeURIComponent(key)}`);
    $('#drawerTitle').textContent = data.channel.name;
    const container = $('#drawerContent');
    container.replaceChildren();
    const summary = el('div', 'stream-card');
    [['分组', data.channel.group_title],
     ['频道 ID', data.channel.tvg_id || '未提供'],
     ['国家/地区代码', data.channel.country?.toUpperCase() || '未提供'],
     ['线路', `主用 ${data.primary_count} · 备选 ${data.backup_count} · 失效 ${data.dead_streams}${data.blocked_streams ? ` · 跳过 ${data.blocked_streams}` : ''}${data.purged_streams ? ` · 已停用 ${data.purged_streams}` : ''} / 共 ${data.total_streams} 条`]].forEach(([label, value]) => {
      const line = el('div', 'detail-line'); line.append(el('span', '', label), el('strong', '', value)); summary.append(line);
    });
    container.append(summary);
    data.streams.forEach((stream, index) => {
      const card = el('div', 'stream-card');
      const top = el('div', 'stream-card-top');
      const role = { primary: '主用', backup: '备选', dead: '失效', blocked: '跳过', purged: '已停用' }[stream.role] || '';
      // 线路地址来自第三方播放列表，脏数据（空串/非法写法）会让 new URL 抛异常，
      // 一旦抛出整个频道抽屉渲染中断 —— 解析失败就回退为不显示协议标签
      let protocolTag = '';
      try { protocolTag = new URL(stream.url).protocol.replace(':', '').toUpperCase(); } catch { /* 脏地址，忽略 */ }
      top.append(el('strong', '', `线路 ${index + 1} · ${stream.source_name}`),
                 el('span', `source-tag role-${stream.role || 'backup'}`, role),
                 el('span', 'source-tag', stream.quality || protocolTag));
      if (stream.health_status === 'ok' && stream.speed_kbps != null) {
        top.append(el('span', 'source-tag speed-tag', `${Number(stream.speed_kbps).toFixed(0)} KB/s · ${stream.latency_ms ?? '—'} ms`));
      } else if (stream.health_status) {
        const label = { ok: '正常', error: '不可用', unsupported: '不支持测速', blocked: '跳过', purged: '已停用' }[stream.health_status] || stream.health_status;
        const fails = Number(stream.fail_count || 0);
        const maxFail = state.meta?.maintenance?.prune_max_fail || 3;
        top.append(el('span', 'source-tag', fails > 0 ? `${label} · 已标记 ${fails}/${maxFail}` : label));
      }
      const code = el('code', '', stream.url);
      const copy = el('button', 'secondary-button', '复制地址');
      copy.addEventListener('click', () => copyText(stream.url, '已复制线路地址'));
      card.append(top, code, copy);
      container.append(card);
    });
  } catch (error) {
    showError($('#drawerContent'), error.message);
  }
}

function closeDrawer() {
  $('#channelDrawer').classList.remove('open');
  $('#channelDrawer').setAttribute('aria-hidden', 'true');
  $('#drawerBackdrop').hidden = true;
}

async function startSync(source = '') {
  try {
    await api(`/api/sync${source ? `?source=${encodeURIComponent(source)}` : ''}`, { method: 'POST' });
    toast(source ? '单源同步已进入队列' : '全量同步已进入队列');
    await refreshMeta();
    pollSync();
  } catch (error) {
    toast(error.message || '同步失败');
  }
}

async function startSpeedTest() {
  const raw = $('#speedScope')?.value || 'favorites';
  const isRegion = raw === 'domestic' || raw === 'international';
  const scope = isRegion ? 'all' : raw;
  const region = isRegion ? raw : '';
  const limit = $('#speedLimit')?.value || '300';
  const button = $('#speedButton');
  const query = new URLSearchParams({ scope, limit });
  if (region) query.set('region', region);
  try {
    await api(`/api/speed-test?${query}`, { method: 'POST' });
    toast(region ? `测速任务已启动（${region === 'domestic' ? '国内' : '国外'}频道）` : '测速任务已启动');
    button.disabled = true;
    pollSpeedTest();
  } catch (error) {
    toast(error.message || '测速启动失败');
  }
}

async function purgeRevivedStreams() {
  const button = $('#purgeButton');
  button.disabled = true;
  try {
    const preview = await api('/api/prune/purge-revived', { method: 'POST' });
    if (!preview.candidates) {
      const info = await api('/api/purged-streams');
      toast(`没有「复活后仍失效」的线路（已停用 ${number(info.count)} 条）`);
      return;
    }
    const ok = window.confirm(
      `将永久停用 ${preview.candidates} 条「复活过、复活后仍然失效」的线路。\n\n`
      + `它们会被标记为 purged：不再参与测速、不再进订阅，同步再抓到同一地址也不会复活。\n`
      + `（可撤销：POST /api/purged-streams/restore）\n\n确定执行？`
    );
    if (!ok) { toast('已取消'); return; }
    const result = await api('/api/prune/purge-revived?apply=true', { method: 'POST' });
    toast(`已永久停用 ${number(result.purged)} 条线路（停用条目 ${number(result.deactivated_entries)} 条）`);
    await Promise.all([loadStats(), loadChannels()]);
  } catch (error) {
    toast(error.message || '季度清理失败');
  } finally {
    button.disabled = false;
  }
}

async function pruneStreams() {
  const button = $('#pruneButton');
  button.disabled = true;
  try {
    const preview = await api('/api/prune', { method: 'POST' });
    if (!preview.would_remove_entries) {
      toast('没有需要剔除的失效线路（可先跑一次测速）');
      return;
    }
    const ok = window.confirm(
      `将剔除 ${preview.would_remove_entries} 条失效线路，涉及 ${preview.affected_channels} 个频道。\n保留 ${preview.kept_healthy_entries} 条已验证可用的线路。\n\n确定执行？`
    );
    if (!ok) {
      toast('已取消');
      return;
    }
    const result = await api('/api/prune?apply=true', { method: 'POST' });
    toast(`已剔除 ${result.removed_entries} 条失效线路`);
    await Promise.all([loadStats(), loadChannels()]);
  } catch (error) {
    toast(error.message || '剔除失败');
  } finally {
    button.disabled = false;
  }
}

async function pollSpeedTest() {
  const button = $('#speedButton');
  const label = button.querySelector('span');
  let progress;
  try {
    progress = await api('/api/speed-test');
  } catch (_) {
    // 一次网络抖动不该让整条轮询链断掉（服务端其实还在跑），否则按钮恢复可点、
    // 进度丢失，再点还会撞 409。这里退避重试，直到服务端说跑完。
    state.speedPollFails = (state.speedPollFails || 0) + 1;
    if (state.speedPollFails <= 5) {
      label.textContent = '测速中…（连接抖动，重试）';
      setTimeout(pollSpeedTest, 2000);
      return;
    }
    state.speedPollFails = 0;
    button.disabled = false;
    label.textContent = '线路测速';
    return;
  }
  state.speedPollFails = 0;
  if (!progress.running) {
    button.disabled = false;
    label.textContent = '线路测速';
    toast(`测速完成：正常 ${progress.ok} 条，异常 ${progress.failed} 条`);
    await Promise.all([loadStats(), loadChannels()]);
    return;
  }
  // 页面加载时测速可能已在跑（init 恢复轮询也走这里）：必须同步禁用按钮，
  // 否则 label 显示「测速中 x/y」按钮却还能点，点了只会撞后端 409。
  button.disabled = true;
  label.textContent = `测速中 ${progress.done}/${progress.total}`;
  setTimeout(pollSpeedTest, 1500);
}

async function toggleFavorite(item) {
  try {
    await api(`/api/favorites/${encodeURIComponent(item.channel_key)}`, { method: item.favorite ? 'DELETE' : 'PUT' });
    toast(item.favorite ? '已取消收藏' : '已加入收藏');
    await Promise.all([loadChannels(), loadStats()]);
  } catch (error) {
    toast(error.message || '操作失败');
  }
}

function renderHistory(rows) {
  const container = $('#historyList');
  container.replaceChildren();
  if (!rows.length) {
    container.append(el('div', 'empty-state', '尚无同步记录'));
    return;
  }
  rows.forEach((run) => {
    const item = el('div', 'history-item');
    const date = el('div'); date.append(el('time', '', formatTime(run.started_at)), el('strong', '', triggerLabel(run.trigger)));
    const source = el('div'); source.append(el('small', '', '同步范围'), el('strong', '', run.source_filter || '全部数据源'));
    const delta = el('div', 'history-delta');
    const added = el('span'); added.append('新增 ', el('b', '', number(run.items_added)));
    const removed = el('span'); removed.append('移除 ', el('b', '', number(run.items_removed)));
    const failed = el('span'); failed.append('异常 ', el('b', '', number(run.sources_failed)));
    delta.append(added, removed, failed);
    item.append(date, source, delta, pill(run.status));
    container.append(item);
  });
}

async function loadStats() {
  try { renderStats(await api('/api/stats')); } catch (error) { showError($('#lastRun'), error.message); }
}

async function refreshMeta() {
  try { updateMeta(await api('/api/meta')); } catch (error) { $('#syncMiniText').textContent = error.message; }
}

async function refreshAll() {
  try {
    const [meta, statsData, sourcesData, groupsData, historyData] = await Promise.all([
      api('/api/meta'), api('/api/stats'), api('/api/sources'), api('/api/groups'), api('/api/sync-runs?limit=30')
    ]);
    updateMeta(meta);
    renderStats(statsData);
    renderSources(sourcesData);
  await loadDiscovered();
    renderGroups(groupsData);
    renderHistory(historyData);
  } catch (error) {
    const message = `无法读取服务：${error.message}`;
    // 令牌相关的失败 api() 已经写明"点右上角状态处填写管理令牌"，别重复追加
    const hint = adminToken() || /管理令牌/.test(message) ? '' : '（点右上角状态处填写管理令牌）';
    // 概览页那块摘要照旧（它就在概览视图里，看得见）
    showError($('#sourceSummary'), message);
    // v42：数据源视图此前完全没有失败提示 —— 只在"还没渲染出任何卡片"时接管，
    // 免得一次偶发失败把已经渲染好的 46 张卡片清空。
    const cards = $('#sourceCards');
    if (cards && !cards.querySelector('.source-card')) {
      showRetryError(cards, message + hint);
    }
    toast(message + hint);
  }
  await loadChannels();
}

function pollSync() {
  clearInterval(pollSync.timer);
  pollSync.timer = setInterval(async () => {
    await refreshMeta();
    if (!state.meta?.syncing) {
      clearInterval(pollSync.timer);
      await refreshAll();
      toast('同步任务已结束');
    }
  }, 2500);
}

function copyWithFallback(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  // HTTP 内网 / WebView 等不安全上下文回退
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:absolute;left:-9999px;top:-9999px;opacity:0;';
  document.body.append(ta);
  ta.select();
  try {
    const ok = document.execCommand('copy');
    ta.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error('execCommand copy failed'));
  } catch (err) {
    ta.remove();
    return Promise.reject(err);
  }
}

async function copyText(text, message) {
  try {
    await copyWithFallback(text);
    toast(message);
  } catch (_) {
    toast('浏览器未允许剪贴板访问：请手动选中地址复制');
  }
}

function navigate(view) {
  state.view = view;
  const titles = { overview: '运行概览', channels: '频道目录', sources: '数据源', history: '同步记录' };
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === view));
  $$('.view').forEach((item) => item.classList.toggle('active', item.id === `view-${view}`));
  $('#pageTitle').textContent = titles[view];
  $('#mobileMenu').closest('.main')?.classList.remove('nav-open');
  $('.sidebar').classList.remove('open');
  // pushState 而不是 replaceState：否则浏览器前进/后退永远切不了视图（历史里没有条目，
  // 按后退直接离开页面）。上面的 hashchange 监听会把 popstate 带来的 hash 变化接住。
  // 同视图重复进入不写历史，避免连点导航把历史塞满。
  if (location.hash.slice(1) !== view) history.pushState(null, '', `#${view}`);
}

function setupAdminTokenEntry() {
  // 右上角那个状态块就是令牌入口：填一次存本机浏览器，之后所有需要鉴权的接口自动带上。
  // 用 prompt 而不是新增页面元素，是为了不动物流布局、把改动面压到最小。
  const box = $('#securityState');
  box.style.cursor = 'pointer';
  box.title = '点击填写 / 清除管理令牌（只保存在本机浏览器，用于调用需要鉴权的接口）';
  box.addEventListener('click', async () => {
    const current = adminToken();
    const input = window.prompt(
      '管理令牌（对应服务端 .env 的 IPTV_ADMIN_TOKEN）。\n留空并确定 = 清除本机保存的令牌。',
      current,
    );
    if (input === null) return;
    const value = input.trim();
    try {
      if (value) localStorage.setItem(ADMIN_TOKEN_KEY, value);
      else localStorage.removeItem(ADMIN_TOKEN_KEY);
    } catch (_) {
      toast('浏览器不允许保存本地数据，令牌只在本次输入生效');
      return;
    }
    toast(value ? '管理令牌已保存到本机浏览器' : '已清除本机保存的管理令牌');
    await refreshAll();     // 数据源等需要鉴权的接口重新拉一遍
  });
}

function setupEvents() {
  $$('.nav-item').forEach((item) => item.addEventListener('click', () => navigate(item.dataset.view)));
  $$('[data-go]').forEach((item) => item.addEventListener('click', () => navigate(item.dataset.go)));
  $('#clearHistory').addEventListener('click', async () => {
    if (!confirm('确定清空全部同步历史记录？（正在进行的任务不受影响）')) return;
    try {
      await api('/api/sync-runs/clear', { method: 'POST' });
      toast('同步记录已清空');
      await refreshAll();
    } catch (error) {
      toast(error.message || '清理失败');
    }
  });
  // 复制订阅地址：toast 里回显完整地址，用户能立刻核对复制到的是哪一个出口
  //（v43 起页面上有多个订阅地址，只说"已复制"容易分不清复制了哪个）
  $$('[data-copy-path]').forEach((item) => item.addEventListener('click', () => {
    const url = `${location.origin}${item.dataset.copyPath}`;
    copyText(url, `已复制订阅地址：${url}`);
  }));
  $('#syncButton').addEventListener('click', () => startSync());
  $('#addSourceForm')?.addEventListener('submit', submitAddSource);
  $('#pasteSourceForm')?.addEventListener('submit', submitPasteSource);
  $('#speedButton')?.addEventListener('click', () => startSpeedTest());
  $('#pruneButton')?.addEventListener('click', () => pruneStreams());
  $('#purgeButton')?.addEventListener('click', () => purgeRevivedStreams());
  $('#mobileMenu').addEventListener('click', () => $('.sidebar').classList.toggle('open'));
  $('#themeButton').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('iptv_theme', next);
  });
  let searchTimer;
  $('#searchInput').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.page = 1; loadChannels(); }, 260); });
  ['sourceFilter', 'groupFilter', 'regionFilter', 'favoriteFilter', 'sortFilter'].forEach((id) => $(`#${id}`).addEventListener('change', () => { state.page = 1; loadChannels(); }));
  // v49：切到/切回「仅看全部线路判死」只读清单。换数据源必须回到第 1 页，
  // 否则会带着目录的页码落进越界页（后端 count 更小）。
  $('#deadOnlyFilter')?.addEventListener('change', () => {
    state.deadOnly = $('#deadOnlyFilter').checked;
    state.page = 1;
    applyDeadMode();
    renderChannelHead(state.deadOnly);
    loadChannels();
  });
  $('#prevPage').addEventListener('click', () => { if (state.page > 1) { state.page -= 1; loadChannels(); } });
  $('#nextPage').addEventListener('click', () => { if (state.page * state.pageSize < state.total) { state.page += 1; loadChannels(); } });
  // 之前只有 navigate() 里的 replaceState，没有监听 hash 变化 —— 点 Logo（<a href="#overview">）、
  // 手改地址栏、浏览器前进后退都不会切视图，URL 与显示长期不一致。
  window.addEventListener('hashchange', () => {
    const view = location.hash.slice(1);
    if (['overview', 'channels', 'sources', 'history'].includes(view) && view !== state.view) navigate(view);
  });
  $('#drawerClose').addEventListener('click', closeDrawer);
  $('#drawerBackdrop').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDrawer(); });
}

function initTheme() {
  const saved = localStorage.getItem('iptv_theme');
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = saved || (dark ? 'dark' : 'light');
}

function renderSubscriptionAddresses() {
  // 订阅地址要能直接粘进播放器，所以显示成 `http://<当前 Host>/discovered.m3u`：
  // Host 只有浏览器知道（局域网 IP / 主机名 / 反代域名都可能），静态 HTML 里写不出来，
  // 页面加载时按 location.origin 填一次即可（v43）。
  $$('[data-abs-path]').forEach((item) => {
    item.textContent = `${location.origin}${item.dataset.absPath}`;
  });
}

initTheme();
setupEvents();
renderSubscriptionAddresses();
setupAdminTokenEntry();
setupDiscover();
// 不能用 `in {}` 做白名单：in 会走原型链，#toString / #constructor 都能通过校验，
// 页面标题会被设成原生函数源码、且没有任何视图被激活（白屏）
navigate(['overview', 'channels', 'sources', 'history'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview');
refreshAll().then(() => {
  if (state.meta?.syncing) pollSync();
  if (state.meta?.speed_testing) pollSpeedTest();
});
