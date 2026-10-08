// HangarIndex static demo: Ask tab. Talks to the owner's live assistant when config.json has an apiBase and
// the PC is reachable; otherwise falls back to the saved (pre-generated) answers shipped with the site.
(function () {
  'use strict';
  const CODE_KEY = 'hangarindex.accessCode';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const highlight = (text, terms) => (window.HangarHighlight ? window.HangarHighlight(text, terms) : esc(text));
  const centerExcerpt = (text, terms) => (window.HangarCenteredExcerpt ? window.HangarCenteredExcerpt(text, terms) : String(text ?? ''));
  const store = {
    get() { try { return localStorage.getItem(CODE_KEY) || ''; } catch (e) { return ''; } },
    set(v) { try { v ? localStorage.setItem(CODE_KEY, v) : localStorage.removeItem(CODE_KEY); } catch (e) { /* private mode */ } },
  };

  let apiBase = '';
  let online = false;
  let docsByName = new Map();
  let savedAnswers = [];
  let busy = false;

  const SUGGESTED = [
    'Can I strip paint off a high-strength steel landing gear part the same way as an aluminium panel?',
    'Our stainless screws keep eating the aluminium around them: why, and what do we change?',
    'Do EASA and the FAA agree on who can sign off a component after repair?',
    'If we ship a repaired part to a customer in China, what do we need from CAAC and from export control?',
    'What will an auditor expect our internal audit program and quality manual to cover?',
  ];

  function setStatus(state, text) {
    const el = $('ask-status');
    el.dataset.state = state;
    el.querySelector('span').textContent = text;
  }

  function localPreviewApiBase() { return ''; }

  async function loadData() {
    const [cfg, docs, answers] = await Promise.all([
      fetch('config.json', {cache: 'no-store'}).then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
      fetch('data/docs.json').then((r) => r.json()).catch(() => []),
      fetch('data/answers.json').then((r) => r.json()).catch(() => []),
    ]);
    const previewBase = localPreviewApiBase();
    apiBase = previewBase || String(cfg.apiBase || '').replace(/\/+$/, '');
    if (apiBase && !previewBase && !/^https:\/\//i.test(apiBase)) apiBase = '';  // never send the access code over plain http
    (Array.isArray(docs) ? docs : docs.documents || []).forEach((d) => docsByName.set(String(d.filename).toLowerCase(), d));
    savedAnswers = Array.isArray(answers) ? answers : [];
  }

  function showOffline(message) {
    online = false;
    setStatus('offline', 'Assistant offline');
    const panel = $('ask-offline');
    panel.querySelector('p').textContent = message;
    const category = $('ask-category')?.value || 'All';
    const visibleAnswers = savedAnswers.map((a, i) => [a, i])
      .filter(([a]) => answerHasCategory(a, category)).slice(0, 12);
    if (!visibleAnswers.length) {
      panel.querySelector('p').textContent = category === 'All'
        ? 'No saved answers are available in this copy of the demo. Use Search to open matching source passages.'
        : 'No saved answers are available in this category. Use Search to open matching source passages.';
    }
    panel.querySelector('ul').innerHTML = visibleAnswers
      .map(([a, i]) => `<li><button type="button" data-saved="${i}">${esc(a.query)}</button></li>`).join('');
    panel.hidden = false;
  }

  function answerHasCategory(answer, category) {
    return category === 'All' || (answer.sources || []).some((source) => {
      const name = String(source.doc_name || source.filename || '').toLowerCase();
      return docsByName.get(name)?.category === category;
    });
  }

  function needCode() {
    online = false;
    setStatus('locked', 'Access code needed');
    $('ask-access').hidden = false;
    $('ask-access').open = true;
  }

  async function checkOnline() {
    $('ask-offline').hidden = true;
    if (!apiBase) { showOffline('This copy of the site is not connected to a live assistant. Saved answers from earlier runs are below.'); return; }
    setStatus('checking', 'Checking assistant…');
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 6000);
      const r = await fetch(apiBase + '/api/health', {headers: authHeaders(), signal: ctrl.signal,
        mode: localPreviewApiBase() ? 'same-origin' : 'cors', cache: 'no-store'});
      clearTimeout(t);
      if (r.status === 401) { needCode(); return; }
      const body = r.ok ? await r.json().catch(() => ({})) : {};
      const up = r.ok && !['offline', 'off', 'disabled'].includes(String(body.status || body.state || '').toLowerCase());
      if (!up) { showOffline('The assistant server is not running right now. Saved answers from earlier runs are below.'); return; }
      online = true;
      setStatus('online', 'Live assistant online');
    } catch (e) {
      showOffline('The assistant server is not running right now. Saved answers from earlier runs are below.');
    }
  }

  function authHeaders() {
    const code = store.get();
    return code ? {Authorization: 'Bearer ' + code} : {};
  }

  // Map a source returned by the API (doc id, name or path) to the hosted copy or the official link.
  function sourceLink(src) {
    const raw = String(src.doc_id || src.name || src.doc_name || src.path || '');
    const base = raw.split(/[\\/]/).pop().toLowerCase();
    const doc = docsByName.get(base);
    if (!doc) return {href: '', label: src.name || base, hosted: false, doc: null};
    const page = Number(src.page);
    const frag = Number.isInteger(page) && page > 0 ? '#page=' + page : '';
    if (doc.redistribute === 'yes') return {href: `docs/${encodeURI(doc.subfolder)}/${encodeURI(doc.filename)}${frag}`, label: doc.title || doc.filename, hosted: true, doc};
    return {href: doc.officialUrl || '', label: doc.title || doc.filename, hosted: false, doc};
  }

  function renderAnswerText(text, sources) {
    const ids = new Map(sources.map((s) => [s.id, s]));
    const blocks = String(text).split(/\n{2,}/);
    return blocks.map((block) => {
      const heading = block.match(/^(?:[-*+]\s+)?(?:#{1,3}\s*)?(?:\*\*)?(Direct answer|Answer|Warnings? from (?:the )?source documents?(?: on this topic)?|Watch out|Also worth knowing|Where sources differ|Not covered|Where to look)(?:\*\*)?:?(?:\*\*)?\s*/i);
      let html = esc(block);
      if (heading) {
        const rawLabel = heading[1];
        const label = /^answer$/i.test(rawLabel) ? 'Direct answer'
          : /^warnings?/i.test(rawLabel) ? 'Warnings from the source documents on this topic'
            : rawLabel;
        html = `<span class="ask-section">${esc(label)}</span>` + esc(block.slice(heading[0].length));
      }
      html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      html = html.replace(/\[(S\d+)\]/g, (m, id) => (ids.has(id)
        ? `<button type="button" class="ask-cite" data-cite="${id}">${id}</button>` : `<span class="ask-cite bad" title="Unknown source">${id}</span>`));
      return `<p>${html.replace(/\n/g, '<br>')}</p>`;
    }).join('');
  }

  function renderSources(sources, terms) {
    if (!sources.length) return '';
    return '<div class="ask-sources"><h3>Sources</h3>' + sources.map((s) => {
      const link = sourceLink(s);
      const where = s.page ? `Page ${esc(s.page)}` : 'Location in document';
      const open = link.href
        ? `<a href="${esc(link.href)}" target="_blank" rel="noopener">${link.hosted ? 'Open PDF' : 'Official source'} ↗</a>`
        : '<span class="muted">Not in public library</span>';
      return `<article class="ask-source" id="ask-src-${esc(s.id)}">
        <header><span class="ask-cite static">${esc(s.id)}</span><strong>${esc(link.label)}</strong>${s.caution_source ? '<em>Caution</em>' : ''}</header>
        <div class="ask-excerpt">${highlight(centerExcerpt(s.excerpt || '', terms), terms)}</div>
        <footer><span>${where}${link.hosted ? '' : ' · link only'}</span>${open}</footer></article>`;
    }).join('') + '</div>';
  }

  function bindCites(container) {
    container.querySelectorAll('[data-cite]').forEach((b) => b.addEventListener('click', () => {
      const card = $('ask-src-' + b.dataset.cite);
      if (card) { card.classList.add('flash'); card.scrollIntoView({behavior: 'smooth', block: 'center'}); setTimeout(() => card.classList.remove('flash'), 1400); }
    }));
  }

  function turnShell(question, label) {
    const turn = document.createElement('section');
    turn.className = 'ask-turn';
    turn.innerHTML = `<p class="ask-q">${esc(question)}</p><div class="ask-meta">${esc(label)}</div><div class="ask-body"><p class="ask-wait">Searching the library…</p></div>`;
    $('ask-thread').prepend(turn);
    return turn;
  }

  function overlap(a, b) {
    const words = (s) => new Set(String(s).toLowerCase().match(/[a-z0-9]{4,}/g) || []);
    const A = words(a); const B = words(b);
    let n = 0; A.forEach((w) => { if (B.has(w)) n++; });
    return n / Math.max(1, Math.min(A.size, B.size));
  }

  function answerOffline(question, turn, reason) {
    const category = $('ask-category')?.value || 'All';
    const best = savedAnswers.filter((a) => answerHasCategory(a, category))
      .map((a) => [overlap(question, a.query), a]).sort((x, y) => y[0] - x[0])[0];
    const body = turn.querySelector('.ask-body');
    const meta = turn.querySelector('.ask-meta');
    if (!best || best[0] < 0.34) {
      meta.textContent = reason;
      body.innerHTML = '<p>The live assistant is not reachable, and none of the saved answers matches this question closely. '
        + 'Try one of the suggested questions, or use Search for the source passages.</p>';
      return;
    }
    const a = best[1];
    const sources = (a.sources || []).map((s) => ({...s, name: s.doc_name}));
    meta.textContent = `${reason} · saved answer to “${a.query}” · ${a.model || a.generated_by || ''}`;
    body.innerHTML = renderAnswerText(a.answer, sources) + renderSources(sources, (a.highlight_terms || question.split(/\s+/)));
    bindCites(body);
  }

  async function answerLive(question, turn) {
    const body = turn.querySelector('.ask-body');
    const meta = turn.querySelector('.ask-meta');
    const ctrl = new AbortController();
    // Idle watchdog, not a total limit: queue and token events keep it alive, so waiting in line never times out.
    let timeout = setTimeout(() => ctrl.abort(), 120000);
    const alive = () => { clearTimeout(timeout); timeout = setTimeout(() => ctrl.abort(), 120000); };
    const r = await fetch(apiBase + '/api/ask/stream', {
      method: 'POST', mode: localPreviewApiBase() ? 'same-origin' : 'cors', signal: ctrl.signal,
      headers: {'Content-Type': 'application/json', ...authHeaders()},
      body: JSON.stringify({question, category: $('ask-category')?.value || 'All'}),
    });
    if (r.status === 401) { clearTimeout(timeout); needCode(); throw new Error('locked'); }
    if (r.status === 403) { clearTimeout(timeout); throw new Error('http 403'); }
    if (r.status === 429) { clearTimeout(timeout); throw new Error('busy'); }
    if (!r.ok || !r.body) { clearTimeout(timeout); throw new Error('http ' + r.status); }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = ''; let text = ''; let sources = []; let terms = [];
    const answerEl = document.createElement('div'); answerEl.className = 'ask-answer streaming';
    const sourcesEl = document.createElement('div');
    body.replaceChildren(answerEl, sourcesEl);
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      alive();
      buf += dec.decode(value, {stream: true});
      let cut;
      while ((cut = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, cut).trim(); buf = buf.slice(cut + 2);
        if (!line.startsWith('data:')) continue;
        let ev; try { ev = JSON.parse(line.slice(5)); } catch (e) { continue; }
        if (ev.type === 'meta') {
          sources = ev.data.sources || []; terms = ev.data.highlight_terms || [];
          meta.textContent = `Live assistant · ${ev.data.llm?.model || 'local model'}`;
          sourcesEl.innerHTML = renderSources(sources, terms); bindCites(sourcesEl);
          if (ev.data.answer) text = ev.data.answer;
        } else if (ev.type === 'queue') {
          const n = Number(ev.position);
          const ord = (k) => k + (['th', 'st', 'nd', 'rd'][(k % 100 - 20) % 10] || ['th', 'st', 'nd', 'rd'][k % 100] || 'th');
          if (n > 0) meta.textContent = `Waiting for the assistant: you are ${ord(n)} in line`;
          continue;
        } else if (ev.type === 'token') {
          text += ev.text;
        } else if (ev.type === 'done') {
          if (ev.answer) text = ev.answer;
          answerEl.classList.remove('streaming');
          const bad = ev.citation_check && ev.citation_check.uncited_sentences ? ev.citation_check.uncited_sentences.length : 0;
          if (bad) meta.textContent += ` · ${bad} sentence${bad > 1 ? 's' : ''} without a valid citation`;
        } else if (ev.type === 'error') {
          throw new Error(ev.detail || 'error');
        }
        answerEl.innerHTML = renderAnswerText(text, sources); bindCites(answerEl);
      }
    }
    clearTimeout(timeout);
    answerEl.classList.remove('streaming');
  }

  async function ask(question) {
    question = question.trim();
    if (!question || busy) return;
    busy = true; $('ask-submit').disabled = true;
    const turn = turnShell(question, online ? 'Live assistant' : 'Saved answers');
    try {
      if (online) await answerLive(question, turn);
      else answerOffline(question, turn, apiBase ? 'Assistant offline' : 'Offline demo');
    } catch (e) {
      if (e.message === 'busy') {
        turn.querySelector('.ask-meta').textContent = 'Live assistant';
        turn.querySelector('.ask-body').innerHTML = '<p class="ask-busy">The assistant is busy answering other questions. Please try again in a minute.</p>';
      } else if (e.name === 'AbortError') {
        turn.querySelector('.ask-meta').textContent = 'Live assistant';
        turn.querySelector('.ask-body').innerHTML = '<p class="ask-busy">The assistant stopped responding to this question. Please try again.</p>';
      } else if (e.message === 'locked') {
        answerOffline(question, turn, 'Access code needed');
      } else {
        showOffline('The assistant server is not running right now. Saved answers from earlier runs are below.');
        answerOffline(question, turn, 'Assistant not running');
      }
    } finally {
      busy = false; $('ask-submit').disabled = false;
    }
  }

  function init() {
    if (!$('view-ask')) return;
    $('ask-suggestions').innerHTML = SUGGESTED.map((q) => `<button type="button">${esc(q)}</button>`).join('');
    $('ask-suggestions').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      $('ask-input').value = b.textContent; ask(b.textContent);
    });
    $('ask-form').addEventListener('submit', (e) => { e.preventDefault(); ask($('ask-input').value); });
    $('ask-category').addEventListener('change', () => {
      if (!online) showOffline(apiBase ? 'The assistant server is not running right now. Saved answers from earlier runs are below.'
        : 'This copy of the site is not connected to a live assistant. Saved answers from earlier runs are below.');
    });
    $('ask-input').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask($('ask-input').value); } });
    const code = $('ask-code');
    code.value = store.get();
    $('ask-code-form').addEventListener('submit', (e) => { e.preventDefault(); store.set(code.value.trim()); checkOnline(); });
    $('ask-code-clear').addEventListener('click', () => { store.set(''); code.value = ''; checkOnline(); });
    $('ask-offline').addEventListener('click', (e) => {
      const b = e.target.closest('[data-saved]'); if (!b) return;
      const a = savedAnswers[Number(b.dataset.saved)];
      const turn = turnShell(a.query, 'Saved answer');
      answerOffline(a.query, turn, 'Saved answer');
      turn.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    loadData().then(checkOnline);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
