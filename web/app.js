const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let currentDocuments = [];
let onlyRegulatory = false;
let answerMode = 'quick';
let toastTimer;

function setAnswerMode(mode) {
  if (!['quick', 'thorough'].includes(mode)) return;
  answerMode = mode;
  $$('[data-answer-mode]').forEach((button) => {
    const selected = button.dataset.answerMode === mode;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
  $('#answer-mode-note').textContent = mode === 'thorough'
    ? 'qwen3.5:35b-a3b · larger model'
    : 'qwen3.5:4b · preloaded';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function highlightText(value, terms = []) {
  const text = String(value ?? '');
  const wanted = new Set((terms || []).map((term) => String(term).toLowerCase()));
  if (!wanted.size) return escapeHtml(text);
  const tokenPattern = /[\p{L}\p{N}_]+(?:[-./][\p{L}\p{N}_]+)*/gu;
  let html = ''; let cursor = 0;
  for (const match of text.matchAll(tokenPattern)) {
    if (!wanted.has(match[0].toLowerCase())) continue;
    html += escapeHtml(text.slice(cursor, match.index));
    html += `<mark class="passage-match">${escapeHtml(match[0])}</mark>`;
    cursor = match.index + match[0].length;
  }
  return html + escapeHtml(text.slice(cursor));
}
function sourceAction(path, extension, page) {
  const ext = String(extension || '').toLowerCase();
  if (ext === '.pdf') {
    const pageNumber = Number(page);
    const fragment = Number.isInteger(pageNumber) && pageNumber > 0 ? `#page=${pageNumber}` : '';
    const href = `/api/open?path=${encodeURIComponent(path)}${fragment}`;
    return `<a class="open-source" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">Open PDF ↗</a>`;
  }
  return `<button class="open-source" type="button" data-path="${escapeHtml(path)}">Open in default app ↗</button>`;
}
function passageLocation(extension, page) {
  if (!page) return 'Location in source file';
  const ext = String(extension || '').toLowerCase();
  const label = ext === '.pptx' ? 'Slide' : ext === '.xlsx' ? 'Sheet' : 'Page';
  return `${label} ${page}`;
}
function toast(message) {
  const node = $('#toast'); node.textContent = message; node.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.remove('show'), 2100);
}
function formatBytes(bytes) {
  if (!bytes) return '—';
  const units = ['B','KB','MB','GB','TB']; let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${units[i]}`;
}
function isRegulatory(type) { return /^(FAA |EASA |AESA |CAAC |TCCA )/.test(type || ''); }
function fileGlyph(ext) {
  const normalized = (ext || '').replace('.', '').toUpperCase().slice(0,4) || 'DOC';
  return `<span class="file-glyph ${normalized.toLowerCase()}">${escapeHtml(normalized)}</span>`;
}
function switchView(view) {
  $$('.view').forEach((node) => node.classList.toggle('active', node.id === `view-${view}`));
  $$('.nav-item').forEach((node) => node.classList.toggle('active', node.dataset.view === view));
  $('#breadcrumb-current').textContent = view === 'assistant' ? 'ASSISTANT' : view === 'library' ? 'LIBRARY' : 'SEARCH';
  if (view === 'library') loadLibrary();
  if (view === 'assistant') setTimeout(() => $('#ask-question').focus(), 50);
  if (view === 'search') setTimeout(() => $('#search-query').focus(), 50);
}
async function api(url, options = {}) {
  const response = await fetch(url, {headers:{'Content-Type':'application/json'}, ...options});
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}
async function refreshStatus() {
  try {
    const data = await api('/api/status');
    $('#stat-docs').textContent = Number(data.stats.documents || 0).toLocaleString();
    $('#stat-chunks').textContent = Number(data.stats.chunks || 0).toLocaleString();
    if (data.settings.corpus_dir && document.activeElement !== $('#corpus-path')) $('#corpus-path').value = data.settings.corpus_dir;
    const label = data.llm.provider === 'ollama' ? `Ollama · ${data.llm.model}` : `API model · ${data.llm.model}`;
    $('#model-label').textContent = label;
    $('#assistant-model-chip').textContent = data.llm.provider === 'ollama' ? 'LOCAL MODEL' : 'API MODEL';
    renderIndexState(data.index);
  } catch (error) { $('#model-label').textContent = 'Local search'; }
}
function renderIndexState(state) {
  const button = $('#index-button');
  if (state.running) {
    button.disabled = true; button.innerHTML = '<span class="index-icon">◌</span> Indexing…';
    $('#index-progress').classList.remove('hidden');
    $('#progress-label').textContent = state.phase === 'scanning' ? 'Scanning folder…' : 'Indexing files…';
    $('#progress-count').textContent = `${Number(state.processed).toLocaleString()} / ${Number(state.total).toLocaleString()}`;
    const ratio = state.total ? Math.min(100, state.processed / state.total * 100) : 4;
    $('#progress-fill').style.width = `${ratio}%`;
    $('#progress-current').textContent = state.current || state.message || '';
  } else {
    button.disabled = false; button.innerHTML = '<span class="index-icon">↻</span> Index this folder';
    if (state.finished_at) {
      $('#index-progress').classList.remove('hidden'); $('#progress-label').textContent = state.message || 'Index run complete';
      $('#progress-count').textContent = `${Number(state.indexed).toLocaleString()} updated · ${Number(state.unchanged).toLocaleString()} unchanged`;
      $('#progress-fill').style.width = state.phase === 'error' ? '0%' : '100%';
      $('#progress-current').textContent = `${Number(state.errors).toLocaleString()} errors · ${Number(state.skipped).toLocaleString()} skipped`;
    }
  }
}
async function startIndex() {
  const corpusDir = $('#corpus-path').value.trim();
  if (!corpusDir) { toast('Enter a folder path first.'); $('#corpus-path').focus(); return; }
  try {
    await api('/api/settings', {method:'POST', body:JSON.stringify({corpus_dir:corpusDir})});
    await api('/api/index', {method:'POST', body:JSON.stringify({corpus_dir:corpusDir})});
    toast('Index run started. You can keep searching while it runs.'); await refreshStatus();
    switchView('search');
  } catch (error) { toast(error.message); }
}
function renderSearch(data) {
  currentDocuments = data.documents || [];
  const visible = onlyRegulatory ? currentDocuments.filter((doc) => isRegulatory(doc.doc_type)) : currentDocuments;
  const target = $('#search-output');
  const regCount = currentDocuments.filter((doc) => isRegulatory(doc.doc_type)).length;
  const heading = `<div class="results-heading"><div><h2>Documents found</h2><span>${visible.length} ${visible.length === 1 ? 'document' : 'documents'} · ranked by matching passages</span></div><button class="filter-toggle ${onlyRegulatory ? 'active' : ''}" id="reg-filter">${onlyRegulatory ? '✓ ' : ''}Regulatory references${regCount ? ` · ${regCount}` : ''}</button></div>`;
  if (!currentDocuments.length) {
    target.innerHTML = `<div class="no-results">No matching passages in this index. Try a shorter part number, manual name, ATA term, or CFR citation.</div>`;
    $('#search-message').textContent = ''; return;
  }
  const cards = visible.map((doc) => {
    const typeClass = isRegulatory(doc.doc_type) ? 'regulatory' : '';
    const pageText = doc.pages.length ? `${doc.location_label} ${doc.pages.slice(0,5).join(', ')}` : 'Passage match';
    const passages = doc.passages?.length ? doc.passages : (doc.excerpts || []).map((excerpt) => ({excerpt, page: null}));
    const passageMarkup = passages.map((passage) => `<div class="result-passage"><div class="result-excerpt">${highlightText(passage.excerpt, data.highlight_terms)}</div><div class="passage-footer"><span>${escapeHtml(passageLocation(doc.extension, passage.page))}</span>${sourceAction(doc.path, doc.extension, passage.page)}</div></div>`).join('');
    return `<article class="result-card"><div class="result-card-header">${fileGlyph(doc.extension)}<div class="result-title"><strong>${escapeHtml(doc.name)}</strong><div class="result-meta"><span class="type-tag ${typeClass}">${escapeHtml(doc.doc_type)}</span><span>Updated ${escapeHtml(doc.modified)}</span><span>${escapeHtml(pageText)}</span></div></div></div>${passageMarkup}<div class="result-footer"><span class="result-location" title="${escapeHtml(doc.path)}">${escapeHtml(doc.path)}</span><button class="copy-path" data-path="${escapeHtml(doc.path)}">Copy path ⧉</button></div></article>`;
  }).join('');
  const regNotice = regCount ? `<div class="regulatory-band"><span>⌖</span><strong>${regCount} related regulatory references</strong><span>in the results below</span></div>` : '';
  target.innerHTML = `${heading}${regNotice}${visible.length ? cards : '<div class="no-results">No regulatory documents matched this query. Clear the filter to view all results.</div>'}`;
  $('#search-message').textContent = '';
  $('#reg-filter')?.addEventListener('click', () => { onlyRegulatory = !onlyRegulatory; renderSearch(data); });
  $$('.copy-path').forEach((button) => button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(button.dataset.path); toast('Source path copied.'); }
    catch { toast(button.dataset.path); }
  }));
}
async function search() {
  const query = $('#search-query').value.trim();
  if (query.length < 2) { $('#search-message').textContent = 'Enter at least two characters.'; return; }
  $('#search-button').disabled = true; $('#search-button').innerHTML = 'Searching <span>◌</span>';
  $('#search-output').innerHTML = '<div class="no-results">Searching the local index…</div>';
  try {
    const data = await api(`/api/search?query=${encodeURIComponent(query)}&limit=20`);
    renderSearch(data);
  } catch (error) { $('#search-output').innerHTML = `<div class="no-results">${escapeHtml(error.message)}</div>`; }
  finally { $('#search-button').disabled = false; $('#search-button').innerHTML = 'Search <span>↗</span>'; }
}
function formatAnswer(text, citationCheck) {
  const flags = new Map((citationCheck?.uncited_sentences || []).map((item) => [item.index, item]));
  let sentenceIndex = 0;
  return String(text).split(/\n{2,}/).map((paragraph) => {
    const sentences = paragraph.split(/(?<=[.!?])\s+|\n+/).filter((sentence) => sentence.trim());
    const rendered = sentences.map((sentence) => {
      const flag = flags.get(sentenceIndex++);
      let html = escapeHtml(sentence).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/\[(S\d+)\]/g,'<span class="inline-citation">[$1]</span>');
      if (flag) {
        const label = flag.reason === 'unknown_source_id' ? 'Check source ID' : 'Uncited';
        html = `<span class="uncited-sentence"><small class="uncited-label">${label}</small>${html}</span>`;
      }
      return html;
    }).join(' ');
    return rendered ? `<p>${rendered}</p>` : '';
  }).join('');
}
function renderAnswer(data) {
  const thread = $('#chat-thread');
  const question = `<div class="chat-turn"><div class="assistant-avatar" style="background:#e8ece8;color:#63796f">↗</div><div style="flex:1"><span class="message-author">YOU <small>· QUESTION</small></span><div class="user-bubble">${escapeHtml(data.question)}</div></div></div>`;
  const sources = data.sources.length ? `<div class="answer-sources"><strong>SOURCE PASSAGES</strong>${data.sources.map((s) => `<div class="source-chip"><span class="source-id">${escapeHtml(s.id)}</span><div class="source-chip-content"><strong>${escapeHtml(s.name)} · ${escapeHtml(s.doc_type)}${s.caution_source ? ' · CAUTION CHECK' : ''}</strong><small>${escapeHtml(passageLocation(s.extension, s.page))} · ${escapeHtml(s.path)}</small><div class="source-excerpt">${highlightText(s.excerpt, data.highlight_terms)}</div><div class="source-actions">${sourceAction(s.path, s.extension, s.page)}</div></div></div>`).join('')}</div>` : '';
  const regulatory = data.regulatory_refs?.length ? `<div class="reg-ref-box"><strong>RELATED REGULATORY REFERENCES</strong>${data.regulatory_refs.map((s) => `<div>↳ ${escapeHtml(s.name)} · ${escapeHtml(s.doc_type)} <span class="inline-citation">[${escapeHtml(s.id)}]</span></div>`).join('')}</div>` : '';
  const error = data.model_error ? `<div class="answer-error">Model status: ${escapeHtml(data.model_error)}</div>` : '';
  const modeLabel = data.mode === 'thorough' ? 'THOROUGH' : 'QUICK';
  const answerLabel = data.streaming ? `${modeLabel} · STREAMING` : data.model_used ? `${modeLabel} ANSWER` : 'SOURCE RETRIEVAL';
  const answer = `<div class="chat-turn"><div class="assistant-avatar">H</div><div style="flex:1"><span class="message-author">HANGARINDEX ASSISTANT <small>· ${answerLabel}</small></span><div class="answer-body">${formatAnswer(data.answer, data.citation_check)}</div>${error}${sources}${regulatory}</div></div>`;
  thread.insertAdjacentHTML('beforeend', question + answer);
  thread.scrollTop = thread.scrollHeight;
  window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'});
}
async function askQuestion(question) {
  const trimmed = question.trim(); if (trimmed.length < 2) return;
  const selectedMode = answerMode;
  $('#ask-button').disabled = true; $('#ask-button').innerHTML = 'Looking up sources <span>◌</span>';
  const pending = document.createElement('div'); pending.className='no-results'; pending.textContent='Searching relevant passages and preparing cited answer…'; $('#chat-thread').append(pending);
  let answerBody = null; let streamedAnswer = ''; let completed = false;
  const showError = (message) => {
    pending.remove();
    if (!answerBody) { toast(message); return; }
    const turn = answerBody.closest('.chat-turn');
    const author = turn?.querySelector('.message-author small');
    if (author) author.textContent = '· SOURCE RETRIEVAL';
    if (!streamedAnswer) answerBody.textContent = 'The model did not finish a grounded answer. Review the source passages below.';
    let errorNode = turn?.querySelector('.answer-error');
    if (!errorNode && turn) { errorNode = document.createElement('div'); errorNode.className = 'answer-error'; answerBody.insertAdjacentElement('afterend', errorNode); }
    if (errorNode) errorNode.textContent = `Model status: ${message}`;
  };
  const handleEvent = (event) => {
    if (event.type === 'meta') {
      pending.remove();
      renderAnswer({...event.data, answer:event.data.answer || '', streaming:!!event.data.streaming});
      if (event.data.streaming) {
        const turns = $$('#chat-thread .chat-turn');
        answerBody = turns.at(-1)?.querySelector('.answer-body') || null;
        if (answerBody) answerBody.style.whiteSpace = 'pre-wrap';
      } else {
        completed = true;
      }
    } else if (event.type === 'token') {
      streamedAnswer += event.text || '';
      if (answerBody && event.text) answerBody.appendChild(document.createTextNode(event.text));
    } else if (event.type === 'done') {
      completed = true;
      if (answerBody) {
        answerBody.style.whiteSpace = '';
        answerBody.innerHTML = formatAnswer(streamedAnswer, event.citation_check);
        const author = answerBody.closest('.chat-turn')?.querySelector('.message-author small');
        if (author) author.textContent = event.model_used === false ? '· SOURCE RETRIEVAL' : `· ${event.mode === 'thorough' ? 'THOROUGH' : 'QUICK'} ANSWER`;
      }
      $('#ask-question').value = '';
    } else if (event.type === 'error') {
      completed = true;
      showError(event.detail || 'The model stream failed.');
    }
  };
  try {
    const response = await fetch('/api/ask/stream', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question:trimmed, mode:selectedMode})});
    if (!response.ok) {
      let data = {}; try { data = await response.json(); } catch {}
      throw new Error(data.detail || `Request failed (${response.status})`);
    }
    if (!response.body) throw new Error('This browser does not support streaming responses.');
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream:!done});
      const blocks = buffer.split(/\r?\n\r?\n/); buffer = blocks.pop() || '';
      for (const block of blocks) {
        const dataLine = block.split(/\r?\n/).find((line) => line.startsWith('data:'));
        if (dataLine) handleEvent(JSON.parse(dataLine.slice(5).trim()));
      }
      if (done) break;
    }
    if (!completed) throw new Error('The answer stream ended before its completion event.');
  }
  catch (error) { showError(error.message); }
  finally { pending.remove(); $('#ask-button').disabled = false; $('#ask-button').innerHTML = 'Ask assistant <span>↗</span>'; $('#ask-question').focus(); }
}
async function loadLibrary() {
  try {
    const data = await api('/api/library?limit=300');
    $('#library-total').textContent = `${Number(data.count).toLocaleString()} FILES`;
    currentLibrary = data.documents || []; filterLibrary();
  } catch (error) { $('#library-body').innerHTML = `<tr><td colspan="5" class="empty-table">${escapeHtml(error.message)}</td></tr>`; }
}
let currentLibrary = [];
function filterLibrary() {
  const query = ($('#library-filter')?.value || '').toLowerCase();
  const rows = currentLibrary.filter((d) => `${d.name} ${d.doc_type} ${d.path}`.toLowerCase().includes(query));
  $('#library-total').textContent = `${rows.length.toLocaleString()} FILES`;
  if (!rows.length) { $('#library-body').innerHTML = '<tr><td colspan="5" class="empty-table">No indexed documents match.</td></tr>'; return; }
  $('#library-body').innerHTML = rows.map((d) => `<tr><td title="${escapeHtml(d.path)}">${escapeHtml(d.name)}</td><td class="table-type">${escapeHtml(d.doc_type)}</td><td>${escapeHtml(d.modified)}</td><td>${formatBytes(d.size)}</td><td>${d.parse_status === 'indexed' ? '<span class="status-ok"><span class="tiny-dot"></span> Indexed</span>' : `<span class="status-error" title="${escapeHtml(d.error || '')}">Needs attention</span>`}</td></tr>`).join('');
}

$$('.nav-item').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)));
$('#index-button').addEventListener('click', startIndex);
$('#search-button').addEventListener('click', search);
$('#search-query').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); search(); } });
$$('.suggestion-chips button').forEach((button) => button.addEventListener('click', () => { $('#search-query').value = button.dataset.query; search(); }));
$$('.prompt-examples button').forEach((button) => button.addEventListener('click', () => askQuestion(button.dataset.question)));
$('#ask-form').addEventListener('submit', (event) => { event.preventDefault(); askQuestion($('#ask-question').value); });
$('#refresh-library').addEventListener('click', loadLibrary);
$('#library-filter').addEventListener('input', filterLibrary);
$('#help-button').addEventListener('click', () => $('#about-dialog').showModal());
$('#about-dialog').addEventListener('click', (event) => { if (event.target === $('#about-dialog')) $('#about-dialog').close(); });
$$('[data-answer-mode]').forEach((button) => button.addEventListener('click', () => setAnswerMode(button.dataset.answerMode)));
document.addEventListener('click', async (event) => {
  const button = event.target instanceof Element ? event.target.closest('button.open-source') : null;
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    await api('/api/open', {method:'POST', body:JSON.stringify({path:button.dataset.path})});
    toast('Opened in the default application.');
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
document.addEventListener('keydown', (event) => { if (event.key === '/' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) { event.preventDefault(); switchView('search'); $('#search-query').focus(); } });
refreshStatus();
setInterval(refreshStatus, 2500);
