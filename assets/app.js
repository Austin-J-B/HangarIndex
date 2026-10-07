// HangarIndex Technical Search & Reference Q&A
(function() {
  'use strict';

  let miniSearch = null;
  let docsMap = {};
  let answersList = [];
  let chunksData = [];

  const dom = {
    navItems: document.querySelectorAll('.nav-item'),
    views: document.querySelectorAll('.view'),
    searchQuery: document.getElementById('search-query'),
    searchButton: document.getElementById('search-button'),
    searchOutput: document.getElementById('search-output'),
    searchMessage: document.getElementById('search-message'),
    preGenBox: document.getElementById('pre-generated-answer-box'),
    suggestionChips: document.getElementById('suggestion-chips'),
    modelLabel: document.getElementById('model-label'),
    breadcrumbCurrent: document.getElementById('breadcrumb-current'),
    questionsList: document.getElementById('assistant-questions-list'),
    answerDisplay: document.getElementById('assistant-answer-display'),
    placeholderDisplay: document.getElementById('assistant-placeholder'),
    filterBtns: document.querySelectorAll('.filter-btn'),
    libraryBody: document.getElementById('library-body'),
    libraryFilter: document.getElementById('library-filter'),
    libraryTotal: document.getElementById('library-total'),
    aboutDialog: document.getElementById('about-dialog'),
    helpButton: document.getElementById('help-button'),
    dialogCloseBtn: document.getElementById('dialog-close-btn')
  };

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function escapeRegExp(string) {
    return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function buildCenteredExcerpt(text, queryTerms, maxLen = 320) {
    if (!text) return '';
    const clean = text.replace(/\s+/g, ' ').trim();
    if (clean.length <= maxLen) return clean;

    const lower = clean.toLowerCase();
    let firstPos = -1;

    for (const term of queryTerms) {
      if (term.length < 2) continue;
      const idx = lower.indexOf(term.toLowerCase());
      if (idx !== -1 && (firstPos === -1 || idx < firstPos)) {
        firstPos = idx;
      }
    }

    if (firstPos === -1) {
      return clean.slice(0, maxLen) + '…';
    }

    const half = Math.floor(maxLen / 2);
    let start = Math.max(0, firstPos - half);
    let end = min(clean.length, start + maxLen);

    if (start > 0) {
      const snap = clean.indexOf(' ', start);
      if (snap !== -1 && snap < start + 25) start = snap + 1;
    }
    if (end < clean.length) {
      const snap = clean.lastIndexOf(' ', end);
      if (snap !== -1 && snap > end - 25) end = snap;
    }

    let result = clean.slice(start, end).trim();
    if (start > 0) result = '…' + result;
    if (end < clean.length) result = result + '…';
    return result;
  }

  function min(a, b) {
    return a < b ? a : b;
  }

  const HL_STOP = new Set(('a an the and or but if of in on at to for from by with without into onto over under is are was were be been being do does did can could may might must shall should will would i we you he she it they this that these those what which who whom how when where why there here as not no yes my our your their its about after before than then so such per via any all each other more most some only also just up out off down vs etc using use used work working need needed want please tell show find give get under').split(' '));
  function hlStem(w) {
    w = w.toLowerCase();
    if (w.length > 5 && w.endsWith('ies')) return w.slice(0, -3) + 'y';
    for (const suf of ['ations', 'ation', 'ings', 'ing', 'ions', 'ion', 'ives', 'ive', 'edly', 'ed', 'es', 'ly', 's']) {
      if (w.length - suf.length >= 4 && w.endsWith(suf)) return w.slice(0, -suf.length);
    }
    return w;
  }
  function hlSame(a, b) {
    if (a === b) return true;
    return a.length >= 5 && b.length >= 5 && a.slice(0, 5) === b.slice(0, 5);
  }
  function highlightTerms(snippet, queryTerms, maxMarks = 8) {
    const text = String(snippet ?? '');
    const words = [];
    for (const t of (queryTerms || [])) for (const w of String(t).toLowerCase().match(/[\p{L}\p{N}]+(?:\.[\p{L}\p{N}]+)*/gu) || []) words.push(w);
    const content = [...new Set(words.filter(w => !HL_STOP.has(w) && (w.length >= 3 || /\d/.test(w))))];
    if (!content.length) return escapeHtml(text);
    const qStems = content.map(hlStem);
    const tokens = [...text.matchAll(/[\p{L}\p{N}]+(?:\.[\p{L}\p{N}]+)*/gu)].map(m => ({start: m.index, end: m.index + m[0].length, stem: hlStem(m[0])}));
    const hitIdx = tokens.map(t => qStems.findIndex(q => hlSame(q, t.stem)));
    const spans = [];
    for (let i = 0; i < tokens.length; i++) {
      if (hitIdx[i] < 0) continue;
      let j = i;
      while (j + 1 < tokens.length && hitIdx[j + 1] >= 0 && hitIdx[j + 1] !== hitIdx[j] && /^[\s\-\/]{1,3}$/.test(text.slice(tokens[j].end, tokens[j + 1].start))) j++;
      spans.push({start: tokens[i].start, end: tokens[j].end, size: j - i + 1, len: tokens[j].end - tokens[i].start});
      i = j;
    }
    // Phrases first, then longer (more specific) single terms; keep at most maxMarks, render in text order.
    const chosen = spans.sort((a, b) => b.size - a.size || b.len - a.len).slice(0, maxMarks).sort((a, b) => a.start - b.start);
    let html = '', cursor = 0;
    for (const s of chosen) {
      html += escapeHtml(text.slice(cursor, s.start)) + `<mark class="passage-match">${escapeHtml(text.slice(s.start, s.end))}</mark>`;
      cursor = s.end;
    }
    return html + escapeHtml(text.slice(cursor));
  }

  function handleSearch(query) {
    const q = (query || '').trim();
    if (!q) {
      dom.searchOutput.innerHTML = `
        <div class="welcome-note">
          <div>
            <strong>Library index ready for query.</strong>
            <p>Contains regulatory standards (FAA, EASA, TCCA), engineering studies (NASA metallurgy and NDT), shop safety standards, and repair practices.</p>
          </div>
        </div>`;
      dom.searchMessage.textContent = '';
      dom.preGenBox.classList.add('hidden');
      return;
    }

    if (!miniSearch) {
      dom.searchMessage.textContent = 'Index is loading…';
      return;
    }

    const t0 = performance.now();
    const results = miniSearch.search(q, {
      boost: { title: 3, docName: 2, text: 1 },
      prefix: true,
      fuzzy: 0.15
    });
    const t1 = performance.now();

    const terms = q.toLowerCase().split(/\s+/).filter(t => t.length >= 2);
    renderSearchResults(results, terms, (t1 - t0).toFixed(1), q);
    checkPreGeneratedMatch(q);
  }

  function renderSearchResults(results, queryTerms, durationMs, rawQuery) {
    if (!results || results.length === 0) {
      dom.searchMessage.textContent = `No matches found for "${rawQuery}" (${durationMs}ms)`;
      dom.searchOutput.innerHTML = `
        <div class="welcome-note" style="border-left: 3px solid #df8a28;">
          <div>
            <strong>No matching passages found.</strong>
            <p>Try searching by regulatory part (e.g. '14 CFR 43', 'Part 145'), material ('Inconel 718', '2024 aluminum'), or process ('cadmium plating', 'eddy current').</p>
          </div>
        </div>`;
      return;
    }

    dom.searchMessage.textContent = `Found ${results.length} passages for "${rawQuery}" in ${durationMs}ms`;

    const topResults = results.slice(0, 15);
    const html = topResults.map(hit => {
      const doc = docsMap[hit.docName] || {};
      const isHosted = hit.redistribute === 'yes';
      const openTarget = isHosted 
        ? `docs/${hit.subfolder}/${hit.docName}${hit.page ? `#page=${hit.page}` : ''}`
        : hit.officialUrl;

      const pageLabel = hit.page ? `Page ${hit.page}` : 'Section';
      const excerptRaw = buildCenteredExcerpt(hit.text, queryTerms, 340);
      const excerptHtml = highlightTerms(excerptRaw, queryTerms);

      return `
        <article class="result-card">
          <div class="result-head">
            <div class="result-title-group">
              <span class="tag-badge ${escapeHtml(hit.tag)}">${escapeHtml(hit.tag)}</span>
              <strong>${escapeHtml(hit.title || hit.docName)}</strong>
            </div>
            <div class="result-meta-right">
              <span>${pageLabel}</span>
              <a class="result-open-link" href="${escapeHtml(openTarget)}" target="_blank" rel="noopener noreferrer">
                ${isHosted ? 'Open PDF ↗' : 'Official Link ↗'}
              </a>
            </div>
          </div>
          <p class="result-excerpt">${excerptHtml}</p>
        </article>`;
    }).join('');

    dom.searchOutput.innerHTML = html;
  }

  function checkPreGeneratedMatch(query) {
    const qClean = query.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    if (!qClean) {
      dom.preGenBox.classList.add('hidden');
      return;
    }

    // Match against real benchmark queries
    const match = answersList.find(a => {
      const aQuery = a.query.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
      return aQuery.includes(qClean) || qClean.includes(aQuery) ||
             (qClean.length >= 8 && aQuery.startsWith(qClean.slice(0, 14)));
    });

    if (match) {
      renderPreGeneratedBox(match);
    } else {
      dom.preGenBox.classList.add('hidden');
    }
  }

  function renderPreGeneratedBox(ans) {
    let cautionHtml = '';
    if (ans.caution_expected && ans.caution_trigger) {
      cautionHtml = `
        <div class="caution-callout">
          <strong>Mandatory Safety / Regulatory Caution</strong>
          ${escapeHtml(ans.caution_trigger)}
        </div>`;
    }

    const formattedAnswer = escapeHtml(ans.answer).replace(
      /\[(S\d+)\]/g,
      '<span class="citation-chip">[$1]</span>'
    );

    let sourcesHtml = '';
    (ans.sources || []).forEach(src => {
      const isHosted = src.is_hosted;
      const statusPill = isHosted
        ? '<span class="hosted-pill" style="font-size:7px;padding:1px 5px;">HOSTED PDF</span>'
        : '<span class="linkonly-pill" style="font-size:7px;padding:1px 5px;">LINK-ONLY</span>';
      const actionLink = isHosted
        ? `<a href="${escapeHtml(src.open_url)}" target="_blank" rel="noopener noreferrer">Open PDF ↗</a>`
        : (src.open_url ? `<a href="${escapeHtml(src.open_url)}" target="_blank" rel="noopener noreferrer">Official Link ↗</a>` : '<span style="color:#8b999f;font-size:10px;">Catalog Record</span>');

      sourcesHtml += `
        <div class="source-item">
          <span class="source-id-badge">[${escapeHtml(src.id)}]</span>
          <div class="source-details">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px;">
              <strong>${escapeHtml(src.title || src.doc_name)} ${src.page ? '(Page ' + src.page + ')' : ''}</strong>
              ${statusPill}
            </div>
            <p>${escapeHtml(src.excerpt)}</p>
          </div>
          <div class="source-action">
            ${actionLink}
          </div>
        </div>`;
    });

    const validityPct = Math.round((ans.citation_validity || 0) * 100);
    const validCount = ans.valid_cited_sentences || 0;
    const uncitedCount = (ans.uncited_sentences && ans.uncited_sentences.length) || 0;
    const invalidList = (ans.invalid_citations || []).join(', ');
    const latency = ans.total_seconds ? `${ans.total_seconds.toFixed(1)}s` : '';
    const nonHostedNotice = ans.has_non_hosted
      ? '<span class="metric-pill metric-info">Cites external / link-only reference</span>'
      : '';

    const evalStatusHtml = `
      <div class="eval-status-box">
        <div class="eval-status-header">
          <strong>Model: ${escapeHtml(ans.model || 'qwen3.5:9b')} (think=off)</strong>
          <span>${latency ? 'Latency: ' + latency : ''}</span>
        </div>
        <div class="eval-status-metrics">
          <span class="metric-pill ${validityPct >= 80 ? 'metric-pass' : 'metric-warn'}">
            Citation check: ${validityPct}% valid (${validCount} sentences cited with [S#])
          </span>
          ${nonHostedNotice}
          ${uncitedCount > 0 ? `<span class="metric-pill metric-info">${uncitedCount} sentence(s) without citation</span>` : ''}
          ${invalidList ? `<span class="metric-pill metric-fail">Invalid citations: ${escapeHtml(invalidList)}</span>` : ''}
        </div>
        <p class="eval-status-note">
          Generated from retrieved technical passages and checked for valid source ID citations.
        </p>
      </div>`;

    dom.preGenBox.innerHTML = `
      <div class="pre-generated-header">
        <span class="pre-generated-badge">Technical reference answer</span>
        <span class="pre-generated-meta">Reference query (${escapeHtml(ans.id)})</span>
      </div>
      <div class="pre-generated-text">${formattedAnswer}</div>
      ${cautionHtml}
      <div class="pre-generated-sources">
        <strong style="font-size:10px;color:#6f828a;letter-spacing:0.5px;text-transform:uppercase;">Cited Sources:</strong>
        ${sourcesHtml}
      </div>
      ${evalStatusHtml}`;
    dom.preGenBox.classList.remove('hidden');
  }

  function renderAssistantQuestions(filter) {
    const filtered = (filter === 'all') 
      ? answersList 
      : answersList.filter(a => a.workstream === filter);

    dom.questionsList.innerHTML = filtered.map(a => `
      <button class="question-btn" data-qid="${escapeHtml(a.id)}">
        <span>${escapeHtml(a.query)}</span>
        <span class="q-workstream-pill">${escapeHtml(a.workstream)}</span>
      </button>
    `).join('');

    dom.questionsList.querySelectorAll('.question-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const qid = btn.dataset.qid;
        const ans = answersList.find(a => a.id === qid);
        if (ans) {
          dom.questionsList.querySelectorAll('.question-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          displayFullAnswer(ans);
        }
      });
    });
  }

  function displayFullAnswer(ans) {
    let sourcesHtml = '';
    (ans.sources || []).forEach(src => {
      const isHosted = src.is_hosted;
      const statusPill = isHosted
        ? '<span class="hosted-pill" style="font-size:7px;padding:1px 5px;">HOSTED PDF</span>'
        : '<span class="linkonly-pill" style="font-size:7px;padding:1px 5px;">LINK-ONLY</span>';
      const actionLink = isHosted
        ? `<a href="${escapeHtml(src.open_url)}" target="_blank" rel="noopener noreferrer">Open PDF ↗</a>`
        : (src.open_url ? `<a href="${escapeHtml(src.open_url)}" target="_blank" rel="noopener noreferrer">Official Link ↗</a>` : '<span style="color:#8b999f;font-size:10px;">Catalog Record</span>');

      sourcesHtml += `
        <div class="source-item">
          <span class="source-id-badge">[${escapeHtml(src.id)}]</span>
          <div class="source-details">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px;">
              <strong>${escapeHtml(src.title || src.doc_name)} ${src.page ? '(Page ' + src.page + ')' : ''}</strong>
              ${statusPill}
            </div>
            <p>${escapeHtml(src.excerpt)}</p>
          </div>
          <div class="source-action">
            ${actionLink}
          </div>
        </div>`;
    });

    let cautionHtml = '';
    if (ans.caution_expected && ans.caution_trigger) {
      cautionHtml = `
        <div class="caution-callout">
          <strong>Mandatory Safety / Regulatory Caution</strong>
          ${escapeHtml(ans.caution_trigger)}
        </div>`;
    }

    const formattedAnswer = escapeHtml(ans.answer).replace(
      /\[(S\d+)\]/g,
      '<span class="citation-chip">[$1]</span>'
    );

    const validityPct = Math.round((ans.citation_validity || 0) * 100);
    const validCount = ans.valid_cited_sentences || 0;
    const uncitedCount = (ans.uncited_sentences && ans.uncited_sentences.length) || 0;
    const invalidList = (ans.invalid_citations || []).join(', ');
    const latency = ans.total_seconds ? `${ans.total_seconds.toFixed(1)}s` : '';
    const nonHostedNotice = ans.has_non_hosted
      ? '<span class="metric-pill metric-info">Cites external / link-only reference</span>'
      : '';

    const evalStatusHtml = `
      <div class="eval-status-box">
        <div class="eval-status-header">
          <strong>Model: ${escapeHtml(ans.model || 'qwen3.5:9b')} (think=off)</strong>
          <span>${latency ? 'Latency: ' + latency : ''}</span>
        </div>
        <div class="eval-status-metrics">
          <span class="metric-pill ${validityPct >= 80 ? 'metric-pass' : 'metric-warn'}">
            Citation check: ${validityPct}% valid (${validCount} sentences cited with [S#])
          </span>
          ${nonHostedNotice}
          ${uncitedCount > 0 ? `<span class="metric-pill metric-info">${uncitedCount} sentence(s) without citation</span>` : ''}
          ${invalidList ? `<span class="metric-pill metric-fail">Invalid citations: ${escapeHtml(invalidList)}</span>` : ''}
        </div>
        <p class="eval-status-note">
          Generated from retrieved technical passages and checked for valid source ID citations.
        </p>
      </div>`;

    dom.answerDisplay.innerHTML = `
      <h3>${escapeHtml(ans.query)}</h3>
      <div class="pre-generated-text">${formattedAnswer}</div>
      ${cautionHtml}
      <div class="pre-generated-sources">
        <strong style="font-size:10px;color:#6f828a;letter-spacing:0.5px;text-transform:uppercase;">Cited Technical References:</strong>
        ${sourcesHtml}
      </div>
      ${evalStatusHtml}`;

    dom.placeholderDisplay.classList.add('hidden');
    dom.answerDisplay.classList.remove('hidden');
    dom.answerDisplay.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function renderLibrary(docs) {
    const filterText = (dom.libraryFilter.value || '').trim().toLowerCase();
    const filtered = docs.filter(d => {
      if (!filterText) return true;
      return d.title.toLowerCase().includes(filterText) ||
             d.filename.toLowerCase().includes(filterText) ||
             d.authority.toLowerCase().includes(filterText) ||
             d.tag.toLowerCase().includes(filterText);
    });

    dom.libraryTotal.textContent = `${filtered.length} DOCUMENTS`;

    if (filtered.length === 0) {
      dom.libraryBody.innerHTML = '<tr><td colspan="5" class="empty-table">No documents matched your filter.</td></tr>';
      return;
    }

    dom.libraryBody.innerHTML = filtered.map(d => {
      const isHosted = d.redistribute === 'yes';
      const openTarget = isHosted ? `docs/${d.subfolder}/${d.filename}` : d.officialUrl;
      const pillClass = isHosted ? 'hosted-pill' : 'linkonly-pill';
      const pillLabel = isHosted ? 'HOSTED PDF' : 'OFFICIAL LINK';

      return `
        <tr>
          <td>
            <strong>${escapeHtml(d.title)}</strong>
            <div style="font-size:9px;color:#88979c;margin-top:3px;">${escapeHtml(d.filename)}</div>
          </td>
          <td><span class="tag-badge ${escapeHtml(d.tag)}">${escapeHtml(d.tag)}</span></td>
          <td>${escapeHtml(d.authority)}</td>
          <td><span class="${pillClass}">${pillLabel}</span></td>
          <td>
            <a href="${escapeHtml(openTarget)}" target="_blank" rel="noopener noreferrer" style="color:#3c705b;font-weight:600;text-decoration:none;">
              Open ↗
            </a>
          </td>
        </tr>`;
    }).join('');
  }

  // Navigation Tabs Switching
  dom.navItems.forEach(item => {
    item.addEventListener('click', () => {
      const targetView = item.dataset.view;
      dom.navItems.forEach(n => n.classList.remove('active'));
      dom.views.forEach(v => v.classList.remove('active'));

      item.classList.add('active');
      const viewEl = document.getElementById(`view-${targetView}`);
      if (viewEl) viewEl.classList.add('active');

      dom.breadcrumbCurrent.textContent = targetView === 'search' ? 'SEARCH' : (targetView === 'assistant' ? 'REFERENCE Q&A' : 'DOCUMENT INDEX');
      window.scrollTo(0, 0);
    });
  });

  // Suggestion Chips
  if (dom.suggestionChips) {
    dom.suggestionChips.querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', () => {
        const q = btn.dataset.query;
        dom.searchQuery.value = q;
        handleSearch(q);
      });
    });
  }

  // Search input events (Debounced 180ms)
  let searchTimer = null;
  dom.searchQuery.addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      handleSearch(e.target.value);
    }, 180);
  });

  dom.searchButton.addEventListener('click', () => {
    handleSearch(dom.searchQuery.value);
  });

  dom.searchQuery.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      clearTimeout(searchTimer);
      handleSearch(dom.searchQuery.value);
    }
  });

  // Global Keyboard Shortcut: '/' to focus search
  window.addEventListener('keydown', (e) => {
    if (e.key === '/' && document.activeElement !== dom.searchQuery && document.activeElement.tagName !== 'INPUT') {
      e.preventDefault();
      const searchNav = document.getElementById('nav-search-item');
      if (searchNav) searchNav.click();
      dom.searchQuery.focus();
    }
  });

  // Workstream Filter buttons in Grounded Q&A
  dom.filterBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      dom.filterBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderAssistantQuestions(btn.dataset.filter);
    });
  });

  // Library Table Filter
  if (dom.libraryFilter) {
    dom.libraryFilter.addEventListener('input', () => {
      renderLibrary(Object.values(docsMap));
    });
  }

  // Help / Architecture Dialog
  if (dom.helpButton && dom.aboutDialog) {
    dom.helpButton.addEventListener('click', () => {
      dom.aboutDialog.showModal();
    });
    if (dom.dialogCloseBtn) {
      dom.dialogCloseBtn.addEventListener('click', () => {
        dom.aboutDialog.close();
      });
    }
    dom.aboutDialog.addEventListener('click', (e) => {
      if (e.target === dom.aboutDialog) dom.aboutDialog.close();
    });
  }

  // URL Query Parameters support (?q=...)
  function checkUrlParams() {
    const params = new URLSearchParams(window.location.search);
    const q = params.get('q');
    const view = params.get('view');

    if (view) {
      const navTarget = document.querySelector(`.nav-item[data-view="${view}"]`);
      if (navTarget && navTarget.style.display !== 'none') navTarget.click();
    }

    if (q) {
      dom.searchQuery.value = q;
      handleSearch(q);
    }
  }

  // Initialize Data
  async function initApp() {
    try {
      const [docsResp, chunksResp, answersResp] = await Promise.all([
        fetch('data/docs.json'),
        fetch('data/chunks.json'),
        fetch('data/answers.json').catch(() => ({ ok: false }))
      ]);

      if (!docsResp.ok || !chunksResp.ok) {
        throw new Error('Failed to load static index data manifests.');
      }

      const docsData = await docsResp.json();
      chunksData = await chunksResp.json();

      if (answersResp.ok) {
        try {
          answersList = await answersResp.json();
        } catch {
          answersList = [];
        }
      } else {
        answersList = [];
      }

      // Populate docsMap
      docsData.forEach(d => { docsMap[d.filename] = d; });

      // Handle Q&A tab visibility per D-006: if empty, hide Q&A
      const qaNavItem = document.getElementById('nav-qa-item');
      const qaViewEl = document.getElementById('view-assistant');

      if (!answersList || answersList.length === 0) {
        if (qaNavItem) qaNavItem.style.display = 'none';
        if (qaViewEl) qaViewEl.style.display = 'none';
      } else {
        if (qaNavItem) qaNavItem.style.display = '';
        renderAssistantQuestions('all');
      }

      // Render Library table
      renderLibrary(docsData);

      // Initialize MiniSearch over chunked passages
      miniSearch = new MiniSearch({
        fields: ['title', 'docName', 'text'],
        storeFields: ['id', 'docName', 'title', 'subfolder', 'tag', 'page', 'redistribute', 'officialUrl', 'text'],
        searchOptions: {
          boost: { title: 3, docName: 2, text: 1 },
          prefix: true,
          fuzzy: 0.15
        }
      });

      miniSearch.addAll(chunksData);

      if (dom.modelLabel) {
        dom.modelLabel.textContent = 'Document Index';
      }

      checkUrlParams();
    } catch (err) {
      console.error('Initialization error:', err);
      if (dom.searchMessage) {
        dom.searchMessage.textContent = 'Error loading search index: ' + err.message;
      }
    }
  }

  // Boot
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initApp);
  } else {
    initApp();
  }
})();
