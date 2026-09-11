/* Shared by the Mac service and Cloudflare. This UI contains no processing logic. */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  items: [], total: 0, query: '', filter: 'all', selected: null, article: null,
  jobs: [], settings: {}, status: {}, capabilities: {}, tab: 'markdown',
  libraryRequest: 0, articleRequest: 0, pickerRequest: 0, run: null,
  markdown: new Map(), reviews: new Map(), commands: [], commandIndex: 0,
  loading: false, settingsDirty: false, authenticated: true, view: 'library', catalogDirty: false, syncDiagnosticKey: '',
};

function element(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'checked') node.checked = Boolean(value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function safeUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(value, location.origin);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function articlePath(id, suffix = '') { return `/api/articles/${encodeURIComponent(id)}${suffix}`; }
function titleOf(article) { return article?.title || 'Article title unavailable'; }
function authorsOf(article) {
  const authors = article?.authors || article?.metadata?.authors || [];
  return (Array.isArray(authors) ? authors : [authors]).map(author => {
    if (typeof author === 'string') return author;
    return author.name || [author.firstName || author.given, author.lastName || author.family].filter(Boolean).join(' ');
  }).filter(Boolean).join(', ');
}
function metadataOf(article) { return [authorsOf(article), article?.year || article?.metadata?.year].filter(Boolean).join(' · ') || 'Bibliographic details pending'; }
function hasMarkdown(article) { return Boolean(article?.artifacts?.markdown || ['ready', 'complete', 'completed', 'ready_with_warnings'].includes(article?.markdown_status)); }
function hasAudio(article) { return Boolean(article?.artifacts?.audio || article?.audio_url || ['ready', 'complete', 'completed', 'ready_with_warnings', 'published'].includes(article?.audio_status)); }
function warningsOf(article) { return Array.isArray(article?.warnings) ? article.warnings : []; }
function hasWarnings(article) { return warningsOf(article).length > 0 || /warn|fail/.test(article?.qa_status || ''); }
function readable(value) { return String(value || 'Pending').replaceAll('_', ' ').replace(/^./, char => char.toUpperCase()); }
function number(value) { return Number.isFinite(Number(value)) ? Number(value).toLocaleString() : '—'; }
function completed(status) { return ['completed', 'complete', 'ready', 'ready_with_warnings', 'completed_with_warnings', 'published'].includes(status); }
function active(status) { return ['queued', 'running', 'pending', 'claimed', 'retrying', 'cancel_requested'].includes(status); }
function badge(text, kind = '') { return element('span', { class: `tag ${kind}` }, text); }
function statusBadge(status) {
  const kind = /warn/.test(status || '') ? 'warning' : /fail|error/.test(status || '') ? 'error' : completed(status) ? 'ready' : active(status) ? 'active' : '';
  return badge(readable(status), kind);
}
function actionLabel(action) { return ({ markdown: 'Markdown', full: 'Full reading', brief: 'Brief', both: 'Brief + full reading', sync: 'Library sync' })[action] || readable(action); }
function scopeLabel(scope) { return ({ new: 'New articles', all: 'Whole library', one: 'Selected article' })[scope] || readable(scope); }

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin', cache: 'no-store', ...options,
    headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  });
  if (!response.ok) {
    let body;
    try { body = await response.json(); } catch { body = {}; }
    const error = new Error(body.error?.message || body.error || body.message || `The request could not complete (${response.status}).`);
    error.status = response.status;
    if (response.status === 401) {
      state.authenticated = false;
      const login = safeUrl(body.login_url || '/login');
      if (login && new URL(login).origin === location.origin) $('#login-link').href = login;
    }
    throw error;
  }
  if (options.raw) return response.text();
  if (response.status === 204) return {};
  return response.json();
}

function showError(error) {
  $('#error-message').textContent = error.status === 401 ? 'Sign in to open your private library.' : (error.message || String(error));
  $('#login-link').hidden = error.status !== 401;
  $('#error-banner').hidden = false;
}
function toast(message) { $('#toast-message').textContent = message; $('#toast').hidden = false; }
function closeDialog(id) { $(`#${id}`).close(); }
function openDialog(id) { const dialog = $(`#${id}`); if (!dialog.open) dialog.showModal(); }

function navigate(view, { updateHash = true } = {}) {
  if (!['library', 'activity', 'settings'].includes(view)) view = 'library';
  state.view = view;
  $$('.view').forEach(node => { node.hidden = node.id !== `${view}-view`; });
  $$('nav [data-view]').forEach(node => {
    if (node.dataset.view === view) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });
  if (updateHash && location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
  document.title = `1 More Paper · ${readable(view)}`;
  if (view === 'activity') renderJobs();
}

async function loadLibrary({ append = false } = {}) {
  const request = ++state.libraryRequest;
  const offset = append ? state.items.length : 0;
  const query = $('#library-search').value.trim();
  state.query = query;
  $('#article-list').setAttribute('aria-busy', 'true');
  $('#library-result-status').textContent = query ? 'Searching your library…' : 'Loading your library…';
  try {
    const result = await api(`/api/library?${new URLSearchParams({ q: query, limit: '100', offset: String(offset) })}`);
    if (request !== state.libraryRequest) return;
    state.items = append ? [...state.items, ...(result.items || [])] : result.items || [];
    state.total = result.total ?? state.items.length;
    // Search includes document text; join snippets without discarding library metadata.
    if (query && !append) {
      try {
        const search = await api(`/api/search?${new URLSearchParams({ q: query, limit: '100' })}`);
        if (request !== state.libraryRequest) return;
        const byId = new Map(state.items.map(item => [String(item.id), item]));
        for (const hit of search.results || []) {
          const id = String(hit.id || hit.article_id || '');
          if (!id) continue;
          byId.set(id, { ...(hit.metadata || {}), ...hit, ...(byId.get(id) || {}), id, snippet: hit.snippet || hit.excerpt || hit.description || '' });
        }
        state.items = [...byId.values()];
        state.total = Math.max(state.total, state.items.length);
      } catch (error) { if (error.status === 401) throw error; }
    }
    renderLibrary();
    if (!state.selected && !query && state.items.length && matchMedia('(min-width: 761px)').matches) selectArticle(state.items[0].id, { focus: false });
  } catch (error) {
    if (request !== state.libraryRequest) return;
    showError(error);
    $('#library-result-status').textContent = 'Library could not be refreshed.';
    if (!state.items.length) $('#article-list').replaceChildren(emptyState('Your library is waiting.', 'Connect to the service or sign in, then try again.'));
  } finally { if (request === state.libraryRequest) $('#article-list').setAttribute('aria-busy', 'false'); }
}

function filteredItems() {
  return state.items.filter(article => ({
    all: true, new: !hasMarkdown(article) || article.source_changed, markdown: hasMarkdown(article), audio: hasAudio(article), warnings: hasWarnings(article),
  })[state.filter]);
}
function emptyState(title, message, action) {
  return element('div', { class: 'empty-state' }, element('h3', {}, title), element('p', {}, message), action);
}
function renderLibrary() {
  const items = filteredItems();
  $('#article-list').replaceChildren(...items.map(article => {
    const tags = [hasMarkdown(article) ? badge('Markdown ready', 'ready') : badge('New article')];
    if (article.source_changed) tags.push(badge('Source updated', 'active'));
    if (hasAudio(article)) tags.push(badge('Audio ready', 'ready'));
    if (hasWarnings(article)) tags.push(badge('Warnings', 'warning'));
    if (article.license_status && !['open', 'pass', 'approved', 'public', 'allowed'].includes(article.license_status)) tags.push(badge('Private'));
    return element('button', { class: 'article-card', type: 'button', 'aria-pressed': String(String(article.id) === String(state.selected)), onclick: () => selectArticle(article.id) },
      element('span', { class: 'article-card-title' }, titleOf(article)),
      element('span', { class: 'article-meta' }, metadataOf(article)),
      article.snippet && element('span', { class: 'article-snippet' }, article.snippet.replace(/<!--[\s\S]*?-->/g, '').replace(/\s+/g, ' ').trim()),
      element('span', { class: 'tags' }, tags));
  }));
  if (!items.length) $('#article-list').append(emptyState(state.query ? 'No matching articles.' : state.filter === 'all' ? 'Your library starts here.' : 'Nothing in this view yet.',
    state.query ? 'Try a shorter phrase, an author, or words from the article.' : state.filter === 'all' ? 'Sync the articles saved in Zotero to get started.' : 'Choose another filter, or create Markdown for new articles.',
    !state.query && state.filter === 'all' ? element('button', { class: 'button', onclick: () => openRun('sync', 'all') }, 'Sync Zotero library') : null));
  $('#library-result-status').textContent = `${number(items.length)} ${items.length === 1 ? 'article' : 'articles'}${state.query ? ` matching “${state.query}”` : state.total > state.items.length ? ` shown · ${number(state.total)} in library` : ''}${state.filter !== 'all' ? ' in this view' : ''}`;
  $('#load-more').hidden = state.items.length >= state.total || Boolean(state.query);
  $$('.filter-row button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.filter === state.filter)));
}

async function selectArticle(id, { focus = true } = {}) {
  const request = ++state.articleRequest;
  state.selected = String(id);
  state.tab = 'markdown';
  renderLibrary();
  $('#library-layout').classList.add('show-detail');
  $('#article-detail').replaceChildren(emptyState('Opening article…', 'Loading the article and its available files.'));
  try {
    const article = await api(articlePath(id));
    if (request !== state.articleRequest) return;
    state.article = article.article || article;
    renderArticle();
    if (focus) { $('#article-detail').scrollIntoView({ block: 'start' }); if (matchMedia('(max-width: 760px)').matches) $('.detail-title').focus(); }
  } catch (error) {
    if (request !== state.articleRequest) return;
    $('#article-detail').replaceChildren(emptyState('This article could not be opened.', error.message,
      element('button', { class: 'button', onclick: () => selectArticle(id) }, 'Try again')));
    showError(error);
  }
}

function renderArticle() {
  const article = state.article;
  if (!article) return;
  const source = safeUrl(article.source_url);
  const back = element('button', { class: 'button quiet back-button', onclick: () => {
    $('#library-layout').classList.remove('show-detail');
    $('.article-card[aria-pressed="true"]')?.focus();
  } }, '← All articles');
  const tabs = [['markdown', 'Markdown'], ['audio', 'Audio'], ['review', 'Quality & status']];
  $('#article-detail').replaceChildren(
    element('header', { class: 'detail-header' }, back,
      element('div', { class: 'detail-topline' }, element('div', { class: 'tags' }, badge('Zotero library'), hasWarnings(article) && badge(hasMarkdown(article) || hasAudio(article) ? 'Ready with warnings' : 'Warnings', 'warning')),
        source && element('a', { class: 'text-button', href: source, target: '_blank', rel: 'noopener noreferrer' }, 'Original source ↗')),
      element('h2', { class: 'detail-title', tabindex: '-1' }, titleOf(article)),
      element('p', { class: 'article-meta' }, metadataOf(article)),
      element('div', { class: 'detail-actions' },
        element('button', { class: 'button primary', onclick: () => openRun('markdown', 'one', article) }, hasMarkdown(article) ? 'Update Markdown' : 'Create Markdown'),
        element('button', { class: 'button', onclick: () => openRun('full', 'one', article) }, 'Full reading'),
        element('button', { class: 'button', onclick: () => openRun('brief', 'one', article) }, 'Brief'))),
    element('div', { class: 'detail-tabs', role: 'tablist', 'aria-label': 'Article files and quality' }, tabs.map(([id, title]) => element('button', {
      id: `tab-${id}`, role: 'tab', 'aria-selected': String(state.tab === id), 'aria-controls': 'detail-content', tabindex: state.tab === id ? '0' : '-1',
      onclick: () => selectTab(id), onkeydown: event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const index = tabs.findIndex(([key]) => key === id);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (index + (event.key === 'ArrowRight' ? 1 : 2)) % 3;
        selectTab(tabs[next][0]); $(`#tab-${tabs[next][0]}`).focus();
      },
    }, title))),
    element('div', { id: 'detail-content', class: 'detail-content', role: 'tabpanel', 'aria-labelledby': `tab-${state.tab}`, tabindex: '0' }));
  renderTab();
}

function selectTab(tab) {
  state.tab = tab;
  $$('.detail-tabs button').forEach(node => { node.setAttribute('aria-selected', String(node.id === `tab-${tab}`)); node.tabIndex = node.id === `tab-${tab}` ? 0 : -1; });
  $('#detail-content').setAttribute('aria-labelledby', `tab-${tab}`);
  renderTab();
}

async function documentText(article, kind) {
  const cache = kind === 'review' ? state.reviews : state.markdown;
  const key = `${article.id}:${article.updated_at || ''}`;
  if (!cache.has(key)) cache.set(key, await api(articlePath(article.id, kind === 'review' ? '/review' : '/markdown'), { raw: true, headers: { Accept: 'text/markdown' } }));
  return cache.get(key);
}

function fileActions(article, kind, edition) {
  const isMd = kind === 'markdown';
  const buttons = [];
  if (isMd) buttons.push(element('button', { class: 'button', onclick: () => downloadDocument(article, 'markdown') }, 'Download .md'));
  if (state.capabilities.local_files) {
    buttons.push(element('button', { class: 'button', onclick: () => openFile(article, kind, false, edition) }, isMd ? 'Open Markdown' : 'Open audio'));
    buttons.push(element('button', { class: 'button', onclick: () => openFile(article, kind, true, edition) }, 'Reveal in Finder'));
  }
  return element('div', { class: 'file-actions' }, buttons);
}

async function renderTab() {
  const container = $('#detail-content');
  const article = state.article;
  const selected = state.selected;
  const tab = state.tab;
  if (!container || !article) return;
  if (tab === 'audio') { renderAudio(container, article); return; }
  if (tab === 'review') { renderReview(container, article); return; }
  if (!hasMarkdown(article)) {
    container.replaceChildren(emptyState('Ready when you are.', 'Create research Markdown first. You can review and search it without generating any audio.',
      element('button', { class: 'button primary', onclick: () => openRun('markdown', 'one', article) }, 'Create Markdown')));
    return;
  }
  container.replaceChildren(element('p', { class: 'subtle' }, 'Opening the complete Markdown…'));
  try {
    const text = await documentText(article, 'markdown');
    if (state.selected !== selected || state.tab !== tab) return;
    const reader = element('article', { class: 'markdown', 'aria-label': 'Complete research Markdown' });
    renderMarkdown(text, reader);
    const raw = element('button', { class: 'button', onclick: () => {
      const showingRaw = reader.classList.contains('raw-markdown');
      reader.className = showingRaw ? 'markdown' : 'raw-markdown';
      if (showingRaw) renderMarkdown(text, reader); else reader.textContent = text;
      raw.textContent = showingRaw ? 'View source' : 'Reading view';
    } }, 'View source');
    const actions = fileActions(article, 'markdown'); actions.append(raw);
    container.replaceChildren(actions, reader,
      element('p', { class: 'preview-note' }, `${number(text.length)} characters · Complete Markdown. Citations and links remain available here; narration is prepared separately.`));
  } catch (error) {
    if (state.selected !== selected || state.tab !== tab) return;
    container.replaceChildren(emptyState('Markdown could not be loaded.', error.message,
      element('button', { class: 'button', onclick: renderTab }, 'Try again')));
  }
}

function renderAudio(container, article) {
  if (!hasAudio(article)) {
    container.replaceChildren(emptyState('Take this paper with you.', 'Create a full reading or a shorter Brief. Quality warnings will stay visible alongside usable audio.',
      element('div', { class: 'detail-actions' },
        element('button', { class: 'button primary', onclick: () => openRun('full', 'one', article) }, 'Create full reading'),
        element('button', { class: 'button', onclick: () => openRun('both', 'one', article) }, 'Create both editions'))));
    return;
  }
  const editions = Array.isArray(article.editions) ? article.editions : article.editions && typeof article.editions === 'object'
    ? Object.entries(article.editions).map(([edition, data]) => ({ edition, ...(typeof data === 'object' ? data : { status: data }) }))
    : [{ edition: 'full', audio_url: article.audio_url, status: article.audio_status }];
  container.replaceChildren(...editions.map(edition => {
    const name = edition.edition || edition.name || 'full';
    const url = safeUrl(edition.audio_url || edition.url || (state.capabilities.local_files ? articlePath(article.id, `/audio?edition=${encodeURIComponent(name)}`) : null));
    const ready = url && !['pending', 'missing', 'failed', 'queued', 'running'].includes(edition.status);
    return element('section', { class: 'audio-edition', 'data-edition': name },
      element('h3', {}, /brief/i.test(name) ? 'Brief' : 'Full reading'),
      element('div', { class: 'tags' }, statusBadge(edition.status || 'ready'), (edition.publication_status || edition.publish_status) && badge(`Podcast: ${readable(edition.publication_status || edition.publish_status)}`)),
      ready ? element('audio', { controls: true, preload: 'none', src: url, 'aria-label': `${/brief/i.test(name) ? 'Brief' : 'Full reading'} of ${titleOf(article)}` })
        : element('p', {}, state.capabilities.local_files ? 'This edition is not ready to play yet.' : 'This audio is available on your Mac or in your iCloud output folder.'),
      ready && !state.capabilities.local_files && element('a', { class: 'button', href: url, target: '_blank', rel: 'noopener noreferrer' }, 'Open audio'),
      state.capabilities.local_files && fileActions(article, 'audio', name));
  }), element('p', { class: 'preview-note' }, 'Audio readiness and podcast publication are tracked separately. The Markdown tab contains the article text.'));
}

async function renderReview(container, article) {
  const selected = state.selected;
  const warnings = warningsOf(article);
  const rows = [['Markdown', article.markdown_status || (hasMarkdown(article) ? 'ready' : 'not_created')], ['Audio', article.audio_status || (hasAudio(article) ? 'ready' : 'not_created')], ['Quality checks', article.qa_status || 'not_run']];
  if (article.publication_status || article.publish_status) rows.push(['Podcast', article.publication_status || article.publish_status]);
  if (article.icloud_status) rows.push(['iCloud export', article.icloud_status]);
  if (article.backup_status) rows.push(['Backup', article.backup_status]);
  container.replaceChildren(element('dl', { class: 'detail-status' }, rows.map(([label, value]) => element('div', {}, element('dt', {}, label), element('dd', {}, statusBadge(value))))));
  if (warnings.length) container.append(element('section', { class: 'warning-panel' }, element('h3', {}, 'Worth a look. Still available to use.'),
    element('ul', {}, warnings.map(warning => element('li', {}, typeof warning === 'string' ? warning : warning.message || warning.finding || warning.description || JSON.stringify(warning))))));
  const reviewContainer = element('div'); container.append(reviewContainer);
  if (!article.artifacts?.review && !hasWarnings(article) && !['passed', 'pass', 'ready', 'completed'].includes(article.qa_status)) {
    reviewContainer.append(element('p', { class: 'field-help' }, 'No quality report is available yet. Optional checks run on Markdown before synthesis and on audio after it is assembled.'));
    return;
  }
  reviewContainer.append(element('p', { class: 'subtle' }, 'Loading the review report…'));
  try {
    const text = await documentText(article, 'review');
    if (state.selected !== selected || state.tab !== 'review') return;
    const content = element('div', { class: 'markdown' }); renderMarkdown(text, content);
    reviewContainer.replaceChildren(element('div', { class: 'button-row review-actions' },
      element('button', { class: 'button', onclick: () => copyText(text, 'Complete AI review instructions copied.') }, 'Copy AI review instructions'),
      element('button', { class: 'button', onclick: () => downloadDocument(article, 'review') }, 'Download review')),
      element('details', {}, element('summary', {}, 'Findings and instructions for an AI'), content));
  } catch (error) {
    if (state.selected !== selected || state.tab !== 'review') return;
    reviewContainer.replaceChildren(element('p', { class: 'field-help' }, error.status === 404 ? 'The detailed report has not synced yet. Findings above remain available.' : error.message));
  }
}

// Source text is untrusted. Construct a small safe reading view with DOM nodes only.
// Raw HTML and image URLs stay as text; no source content can create executable markup.
function inlineMarkdown(text, parent) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let start = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(start, match.index)));
    const token = match[0];
    if (token.startsWith('`')) parent.append(element('code', {}, token.slice(1, -1)));
    else if (token.startsWith('**')) parent.append(element('strong', {}, token.slice(2, -2)));
    else {
      const parts = token.match(/^\[([^\]]+)\]\((.+)\)$/);
      const url = safeUrl(parts[2]);
      parent.append(url ? element('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, parts[1]) : document.createTextNode(token));
    }
    start = match.index + token.length;
  }
  parent.append(document.createTextNode(text.slice(start)));
}
function renderMarkdown(text, parent) {
  parent.replaceChildren();
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let paragraph = [], code = null, list = null;
  let startLine = 0;
  if (lines[0]?.trim() === '---') {
    const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
    if (end > 0) {
      parent.append(element('details', { class: 'source-metadata' }, element('summary', {}, 'Source & extraction details'), element('pre', {}, lines.slice(1, end).join('\n'))));
      startLine = end + 1;
    }
  }
  const flush = () => { if (paragraph.length) { const node = element('p'); inlineMarkdown(paragraph.join('\n'), node); parent.append(node); paragraph = []; } };
  const tableCells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(cell => cell.trim().replaceAll('\\|', '|'));
  for (let index = startLine; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      flush(); list = null;
      if (code !== null) { parent.append(element('pre', { tabindex: '0' }, element('code', {}, code.join('\n')))); code = null; } else code = [];
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    if (!line.trim()) { flush(); list = null; continue; }
    const pageMarker = line.match(/^\s*<!--\s*pdf-page:\s*(\d+)\s*-->\s*$/);
    if (pageMarker) { flush(); list = null; parent.append(element('p', { class: 'page-marker' }, `PDF page ${pageMarker[1]}`)); continue; }
    if (line.includes('|') && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1])) {
      flush(); list = null;
      const headings = tableCells(line);
      const header = element('tr', {}, headings.map(cell => { const node = element('th', { scope: 'col' }); inlineMarkdown(cell, node); return node; }));
      const body = element('tbody'); index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const cells = tableCells(lines[index]);
        body.append(element('tr', {}, headings.map((_, cellIndex) => { const node = element('td'); inlineMarkdown(cells[cellIndex] || '', node); return node; })));
        index += 1;
      }
      index -= 1;
      parent.append(element('div', { class: 'markdown-table', tabindex: '0', role: 'region', 'aria-label': 'Article table' }, element('table', {}, element('thead', {}, header), body)));
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    const item = line.match(/^\s*(?:([-*+])|\d+[.)])\s+(.+)$/);
    if (heading) { flush(); list = null; const node = element(`h${Math.min(6, heading[1].length + 2)}`); inlineMarkdown(heading[2], node); parent.append(node); }
    else if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { flush(); list = null; parent.append(element('hr')); }
    else if (item) {
      flush(); const type = item[1] ? 'ul' : 'ol';
      if (!list || list.tagName.toLowerCase() !== type) { list = element(type); parent.append(list); }
      const node = element('li'); inlineMarkdown(item[2], node); list.append(node);
    } else if (/^>\s?/.test(line)) { flush(); list = null; const node = element('blockquote'); inlineMarkdown(line.replace(/^>\s?/, ''), node); parent.append(node); }
    else { list = null; paragraph.push(line); }
  }
  flush(); if (code !== null) parent.append(element('pre', {}, element('code', {}, code.join('\n'))));
}

async function downloadDocument(article, kind) {
  try {
    const text = await documentText(article, kind);
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
    const filename = `${titleOf(article).replace(/[\/:*?"<>|\u0000-\u001f]/g, ' ').slice(0, 180)}${kind === 'review' ? ' — AI review' : ''}.md`;
    const link = element('a', { href: url, download: filename }); document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { showError(error); }
}
async function copyText(text, message = 'Copied.') {
  try { await navigator.clipboard.writeText(text); toast(message); }
  catch { showError(new Error('Clipboard access is unavailable. Download the file or select and copy the text.')); }
}
async function openFile(article, artifact, reveal, edition) {
  try { await api(articlePath(article.id, '/open'), { method: 'POST', body: JSON.stringify({ artifact, reveal, ...(edition ? { edition } : {}) }) }); toast(reveal ? `Revealed in Finder: ${titleOf(article)}` : `Opened: ${titleOf(article)}`); }
  catch (error) { showError(error); }
}

async function loadStatus() {
  const result = await api('/api/status');
  if (JSON.stringify(result.counts) !== JSON.stringify(state.status.counts) || result.catalog_revision !== state.status.catalog_revision) state.catalogDirty = true;
  state.status = result;
  state.capabilities = { ...state.capabilities, ...result.capabilities };
  renderSyncDiagnostics(result);
  const worker = result.worker || {};
  const online = Boolean(worker.online);
  $('#worker-status').className = `worker-status${online ? ' online' : ''}`;
  $('#worker-status').replaceChildren(element('span', { class: 'status-dot', 'aria-hidden': 'true' }), online ? 'Your Mac is online' : 'Your Mac is offline');
  $('#connection-banner').hidden = online;
  $('#connection-banner').textContent = 'Your synced Markdown is still available. New processing jobs will wait until your Mac is online.';
  const zotero = result.zotero;
  $('#zotero-status').hidden = !zotero;
  if (zotero) {
    const checked = zotero.last_success_at || (zotero.online ? zotero.checked_at : null);
    const lastCheck = checked ? `Last checked Zotero ${new Date(checked).toLocaleString()}.` : 'Waiting for the first Zotero check.';
    const automatic = { off: 'Automatic generation is off.', markdown: 'New PDFs become Markdown automatically.', full: 'New PDFs become Markdown and Full audio automatically.', brief: 'New PDFs become Markdown and Brief audio automatically.', both: 'New PDFs become Markdown, Brief and Full audio automatically.' }[zotero.auto_generate] || '';
    const stale = checked && Date.now() - Date.parse(checked) > 180000;
    $('#zotero-status').textContent = !online ? `${lastCheck} Checks resume when your Mac is online.`
      : zotero.online === false ? `${lastCheck} ${zotero.message || 'Open Zotero to resume automatic updates.'} Retrying automatically.`
      : stale ? `${lastCheck} Zotero updates are delayed; check Activity for service status.`
      : `${lastCheck} Checks run every minute while Zotero is open. ${automatic}`;
  }
  const counts = result.counts || {};
  $('#count-articles').textContent = number(counts.articles ?? counts.total ?? state.total);
  $('#count-markdown').textContent = number(counts.markdown ?? counts.markdown_ready ?? 0);
  $('#count-audio').textContent = number(counts.audio ?? counts.audio_ready ?? 0);
  $('#count-warnings').textContent = number(counts.warnings ?? 0);
  $('#last-refreshed').textContent = `Updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  const remoteMcp = safeUrl(result.mcp_url || state.settings.mcp_url || (result.cloud?.url ? `${result.cloud.url.replace(/\/$/, '')}/mcp` : null));
  const mcpUrl = remoteMcp || (state.capabilities.remote ? `${location.origin}/mcp` : '');
  $('#mcp-url').value = mcpUrl;
  $('#mcp-url').placeholder = 'Cloudflare connection is not configured yet';
  $('#copy-mcp-url').disabled = !mcpUrl;
  $('#mcp-note').textContent = state.capabilities.local_files && !remoteMcp ? 'Your Cloudflare MCP URL will appear here after the cloud connection is configured.' : 'The server requires your sign-in. Synced Markdown is available even when your Mac is offline.';
}

function renderSyncDiagnostics(result) {
  const cloud = result.cloud || {};
  const warnings = (Array.isArray(cloud.sync_warnings) ? cloud.sync_warnings : []).map(warning => ({
    title: typeof warning.title === 'string' && warning.title ? warning.title : 'An article needs sync attention',
    code: Number.isInteger(warning.code) && warning.code >= 100 && warning.code <= 599 ? warning.code : null,
    nextAttempt: typeof warning.next_attempt === 'number' && Number.isFinite(warning.next_attempt) ? warning.next_attempt : null,
  }));
  const model = {
    cloudDelayed: cloud.online === false && Boolean(cloud.url || cloud.configured),
    jobsDelayed: result.cloud_lease?.online === false,
    warnings,
  };
  const key = JSON.stringify(model);
  if (key === state.syncDiagnosticKey) return;
  state.syncDiagnosticKey = key;
  const banner = $('#sync-banner');
  banner.hidden = !model.cloudDelayed && !model.jobsDelayed && !warnings.length;
  if (banner.hidden) { banner.replaceChildren(); return; }
  const expanded = $('details', banner)?.open;
  const content = element('div', { class: 'sync-diagnostics' },
    element('h2', {}, 'Cloud connection needs attention'),
    model.cloudDelayed && element('p', {}, 'Cloud sync is delayed. Local processing continues, and your Mac will retry automatically.'),
    model.jobsDelayed && element('p', {}, 'Cloud job updates are delayed. Check Activity for paused or cancelled runs. Connection updates retry automatically; completed files remain available.'),
    warnings.length > 0 && element('p', {}, `${number(warnings.length)} ${warnings.length === 1 ? 'article is' : 'articles are'} waiting to sync. Local files remain available, and automatic retries continue.`));
  if (warnings.length) content.append(element('details', { open: expanded },
    element('summary', {}, 'Articles awaiting sync'),
    element('ul', {}, warnings.map(warning => {
      const next = warning.nextAttempt && new Date(warning.nextAttempt * 1000);
      return element('li', {}, warning.title, warning.code ? ` · HTTP ${warning.code}` : '',
        next && !Number.isNaN(next.getTime()) && element('small', {}, `Next automatic attempt after ${next.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}`));
    }))));
  // Display only explicitly selected fields; never raw errors, credentials, or internal job data.
  banner.replaceChildren(content);
}

async function loadJobs() {
  const result = await api('/api/jobs');
  const jobs = result.jobs || [];
  const changed = JSON.stringify(jobs) !== JSON.stringify(state.jobs);
  state.jobs = jobs;
  if (changed) state.catalogDirty = true;
  const count = jobs.filter(job => active(job.status)).length;
  $('#activity-count').textContent = count; $('#activity-count').hidden = !count;
  if (changed || !$('#jobs-list').childElementCount) renderJobs();
}
function renderJobs() {
  const list = $('#jobs-list');
  if (!state.jobs.length) { list.replaceChildren(emptyState('All clear.', 'Start a run from your library or the command palette. Its progress will appear here.', element('button', { class: 'button', onclick: openCommands }, 'Open commands'))); return; }
  list.replaceChildren(...state.jobs.map(job => {
    const progress = typeof job.progress === 'number' ? Math.max(0, Math.min(100, state.capabilities.remote ? job.progress * 100 : job.progress)) : null;
    const jobTitle = job.title || job.article_title || (job.scope === 'one' ? titleOf(state.items.find(article => String(article.id) === String(job.article_id))) : scopeLabel(job.scope));
    const actions = [];
    if (job.article_id) actions.push(element('button', { class: 'button', onclick: () => { navigate('library'); selectArticle(job.article_id); } }, 'Open article'));
    if (active(job.status) && job.status !== 'cancel_requested') actions.push(element('button', { class: 'button quiet', onclick: () => jobAction(job.id, 'cancel') }, 'Cancel run'));
    if (['failed', 'error', 'cancelled', 'canceled'].includes(job.status)) actions.push(element('button', { class: 'button', onclick: () => jobAction(job.id, 'retry') }, 'Retry run'));
    return element('article', { class: 'job-card' },
      element('div', { class: 'job-heading' }, element('h2', {}, jobTitle), statusBadge(job.status)),
      element('div', { class: 'job-meta' }, element('span', {}, actionLabel(job.action)), element('span', {}, scopeLabel(job.scope)),
        job.created_at && element('time', { datetime: job.created_at }, new Date(job.created_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }))),
      element('p', { class: 'job-stage' }, readable(['cancelled', 'canceled'].includes(job.status) ? job.status : job.stage || job.status), progress !== null && !['cancelled', 'canceled'].includes(job.status) ? ` · ${Math.round(progress)}%` : ''),
      active(job.status) && element('progress', { max: 100, value: progress, 'aria-label': `${actionLabel(job.action)} progress for ${jobTitle}` }),
      job.message && element('p', { class: 'job-message' }, job.message),
      job.error && element('p', { class: 'form-error' }, typeof job.error === 'string' ? job.error : job.error.message || JSON.stringify(job.error)),
      actions.length > 0 && element('div', { class: 'job-actions' }, actions));
  }));
}
async function jobAction(id, action) {
  try { await api(`/api/jobs/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: '{}' }); toast(action === 'cancel' ? 'Cancellation requested. Completed files remain available.' : 'Run queued for retry.'); await loadJobs(); }
  catch (error) { showError(error); }
}

async function loadSettings() {
  const result = await api('/api/settings'); state.settings = result.settings || result;
  if (state.settingsDirty) return;
  for (const name of ['qa_enabled', 'opening_sound', 'closing_sound', 'icloud_folder', 'backup_enabled', 'backup_folder', 'tts_model', 'spoken_intro', 'auto_publish', 'auto_generate']) {
    const input = $(`[name="${name}"]`, $('#settings-form')); if (!input) continue;
    const fallback = { qa_enabled: true, opening_sound: 'typing', closing_sound: 'none', tts_model: 'kokoro', spoken_intro: true, auto_publish: true, auto_generate: 'markdown' }[name];
    const value = state.settings[name] ?? fallback ?? '';
    if (input.type === 'checkbox') input.checked = Boolean(value); else input.value = value;
  }
  $('#backup-folder').disabled = !$('#backup-enabled').checked;
}
async function saveSettings(event) {
  event.preventDefault(); const button = $('#save-settings'); button.disabled = true;
  const values = {};
  $$('[name]', $('#settings-form')).forEach(input => { values[input.name] = input.type === 'checkbox' ? input.checked : input.value.trim(); });
  try {
    await api('/api/settings', { method: 'PATCH', body: JSON.stringify(values) });
    state.settings = { ...state.settings, ...values }; state.settingsDirty = false;
    $('#settings-state').textContent = state.status.worker?.online ? 'Saved. Applies to new jobs.' : 'Saved. Your Mac will apply these settings when it reconnects.';
    toast('Settings saved.');
  } catch (error) { $('#settings-state').textContent = `Not saved: ${error.message}`; showError(error); }
  finally { button.disabled = false; }
}

function openRun(action, scope, article) {
  if ($('#command-dialog').open) closeDialog('command-dialog');
  state.run = { action, scope, article: article || null };
  $('#run-heading').textContent = action === 'sync' ? 'Sync your Zotero library' : action === 'markdown' ? 'Create Markdown' : `Create ${actionLabel(action).toLowerCase()}`;
  $('#run-description').textContent = article ? titleOf(article) : scope === 'new' ? 'For new articles that still need this output.' : scope === 'all' ? 'For every article in your saved Zotero library. Completed work is reused.' : 'For one article from your saved Zotero library.';
  $('#run-article-picker').hidden = scope !== 'one' || Boolean(article);
  $('#run-article-search').value = '';
  $('#run-qa').checked = state.settings.qa_enabled ?? true;
  $('#run-force').checked = false;
  $('#run-error').hidden = true;
  $('#start-run').disabled = false;
  $('#run-worker-note').textContent = state.status.worker?.online ? 'Your Mac will process this run. You can keep browsing or close this page.' : 'This run will be queued until your Mac is online. You can close this page.';
  if (['full', 'brief', 'both'].includes(action)) $('#run-worker-note').textContent += state.settings.auto_publish !== false
    ? ' Eligible episodes publish as each is ready. Private audio goes to your iCloud output folder.'
    : ' Automatic podcast publishing is off. Audio will remain available on your Mac.';
  $('#start-run').textContent = action === 'sync' ? 'Sync library' : 'Start run';
  if (scope === 'one' && !article) loadPicker('');
  openDialog('run-dialog');
}
async function loadPicker(query) {
  const request = ++state.pickerRequest;
  const list = $('#run-article-options');
  list.replaceChildren(element('p', { class: 'field-help' }, 'Loading articles…'));
  try {
    const result = await api(`/api/library?${new URLSearchParams({ q: query, limit: '100', offset: '0' })}`);
    if (request !== state.pickerRequest) return;
    list.replaceChildren(...(result.items || []).map(article => element('label', { class: 'article-choice' },
      element('input', { type: 'radio', name: 'chosen-article', value: article.id, checked: String(state.run?.article?.id) === String(article.id), onchange: () => { state.run.article = article; } }),
      element('span', {}, titleOf(article), element('small', {}, metadataOf(article))))));
    if (!result.items?.length) list.append(element('p', { class: 'field-help' }, 'No articles match that search.'));
  } catch (error) { list.replaceChildren(element('p', { class: 'form-error' }, error.message)); }
}
async function startRun(event) {
  event.preventDefault(); const run = state.run;
  if (!run) return;
  if (run.scope === 'one' && !run.article) { $('#run-error').textContent = 'Choose an article first.'; $('#run-error').hidden = false; return; }
  const button = $('#start-run'); button.disabled = true; button.textContent = 'Starting…';
  try {
    await api('/api/jobs', { method: 'POST', body: JSON.stringify({ action: run.action, scope: run.scope,
      ...(run.article ? { article_id: run.article.id } : {}), qa: $('#run-qa').checked, force: $('#run-force').checked }) });
    closeDialog('run-dialog'); navigate('activity');
    toast(`${actionLabel(run.action)} queued${run.article ? `: ${titleOf(run.article)}` : ` for ${scopeLabel(run.scope).toLowerCase()}`}.`);
    await loadJobs();
  } catch (error) { $('#run-error').textContent = error.message; $('#run-error').hidden = false; }
  finally { button.disabled = false; button.textContent = 'Start run'; }
}

function baseCommands() {
  return [
    { label: 'Create Markdown for new articles', description: 'Research text, ready to read and search', symbol: '¶', run: () => openRun('markdown', 'new') },
    { label: 'Create Markdown for the whole library', description: 'Reuse completed articles and process the rest', symbol: '¶', run: () => openRun('markdown', 'all') },
    { label: 'Create a full episode for one article', description: 'Choose from your saved Zotero library', symbol: '▷', run: () => openRun('full', 'one') },
    { label: 'Create a Brief for one article', description: 'A shorter listening edition', symbol: '▷', run: () => openRun('brief', 'one') },
    { label: 'Create both editions for one article', description: 'Brief and full reading, tracked independently', symbol: '⇉', run: () => openRun('both', 'one') },
    { label: 'Create full readings for new articles', description: 'Generate missing full editions', symbol: '▷', run: () => openRun('full', 'new') },
    { label: 'Create both editions for the whole library', description: 'Reuse finished files and fill the gaps', symbol: '⇉', run: () => openRun('both', 'all') },
    { label: 'Sync Zotero library', description: 'Find newly saved articles through the local API', symbol: '↻', run: () => openRun('sync', 'all') },
    { label: 'Search my articles', description: 'Find titles, authors, and ideas in Markdown', symbol: '⌕', run: () => { navigate('library'); $('#library-layout').classList.remove('show-detail'); $('#library-search').focus(); } },
    { label: 'See processing activity', description: 'Follow stages, warnings, and delivery', symbol: '↗', run: () => navigate('activity') },
    { label: 'Change sounds and settings', description: 'Quality checks, iCloud output, and backups', symbol: '⚙', run: () => navigate('settings') },
  ];
}
function fuzzyMatch(text, query) {
  text = text.toLowerCase(); query = query.toLowerCase();
  if (query.split(/\s+/).every(word => text.includes(word))) return true;
  let offset = 0; for (const char of query.replaceAll(' ', '')) { offset = text.indexOf(char, offset); if (offset === -1) return false; offset += 1; } return true;
}
function openCommands() { $('#command-search').value = ''; renderCommands(); openDialog('command-dialog'); $('#command-search').focus(); }
function renderCommands() {
  const query = $('#command-search').value.trim();
  const articles = query ? state.items.filter(article => fuzzyMatch(`${titleOf(article)} ${metadataOf(article)}`, query)).slice(0, 8).map(article => ({ label: titleOf(article), description: metadataOf(article), symbol: '¶', run: () => { navigate('library'); selectArticle(article.id); } })) : [];
  state.commands = [...baseCommands().filter(command => !query || fuzzyMatch(`${command.label} ${command.description}`, query)), ...articles];
  state.commandIndex = 0;
  $('#command-list').replaceChildren(...state.commands.map((command, index) => element('button', { id: `command-option-${index}`, role: 'option', 'aria-selected': String(index === 0), tabindex: '-1', class: `command-item${index === 0 ? ' active' : ''}`, onclick: () => chooseCommand(index) },
    element('span', { class: 'command-symbol', 'aria-hidden': 'true' }, command.symbol), element('span', {}, element('span', { class: 'command-label' }, command.label), element('span', { class: 'command-description' }, command.description)))));
  if (!state.commands.length) $('#command-list').append(element('div', { class: 'empty-state', role: 'option', 'aria-disabled': 'true' }, 'No matching commands. Try “Markdown”, “episode”, “settings”, or an article title.'));
  if (state.commands.length) $('#command-search').setAttribute('aria-activedescendant', 'command-option-0');
  else $('#command-search').removeAttribute('aria-activedescendant');
}
function chooseCommand(index) { const command = state.commands[index]; if (!command) return; closeDialog('command-dialog'); command.run(); }

async function refresh({ full = false } = {}) {
  if (state.loading) return; state.loading = true;
  const selectedUpdate = !full && state.selected && state.view === 'library' ? api(articlePath(state.selected)).then(result => {
    const article = result.article || result;
    if (String(article.id) !== state.selected) return;
    if (JSON.stringify(article) === JSON.stringify(state.article)) return;
    state.article = article;
    // Delivery continues after jobs finish. Refresh its labels without resetting audio playback.
    if (state.tab === 'audio' && $('#detail-content audio')) {
      $$('.audio-edition').forEach(section => {
        const edition = article.editions?.[section.dataset.edition];
        if (!edition) return;
        $('.tags', section).replaceChildren(statusBadge(edition.status || 'ready'),
          ...((edition.publication_status || edition.publish_status) ? [badge(`Podcast: ${readable(edition.publication_status || edition.publish_status)}`)] : []));
      });
    } else renderArticle();
  }) : null;
  const results = await Promise.allSettled([loadStatus(), loadJobs(), ...(selectedUpdate ? [selectedUpdate] : []), ...(full ? [loadLibrary(), loadSettings()] : [])]);
  for (const result of results) if (result.status === 'rejected') showError(result.reason);
  if (!full && state.catalogDirty) {
    const refreshed = await Promise.allSettled([loadLibrary()]);
    for (const result of refreshed) if (result.status === 'rejected') showError(result.reason);
  }
  state.catalogDirty = false;
  state.loading = false;
}
function debounce(fn, delay = 300) { let timer; return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); }; }

$$('[data-close-dialog]').forEach(button => button.addEventListener('click', () => closeDialog(button.dataset.closeDialog)));
$$('dialog').forEach(dialog => dialog.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } }));
$$('nav [data-view]').forEach(link => link.addEventListener('click', event => { event.preventDefault(); navigate(link.dataset.view); }));
window.addEventListener('hashchange', () => navigate(location.hash.slice(1), { updateHash: false }));
$('#open-commands').addEventListener('click', openCommands);
$('#new-markdown').addEventListener('click', () => openRun('markdown', 'new'));
$('#search-form').addEventListener('submit', event => { event.preventDefault(); loadLibrary(); });
$('#library-search').addEventListener('input', debounce(() => loadLibrary()));
$('#refresh-library').addEventListener('click', () => refresh({ full: true }));
$('#refresh-activity').addEventListener('click', () => refresh());
$('#retry-load').addEventListener('click', () => { $('#error-banner').hidden = true; refresh({ full: true }); });
$('#dismiss-error').addEventListener('click', () => { $('#error-banner').hidden = true; });
$('#dismiss-toast').addEventListener('click', () => { $('#toast').hidden = true; });
$('#load-more').addEventListener('click', () => loadLibrary({ append: true }));
$$('[data-filter]').forEach(button => button.addEventListener('click', () => { state.filter = button.dataset.filter; renderLibrary(); }));
$('#settings-form').addEventListener('submit', saveSettings);
$('#settings-form').addEventListener('input', () => { state.settingsDirty = true; $('#settings-state').textContent = 'You have unsaved changes.'; $('#backup-folder').disabled = !$('#backup-enabled').checked; });
$('#copy-mcp-url').addEventListener('click', () => copyText($('#mcp-url').value, 'MCP server URL copied.'));
$('#run-form').addEventListener('submit', startRun);
$('#run-article-search').addEventListener('input', debounce(event => loadPicker(event.target.value.trim())));
$('#command-search').addEventListener('input', renderCommands);
$('#command-search').addEventListener('keydown', event => {
  if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
    event.preventDefault(); if (!state.commands.length) return;
    state.commandIndex = (state.commandIndex + (event.key === 'ArrowDown' ? 1 : state.commands.length - 1)) % state.commands.length;
    $$('.command-item').forEach((node, index) => { node.classList.toggle('active', index === state.commandIndex); node.setAttribute('aria-selected', String(index === state.commandIndex)); });
    $('#command-search').setAttribute('aria-activedescendant', `command-option-${state.commandIndex}`);
    $$('.command-item')[state.commandIndex]?.scrollIntoView({ block: 'nearest' });
  } else if (event.key === 'Enter') { event.preventDefault(); chooseCommand(state.commandIndex); }
});
document.addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    if ($('#run-dialog').open) return;
    if ($('#command-dialog').open) closeDialog('command-dialog'); else openCommands();
  }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

navigate(location.hash.slice(1) || 'library', { updateHash: false });
api('/api/capabilities').then(result => { state.capabilities = { ...state.capabilities, ...(result.capabilities || result) }; }).catch(() => {});
refresh({ full: true });
const directArticle = location.pathname.match(/^\/articles\/([^/]+)\/?$/);
if (directArticle) selectArticle(decodeURIComponent(directArticle[1]));
setInterval(() => { if (!document.hidden && state.authenticated) refresh(); }, 8000);
