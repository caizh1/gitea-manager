/* PR 审批规则只读组件：不替代原生按钮，任何过期或失败都清除通过提示。 */
(() => {
  'use strict';
  const labels = { draft: '草稿', shadow: '影子观察', prepared_shadow: '等待安装验证',
    prepared_isolated_enforced: '等待隔离安装验证', isolated_enforced: '隔离强制测试', disabled: '已停用', disable_pending: '停用待完成' };
  function valid(p, identity, now = Date.now() / 1000) {
    return !!p && p.repository_id === identity.repo && p.number === identity.number && p.viewer_id === identity.viewer &&
      (p.head === identity.head || (identity.closed === true && p.closed === true)) && Number.isSafeInteger(p.revision) && p.revision > 0 &&
      Number.isFinite(p.checked_at) && Number.isFinite(p.valid_until) && p.checked_at <= now + 5 && p.valid_until > now &&
      typeof p.satisfied === 'boolean' && typeof p.grandfathered === 'boolean' && Object.hasOwn(labels, p.mode);
  }
  function displayError(error) {
    if (error?.name === 'AbortError') return '审批服务响应超时，请稍后更新状态';
    if (error?.name === 'SyntaxError') return '审批数据格式异常，请刷新原生页面';
    const expected = ['登录失效或没有权限，请刷新原生页面', '审批状态暂不可用',
      '审批数据超过显示预算', '代码、身份或规则版本已变化，请刷新原生页面'];
    return expected.includes(error?.message) ? error.message : '审批状态暂不可用，请稍后更新状态';
  }
  if (typeof module === 'object' && module.exports) { module.exports = { valid, labels, displayError }; return; }
  const script = document.currentScript;
  const context = document.getElementById('gm-approval-context');
  if (!context || document.getElementById('gm-approval-panel')) return;
  const identity = { repo: Number(context.dataset.repo), number: Number(context.dataset.number),
    viewer: Number(context.dataset.viewer), head: context.dataset.head, closed: context.dataset.closed === 'true' };
  const base = script?.dataset.base || '';
  const server = Number(script?.dataset.server);
  if (!Number.isSafeInteger(server) || server <= 0 || (base && (!base.startsWith('/') || base.startsWith('//') || /[\\?#]/.test(base)))) return;
  const heading = document.querySelector('.repository.view.issue.pull .issue-title-header');
  if (!heading) return;
  const panel = document.createElement('section'); panel.id = 'gm-approval-panel'; panel.className = 'gm-approval';
  panel.setAttribute('aria-label', '新增审批规则');
  const title = document.createElement('h3'); title.textContent = '新增审批规则';
  const state = document.createElement('p'); state.setAttribute('role', 'status');
  const details = document.createElement('details'); details.open = true;
  const summary = document.createElement('summary'); summary.textContent = '查看规则与逐项计票';
  const rows = document.createElement('div'); rows.className = 'gm-approval-rows';
  details.append(summary, rows);
  const info = document.createElement('p'); info.className = 'gm-approval-note';
  const refresh = document.createElement('button'); refresh.type = 'button'; refresh.textContent = '更新状态';
  panel.append(title, state, details, info, refresh); heading.after(panel);
  let sequence = 0, timer, expiry, requestTimeout, controller, disposed = false, backoff = 15000, revision = 0;
  function cancelRequest() {
    controller?.abort(); clearTimeout(requestTimeout); requestTimeout = undefined;
  }
  function unknown(message) {
    panel.dataset.state = 'unknown'; state.textContent = message; rows.replaceChildren();
    info.textContent = '没有当前有效的审批结论。原生审批与合并操作保持，服务端仍会重新核验。';
  }
  function row(name, count, required, names) {
    const element = document.createElement('div'); element.className = 'gm-approval-row';
    for (const text of [name, `${count} / ${required}`, names || '尚无有效批准']) {
      const span = document.createElement('span'); span.textContent = text; element.append(span);
    }
    rows.append(element);
  }
  function render(p) {
    rows.replaceChildren(); title.textContent = p.name || '新增审批规则';
    const active = p.mode === 'isolated_enforced';
    panel.dataset.state = p.grandfathered || !p.applicable ? 'exempt' : p.satisfied ? 'observed' : 'missing';
    state.textContent = p.closed ? 'PR 已结束；以下为当前规则核对，不是合并时的审批快照' : p.grandfathered ? '存量 PR：继续原生流程，免于新增审批' : !p.applicable ? '当前目标分支不适用新增规则' :
      `${labels[p.mode]} · ${p.satisfied ? '最近核对：新增要求已满足' : '新增要求尚未满足'}`;
    if (p.applicable && p.total && Array.isArray(p.groups) && Array.isArray(p.approved)) {
      row('不同合格用户总票数', p.total.count, p.total.required, p.approved.map(u => u.login).join('、'));
      for (const g of p.groups) row(g.name, g.count, g.required,
        p.approved.filter(u => g.approved_ids.includes(u.id)).map(u => u.login).join('、'));
    }
    if (Array.isArray(p.missing)) {
      for (const reason of p.missing) {
        const item = document.createElement('p');
        item.textContent = `尚未满足：${reason}`;
        rows.append(item);
      }
    }
    const checked = new Date(p.checked_at * 1000).toLocaleTimeString();
    info.textContent = `核对于 ${checked}。原生审批要求 ${p.native_required ?? '见原生面板'}；CI、冲突等仍独立生效。` +
      (active ? ' 当前仅为隔离强制测试。' : ' 当前组件不新增生产合并限制。') + ' APR-RACE-001 尚未修复。';
    clearTimeout(expiry);
    expiry = setTimeout(() => unknown('状态已过期，等待重新核对'), Math.max(0, p.valid_until * 1000 - Date.now()));
  }
  async function poll() {
    if (disposed || document.hidden || !identity.viewer) return;
    clearTimeout(timer); const current = ++sequence;
    cancelRequest(); controller = new AbortController(); const active = controller;
    const timeout = setTimeout(() => active.abort(), 12000); requestTimeout = timeout; refresh.disabled = true;
    try {
      const response = await fetch(`${base}/-/manager-approval/${server}/${identity.repo}/${identity.number}`, {
        credentials: 'same-origin', redirect: 'error', cache: 'no-store', signal: active.signal, headers: { Accept: 'application/json' }
      });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? '登录失效或没有权限，请刷新原生页面' : '审批状态暂不可用');
      const raw = await response.text(); if (raw.length > 128000) throw new Error('审批数据超过显示预算');
      const value = JSON.parse(raw);
      if (current !== sequence || disposed || document.hidden) return;
      if (!valid(value, identity) || value.revision < revision) throw new Error('代码、身份或规则版本已变化，请刷新原生页面');
      revision = value.revision; backoff = 15000; render(value);
    } catch (error) {
      if (current !== sequence || disposed) return;
      clearTimeout(expiry); unknown(displayError(error)); backoff = Math.min(60000, backoff * 2);
    } finally {
      clearTimeout(timeout);
      if (requestTimeout === timeout) requestTimeout = undefined;
      if (current === sequence && !disposed) { refresh.disabled = false; timer = setTimeout(poll, backoff); }
    }
  }
  refresh.addEventListener('click', () => { if (!refresh.disabled) poll(); });
  document.addEventListener('visibilitychange', () => {
    ++sequence; cancelRequest(); clearTimeout(timer); clearTimeout(expiry); unknown('正在重新确认当前状态');
    if (!document.hidden) poll();
  });
  window.addEventListener('pagehide', () => { disposed = true; ++sequence; cancelRequest(); clearTimeout(timer); clearTimeout(expiry); });
  window.addEventListener('pageshow', e => { if (e.persisted) { disposed = false; poll(); } });
  unknown(identity.viewer ? '正在核对审批规则' : '请使用现有 Gitea 账号登录后查看'); poll();
})();
