const appRoot = document.querySelector('.app');
const appVersion = (appRoot && appRoot.dataset.uiVersion) || '';
const voiceBtn = document.getElementById('voiceBtn');
const textBtn = document.getElementById('textBtn');
const textInput = document.getElementById('textInput');
const statusEl = document.getElementById('status');
const turnsEl = document.getElementById('turns');
const clearTurnsBtn = document.getElementById('clearTurns');
const redirectHintEl = document.getElementById('redirectHint');
const langToggle = document.getElementById('langToggle');
const photoModalEl = document.getElementById('photoModal');
const photoModalTitleEl = document.getElementById('photoModalTitle');
const photoModalCloseEl = document.getElementById('photoModalClose');
const photoGridEl = document.getElementById('photoGrid');
const photoEmptyEl = document.getElementById('photoEmpty');
let _taskBusy = false;
let _liveTurnEl = null;
let _photoResults = null;

const tr = (key, fallback = '') => {
  if (window.voiceI18n && typeof window.voiceI18n.t === 'function') {
    return window.voiceI18n.t(key, fallback);
  }
  return fallback || key;
};

if (statusEl) {
  statusEl.style.display = 'none';
}

function _ensureLiveTurn() {
  if (!turnsEl) return null;
  if (_liveTurnEl && _liveTurnEl.isConnected) return _liveTurnEl;
  const div = document.createElement('div');
  div.className = 'turn turn-live';
  div.innerHTML = `<div class="turn-src">🧾 ${tr('realTimeStatus')}</div><div class="turn-txt"></div>`;
  turnsEl.prepend(div);
  _liveTurnEl = div;
  return div;
}

function setStatusText(text) {
  const live = _ensureLiveTurn();
  if (!live) return;
  const textEl = live.querySelector('.turn-txt');
  if (textEl) {
    const display = localizeDisplayText(text || '');
    textEl.textContent = display;
    textEl.dataset.rawText = text || '';
  }
  if (statusEl) {
    statusEl.textContent = localizeDisplayText(text || '');
  }
}

window.__voiceSetStatus = setStatusText;

let _statusRenderer = null;

function setStatus(build) {
  _statusRenderer = build;
  setStatusText(build());
}

function apiUrl(path) {
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'v=' + encodeURIComponent(appVersion);
}

function setBusy(busy) {
  _taskBusy = busy;
  document.querySelectorAll('.qa').forEach((el) => {
    el.disabled = busy;
  });
  if (voiceBtn) {
    voiceBtn.disabled = busy;
    voiceBtn.textContent = busy ? tr('voiceBtnBusy') : tr('voiceBtnStart');
  }
  if (textBtn) {
    textBtn.disabled = busy;
  }
  if (textInput) {
    textInput.disabled = busy;
  }
}

function clearRedirectHint() {
  if (!redirectHintEl) return;
  redirectHintEl.style.display = 'none';
  redirectHintEl.innerHTML = '';
}

function showRedirectHint(url) {
  if (!redirectHintEl || !url) return;
  const safeUrl = String(url).trim();
  if (!safeUrl) return;
  redirectHintEl.innerHTML = `${tr('openResult')} <a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${safeUrl}</a>`;
  redirectHintEl.style.display = 'block';
}

function nasFileBaseUrl() {
  const override = (window.localStorage && window.localStorage.getItem('nas_file_base_url')) || '';
  if (override) return override.replace(/\/$/, '');
  const host = window.location.hostname || '127.0.0.1';
  return `${window.location.protocol}//${host}:28085`;
}

function buildNasFileUrl(relPath) {
  const path = String(relPath || '').replace(/^\/+/, '');
  if (!path) return nasFileBaseUrl();
  const encoded = path.split('/').map((seg) => encodeURIComponent(seg)).join('/');
  return `${nasFileBaseUrl()}/#/files/${encoded}`;
}

function closePhotoModal() {
  if (!photoModalEl) return;
  photoModalEl.classList.remove('show');
  photoModalEl.setAttribute('aria-hidden', 'true');
}

function showPhotoModal() {
  if (!photoModalEl) return;
  photoModalEl.classList.add('show');
  photoModalEl.setAttribute('aria-hidden', 'false');
}

function photoSemanticLabel(results) {
  const keys = {
    seaside: 'photoSemanticSeaside',
    animals: 'photoSemanticAnimals',
    vintage: 'photoSemanticVintage',
  };
  const key = results && keys[results.preset];
  return key ? tr(key) : ((results && results.semantic) ? String(results.semantic) : tr('photoResultTitle'));
}

function renderPhotoResults(results) {
  if (!photoGridEl || !photoEmptyEl || !photoModalTitleEl) return;
  _photoResults = results || { items: [] };
  const items = (results && Array.isArray(results.items)) ? results.items : [];
  const semantic = photoSemanticLabel(results);
  photoModalTitleEl.textContent = `${tr('photoResultTitle')} · ${semantic}`;
  photoGridEl.innerHTML = '';

  if (!items.length) {
    photoEmptyEl.style.display = 'block';
    showPhotoModal();
    return;
  }

  photoEmptyEl.style.display = 'none';
  const escapeHtml = (value) => String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  items.forEach((it) => {
    const name = it.name || it.path || tr('photoUnknownName');
    const path = it.path || '';
    const preview = it.preview_url || '';
    const openUrl = buildNasFileUrl(path);
    const card = document.createElement('article');
    card.className = 'photo-item';
    card.innerHTML =
      `<img loading="lazy" src="${escapeHtml(preview)}" alt="${escapeHtml(name)}" />` +
      `<div class="photo-meta">` +
      `<div class="photo-name">${escapeHtml(name)}</div>` +
      `<a class="photo-open" href="${escapeHtml(openUrl)}" target="_blank" rel="noopener noreferrer">${tr('openInNasFiles')}</a>` +
      `</div>`;
    photoGridEl.appendChild(card);
  });

  showPhotoModal();
}

async function pollTask(taskId) {
  for (;;) {
    const resp = await fetch(apiUrl('/api/task/' + encodeURIComponent(taskId)), { cache: 'no-store' });
    const data = await resp.json();
    if (!resp.ok) {
      setStatus(() => `${tr('taskQueryFailed')}${data.error || resp.statusText}`);
      setBusy(false);
      return { ok: false, retriableBusy: false };
    }
    const task = data.task;
    if (task.status === 'error') {
      const busyConflict = task.http_status === 409 || (task.result && task.result.error === 'busy');
      if (!busyConflict) {
        setStatus(() => `${tr('processingFailed')}${task.error || tr('unknownError')}`);
        setBusy(false);
      }
      return { ok: false, retriableBusy: busyConflict, task };
    }
    if (task.status === 'done') {
      return { ok: true, task };
    }
    if (task.status === 'running') {
      const cost = task.started_ms ? Math.max(0, Date.now() - task.started_ms) : 0;
      if (cost > 0) {
        setStatus(() => `${tr('taskStart')} (${cost} ms)`);
      }
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
}

async function submitTask(payload, modeKey, retryCount = 0) {
  if (retryCount === 0) {
    clearRedirectHint();
  }
  setBusy(true);
  setStatus(() => `${tr(modeKey)} ${tr('taskQueued')}`);
  try {
    const resp = await fetch(apiUrl('/api/trigger'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      cache: 'no-store'
    });
    const data = await resp.json();
    if (!(resp.status === 202 && data.task_id)) {
      setStatus(() => `${tr('triggerFailed')}${data.error || resp.statusText}`);
      setBusy(false);
      return;
    }
    setStatus(() => `${tr('taskCreated')}${data.task_id}\n${tr('taskStart')}`);
    const polled = await pollTask(data.task_id);
    if (!polled || !polled.ok) {
      if (polled && polled.retriableBusy && retryCount < 2) {
        setStatus(() => tr('taskRetryBusy'));
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
        await submitTask(payload, modeKey, retryCount + 1);
        return;
      }
      if (polled && polled.retriableBusy) {
        setStatus(() => tr('taskBusyGiveup'));
      }
      setBusy(false);
      return;
    }

    const doneTask = polled.task || {};
    const doneCost = Number.isFinite(doneTask.cost_ms) ? `${tr('taskDoneWithCost')} ${doneTask.cost_ms} ms` : tr('taskDone');
    setStatus(() => doneCost);
    const redirectUrl = doneTask.result && doneTask.result.redirect_url ? String(doneTask.result.redirect_url) : '';
    if (redirectUrl) {
      showRedirectHint(redirectUrl);
    }
    const photoResults = doneTask.result && doneTask.result.photo_results;
    if (photoResults) {
      renderPhotoResults(photoResults);
    }
    setBusy(false);
  } catch (err) {
    setStatus(() => `${tr('requestFailed')}${err}`);
    setBusy(false);
  }
}

async function triggerVoice() {
  await submitTask({}, 'voiceMode');
}

async function triggerText() {
  const text = textInput ? textInput.value.trim() : '';
  if (!text) {
    setStatus(() => tr('pleaseInputText'));
    return;
  }
  await submitTask({ text }, 'textMode');
}

if (voiceBtn) {
  voiceBtn.addEventListener('click', triggerVoice);
}
if (textBtn) {
  textBtn.addEventListener('click', triggerText);
}
if (textInput) {
  textInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') triggerText();
  });
}

let lastTurnTs = Date.now() / 1000 - 3600;
const srcLabels = () => ({
  wake: tr('sourceWake'),
  button: tr('sourceButton'),
  text: tr('sourceText'),
});

function localizeDisplayText(text) {
  if (typeof text !== 'string' || !text.trim()) return text;
  if (!window.voiceI18n || typeof window.voiceI18n.getLang !== 'function') return text;
  if (window.voiceI18n.getLang() !== 'en') return text;

  let value = text;
  const categoryMap = {
    风景: 'scenery',
    美食: 'food',
    人物: 'people',
    动物: 'animals',
    建筑: 'architecture',
    交通工具: 'transportation',
    日常用品: 'daily items',
    植物: 'plants',
    家庭相册: 'Family album',
    手机相册: 'Phone album',
    旅行: 'Trip',
    备份: 'Backup',
  };

  const naturalSummaryPattern = /^(.*?)(?:\s*[:：]\s*)?((?:[^,]+,\s*)+[^,]+)$/;

  const replacements = [
    ['正在播放：', 'Playing: '],
    ['下载已完成，已保存到', 'Download complete. Saved to '],
    ['，文件名', ', file name '],
    ['文件名', 'file name '],
    ['家庭相册里有这些文件：', 'Family album contains these files: '],
    ['手机相册里有这些文件：', 'Phone album contains these files: '],
    ['旅行里有这些文件：', 'Trip folder contains these files: '],
    ['备份里有这些文件：', 'Backup folder contains these files: '],
    ['预览结果：', 'Preview result: '],
    ['已是最新，跳过', ' is already up to date; skipped '],
    ['无需重复处理。', ' no duplicate processing is required.'],
    ['知识库中未找到与"', 'No relevant result found for "'],
    ['"相关的内容', '".'],
    ['找到了，', 'Found: '],
    ['在文档文件夹里。', ' in the documents folder.'],
    ['在文档/文件夹里。', ' in the documents folder.'],
    ['第一个是', 'First is '],
    ['，在', ', in '],
    ['未做任何改动。', 'No changes were made.'],
    ['未做任何改动', 'No changes were made'],
    ['。', '.'],
    ['，', ', '],
  ];

  Object.entries(categoryMap).forEach(([source, target]) => {
    value = value.split(source).join(target);
  });

  replacements.forEach(([source, target]) => {
    value = value.split(source).join(target);
  });

  // Avoid broad "N张 -> N images" because it breaks phrases like "第1张".
  value = value.replace(/找到(\d+)张和(.+?)相关的照片，比如：/g, 'Found $1 photos related to $2, for example: ');
  value = value.replace(/找到1张(.+?)照片：/g, 'Found one photo matching $1: ');
  value = value.replace(/第(\d+)张/g, 'photo $1');
  value = value.replace(/\b(scenery|food|people|animals|architecture|transportation|daily items|plants)(\d+)张/gi, '$1: $2');
  value = value.replace(/\b(scenery|food|people|animals|architecture|transportation|daily items|plants)\s+(\d+)\s+images/gi, '$1: $2');
  value = value.replace(/\b(Family album|Phone album|Trip|Backup)\s+([A-Za-z ]+)/g, '$1: $2');

  if (naturalSummaryPattern.test(value)) {
    const m = value.match(naturalSummaryPattern);
    const prefix = (m && m[1] && !m[1].includes('Preview result')) ? m[1] : 'Preview result:';
    const summary = (m && m[2]) ? m[2] : value;
    const normalizedSummary = summary
      .replace(/\s*:\s*/g, ': ')
      .replace(/\s*,\s*/g, ', ')
      .replace(/\s+([a-zA-Z]+)/g, ' $1');
    if (normalizedSummary.includes(':')) {
      value = `${prefix} ${normalizedSummary}.`;
    }
  }

  value = value.replace(/\s+,\s+/g, ', ');
  value = value.replace(/\s+\./g, '.');

  return value;
}

function renderNewTurns(turns) {
  if (!turns || !turns.length || !turnsEl) return;
  const live = (_liveTurnEl && _liveTurnEl.isConnected) ? _liveTurnEl : null;
  const labels = srcLabels();
  const escapeHtml = (value) => String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
  turns.forEach((turn) => {
    const div = document.createElement('div');
    div.className = 'turn';
    const src = labels[turn.source] || turn.source;
    const locale = (window.voiceI18n && window.voiceI18n.getLang && window.voiceI18n.getLang() === 'en') ? 'en-US' : 'zh-CN';
    const hhmm = new Date(turn.ts * 1000).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const costText = `${tr('turnCost')} ${turn.cost_ms} ms`;
    const textContent = turn.text ? localizeDisplayText(turn.text) : '';
    const replyContent = turn.reply ? localizeDisplayText(turn.reply) : '';
    div.innerHTML =
      `<div class="turn-src"><span class="turn-src-label" data-src-key="${turn.source}">${src}</span> · ${hhmm}</div>` +
      (turn.text ? `<div class="turn-txt" data-raw-text="${escapeHtml(turn.text)}">🗣 ${escapeHtml(textContent)}</div>` : '') +
      (turn.reply ? `<div class="turn-rep" data-raw-text="${escapeHtml(turn.reply)}">💬 ${escapeHtml(replyContent)}</div>` : '') +
      `<div class="turn-meta" data-cost-ms="${turn.cost_ms}">${costText}</div>`;
    if (live) {
      turnsEl.insertBefore(div, live.nextSibling);
    } else {
      turnsEl.prepend(div);
    }
    if (turn.ts > lastTurnTs) lastTurnTs = turn.ts;
  });
  turnsEl.scrollTop = 0;
}

async function fetchTurns() {
  if (document.hidden) return;
  try {
    const resp = await fetch(apiUrl('/api/turns?since=' + lastTurnTs), { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    renderNewTurns(data.turns);
  } catch (_) {}
}

function refreshDynamicI18n() {
  const labels = srcLabels();
  document.querySelectorAll('.turn-src-label[data-src-key]').forEach((el) => {
    const val = labels[el.dataset.srcKey];
    if (val !== undefined) el.textContent = val;
  });
  document.querySelectorAll('.turn-meta[data-cost-ms]').forEach((el) => {
    el.textContent = `${tr('turnCost')} ${el.dataset.costMs} ms`;
  });
  document.querySelectorAll('.turn-live .turn-src').forEach((el) => {
    el.textContent = `🧾 ${tr('realTimeStatus')}`;
  });
  document.querySelectorAll('.turn-txt, .turn-rep').forEach((el) => {
    const raw = el.dataset.rawText || '';
    if (!raw) return;
    const prefix = el.classList.contains('turn-txt') ? '🗣 ' : '💬 ';
    el.textContent = prefix + localizeDisplayText(raw);
  });
  if (_statusRenderer) setStatusText(_statusRenderer());
}

if (clearTurnsBtn) {
  clearTurnsBtn.addEventListener('click', () => {
    if (turnsEl) {
      turnsEl.innerHTML = '';
    }
    _liveTurnEl = null;
    lastTurnTs = Date.now() / 1000;
    clearRedirectHint();
    setStatus(() => tr('statusIdle'));
  });
}

setInterval(fetchTurns, 1500);
fetchTurns();
setStatus(() => tr('statusIdle'));

const STATE_LABELS = {
  awake: 'stateAwake',
  listening: 'stateListening',
  asr: 'stateAsr',
  processing: 'stateProcessing',
  speaking: 'stateSpeaking',
};

let _lastBridgeState = 'idle';

function _applyBridgeState(state, busy) {
  const active = _taskBusy || busy || (state !== 'idle');
  if (voiceBtn) {
    voiceBtn.disabled = active;
    voiceBtn.textContent = STATE_LABELS[state] ? tr(STATE_LABELS[state]) : (active ? tr('voiceBtnBusy') : tr('voiceBtnStart'));
  }
  if (textBtn) {
    textBtn.disabled = active;
  }
  if (textInput) {
    textInput.disabled = active;
  }
  if (state !== 'idle') {
    setStatus(() => STATE_LABELS[state] ? tr(STATE_LABELS[state]) : `Voice Assistant status: ${state}`);
  } else if (_lastBridgeState !== 'idle') {
    setStatus(() => tr('statusIdle'));
  }
  _lastBridgeState = state;
}

async function pollBridgeStatus() {
  try {
    const resp = await fetch(apiUrl('/api/status'), { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    _applyBridgeState(data.state || 'idle', !!data.busy);
  } catch (_) {}
}

setInterval(pollBridgeStatus, 800);
pollBridgeStatus();

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  fetchTurns();
  pollBridgeStatus();
});

document.addEventListener('voice:lang-change', () => {
  if (langToggle && window.voiceI18n && typeof window.voiceI18n.getLang === 'function') {
    const nextLang = window.voiceI18n.getLang();
    langToggle.textContent = nextLang === 'en' ? '中文' : 'English';
  }
  refreshDynamicI18n();
  refreshQaLabels();
  if (_photoResults && photoModalEl && photoModalEl.classList.contains('show')) {
    renderPhotoResults(_photoResults);
  }
});

function currentLang() {
  return (window.voiceI18n && typeof window.voiceI18n.getLang === 'function')
    ? window.voiceI18n.getLang() : 'zh';
}

function qaPromptFor(el) {
  const isEn = currentLang() === 'en';
  const prompt = isEn ? el.dataset.promptEn : el.dataset.promptZh;
  return (prompt || el.dataset.promptZh || '').trim();
}

function refreshQaLabels() {
  const isEn = currentLang() === 'en';
  document.querySelectorAll('.qa[data-label-zh]').forEach((el) => {
    const label = isEn ? (el.dataset.labelEn || el.dataset.labelZh) : el.dataset.labelZh;
    if (label) el.textContent = label;
  });
}

function bindQuickActions() {
  document.querySelectorAll('.qa').forEach((el) => {
    el.addEventListener('click', async () => {
      if (_taskBusy) return;
      const prompt = qaPromptFor(el);
      if (!prompt) return;
      if (textInput) textInput.value = prompt;
      await submitTask({ text: prompt }, 'textMode');
    });
  });
}

bindQuickActions();
refreshQaLabels();

if (photoModalCloseEl) {
  photoModalCloseEl.addEventListener('click', closePhotoModal);
}
if (photoModalEl) {
  photoModalEl.addEventListener('click', (event) => {
    if (event.target === photoModalEl) closePhotoModal();
  });
}
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closePhotoModal();
});

window.__voiceUiLoaded = true;
