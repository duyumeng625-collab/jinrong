const $ = (selector) => document.querySelector(selector);
const STORAGE_KEY = 'smartcs.customer.chats.v1';

function readChats() {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    return Array.isArray(value) ? value.filter(chat => chat && chat.sessionId && Array.isArray(chat.messages)) : [];
  } catch { return []; }
}

const state = { chats: readChats(), activeSessionId: null, busy: false };

function createChat() {
  const chat = { sessionId: crypto.randomUUID(), title: '新对话', messages: [], updatedAt: Date.now() };
  state.chats.unshift(chat); state.activeSessionId = chat.sessionId; persistChats();
  return chat;
}

if (state.chats.length) state.activeSessionId = [...state.chats].sort((a, b) => b.updatedAt - a.updatedAt)[0].sessionId;
else createChat();

function currentChat() { return state.chats.find(chat => chat.sessionId === state.activeSessionId); }

function persistChats() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.chats)); }
  catch { toast('浏览器本地空间不足，对话可能无法保存'); }
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`);
  return body;
}

function toast(message) {
  const node = $('#toast'); node.textContent = message; node.classList.remove('hidden');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.add('hidden'), 2600);
}

function messageRow(role, content, sources = []) {
  const row = document.createElement('div'); row.className = `message-row ${role}`;
  const avatar = document.createElement('div'); avatar.className = 'message-avatar'; avatar.textContent = role === 'user' ? '我' : '知';
  const body = document.createElement('div'); body.className = 'message-body';
  const meta = document.createElement('div'); meta.className = 'message-meta'; meta.textContent = role === 'user' ? '你' : '智能客服';
  const bubble = document.createElement('div'); bubble.className = 'bubble'; bubble.textContent = content;
  body.append(meta, bubble);
  if (role === 'assistant' && sources.length) {
    const refs = document.createElement('div'); refs.className = 'inline-sources';
    sources.forEach((source, index) => {
      const chip = document.createElement('button'); chip.className = 'source-chip';
      chip.innerHTML = '<span>▤</span><span></span><small></small>';
      chip.children[1].textContent = source.source || `参考片段 ${index + 1}`;
      chip.children[2].textContent = `RRF ${Number(source.score || 0).toFixed(4)}`;
      chip.addEventListener('click', () => showSource(source)); refs.append(chip);
    });
    body.append(refs);
  }
  row.append(avatar, body); return row;
}

function showSource(source) {
  $('#source-title').textContent = source.source || '参考片段';
  $('#source-content').textContent = source.content || '';
  $('#source-modal').classList.remove('hidden');
}

function showSources(sources) {
  const list = $('#source-list'); list.replaceChildren();
  if (!sources.length) return;
  const title = document.createElement('h4'); title.textContent = '回答参考'; list.append(title);
  sources.forEach(source => {
    const button = document.createElement('button'); button.textContent = `▤  ${source.source || '参考片段'}`;
    button.addEventListener('click', () => showSource(source)); list.append(button);
  });
}

function renderMessages() {
  const chat = currentChat(), container = $('#messages'); container.replaceChildren();
  if (!chat || !chat.messages.length) {
    container.innerHTML = '<div class="welcome-card"><div class="welcome-icon">✳</div><h2>你好，有什么可以帮你？</h2><p>系统会根据你的问题选择对应的客服流程；查询政策和业务资料时，会展示参考来源。</p><div class="suggestions"><button data-prompt="退款申请的流程和时限是什么？">退款政策</button><button data-prompt="开户需要准备什么材料？">开户流程</button><button data-prompt="帮我创建一个售后工单">创建工单</button></div></div>';
  } else {
    chat.messages.forEach(message => container.append(messageRow(message.role, message.content, message.sources || [])));
    container.scrollTop = container.scrollHeight;
  }
  showSources(chat?.messages.filter(message => message.role === 'assistant').at(-1)?.sources || []);
  renderChatHistory();
}

function renderChatHistory() {
  const list = $('#chat-history-list'); list.replaceChildren();
  const chats = [...state.chats].sort((a, b) => b.updatedAt - a.updatedAt);
  if (!chats.length) { list.innerHTML = '<div class="history-empty">暂无对话</div>'; return; }
  chats.forEach(chat => {
    const button = document.createElement('button');
    button.className = `history-item ${chat.sessionId === state.activeSessionId ? 'active' : ''}`;
    button.title = chat.title;
    const icon = document.createElement('span'); icon.className = 'history-icon'; icon.textContent = '◦';
    const name = document.createElement('span'); name.className = 'history-name'; name.textContent = chat.title || '新对话';
    button.append(icon, name); button.disabled = state.busy;
    button.addEventListener('click', () => { state.activeSessionId = chat.sessionId; renderMessages(); });
    list.append(button);
  });
}

function saveMessage(role, content, sources = []) {
  const chat = currentChat(); if (!chat) return;
  chat.messages.push({ role, content, sources }); chat.updatedAt = Date.now();
  if (role === 'user' && chat.title === '新对话') chat.title = content.slice(0, 28) || '新对话';
  persistChats(); renderMessages();
}

async function sendMessage(text) {
  const message = text.trim(); if (!message || state.busy) return;
  const chat = currentChat(); if (!chat) return;
  saveMessage('user', message);
  const pending = messageRow('assistant', '正在为你处理…'); $('#messages').append(pending); $('#messages').scrollTop = $('#messages').scrollHeight;
  state.busy = true; $('#message-input').disabled = true; $('.send-button').disabled = true; renderChatHistory();
  try {
    const result = await api('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, user_id: 'web_user', session_id: chat.sessionId }) });
    pending.remove();
    chat.messages.push({ role: 'assistant', content: result.response, sources: result.sources || [] });
    chat.updatedAt = Date.now(); persistChats();
  } catch (error) {
    pending.remove();
    chat.messages.push({ role: 'assistant', content: `暂时无法处理：${error.message}\n\n请稍后重试，或联系人工客服。`, sources: [] });
    chat.updatedAt = Date.now(); persistChats();
  } finally {
    state.busy = false; $('#message-input').disabled = false; $('.send-button').disabled = false;
    renderMessages(); $('#message-input').focus();
  }
}

function newChat() {
  if (state.busy) return;
  if (!currentChat()?.messages.length) { $('#message-input').focus(); return; }
  createChat(); renderMessages(); $('#message-input').focus();
}

$('#chat-form').addEventListener('submit', event => { event.preventDefault(); const input = $('#message-input'); const text = input.value; input.value = ''; sendMessage(text); });
$('#message-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#chat-form').requestSubmit(); } });
$('#messages').addEventListener('click', event => { const prompt = event.target.closest('[data-prompt]'); if (prompt) sendMessage(prompt.dataset.prompt); });
$('#new-chat').addEventListener('click', newChat);
$('#source-close').addEventListener('click', () => $('#source-modal').classList.add('hidden'));
$('#source-modal').addEventListener('click', event => { if (event.target.id === 'source-modal') event.currentTarget.classList.add('hidden'); });

renderMessages();
api('/health').then(() => { $('#api-status').innerHTML = '<i></i>服务在线'; }).catch(() => { $('#api-status').classList.add('offline'); $('#api-status').innerHTML = '<i></i>服务离线'; });
