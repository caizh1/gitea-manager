/* PR 进度组件：仅使用当前 Gitea 登录会话，所有展示均采用文本节点。 */
(() => {
  'use strict';
  const mark = '<!-- ai-review:summary:v1 -->';
  const labels = { queued: '已排队', running: '正在审查', publishing: '正在发布', recovering: '正在核对发布结果',
    succeeded: '审查完成', failed: '审查失败', superseded: '版本已过期', publish_unknown: '发布结果待核对',
    cancelling: '取消中', cancelled: '已取消', interrupted: '工作进程中断，等待恢复', stopped: '已停止审查', outdated: '新提交等待审查' };
  const sha = value => typeof value === 'string' && /^(?:[a-f0-9]{40})?$/.test(value);
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  const timeValue = value => value === null || (Number.isFinite(value) && value >= 0);
  function parse(row, identity, bots) {
    if (!row || !bots.includes(row.user?.id) || typeof row.body !== 'string' || row.body.length > 32000 || !row.body.startsWith(mark)) return null;
    const matches = [...row.body.matchAll(/<!-- ai-review:progress:v1:([A-Za-z0-9_=-]+) -->/g)];
    if (matches.length !== 1) return null;
    try {
      if (identity.path && new URL(row.pull_request_url || row.html_url, 'http://gitea.invalid').pathname !== identity.path) return null;
      const bytes = Uint8Array.from(atob(matches[0][1].replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
      const p = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
      if (p.version !== 1 || p.repository_id !== identity.id || p.repository !== identity.name || p.number !== identity.number ||
          !Object.hasOwn(labels, p.status) || !/^[0-9a-f]{32}$/.test(p.job) || !sha(p.head) || !sha(p.current_head) ||
          ![p.generation, p.job_generation, p.sequence].every(integer) || p.job_generation > p.generation ||
          !(integer(p.units_completed) || (p.native_session === true && p.units_completed === null && p.units_total === null)) ||
          !(p.units_total === null || (integer(p.units_total) && integer(p.units_completed) && p.units_completed <= p.units_total)) ||
          ![p.received_at, p.started_at, p.finished_at, p.lease_until, p.checked_at].every(timeValue) ||
          typeof p.enabled !== 'boolean' || typeof p.detail !== 'string' || p.detail.length > 1000 ||
          typeof p.error !== 'string' || p.error.length > 1000 || typeof p.stage !== 'string' || p.stage.length > 80 ||
          !(p.findings === null || integer(p.findings)) || !(p.review_id === null || integer(p.review_id)) ||
          typeof p.review_fragment !== 'string' || !/^(?:issuecomment-[1-9][0-9]*)?$/.test(p.review_fragment)) return null;
      if (p.coverage !== null && (!p.coverage || typeof p.coverage.complete !== 'boolean' ||
          !integer(p.coverage.files_reviewed) || !integer(p.coverage.files_total) || p.coverage.files_reviewed > p.coverage.files_total)) return null;
      // 老版本没有待办字段；新字段必须属于当前 PR 且计数与开关一致。
      if (p.attention != null) {
        const attention = p.attention;
        if (typeof attention !== 'object' || Array.isArray(attention) ||
            typeof attention.required !== 'boolean' || !integer(attention.pending_count) ||
            attention.required !== (attention.pending_count > 0) || attention.number !== identity.number ||
            typeof attention.reason !== 'string' || attention.reason.length > 1000 ||
            ('since' in attention && !timeValue(attention.since)) ||
            ('repository' in attention && (!integer(attention.repository) || attention.repository === 0))) return null;
      }
      return p;
    } catch { return null; }
  }
  function extractFragment(raw) {
    // Go 模板会移除静态 HTML 注释，因此以受管理模板元素本身作为边界。
    const startMark = '<template id="ai-review-page-data" ';
    const endMark = '</template>';
    const start = raw.lastIndexOf(startMark), end = raw.indexOf(endMark, start);
    if (start < 0 || end < start || end - start > 256000) throw new Error('未收到可信页面状态');
    return raw.slice(start, end + endMark.length);
  }
  function effectiveStatus(p, pr, now = Date.now() / 1000) {
    if (!p.enabled || pr.state !== 'open' || pr.merged) return 'stopped';
    if ((p.head && p.head !== pr.head.sha) || (!p.head && p.status === 'succeeded')) return 'outdated';
    if (['running', 'publishing', 'recovering'].includes(p.status) && p.lease_until && p.lease_until < now) {
      return p.status === 'running' ? 'interrupted' : 'publish_unknown';
    }
    return p.status;
  }
  if (typeof module === 'object' && module.exports) { module.exports = { parse, effectiveStatus, extractFragment }; return; }
  const script = document.currentScript;
  const base = script?.dataset.reviewBase || '';
  const bots = (script?.dataset.reviewBots || '').split(',').map(Number).filter(n => Number.isSafeInteger(n) && n > 0);
  if (!bots.length || (base && (!base.startsWith('/') || base.startsWith('//') || /[\\?#]/.test(base)))) return;
  const prefix = base.replace(/\/$/, '');
  if (!location.pathname.startsWith(prefix + '/')) return;
  const match = location.pathname.slice(prefix.length).match(/^\/([^/]+)\/([^/]+)\/pulls\/([1-9][0-9]*)(?:\/(?:files|commits))?\/?$/);
  if (!match) return;
  let owner, repo;
  try { owner = decodeURIComponent(match[1]); repo = decodeURIComponent(match[2]); } catch { return; }
  const number = Number(match[3]);
  const heading = document.querySelector('.repository.view.issue.pull .issue-title-header');
  if (!heading || document.getElementById('ai-review-progress')) return;
  const prPath = `${prefix}/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/pulls/${number}`;
  const card = document.createElement('section'); card.id = 'ai-review-progress'; card.className = 'ai-review-progress';
  card.setAttribute('aria-label', 'AI Code Review 审查进度');
  const top = document.createElement('div'); top.className = 'air-heading';
  const title = document.createElement('strong'); title.textContent = 'AI Code Review';
  const status = document.createElement('span'); status.className = 'air-status'; status.setAttribute('role', 'status');
  status.textContent = '正在读取状态'; top.append(title, status);
  const detail = document.createElement('p'), metrics = document.createElement('div'), bottom = document.createElement('div');
  metrics.className = 'air-metrics'; bottom.className = 'air-footer';
  const link = document.createElement('a'); link.textContent = '查看审查结果'; link.hidden = true;
  const notice = document.createElement('span'); notice.className = 'air-notice'; bottom.append(link, notice);
  card.append(top, detail, metrics, bottom); heading.after(card);
  let timer, controller, disposed = false, cycle = 0, lastPayload = null, lastPR = null;
  const fmt = value => value ? new Date(value * 1000).toLocaleString('zh-CN') : '尚无';
  function textMetric(label, value) { const span = document.createElement('span'); span.textContent = `${label}：${value}`; metrics.append(span); }
  function render(p, pr) {
    const state = effectiveStatus(p, pr); card.dataset.status = state; status.textContent = labels[state];
    detail.textContent = state === 'outdated' ? '当前 PR 已有新提交，下方审查结果属于历史版本。' : state === 'interrupted' ? labels[state] : p.detail;
    metrics.replaceChildren(); textMetric('审查版本', p.head ? p.head.slice(0, 12) : '等待读取');
    textMetric('接收', fmt(p.received_at));
    if (p.started_at) textMetric('开始', fmt(p.started_at));
    if (p.finished_at) textMetric('结束', fmt(p.finished_at));
    if (p.started_at) textMetric('已用时', `${Math.max(0, Math.floor((p.finished_at || Date.now()/1000) - p.started_at))} 秒`);
    if (p.units_total !== null) textMetric('完成分析单元', `${p.units_completed}/${p.units_total}`);
    if (p.findings !== null) textMetric('发现问题', `${p.findings} 项`);
    if (p.coverage) textMetric('变更文件', `${p.coverage.files_reviewed}/${p.coverage.files_total}${p.coverage.complete ? '' : '（部分覆盖）'}`);
    if (p.error) textMetric('说明', p.error);
    link.hidden = !p.review_id || !p.review_fragment;
    link.href = prPath + '#' + p.review_fragment;
    link.textContent = state === 'outdated' ? '查看历史审查结果' : '查看审查结果';
    notice.textContent = '每 5 秒自动更新 · 辅助审查';
  }
  async function get(path, signal) {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal, headers: { Accept: 'text/html' } });
    if (!response.ok) { const error = new Error('状态读取失败'); error.status = response.status; throw error; }
    const raw = await response.text(); if (raw.length > 4 * 1024 * 1024) throw new Error('响应超过上限');
    return raw;
  }
  async function poll() {
    if (disposed || document.hidden) return;
    clearTimeout(timer); const current = ++cycle; const started = Date.now();
    controller?.abort(); controller = new AbortController(); const activeController = controller; const signal = activeController.signal;
    const timeout = setTimeout(() => activeController.abort(), 4500);
    try {
      const raw = await get(prPath, signal);
      // 只解析服务器模板专用片段；绝不把整页 HTML 插回 DOM，避免执行脚本或加载外部图片。
      const source = new DOMParser().parseFromString(extractFragment(raw), 'text/html');
      const template = source.querySelector('template#ai-review-page-data');
      if (!template || template.dataset.repository !== `${owner}/${repo}` || Number(template.dataset.number) !== number) throw new Error('PR 身份不匹配');
      const head = template.dataset.head || '';
      if (!sha(head) || (!head && template.dataset.closed !== 'true')) throw new Error('当前提交版本暂时不可用');
      const pr = { state: template.dataset.closed === 'true' ? 'closed' : 'open', head: { sha: head } };
      const identity = { id: Number(template.dataset.repositoryId), name: `${owner}/${repo}`, number };
      const matches = [...template.content.querySelectorAll('div[data-comment-id][data-author-id]')].map(element => parse({
        body: element.textContent, user: { id: Number(element.dataset.authorId) }
      }, identity, bots)).filter(Boolean);
      if (matches.length > 1) throw new Error('存在多条可信状态，需管理员核对');
      const parsed = matches[0] || null;
      if (current !== cycle || disposed || document.hidden) return;
      if (parsed) { lastPayload = parsed; lastPR = pr; render(parsed, pr); }
      else { lastPayload = null; status.textContent = '尚未收到审查状态'; detail.textContent = '等待机器人发布状态；可由管理员检查仓库开关及事件通知。'; metrics.replaceChildren(); link.hidden = true; notice.textContent = '每 5 秒自动检查'; }
    } catch (e) {
      if (current !== cycle || disposed || document.hidden) return;
      if (lastPayload) render(lastPayload, lastPR);
      else status.textContent = '暂时无法读取审查状态';
      notice.textContent = [401, 403, 404].includes(e.status) ? '请检查登录状态与仓库访问权限' : '状态更新暂时失败，正在自动重试；当前显示可能已过期';
    } finally {
      clearTimeout(timeout);
      if (current === cycle && !disposed && !document.hidden) timer = setTimeout(poll, Math.max(0, 5000 - (Date.now() - started)));
    }
  }
  document.addEventListener('visibilitychange', () => {
    ++cycle; clearTimeout(timer); controller?.abort(); if (!document.hidden) poll();
  });
  window.addEventListener('pagehide', () => { disposed = true; ++cycle; clearTimeout(timer); controller?.abort(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { disposed = false; poll(); } });
  poll();
})();
