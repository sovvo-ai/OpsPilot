/* OpsPilot 前端（原生 JS，无构建链）
 *
 * 契约要点（**不要改坏**）：
 *
 *  1. **引用角标用 `citations[].index` 匹配**，禁止用数组下标。
 *     —— 后端 `extract_citations` 曾按"出现顺序"收集，导致正文 `[1]` 点开是 2 号片段；
 *       现契约（commit fa6767d）：每条引用带显式 `index`（= 正文角标数字）。
 *
 *  2. **流式中断必须保留半截答案 + 提示"回答可能不完整"**（UI-09）。
 *
 *  3. 错误提示必须带 `request_id`（UI-08）。
 */
'use strict';

/* ═════════════════════════════════════════════════════════════
 * 常量与状态
 * ═════════════════════════════════════════════════════════════ */

const POLL_INTERVAL_MS = 2000;      // UI-01：文档状态轮询 2s（不走 WebSocket）
const STORAGE_TOKEN = 'opspilot.token';
const STORAGE_USER = 'opspilot.user';

const state = {
  token: null,
  user: 'demo-user',
  sessions: [],
  currentSessionId: null,
  documents: [],
  messages: [],
  /** 当前打开的引用/轨迹上下文（供抽屉渲染） */
  drawer: { open: false, kind: null, payload: null },
  /** 正在流式生成的会话 id（防重复、供中断判断） */
  streamingSessionId: null,
  sending: false,
  pollTimer: null,
};

/* ═════════════════════════════════════════════════════════════
 * 工具
 * ═════════════════════════════════════════════════════════════ */

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (v !== null && v !== undefined && v !== false) {
      node.setAttribute(k, v === true ? '' : String(v));
    }
  }
  for (const c of [].concat(children)) {
    if (c) node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

/** 转义 HTML（用于把答案文本安全插入，再把 `[n]` 替换成角标） */
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** UI-08：把后端错误体渲染成可读文案，**必须带 request_id** */
function renderError(err) {
  const code = (err && err.code) || 'NETWORK_ERROR';
  const msg = (err && err.message) || '请求失败，请稍后重试';
  const rid = (err && err.request_id) || '';
  return { code, msg, rid };
}

function toast(message, kind = 'error', requestId = '') {
  const box = $('#toast');
  box.className = 'toast' + (kind === 'info' ? ' toast-info' : '');
  box.textContent = message;
  if (requestId) {
    box.appendChild(el('span', { class: 'toast-rid', text: 'request_id: ' + requestId }));
  }
  box.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { box.hidden = true; }, 8000);
}

/* ═════════════════════════════════════════════════════════════
 * API 客户端
 * ═════════════════════════════════════════════════════════════ */

const api = {
  base: '',        // 同源（Go embed 托管）

  headers(extra = {}) {
    const h = { ...extra };
    if (state.token) h['Authorization'] = 'Bearer ' + state.token;
    return h;
  },

  async request(method, path, body, extraHeaders = {}) {
    const opts = { method, headers: this.headers(extraHeaders) };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(this.base + path, opts);
    } catch (e) {
      throw { code: 'NETWORK_ERROR', message: '无法连接服务，请确认后端已启动', request_id: '' };
    }
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { /* 非 JSON */ }
    if (!resp.ok) {
      const err = (data && data.error) || {};
      throw {
        code: err.code || ('HTTP_' + resp.status),
        message: err.message || text || ('请求失败（' + resp.status + '）'),
        request_id: err.request_id || '',
      };
    }
    return data;
  },

  get(p) { return this.request('GET', p); },
  post(p, b) { return this.request('POST', p, b); },
  del(p) { return this.request('DELETE', p); },

  /* 文档列表：无此端点，用会话侧无关；这里按需组合（见 loadDocuments） */
};

/* ═════════════════════════════════════════════════════════════
 * 引用角标渲染（**契约：用 citations[].index 匹配**）
 * ═════════════════════════════════════════════════════════════ */

/**
 * 把答案文本里的 `[n]` 渲染成可点击角标。
 *
 * **关键**：角标点击后要能在 `citations` 里按 **`index`** 找到对应项 ——
 * 绝不能用数组下标（`citations[i]` ↔ 正文 `[i+1]`）匹配，那会在
 * "答案里 `[2]` 先于 `[1]` 出现"时错位（S8 实测缺陷）。
 */
function renderAnswerHtml(text, citations) {
  const safe = escapeHtml(text || '');
  const idxSet = new Set((citations || []).map((c) => c.index));
  return safe.replace(/\[(\d{1,2})\]/g, (m, n) => {
    const num = parseInt(n, 10);
    // 只有本次引用集里真实存在的 index 才渲染成可点击角标
    if (!idxSet.has(num)) return m;
    return `<span class="cite-mark" data-index="${num}" title="查看引用 ${num}">[${num}]</span>`;
  });
}

/** 按 `index` 查引用（契约函数，供各处复用） */
function findCitation(citations, index) {
  return (citations || []).find((c) => c.index === index) || null;
}

/* ═════════════════════════════════════════════════════════════
 * 启动
 * ═════════════════════════════════════════════════════════════ */

async function boot() {
  bindEvents();
  await ensureToken();
  await loadSessions();
  await loadDocuments();
  routeFromHash();      // UI-06：hash 路由恢复会话
  startPolling();       // UI-01：轮询文档状态
  updateConnBadge();
}

/** 取 dev token（演示环境；生产应走登录） */
async function ensureToken() {
  const cached = sessionStorage.getItem(STORAGE_TOKEN);
  if (cached) { state.token = cached; return; }
  try {
    const r = await fetch('/api/v1/auth/dev-token?user=' + encodeURIComponent(state.user));
    const d = await r.json();
    state.token = d.token;
    sessionStorage.setItem(STORAGE_TOKEN, d.token);
  } catch (e) {
    toast('无法获取访问令牌，请确认后端已启动。', 'error');
  }
}

async function updateConnBadge() {
  const badge = $('#conn-badge');
  try {
    const r = await fetch('/health/ready');
    const d = await r.json();
    if (d.status === 'ok') {
      badge.className = 'badge badge-completed';
      badge.textContent = '就绪';
    } else {
      badge.className = 'badge badge-failed';
      badge.textContent = '依赖异常';
    }
  } catch (e) {
    badge.className = 'badge badge-failed';
    badge.textContent = '服务不可用';
  }
}

document.addEventListener('DOMContentLoaded', boot);
/* ═════════════════════════════════════════════════════════════
 * 会话（UI-05/UI-06）
 * ═════════════════════════════════════════════════════════════ */

async function loadSessions() {
  try {
    const d = await api.get('/api/v1/sessions');
    state.sessions = d.items || d.sessions || [];
  } catch (e) {
    state.sessions = [];
    const { msg, rid } = renderError(e);
    toast('加载会话失败：' + msg, 'error', rid);
  }
  renderSessions();
}

function renderSessions() {
  const box = $('#session-list');
  const empty = $('#session-empty');
  box.innerHTML = '';
  if (!state.sessions.length) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  for (const s of state.sessions) {
    const active = s.id === state.currentSessionId;
    box.appendChild(el('button', {
      class: 'list-item' + (active ? ' active' : ''),
      type: 'button',
      role: 'option',
      'aria-selected': active ? 'true' : 'false',
      onclick: () => switchSession(s.id),
    }, [
      el('span', { class: 'list-item-title', text: s.title || '未命名会话' }),
    ]));
  }
}

async function createSession() {
  try {
    const s = await api.post('/api/v1/sessions', { title: '新会话' });
    state.sessions.unshift(s);
    renderSessions();
    await switchSession(s.id);
  } catch (e) {
    const { msg, rid } = renderError(e);
    toast('创建会话失败：' + msg, 'error', rid);
  }
}

/** UI-06：hash 路由 —— 切换会话写 hash，刷新后由 routeFromHash 恢复 */
async function switchSession(sid) {
  state.currentSessionId = sid;
  location.hash = '#/s/' + sid;
  renderSessions();
  await loadMessages();
}

function routeFromHash() {
  const m = /^#\/s\/([0-9a-f-]+)$/i.exec(location.hash || '');
  if (m && state.sessions.some((s) => s.id === m[1])) {
    state.currentSessionId = m[1];
    renderSessions();
    return loadMessages();
  }
  if (state.sessions.length) {
    // 无有效 hash → 打开第一个会话（保持"刷新后仍停留在当前会话"的体验）
    return switchSession(state.sessions[0].id);
  }
  renderEmptyChat();
}

/* ═════════════════════════════════════════════════════════════
 * 文档（UI-01 / UI-02）
 * ═════════════════════════════════════════════════════════════ */

async function loadDocuments() {
  // 后端未提供"列出全部文档"端点；通过会话无关的方式获取：
  // 这里用最近上传集合（本地维护）+ 逐个查询状态。
  // 若用户刷新，列表为空 —— 属已知限制（见 README「已知限制」）。
  renderDocuments();
}

function renderDocuments() {
  const box = $('#doc-list');
  const empty = $('#doc-empty');
  box.innerHTML = '';
  if (!state.documents.length) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  for (const d of state.documents) {
    const row = el('div', { class: 'list-item' }, [
      el('span', { class: 'list-item-title', text: d.title || d.name, title: d.title || d.name }),
    ]);
    const statusMap = {
      pending: ['badge-pending', '等待中'],
      processing: ['badge-processing', '处理中'],
      completed: ['badge-completed', '已完成'],
      failed: ['badge-failed', '失败'],
      deleting: ['badge-deleting', '删除中'],
    };
    const [cls, label] = statusMap[d.ingest_status] || ['badge-muted', d.ingest_status || '未知'];
    row.appendChild(el('span', { class: 'badge ' + cls, text: label }));

    // UI-02：failed 显示原因 + 重试按钮
    if (d.ingest_status === 'failed') {
      const wrap = el('div', { class: 'list-item-fail' }, [
        el('span', { class: 'fail-reason', text: d.error_code || '摄取失败' }),
        el('button', {
          class: 'btn btn-ghost btn-sm', type: 'button', text: '重试',
          onclick: (ev) => { ev.stopPropagation(); retryDocument(d.doc_id); },
        }),
      ]);
      box.appendChild(el('div', { class: 'doc-item-wrap' }, [row, wrap]));
    } else {
      box.appendChild(row);
    }
  }
}

async function uploadFiles(files) {
  const list = Array.from(files || []).filter((f) => /\.(md|txt)$/i.test(f.name));
  if (!list.length) {
    toast('仅支持 .md 与 .txt 文件', 'error');
    return;
  }
  const prog = $('#upload-progress');
  const progText = $('#upload-progress-text');
  prog.hidden = false;

  for (let i = 0; i < list.length; i++) {
    const f = list[i];
    progText.textContent = `上传中（${i + 1}/${list.length}）：${f.name}`;
    try {
      const fd = new FormData();
      fd.append('file', f, f.name);
      const resp = await fetch('/api/v1/documents', {
        method: 'POST', headers: api.headers(), body: fd,
      });
      const text = await resp.text();
      let data = null;
      try { data = JSON.parse(text); } catch (e) { /* */ }
      if (!resp.ok) {
        const err = (data && data.error) || {};
        toast(`上传「${f.name}」失败：${err.message || text}`, 'error', err.request_id || '');
        continue;
      }
      // UI-01：上传后**立即**出现在列表（状态 pending）
      state.documents.unshift({
        doc_id: data.doc_id,
        title: f.name,
        ingest_status: data.ingest_status || 'pending',
        error_code: null,
        deduplicated: data.deduplicated,
      });
      renderDocuments();
    } catch (e) {
      toast(`上传「${f.name}」失败：无法连接服务`, 'error');
    }
  }
  prog.hidden = true;
  await refreshDocumentStatuses();
}

async function retryDocument(docId) {
  try {
    await api.post(`/api/v1/documents/${docId}/reingest`);
    const d = state.documents.find((x) => x.doc_id === docId);
    if (d) { d.ingest_status = 'processing'; d.error_code = null; renderDocuments(); }
    await refreshDocumentStatuses();
  } catch (e) {
    const { msg, rid } = renderError(e);
    toast('重试失败：' + msg, 'error', rid);
  }
}

/** UI-01：轮询 2s 刷新文档状态（pending → processing → completed） */
async function refreshDocumentStatuses() {
  const pending = state.documents.filter(
    (d) => d.ingest_status === 'pending' || d.ingest_status === 'processing');
  if (!pending.length) return;
  await Promise.all(pending.map(async (d) => {
    try {
      const info = await api.get('/api/v1/documents/' + d.doc_id);
      d.ingest_status = info.ingest_status;
      d.error_code = info.error_code || null;
      d.chunk_count = info.chunk_count;
    } catch (e) { /* 单条失败不影响其他 */ }
  }));
  renderDocuments();
}

function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(refreshDocumentStatuses, POLL_INTERVAL_MS);
}

/* ═════════════════════════════════════════════════════════════
 * 消息（UI-04 引用 / UI-05 轨迹 / UI-07 空态）
 * ═════════════════════════════════════════════════════════════ */

async function loadMessages() {
  if (!state.currentSessionId) return renderEmptyChat();
  const box = $('#message-list');
  try {
    const d = await api.get(`/api/v1/sessions/${state.currentSessionId}/messages`);
    state.messages = d.items || d.messages || [];
  } catch (e) {
    state.messages = [];
    const { msg, rid } = renderError(e);
    toast('加载消息失败：' + msg, 'error', rid);
  }
  box.innerHTML = '';
  if (!state.messages.length) return renderEmptyChat();
  $('#chat-empty').hidden = true;
  for (const m of state.messages) box.appendChild(renderMessage(m));
  scrollToBottom();
}

function renderEmptyChat() {
  $('#message-list').innerHTML = '';
  $('#chat-empty').hidden = false;
}

function scrollToBottom() {
  const box = $('#message-list');
  box.scrollTop = box.scrollHeight;
}

/**
 * 渲染一条消息。
 * `extra` 用于流式过程中的临时态（如 `streaming: true` / `interrupted: true`）。
 */
function renderMessage(m, extra = {}) {
  const isUser = m.role === 'user';
  const wrap = el('div', { class: 'msg ' + (isUser ? 'msg-user' : 'msg-assistant') });
  wrap.appendChild(el('div', { class: 'msg-role', text: isUser ? '你' : 'OpsPilot' }));

  const body = el('div', { class: 'msg-body' });

  if (isUser) {
    body.textContent = m.content || '';
  } else {
    // UI-04：`[n]` → 可点击角标（按 citations[].index 匹配）
    body.innerHTML = renderAnswerHtml(m.content || '', m.citations);
    if (extra.streaming) {
      body.appendChild(el('span', { class: 'cursor' }));
    }
  }
  wrap.appendChild(body);

  // UI-09：流中断 → **保留半截答案** + 提示不完整
  if (extra.interrupted) {
    wrap.appendChild(el('div', {
      class: 'stream-warn',
      text: '回答可能不完整（连接中断，已保留已生成内容）',
    }));
  }

  // 降级标注
  if (!isUser && m.degraded) {
    const reasons = (m.degrade_reasons || []).join('、') || '部分校验未完成';
    wrap.appendChild(el('div', { class: 'degrade-note', text: '降级：' + reasons }));
  }

  // 助手消息操作区（UI-05 轨迹入口）
  if (!isUser && m.run_id && !extra.streaming) {
    wrap.appendChild(el('div', { class: 'msg-actions' }, [
      el('button', {
        class: 'msg-action', type: 'button', text: '查看轨迹',
        onclick: () => openTraceDrawer(m.run_id),
      }),
    ]));
  }
  return wrap;
}

/* ═════════════════════════════════════════════════════════════
 * 提问 + SSE 流式（UI-03 / UI-09）
 * ═════════════════════════════════════════════════════════════ */

/**
 * 提交提问。
 *
 * UI-03：发送中按钮 loading、**防重复提交**（连点 3 次只产生 1 条用户消息）。
 * UI-09：走 SSE 逐字渲染；引用在 `citation` 事件到达后**一次性补齐**；
 *        流中断时保留半截内容并提示。
 */
async function askQuestion() {
  if (state.sending) return;                       // 防重复（UI-03）
  const input = $('#question');
  const question = (input.value || '').trim();
  if (!question) return;
  if (!state.currentSessionId) await createSession();
  if (!state.currentSessionId) return;

  state.sending = true;
  const btn = $('#btn-send');
  btn.disabled = true;
  btn.textContent = '生成中';
  input.value = '';
  autosize(input);

  const attachEl = $('#attach-log');
  const attachLog = (!attachEl.hidden && attachEl.value.trim()) ? attachEl.value : null;

  // 立即渲染用户消息（不等待服务端回执，保证"立即出现"）
  $('#chat-empty').hidden = true;
  const list = $('#message-list');
  list.appendChild(renderMessage({ role: 'user', content: question }));

  // 助手占位（流式追加目标）
  const assistantMsg = { role: 'assistant', content: '', citations: [], degraded: false, run_id: null };
  const assistantNode = renderMessage(assistantMsg, { streaming: true });
  list.appendChild(assistantNode);
  scrollToBottom();

  const bodyEl = assistantNode.querySelector('.msg-body');
  const abortCtrl = new AbortController();
  let interrupted = false;
  let receivedDone = false;

  try {
    const resp = await fetch(
      `/api/v1/sessions/${state.currentSessionId}/messages/stream`,
      {
        method: 'POST',
        headers: api.headers({ 'Content-Type': 'application/json', 'Accept': 'text/event-stream' }),
        body: JSON.stringify(attachLog ? { question, attach_log: attachLog } : { question }),
        signal: abortCtrl.signal,
      },
    );
    if (!resp.ok || !resp.body) {
      const text = await resp.text().catch(() => '');
      let err = {};
      try { err = (JSON.parse(text).error) || {}; } catch (e) { /* */ }
      throw { code: err.code || ('HTTP_' + resp.status),
              message: err.message || text || '流式请求失败',
              request_id: err.request_id || '' };
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';

    // 逐块解析 SSE
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      let sep;
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const raw = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        const ev = parseSseBlock(raw);
        if (!ev) continue;

        if (ev.type === 'meta') {
          assistantMsg.run_id = ev.run_id;
        } else if (ev.type === 'delta') {
          // UI-09：逐字追加
          assistantMsg.content += ev.text || '';
          bodyEl.innerHTML = renderAnswerHtml(assistantMsg.content, assistantMsg.citations);
          bodyEl.appendChild(el('span', { class: 'cursor' }));
          scrollToBottom();
        } else if (ev.type === 'citation') {
          // UI-09：引用**流结束后一次性补齐**（此处即流尾）
          assistantMsg.citations = ev.citations || [];
          bodyEl.innerHTML = renderAnswerHtml(assistantMsg.content, assistantMsg.citations);
          bodyEl.appendChild(el('span', { class: 'cursor' }));
        } else if (ev.type === 'done') {
          receivedDone = true;
          const res = ev.result || {};
          assistantMsg.content = res.final_text || assistantMsg.content;
          assistantMsg.citations = res.citations || assistantMsg.citations;
          assistantMsg.degraded = !!res.degraded;
          assistantMsg.degrade_reasons = res.degrade_reasons || [];
          assistantMsg.answer_type = res.answer_type;
        } else if (ev.type === 'error') {
          if (ev.code === 'CLIENT_DISCONNECTED') {
            interrupted = true;
          } else {
            throw { code: ev.code || 'STREAM_ERROR', message: ev.message || '流式错误', request_id: '' };
          }
        }
        // `saved` 事件：落库确认，前端无需处理
      }
    }
  } catch (e) {
    if (e && e.name === 'AbortError') {
      interrupted = true;
    } else {
      interrupted = true;                 // 网络中断 → 保留半截（UI-09）
      const { msg, rid } = renderError(e);
      if (!(e && e.code === 'CLIENT_DISCONNECTED')) toast('生成失败：' + msg, 'error', rid);
    }
  } finally {
    state.sending = false;
    state.streamingSessionId = null;
    btn.disabled = false;
    btn.textContent = '发送';
    bindCiteMarks(assistantNode);

    // 用最终状态重渲染这条助手消息（去掉光标；中断则加提示）
    const finalNode = renderMessage(assistantMsg, { interrupted: interrupted && !receivedDone });
    assistantNode.replaceWith(finalNode);
    scrollToBottom();

    // 刷新消息列表以拿到服务端落库后的 id 等信息
    loadMessages();
  }
}

/** 解析一个 SSE 块（`event: x\ndata: {...}`） */
function parseSseBlock(raw) {
  let type = null, data = null;
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) type = line.slice(6).trim();
    else if (line.startsWith('data:')) {
      const payload = line.slice(5).trim();
      if (!payload) continue;
      try { data = JSON.parse(payload); } catch (e) { return null; }
    }
    // `:` 开头的心跳注释行忽略
  }
  if (data && !data.type && type) data.type = type;
  return data;
}

/** 给容器内所有引用角标绑定点击（UI-04） */
function bindCiteMarks(root) {
  root.querySelectorAll('.cite-mark').forEach((node) => {
    node.addEventListener('click', () => {
      const idx = parseInt(node.dataset.index, 10);
      openCitationDrawer(idx);
    });
  });
}

function autosize(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
}

/* ═════════════════════════════════════════════════════════════
 * 抽屉：引用详情（UI-04）/ 执行轨迹（UI-05）
 * ═════════════════════════════════════════════════════════════ */

function openDrawer(title, contentNode) {
  $('#drawer-title').textContent = title;
  const body = $('#drawer-body');
  body.innerHTML = '';
  body.appendChild(contentNode);
  $('#drawer').hidden = false;
  $('.layout').classList.remove('no-drawer');
  state.drawer.open = true;
}

function closeDrawer() {
  $('#drawer').hidden = true;
  $('.layout').classList.add('no-drawer');
  state.drawer.open = false;
}

/**
 * 打开引用详情（UI-04）。
 *
 * **契约**：`index` 是正文角标数字；从**当前消息的 citations** 里按 `index` 查找，
 * **不用数组下标**。
 *
 * `deleted` 状态必须显示占位文案（U4）。
 */
function openCitationDrawer(index) {
  const citations = currentAssistantCitations();
  const cit = findCitation(citations, index);

  const wrap = el('div', { class: 'drawer-section' });

  if (!cit) {
    wrap.appendChild(el('div', {
      class: 'deleted-placeholder',
      text: `未找到引用 [${index}] 对应的片段（可能已被清理）`,
    }));
    return openDrawer('引用 [' + index + ']', wrap);
  }

  wrap.appendChild(el('div', { class: 'drawer-label', text: '原文片段' }));
  // U4：`deleted` 状态 → 占位文案，不显示已删除的正文
  if (cit.status === 'deleted') {
    wrap.appendChild(el('div', { class: 'deleted-placeholder', text: '原文已删除' }));
  } else {
    wrap.appendChild(el('div', { class: 'snippet', text: cit.snippet || '（无片段内容）' }));
  }

  wrap.appendChild(el('div', { class: 'drawer-label', text: '来源与相似度' }));
  wrap.appendChild(el('div', { class: 'kv' }, [
    kvRow('文档', cit.doc_title || '未知'),
    kvRow('角标', '[' + index + ']'),
    kvRow('相似度', cit.score != null ? Number(cit.score).toFixed(4) : '—'),
    kvRow('状态', cit.status || 'live'),
    kvRow('chunk_id', (cit.chunk_id || '').slice(0, 8) + '…'),
  ]));

  openDrawer('引用 [' + index + ']', wrap);
}

/** 取"最后一条助手消息"的 citations（角标点击的上下文） */
function currentAssistantCitations() {
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const m = state.messages[i];
    if (m.role === 'assistant' && (m.citations || []).length) return m.citations;
  }
  return [];
}

/** 打开执行轨迹（UI-05）。
 *
 * 数字必须与后端 trace **一致**（`llm_calls` / `tool_calls` 直接取自 `/runs/{id}`）。
 */
async function openTraceDrawer(runId) {
  const wrap = el('div', { class: 'drawer-section' });
  wrap.appendChild(el('div', { class: 'drawer-label', text: '加载中…' }));
  openDrawer('执行轨迹', wrap);

  let run;
  try {
    run = await api.get('/api/v1/runs/' + runId);
  } catch (e) {
    const { msg, rid } = renderError(e);
    wrap.innerHTML = '';
    wrap.appendChild(el('div', { class: 'deleted-placeholder', text: '轨迹加载失败：' + msg }));
    if (rid) wrap.appendChild(el('div', { class: 'toast-rid', text: 'request_id: ' + rid }));
    return;
  }

  wrap.innerHTML = '';

  // 概览（数字直接来自后端，不做本地估算）
  wrap.appendChild(el('div', { class: 'drawer-label', text: '概览' }));
  wrap.appendChild(el('div', { class: 'kv' }, [
    kvRow('路由', run.route || '—'),
    kvRow('模型调用', String(run.llm_calls != null ? run.llm_calls : 0)),
    kvRow('工具调用', String(run.tool_calls != null ? run.tool_calls : 0)),
    kvRow('总耗时', (run.duration_ms != null ? run.duration_ms : '—') + ' ms'),
    kvRow('检索最高分', run.retrieval_top_score != null
      ? Number(run.retrieval_top_score).toFixed(4) : '—'),
    kvRow('终止原因', run.terminated_reason || '—'),
    kvRow('降级', run.degraded ? '是（' + (run.degrade_reasons || []).join('、') + '）' : '否'),
  ]));

  // 节点时间线
  wrap.appendChild(el('div', { class: 'drawer-label', text: '节点时间线' }));
  const tl = el('div', { class: 'timeline' });
  for (const s of (run.steps || [])) {
    tl.appendChild(el('div', { class: 'tl-row' }, [
      el('span', { class: 'tl-node', text: s.node || '—' }),
      el('span', { class: 'tl-summary', text: s.output_summary || '', title: s.output_summary || '' }),
      el('span', { class: 'tl-ms', text: (s.duration_ms != null ? s.duration_ms : 0) + 'ms' }),
    ]));
  }
  if (!(run.steps || []).length) {
    tl.appendChild(el('div', { class: 'empty-hint', text: '本次没有记录到节点步骤。' }));
  }
  wrap.appendChild(tl);

  // 工具调用
  const tools = run.tool_call_records || [];
  wrap.appendChild(el('div', { class: 'drawer-label', text: '工具调用（' + tools.length + '）' }));
  if (!tools.length) {
    wrap.appendChild(el('div', { class: 'empty-hint', text: '本轮未调用工具。' }));
  } else {
    const tbox = el('div', { class: 'timeline' });
    for (const t of tools) {
      tbox.appendChild(el('div', { class: 'tl-row' }, [
        el('span', { class: 'tl-node', text: t.tool_name || '—' }),
        el('span', { class: 'tl-summary', text: t.args_digest || '', title: t.output_summary || '' }),
        el('span', { class: 'tl-ms', text: (t.duration_ms != null ? t.duration_ms : 0) + 'ms' }),
      ]));
    }
    wrap.appendChild(tbox);
  }

  // 引用越界检测（S9 新增的观测字段）
  if (run.citation_overreach && run.citation_overreach.length) {
    wrap.appendChild(el('div', { class: 'drawer-label', text: '引用越界检测' }));
    const obox = el('div', { class: 'timeline' });
    for (const o of run.citation_overreach) {
      obox.appendChild(el('div', { class: 'tl-row' }, [
        el('span', { class: 'tl-node', text: '[' + o.mark + ']' }),
        el('span', { class: 'tl-summary', text: (o.tokens_not_in_source || []).join(', '),
                     title: o.sentence || '' }),
        el('span', { class: 'tl-ms', text: '越界' }),
      ]));
    }
    wrap.appendChild(obox);
  }
}

function kvRow(k, v) {
  return el('div', { class: 'kv-row' }, [
    el('span', { class: 'kv-key', text: k }),
    el('span', { class: 'kv-val', text: v, title: v }),
  ]);
}

/* ═════════════════════════════════════════════════════════════
 * 事件绑定
 * ═════════════════════════════════════════════════════════════ */

function bindEvents() {
  $('#btn-new-session').addEventListener('click', createSession);
  $('#btn-upload').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', (e) => {
    uploadFiles(e.target.files);
    e.target.value = '';
  });
  $('#btn-close-drawer').addEventListener('click', closeDrawer);

  // 拖拽上传（UI-01）
  const dz = $('#dropzone');
  dz.addEventListener('click', () => $('#file-input').click());
  dz.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#file-input').click(); }
  });
  ['dragenter', 'dragover'].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('dragover'); }));
  dz.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files) uploadFiles(e.dataTransfer.files);
  });

  // 提问（UI-03）
  const form = $('#ask-form');
  const input = $('#question');
  form.addEventListener('submit', (e) => { e.preventDefault(); askQuestion(); });
  input.addEventListener('keydown', (e) => {
    // Enter 发送，Shift+Enter 换行
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      askQuestion();
    }
  });
  input.addEventListener('input', () => autosize(input));

  // 附日志开关
  const chk = $('#chk-attach');
  const attach = $('#attach-log');
  const btnClear = $('#btn-clear-attach');
  chk.addEventListener('change', () => {
    attach.hidden = !chk.checked;
    btnClear.hidden = !chk.checked;
    if (chk.checked) attach.focus();
  });
  btnClear.addEventListener('click', () => {
    attach.value = '';
    attach.hidden = true;
    btnClear.hidden = true;
    chk.checked = false;
  });

  // UI-06：hash 变化（浏览器前进/后退）
  window.addEventListener('hashchange', routeFromHash);

  // 引用角标点击（事件委托，覆盖动态插入的角标）
  $('#message-list').addEventListener('click', (e) => {
    const mark = e.target.closest('.cite-mark');
    if (mark) openCitationDrawer(parseInt(mark.dataset.index, 10));
  });
}
