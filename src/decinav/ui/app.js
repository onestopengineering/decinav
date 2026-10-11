/* decinav Decinav. No framework, no CDN, no build step. */
(() => {
  'use strict';

  const config = (() => {
    try {
      return JSON.parse(document.getElementById('decinav-ui-config').textContent) || {};
    } catch (error) {
      return {};
    }
  })();
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const DOMAINS = ['frontend', 'backend', 'infrastructure', 'data', 'system'];
  const STATUSES = ['proposed', 'accepted', 'baseline', 'deprecated', 'superseded'];
  const FRESHNESS_SYMBOL = { fresh: '●', aging: '◐', 'sources-changed': '▲', unknown: '?' };
  const FRESHNESS_LABEL = { fresh: 'fresh', aging: 'aging', 'sources-changed': 'sources changed', unknown: 'unknown' };
  const CLUSTER_THRESHOLD = 60;   // above this many decisions the graph opens collapsed by domain
  const NODE_W = 220, NODE_H = 64, CLUSTER_W = 200, CLUSTER_H = 70;

  // --- theme ---------------------------------------------------------------------
  const THEME_KEY = 'arch-ui-theme';
  function readStoredTheme() { try { return localStorage.getItem(THEME_KEY); } catch (error) { return null; } }
  function storeTheme(value) { try { localStorage.setItem(THEME_KEY, value); } catch (error) { /* private mode or blocked storage */ } }
  function systemTheme() { return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
  function applyTheme(value) {
    document.documentElement.setAttribute('data-theme', value);
    const toggle = document.getElementById('theme-toggle');
    if (toggle) { toggle.setAttribute('aria-label', value === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'); toggle.querySelector('.theme-glyph').textContent = value === 'dark' ? '☀' : '◐'; }
  }
  applyTheme(readStoredTheme() || systemTheme());
  if (window.matchMedia) {
    try { window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (!readStoredTheme()) applyTheme(systemTheme()); }); } catch (error) { /* older browsers */ }
  }

  const state = {
    project: config.defaultProject || '',
    status: null,
    search: '',
    statusFilter: new Set(),
    domainFilter: new Set(),
    focus: null,
    focusDepth: 1,
    selected: null,
    lastTrigger: null,
    viewMode: 'auto',            // auto | full | clusters
    expandedDomains: new Set(),  // clusters the user opened
    revealed: new Set(),         // individual decisions surfaced by search or selection
  };

  // --- utilities -------------------------------------------------------------

  function escapeHTML(value) {
    return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  }

  function inlineMarkdown(escaped) {
    return escaped
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/\[(ADR-\d{4})\]/g, '<span class="citation">[$1]</span>')
      .replace(/\[(DOCUMENTED|VERIFIED_IN_CODE|CONTRADICTED|UNKNOWN)\]/g, '<span class="claim-state $1">$1</span>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" rel="noopener noreferrer" target="_blank">$1</a>');
  }

  /* Small, safe Markdown renderer: escapes first, then recognises a handful of block forms. */
  function renderMarkdown(source) {
    const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
    const html = [];
    let paragraph = [];
    let list = null;
    let code = null;
    const flushParagraph = () => { if (paragraph.length) { html.push(`<p>${inlineMarkdown(escapeHTML(paragraph.join(' ')))}</p>`); paragraph = []; } };
    const flushList = () => { if (list) { html.push(`<${list.tag}>${list.items.map(item => `<li>${inlineMarkdown(escapeHTML(item))}</li>`).join('')}</${list.tag}>`); list = null; } };
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, '');
      if (code !== null) {
        if (/^\s*```/.test(line)) { html.push(`<pre><code>${escapeHTML(code.join('\n'))}</code></pre>`); code = null; }
        else code.push(raw);
        continue;
      }
      if (/^\s*```/.test(line)) { flushParagraph(); flushList(); code = []; continue; }
      const heading = /^(#{1,6})\s+(.+?)\s*#*$/.exec(line);
      if (heading) { flushParagraph(); flushList(); const level = Math.min(heading[1].length, 4); html.push(`<h${level}>${inlineMarkdown(escapeHTML(heading[2]))}</h${level}>`); continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flushParagraph(); flushList(); html.push('<hr>'); continue; }
      const bullet = /^\s*[-*+]\s+(.+)$/.exec(line);
      const numbered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
      if (bullet || numbered) {
        flushParagraph();
        const tag = bullet ? 'ul' : 'ol';
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push((bullet || numbered)[1]);
        continue;
      }
      const quote = /^\s*>\s?(.*)$/.exec(line);
      if (quote) { flushParagraph(); flushList(); html.push(`<blockquote>${inlineMarkdown(escapeHTML(quote[1]))}</blockquote>`); continue; }
      if (!line.trim()) { flushParagraph(); flushList(); continue; }
      if (list && /^\s{2,}/.test(raw)) { list.items[list.items.length - 1] += ' ' + line.trim(); continue; }
      flushList();
      paragraph.push(line.trim());
    }
    if (code !== null) html.push(`<pre><code>${escapeHTML(code.join('\n'))}</code></pre>`);
    flushParagraph(); flushList();
    return html.join('\n');
  }

  /* Extract one Markdown section by heading title (case-insensitive); prefix=true matches "Baseline architecture". */
  function extractSection(body, title, prefix = false) {
    const text = String(body ?? '').replace(/\r\n?/g, '\n');
    const pattern = /^(#{1,6})[ \t]+([^\r\n#]+?)[ \t]*#*[ \t]*$/gm;
    const matches = [...text.matchAll(pattern)];
    for (let index = 0; index < matches.length; index += 1) {
      const heading = matches[index][2].trim().toLowerCase();
      const wanted = title.toLowerCase();
      if (!(prefix ? heading.startsWith(wanted) : heading === wanted)) continue;
      const level = matches[index][1].length;
      const start = matches[index].index + matches[index][0].length;
      let end = text.length;
      for (const later of matches.slice(index + 1)) {
        if (later[1].length <= level || later[2].trim().toLowerCase().startsWith('alternative')) { end = later.index; break; }
      }
      return text.slice(start, end).trim();
    }
    return null;
  }

  function firstSentence(markdown) {
    const plain = String(markdown ?? '').replace(/```[\s\S]*?```/g, ' ').replace(/^[#>*\-\d.\s]+/gm, '').replace(/[*_`]/g, '').replace(/\s+/g, ' ').trim();
    if (!plain) return '';
    const match = /^(.+?[.!?])(\s|$)/.exec(plain);
    const sentence = match ? match[1] : plain;
    return sentence.length > 220 ? sentence.slice(0, 217).replace(/\s+\S*$/, '') + '…' : sentence;
  }

  async function api(path, payload = null, query = {}) {
    const options = { headers: { 'X-Decinav-UI-Token': config.token } };
    const params = new URLSearchParams({ project: state.project, ...query });
    let url = `/api/${path}`;
    if (payload === null) url += `?${params.toString()}`;
    else {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify({ project: state.project, ...payload });
    }
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function toast(message, error = false) {
    const node = $('#toast');
    node.textContent = message;
    node.className = `toast visible${error ? ' error' : ''}`;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.className = 'toast'; }, 4200);
  }

  async function withBusy(button, task) {
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Working…';
    try { return await task(); }
    catch (error) { toast(error.message, true); throw error; }
    finally { button.disabled = button.dataset.locked === 'true'; button.innerHTML = original; }
  }

  function svgElement(name, attributes = {}, text = null) {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== null) node.textContent = text;
    return node;
  }

  function wrapTitle(title, maxChars = 27, maxLines = 2) {
    const words = String(title).split(/\s+/);
    const lines = [];
    let current = '';
    for (const word of words) {
      const candidate = current ? `${current} ${word}` : word;
      if (candidate.length <= maxChars) { current = candidate; continue; }
      if (current) lines.push(current);
      current = word;
      if (lines.length === maxLines) break;
    }
    if (lines.length < maxLines && current) lines.push(current);
    const shown = lines.slice(0, maxLines);
    if (shown.join(' ').length < words.join(' ').length) {
      const last = shown[shown.length - 1] || '';
      shown[shown.length - 1] = last.length > maxChars - 1 ? last.slice(0, maxChars - 1).replace(/\s+\S*$/, '') + '…' : last + '…';
    }
    return shown;
  }

  const freshnessOf = record => (record && record.freshness && record.freshness.state) || 'unknown';

  // --- graph view --------------------------------------------------------------

  class GraphView {
    constructor(container, options = {}) {
      this.container = container;
      this.options = options;
      this.graph = { nodes: [], edges: [] };
      this.records = new Map();
      this.transform = { x: 0, y: 0, k: 1 };
      this.bounds = { width: 0, height: 0 };
      this.svg = null;
      this.viewport = null;
      this.tooltip = document.createElement('div');
      this.tooltip.className = 'graph-tooltip';
      this.nodeElements = new Map();
      this.nodeOrder = [];
      this.edgeElements = [];
      this.lastRender = null;
    }

    /* graph.nodes: {id,title,domain,status,class,kind?:'adr'|'cluster',count?}; graph.edges: {source,target,type,reason,count?} */
    render(graph, { records = [], emptyMessage = 'No ADRs yet.', emptyAction = null } = {}) {
      const started = performance.now();
      this.graph = graph || { nodes: [], edges: [] };
      this.records = new Map(records.map(record => [record.id, record]));
      const container = this.container;
      container.replaceChildren();
      this.nodeElements = new Map();
      this.nodeOrder = [];
      this.edgeElements = [];
      const nodes = this.graph.nodes;
      const edges = this.graph.edges;
      if (!nodes.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = emptyMessage;
        if (emptyAction) empty.append(document.createElement('br'), emptyAction);
        container.append(empty);
        this.svg = null;
        return;
      }

      const layout = new dagre.graphlib.Graph({ multigraph: true });
      layout.setGraph({ rankdir: 'TB', nodesep: 34, ranksep: 62, marginx: 24, marginy: 24 });
      layout.setDefaultEdgeLabel(() => ({}));
      nodes.forEach(node => layout.setNode(node.id, node.kind === 'cluster' ? { width: CLUSTER_W, height: CLUSTER_H } : { width: NODE_W, height: NODE_H }));
      edges.forEach((edge, index) => layout.setEdge(edge.source, edge.target, { minlen: 1 }, `e${index}`));
      const layoutStarted = performance.now();
      dagre.layout(layout);
      const layoutMs = performance.now() - layoutStarted;
      const size = layout.graph();
      this.bounds = { width: Math.max(size.width || 0, NODE_W + 48), height: Math.max(size.height || 0, NODE_H + 48) };

      const svg = svgElement('svg', { role: 'group', 'aria-label': this.options.ariaLabel || 'Architecture decision graph', tabindex: '-1' });
      const defs = svgElement('defs');
      [['arrow', ''], ['arrow-active', ' active']].forEach(([name, extra]) => {
        const marker = svgElement('marker', { id: `${container.id}-${name}`, class: `arrow-marker${extra}`, markerWidth: 9, markerHeight: 7, refX: 8, refY: 3.5, orient: 'auto', markerUnits: 'userSpaceOnUse' });
        marker.append(svgElement('path', { d: 'M0,0 L9,3.5 L0,7 Z' }));
        defs.append(marker);
      });
      svg.append(defs);
      const viewport = svgElement('g', { class: 'viewport' });
      svg.append(viewport);
      this.svg = svg;
      this.viewport = viewport;

      edges.forEach((edge, index) => {
        const points = layout.edge(edge.source, edge.target, `e${index}`).points;
        const d = points.map((point, i) => `${i === 0 ? 'M' : 'L'} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ');
        const aggregate = Boolean(edge.count && edge.count > 1) || edge.source.startsWith('cluster:') || edge.target.startsWith('cluster:');
        const group = svgElement('g', { class: `edge ${edge.type}${aggregate ? ' aggregate' : ''}`, 'data-source': edge.source, 'data-target': edge.target });
        const line = svgElement('path', { class: 'line', d, 'marker-end': `url(#${container.id}-arrow)` });
        const label = aggregate ? `${edge.count || 1} ${edge.type.replace('_', ' ')} relationship${(edge.count || 1) === 1 ? '' : 's'}` : edge.reason;
        const hit = svgElement('path', { class: 'hit', d, tabindex: '0', role: 'button', 'aria-label': `${edge.source} ${edge.type.replace('_', ' ')} ${edge.target}: ${label}` });
        group.append(line, hit);
        if (aggregate && points.length) {
          const mid = points[Math.floor(points.length / 2)];
          group.append(svgElement('circle', { cx: mid.x.toFixed(1), cy: mid.y.toFixed(1), r: 11 }));
          group.append(svgElement('text', { x: mid.x.toFixed(1), y: (mid.y + 3.5).toFixed(1), 'text-anchor': 'middle' }, String(edge.count || 1)));
        }
        const show = event => this.showTooltip(event, `<strong>${escapeHTML(edge.type.replace('_', ' '))} · ${escapeHTML(edge.source.replace('cluster:', ''))} → ${escapeHTML(edge.target.replace('cluster:', ''))}</strong>${escapeHTML(label)}`);
        hit.addEventListener('mouseenter', show);
        hit.addEventListener('mousemove', show);
        hit.addEventListener('mouseleave', () => this.hideTooltip());
        const select = event => { event.stopPropagation(); if (!aggregate && this.options.onEdgeSelect) this.options.onEdgeSelect(edge, hit); };
        hit.addEventListener('click', select);
        hit.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(event); } });
        viewport.append(group);
        this.edgeElements.push({ edge, group });
      });

      nodes.forEach(node => {
        const position = layout.node(node.id);
        if (node.kind === 'cluster') this.drawCluster(node, position, viewport);
        else this.drawNode(node, position, viewport);
      });

      container.append(svg, this.tooltip);
      this.installPanZoom();
      this.fit();
      this.applyHighlight();
      const totalMs = performance.now() - started;
      this.lastRender = { nodes: nodes.length, edges: edges.length, layoutMs: Math.round(layoutMs), totalMs: Math.round(totalMs) };
      console.info('[arch-ui] graph rendered', this.lastRender);
    }

    drawNode(node, position, viewport) {
      const x = position.x - NODE_W / 2, y = position.y - NODE_H / 2;
      const record = this.records.get(node.id);
      const freshness = node.id.startsWith('PROPOSED-') ? null : freshnessOf(record);
      const group = svgElement('g', { class: `node ${node.class}`, 'data-id': node.id, tabindex: '0', role: 'button', transform: `translate(${x.toFixed(1)}, ${y.toFixed(1)})` });
      group.setAttribute('aria-label', `${node.id}: ${node.title}. ${node.domain}, ${node.status}${freshness ? `, freshness ${FRESHNESS_LABEL[freshness]}` : ''}`);
      group.append(svgElement('rect', { class: 'box', x: 0.5, y: 0.5, width: NODE_W - 1, height: NODE_H - 1, rx: 7 }));
      group.append(svgElement('title', {}, `${node.id}: ${node.title} · ${node.status}${freshness ? ` · ${FRESHNESS_LABEL[freshness]}` : ''}`));
      group.append(svgElement('text', { class: 'node-id', x: 12, y: 17 }, node.id));
      const titleLines = wrapTitle(node.title);
      titleLines.forEach((line, index) => group.append(svgElement('text', { class: 'node-title', x: 12, y: 35 + index * 15 }, line)));
      const statusClass = String(node.status || '').replace(/[^a-z-]/g, '');
      group.append(svgElement('circle', { class: `status-dot-svg ${statusClass}`, cx: NODE_W - 14, cy: 13, r: 3.5 }));
      if (freshness) {
        group.append(svgElement('text', { class: `fresh-glyph ${freshness}`, x: NODE_W - 25, y: 17.5, 'text-anchor': 'middle' }, FRESHNESS_SYMBOL[freshness]));
      }
      const select = event => { event.stopPropagation(); if (this.dragMoved) { this.dragMoved = false; return; } this.options.onNodeSelect && this.options.onNodeSelect(node, group); };
      group.addEventListener('click', select);
      group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(event); } });
      viewport.append(group);
      this.nodeElements.set(node.id, group);
      this.nodeOrder.push(group);
    }

    drawCluster(node, position, viewport) {
      const x = position.x - CLUSTER_W / 2, y = position.y - CLUSTER_H / 2;
      const group = svgElement('g', { class: `node cluster ${node.domain}`, 'data-id': node.id, tabindex: '0', role: 'button', transform: `translate(${x.toFixed(1)}, ${y.toFixed(1)})` });
      group.setAttribute('aria-label', `${node.domain} area: ${node.count} decisions collapsed. Press Enter to expand.`);
      group.append(svgElement('rect', { class: 'box', x: 0.5, y: 0.5, width: CLUSTER_W - 1, height: CLUSTER_H - 1, rx: 9 }));
      group.append(svgElement('title', {}, `${node.domain}: ${node.count} decisions (click to expand)`));
      group.append(svgElement('text', { class: 'node-id', x: 14, y: 24 }, node.domain));
      group.append(svgElement('text', { class: 'node-title', x: 14, y: 42 }, `${node.count} decision${node.count === 1 ? '' : 's'} collapsed`));
      group.append(svgElement('text', { class: 'node-meta', x: 14, y: 58 }, 'Click to expand'));
      const expand = event => { event.stopPropagation(); this.options.onClusterSelect && this.options.onClusterSelect(node, group); };
      group.addEventListener('click', expand);
      group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); expand(event); } });
      viewport.append(group);
      this.nodeElements.set(node.id, group);
      this.nodeOrder.push(group);
    }

    installPanZoom() {
      const svg = this.svg;
      // Pointer capture is taken only once a real drag starts. Capturing on
      // pointerdown retargets the resulting click to the svg, which silently
      // turned every node click into a background click.
      let dragging = null;
      this.dragMoved = false;
      svg.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        dragging = { x: event.clientX, y: event.clientY, tx: this.transform.x, ty: this.transform.y, pointerId: event.pointerId, captured: false };
        this.dragMoved = false;
      });
      svg.addEventListener('pointermove', event => {
        if (!dragging) return;
        const dx = event.clientX - dragging.x, dy = event.clientY - dragging.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) {
          if (!dragging.captured) { try { svg.setPointerCapture(dragging.pointerId); } catch (error) { /* ignore */ } dragging.captured = true; }
          this.dragMoved = true;
          svg.classList.add('dragging');
        }
        if (!this.dragMoved) return;
        this.transform.x = dragging.tx + dx;
        this.transform.y = dragging.ty + dy;
        this.applyTransform();
      });
      const end = () => { dragging = null; svg.classList.remove('dragging'); };
      svg.addEventListener('pointerup', end);
      svg.addEventListener('pointercancel', end);
      svg.addEventListener('wheel', event => {
        if (!(event.ctrlKey || event.metaKey)) return;
        event.preventDefault();
        const rect = svg.getBoundingClientRect();
        this.zoomAt(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX - rect.left, event.clientY - rect.top);
      }, { passive: false });
      svg.addEventListener('click', () => { if (this.dragMoved) { this.dragMoved = false; return; } this.options.onBackgroundClick && this.options.onBackgroundClick(); });
      svg.addEventListener('keydown', event => {
        const current = event.target.closest('.node');
        if (!current) return;
        const index = this.nodeOrder.indexOf(current);
        let next = null;
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = this.nodeOrder[index + 1] || this.nodeOrder[0];
        if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = this.nodeOrder[index - 1] || this.nodeOrder[this.nodeOrder.length - 1];
        if (event.key === 'Home') next = this.nodeOrder[0];
        if (event.key === 'End') next = this.nodeOrder[this.nodeOrder.length - 1];
        if (next) { event.preventDefault(); next.focus({ preventScroll: true }); this.scrollNodeIntoView(next); }
      });
    }

    scrollNodeIntoView(group) {
      if (!this.svg) return;
      const rect = this.svg.getBoundingClientRect();
      const box = group.getBoundingClientRect();
      const margin = 24;
      if (box.left < rect.left + margin) this.transform.x += rect.left + margin - box.left;
      if (box.right > rect.right - margin) this.transform.x -= box.right - (rect.right - margin);
      if (box.top < rect.top + margin) this.transform.y += rect.top + margin - box.top;
      if (box.bottom > rect.bottom - margin) this.transform.y -= box.bottom - (rect.bottom - margin);
      this.applyTransform();
    }

    applyTransform() {
      if (!this.viewport) return;
      const { x, y, k } = this.transform;
      this.viewport.setAttribute('transform', `translate(${x.toFixed(2)}, ${y.toFixed(2)}) scale(${k.toFixed(3)})`);
    }

    zoomAt(factor, px, py) {
      const k = Math.min(3, Math.max(0.15, this.transform.k * factor));
      const ratio = k / this.transform.k;
      this.transform.x = px - (px - this.transform.x) * ratio;
      this.transform.y = py - (py - this.transform.y) * ratio;
      this.transform.k = k;
      this.applyTransform();
    }

    zoom(factor) {
      if (!this.svg) return;
      const rect = this.svg.getBoundingClientRect();
      this.zoomAt(factor, rect.width / 2, rect.height / 2);
    }

    fit() {
      if (!this.svg) return;
      const rect = this.svg.getBoundingClientRect();
      const width = rect.width || this.container.clientWidth || 800;
      const height = rect.height || this.container.clientHeight || 500;
      // Small graphs may scale up (to 1.6x) so they fill the canvas; large ones scale down to fit.
      const k = Math.min(1.6, (width - 24) / this.bounds.width, (height - 24) / this.bounds.height);
      this.transform = { k, x: (width - this.bounds.width * k) / 2, y: (height - this.bounds.height * k) / 2 };
      this.applyTransform();
    }

    reset() { this.transform = { x: 24, y: 24, k: 1 }; this.applyTransform(); }

    showTooltip(event, html) {
      const rect = this.container.getBoundingClientRect();
      this.tooltip.innerHTML = html;
      this.tooltip.classList.add('visible');
      this.tooltip.style.left = `${Math.max(4, Math.min(event.clientX - rect.left + 14, rect.width - 330))}px`;
      this.tooltip.style.top = `${Math.max(4, Math.min(event.clientY - rect.top + 14, rect.height - 80))}px`;
    }

    hideTooltip() { this.tooltip.classList.remove('visible'); }

    applyHighlight(search = '', selectedNode = null, selectedEdge = null) {
      const needle = search.trim().toLowerCase();
      const hits = new Set();
      this.nodeElements.forEach((group, id) => {
        const node = this.graph.nodes.find(item => item.id === id);
        const hit = needle && node && node.kind !== 'cluster' && (id.toLowerCase().includes(needle) || node.title.toLowerCase().includes(needle));
        if (hit) hits.add(id);
        group.classList.toggle('hit', Boolean(hit));
        group.classList.toggle('dim', Boolean(needle) && !hit);
        group.classList.toggle('selected', selectedNode === id);
      });
      this.edgeElements.forEach(({ edge, group }) => {
        group.classList.toggle('dim', Boolean(needle) && !hits.has(edge.source) && !hits.has(edge.target));
        const selected = selectedEdge && selectedEdge.source === edge.source && selectedEdge.target === edge.target && selectedEdge.type === edge.type;
        group.classList.toggle('selected', Boolean(selected));
        group.querySelector('path.line').setAttribute('marker-end', `url(#${this.container.id}-arrow${selected ? '-active' : ''})`);
      });
    }

    focusNode(id) {
      const group = this.nodeElements.get(id);
      if (group) { group.focus({ preventScroll: true }); this.scrollNodeIntoView(group); }
    }
  }

  // --- inspector ---------------------------------------------------------------

  const inspector = $('#inspector');
  const workspace = $('#graph-workspace');

  function openInspector(html, trigger) {
    state.lastTrigger = trigger || state.lastTrigger;
    inspector.innerHTML = html;
    workspace.classList.remove('inspector-closed');
    const close = $('.inspector-close', inspector);
    if (close) { close.addEventListener('click', closeInspector); close.focus({ preventScroll: true }); }
    $$('.rel-id', inspector).forEach(button => button.addEventListener('click', () => selectNode(button.dataset.id, button)));
  }

  function closeInspector() {
    workspace.classList.add('inspector-closed');
    inspector.innerHTML = '<div class="placeholder">Select a decision or a relationship to inspect it.</div>';
    state.selected = null;
    overviewGraph.applyHighlight(state.search, null, null);
    if (state.lastTrigger && document.contains(state.lastTrigger)) state.lastTrigger.focus({ preventScroll: true });
  }

  const chip = (text, className = '') => `<span class="chip ${className}">${escapeHTML(text)}</span>`;

  function relationshipList(items, kind) {
    if (!items || !items.length) return '<div class="empty-inline">None</div>';
    return `<ul class="rel">${items.map(item => `<li><button class="rel-id" data-id="${escapeHTML(item.id)}">${escapeHTML(item.id)}</button>${item.type ? `<span class="rel-type">${escapeHTML(item.type.replace('_', ' '))}</span>` : ''}<div class="rel-reason">${escapeHTML(item.reason || kind)}</div></li>`).join('')}</ul>`;
  }

  function freshnessBlock(freshness) {
    const item = freshness || { state: 'unknown', detail: 'no provenance recorded', commits_since: null, changed_sources: [] };
    const parts = [];
    if (item.reference_commit) parts.push(`checked at commit <code>${escapeHTML(String(item.reference_commit).slice(0, 10))}</code>`);
    if (typeof item.commits_since === 'number') parts.push(`${item.commits_since} project commit${item.commits_since === 1 ? '' : 's'} since then`);
    const plain = { fresh: 'Nothing in the project changed since this decision was last checked.', aging: 'The project changed since this decision was last checked; it may still be right, but nobody has re-checked it.', 'sources-changed': 'The documents this decision came from changed. Re-read it before relying on it.', unknown: 'Nobody has recorded when this decision was last checked against the project.' };
    return `<div class="freshness-box ${escapeHTML(item.state)}">${chip(`${FRESHNESS_SYMBOL[item.state] || '?'} ${FRESHNESS_LABEL[item.state] || item.state}`, item.state)}<div class="detail">${escapeHTML(plain[item.state] || item.detail || '')}</div>${parts.length ? `<div class="detail">${parts.join(' · ')}</div>` : ''}${item.changed_sources && item.changed_sources.length ? `<div class="detail">Changed sources: ${item.changed_sources.map(source => `<code>${escapeHTML(source)}</code>`).join(' ')}</div>` : ''}</div>`;
  }

  function provenanceBlock(provenance) {
    if (!provenance) return '<div class="empty-inline">Not recorded. After reviewing, run <code>decinav attest</code> to record that it is current.</div>';
    const rows = [
      ['Created by', `${provenance.generated_by} (${provenance.generator})`], ['Created at', provenance.generated_at],
      ['At commit', provenance.commit ? String(provenance.commit).slice(0, 10) : '—'],
      ['From sources', provenance.sources && provenance.sources.length ? provenance.sources.join(', ') : '—'],
      ['Accepted', provenance.accepted_at ? `${provenance.accepted_at} at ${String(provenance.accepted_commit || '').slice(0, 10)}` : 'not yet'],
      ['Attested', provenance.attested_at ? `${provenance.attested_at} at ${String(provenance.attested_commit || '').slice(0, 10)}` : 'not yet'],
    ];
    return `<dl>${rows.map(([label, value]) => `<dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd>`).join('')}</dl>`;
  }

  function sectionBlock(markdown, fallback) {
    return markdown ? `<div class="md">${renderMarkdown(markdown)}</div>` : `<div class="empty-inline">${escapeHTML(fallback)}</div>`;
  }

  async function selectNode(id, trigger) {
    if (id.startsWith('PROPOSED-')) {
      const node = (impactGraph.graph.nodes || []).find(item => item.id === id);
      openInspector(`<div class="inspector-head"><div><div class="id">${escapeHTML(id)}</div><h4>${escapeHTML(node ? node.title : id)}</h4></div><button class="inspector-close" aria-label="Close inspector">×</button></div><div class="meta-row">${chip('proposed new decision', 'status-proposed')}</div><p>This decision does not exist yet. The impact analysis proposed it. Create it with <code>decinav propose</code> after review.</p>`, trigger);
      return;
    }
    if (id.startsWith('cluster:')) return;
    state.selected = id;
    if (state.status && !visibleAdrIds().has(id)) { state.revealed.add(id); renderOverviewGraph(); }
    overviewGraph.applyHighlight(state.search, id, null);
    openInspector('<div class="placeholder">Loading…</div>', trigger);
    let detail;
    try { detail = await api('adr', null, { id }); }
    catch (error) { openInspector(`<div class="inspector-head"><div class="id">${escapeHTML(id)}</div><button class="inspector-close" aria-label="Close inspector">×</button></div><div class="issue">${escapeHTML(error.message)}</div>`, trigger); return; }
    if (state.selected !== id) return;
    const isBaseline = detail.status === 'baseline';
    const decision = extractSection(detail.body, 'Decision') || (isBaseline ? extractSection(detail.body, 'Baseline', true) : null);
    const context = extractSection(detail.body, 'Context');
    const consequences = extractSection(detail.body, 'Consequences');
    const summary = firstSentence(decision) || firstSentence(context) || detail.title;
    const referencedBy = (detail.referenced_by || []).map(item => ({ id: item.id, type: item.type, reason: item.reason }));
    openInspector(`
      <div class="inspector-head">
        <div><div class="id">${escapeHTML(detail.id)}</div><h4>${escapeHTML(detail.title)}</h4></div>
        <button class="inspector-close" aria-label="Close inspector">×</button>
      </div>
      <div class="meta-row">${chip(detail.status, `status-${detail.status}`)}${chip(detail.domain)}${chip(detail.date)}${chip(`${FRESHNESS_SYMBOL[detail.freshness?.state || 'unknown']} ${FRESHNESS_LABEL[detail.freshness?.state || 'unknown']}`, detail.freshness?.state || 'unknown')}</div>
      <div class="summary">${escapeHTML(summary)}</div>
      <h5>Why this decision exists</h5>${sectionBlock(context, 'No Context section in this record.')}
      <h5>${isBaseline ? 'What was observed' : 'What was decided'}</h5>${sectionBlock(decision, 'No Decision section in this record.')}
      <h5>What it means</h5>${sectionBlock(consequences, 'No Consequences section in this record.')}
      ${(detail.depends_on || []).length + (detail.supersedes || []).length + referencedBy.length === 0 ? '<div class="issue">No relationships recorded. This decision stands alone in the graph: the record has empty <code>depends_on</code> and <code>supersedes</code>, and nothing references it. If it builds on another decision, add the relationship with a reason, or ask the agent to revise it.</div>' : ''}
      <h5>Builds on</h5>${relationshipList(detail.depends_on, 'Dependency')}
      <h5>Replaces</h5>${relationshipList(detail.supersedes, 'Superseded')}
      <h5>Used by later decisions</h5>${relationshipList(referencedBy, 'Reference')}
      <h5>Is this still current?</h5>${freshnessBlock(detail.freshness)}
      <h5>Where it came from</h5>${provenanceBlock(detail.provenance)}
      <div class="path"><code>${escapeHTML(detail.path)}</code></div>
      <details><summary>Full record</summary><div class="md">${renderMarkdown(detail.body)}</div></details>
    `, trigger);
  }

  function selectEdge(edge, trigger) {
    state.selected = null;
    overviewGraph.applyHighlight(state.search, null, edge);
    openInspector(`
      <div class="inspector-head">
        <div><div class="id">${escapeHTML(edge.source)} → ${escapeHTML(edge.target)}</div><h4>${escapeHTML(edge.type === 'supersedes' ? 'Replaces' : 'Builds on')}</h4></div>
        <button class="inspector-close" aria-label="Close inspector">×</button>
      </div>
      <div class="summary">${escapeHTML(edge.type === 'supersedes' ? `${edge.target} replaces ${edge.source}.` : `${edge.target} builds on ${edge.source}.`)}</div>
      <h5>Why</h5><p>${escapeHTML(edge.reason)}</p>
      <h5>Decisions</h5>${relationshipList([{ id: edge.source, reason: 'Earlier decision' }, { id: edge.target, reason: 'Later decision' }], '')}
    `, trigger);
  }

  // --- view model: what is visible ------------------------------------------------

  const overviewGraph = new GraphView($('#overview-graph'), {
    ariaLabel: 'Architecture decision graph',
    onNodeSelect: (node, group) => selectNode(node.id, group),
    onEdgeSelect: (edge, hit) => selectEdge(edge, hit),
    onClusterSelect: node => { state.expandedDomains.add(node.domain); renderOverviewGraph(); },
  });
  const impactGraph = new GraphView($('#impact-graph'), {
    ariaLabel: 'Impact graph',
    onNodeSelect: (node, group) => { switchView('overview'); selectNode(node.id, group); },
    onEdgeSelect: (edge, hit) => { switchView('overview'); selectEdge(edge, hit); },
  });

  function neighborhood(startId, depth) {
    const adjacency = new Map();
    (state.status.graph.edges || []).forEach(edge => {
      if (!adjacency.has(edge.source)) adjacency.set(edge.source, new Set());
      if (!adjacency.has(edge.target)) adjacency.set(edge.target, new Set());
      adjacency.get(edge.source).add(edge.target);
      adjacency.get(edge.target).add(edge.source);
    });
    const visited = new Set([startId]);
    let frontier = [startId];
    for (let level = 0; level < depth; level += 1) {
      const next = [];
      frontier.forEach(id => (adjacency.get(id) || []).forEach(neighbor => { if (!visited.has(neighbor)) { visited.add(neighbor); next.push(neighbor); } }));
      frontier = next;
    }
    return visited;
  }

  function effectiveMode() {
    const total = state.status?.graph?.nodes?.length || 0;
    if (state.viewMode === 'auto') return total > CLUSTER_THRESHOLD ? 'clusters' : 'full';
    return state.viewMode;
  }

  function passesFilters(node) {
    if (state.statusFilter.size && !state.statusFilter.has(node.status)) return false;
    if (state.domainFilter.size && !state.domainFilter.has(node.domain)) return false;
    return true;
  }

  /* The set of individual decisions drawn on the canvas under the current mode, focus, filters, and reveals. */
  function visibleAdrIds() {
    const graph = state.status?.graph || { nodes: [], edges: [] };
    const focusSet = state.focus && graph.nodes.some(node => node.id === state.focus) ? neighborhood(state.focus, state.focusDepth) : null;
    const mode = effectiveMode();
    const needle = state.search.trim().toLowerCase();
    const ids = new Set();
    graph.nodes.forEach(node => {
      if (!passesFilters(node)) return;
      if (focusSet) { if (focusSet.has(node.id)) ids.add(node.id); return; }
      if (mode === 'full') { ids.add(node.id); return; }
      const searched = needle && (node.id.toLowerCase().includes(needle) || node.title.toLowerCase().includes(needle));
      if (state.expandedDomains.has(node.domain) || state.revealed.has(node.id) || searched) ids.add(node.id);
    });
    return ids;
  }

  function buildViewGraph(visible) {
    const graph = state.status.graph;
    const records = new Map(state.status.records.map(record => [record.id, record]));
    const nodes = [];
    const collapsedCounts = new Map();
    graph.nodes.forEach(node => {
      if (visible.has(node.id)) { nodes.push({ ...node, kind: 'adr' }); return; }
      if (!passesFilters(node)) return;
      collapsedCounts.set(node.domain, (collapsedCounts.get(node.domain) || 0) + 1);
    });
    const focused = Boolean(state.focus && graph.nodes.some(node => node.id === state.focus));
    if (!focused) {
      [...collapsedCounts.entries()].sort().forEach(([domain, count]) => nodes.push({ id: `cluster:${domain}`, title: domain, domain, status: 'collapsed', class: domain, kind: 'cluster', count }));
    }
    const nodeIds = new Set(nodes.map(node => node.id));
    const domainOf = id => (records.get(id) || graph.nodes.find(node => node.id === id) || {}).domain;
    const endpoint = id => (visible.has(id) ? id : (focused ? null : `cluster:${domainOf(id)}`));
    const aggregated = new Map();
    const edges = [];
    graph.edges.forEach(edge => {
      const source = endpoint(edge.source), target = endpoint(edge.target);
      if (!source || !target || !nodeIds.has(source) || !nodeIds.has(target) || source === target) return;
      if (visible.has(edge.source) && visible.has(edge.target)) { edges.push(edge); return; }
      const key = `${source}|${target}|${edge.type}`;
      if (!aggregated.has(key)) aggregated.set(key, { source, target, type: edge.type, reason: '', count: 0 });
      aggregated.get(key).count += 1;
    });
    return { nodes, edges: [...edges, ...aggregated.values()] };
  }

  function renderOverviewGraph() {
    const status = state.status;
    if (!status) return;
    const graph = status.graph || { nodes: [], edges: [] };
    const total = graph.nodes.length;
    const visible = visibleAdrIds();
    const mode = effectiveMode();
    const focusActive = Boolean(state.focus && graph.nodes.some(node => node.id === state.focus));

    const banner = $('#focus-banner');
    banner.classList.toggle('visible', focusActive);
    if (focusActive) $('#focus-label').textContent = `Focused on ${state.focus}: ${visible.size - 1} related decision${visible.size === 2 ? '' : 's'} within ${state.focusDepth} step${state.focusDepth === 1 ? '' : 's'}`;

    const counter = $('#shown-count');
    if (!total) counter.innerHTML = '';
    else {
      const hiddenByFilters = graph.nodes.filter(node => !passesFilters(node)).length;
      let hint = '';
      if (focusActive) hint = 'Focus mode. Use "Show all" to leave it.';
      else if (mode === 'clusters') hint = `Large project: decisions open collapsed by area. Click an area to expand it, search, or pick one from the list.`;
      else if (hiddenByFilters) hint = `${hiddenByFilters} hidden by filters.`;
      else hint = 'Every decision is on the canvas.';
      const actions = [];
      if (mode === 'clusters' && !focusActive) actions.push(`<button class="button secondary small" id="show-all-nodes">Show all ${total}</button>`);
      if (mode === 'clusters' && state.expandedDomains.size) actions.push('<button class="button secondary small" id="collapse-all">Collapse areas</button>');
      if (state.viewMode === 'full' && total > CLUSTER_THRESHOLD) actions.push('<button class="button secondary small" id="use-clusters">Collapse by area</button>');
      if (hiddenByFilters || state.search) actions.push('<button class="button secondary small" id="clear-filters">Clear filters</button>');
      counter.innerHTML = `<span class="hint">Showing <strong>${visible.size}</strong> of <strong>${total}</strong> decisions. ${escapeHTML(hint)}</span>${actions.join('')}`;
      const showAll = $('#show-all-nodes', counter);
      if (showAll) showAll.addEventListener('click', () => { state.viewMode = 'full'; toast(`Rendering all ${total} decisions; layout may take a moment.`); renderOverviewGraph(); });
      const collapse = $('#collapse-all', counter);
      if (collapse) collapse.addEventListener('click', () => { state.expandedDomains.clear(); state.revealed.clear(); renderOverviewGraph(); });
      const useClusters = $('#use-clusters', counter);
      if (useClusters) useClusters.addEventListener('click', () => { state.viewMode = 'auto'; renderOverviewGraph(); });
      const clear = $('#clear-filters', counter);
      if (clear) clear.addEventListener('click', clearFilters);
    }

    let emptyMessage = status.initialized ? 'No decisions yet. Open Setup & validate to create the first one.' : 'Repository is not initialized. Open Setup & validate.';
    let emptyAction = null;
    const view = total ? buildViewGraph(visible) : { nodes: [], edges: [] };
    if (total && !view.nodes.length) {
      emptyMessage = 'Every decision is hidden by the current filters.';
      emptyAction = document.createElement('button');
      emptyAction.className = 'button secondary small';
      emptyAction.textContent = 'Clear filters';
      emptyAction.addEventListener('click', clearFilters);
    }
    overviewGraph.render(view, { records: status.records || [], emptyMessage, emptyAction });
    overviewGraph.applyHighlight(state.search, state.selected, null);
    renderLegend($('#graph-legend'), false);
  }

  function renderLegend(target, impact) {
    const domains = '';
    const statusDots = `<span><span class="dot-sample" style="background:var(--amber)"></span>proposed</span><span><span class="dot-sample" style="background:var(--green)"></span>accepted</span><span><span class="dot-sample" style="background:var(--violet)"></span>baseline</span>`;
    const impactItems = impact ? '<span><span class="swatch affected"></span>directly affected</span><span><span class="swatch prerequisite"></span>prerequisite</span><span><span class="swatch proposed"></span>proposed new</span>' : '';
    target.innerHTML = `${domains}${statusDots}${impactItems}<span><span class="line-sample"></span>builds on</span><span><span class="line-sample supersedes"></span>replaced by</span><span>● fresh · ◐ aging · ▲ sources changed · ? unknown</span>`;
  }

  function clearFilters() {
    state.statusFilter.clear();
    state.domainFilter.clear();
    state.focus = null;
    state.search = '';
    $('#graph-search').value = '';
    renderFilterChips();
    renderOverviewGraph();
  }

  function renderFilterChips() {
    const build = (container, values, selected, label) => {
      container.innerHTML = `<span class="chip-group-label">${label}</span>`;
      values.forEach(value => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `chip ${label === 'Status' ? `status-${value}` : ''}`;
        button.textContent = value;
        button.setAttribute('aria-pressed', selected.has(value) ? 'true' : 'false');
        button.addEventListener('click', () => { if (selected.has(value)) selected.delete(value); else selected.add(value); renderFilterChips(); renderOverviewGraph(); });
        container.append(button);
      });
    };
    const present = new Set((state.status?.records || []).map(record => record.status));
    const presentDomains = new Set((state.status?.records || []).map(record => record.domain));
    build($('#status-filters'), STATUSES.filter(value => present.has(value) || state.statusFilter.has(value)), state.statusFilter, 'Status');
    build($('#domain-filters'), DOMAINS.filter(value => presentDomains.has(value) || state.domainFilter.has(value)), state.domainFilter, 'Domain');
  }

  let searchTimer = null;
  $('#graph-search').addEventListener('input', event => {
    state.search = event.target.value;
    if (effectiveMode() === 'clusters' && !state.focus) {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(renderOverviewGraph, 180);   // search reveals hidden matches in cluster mode
    } else overviewGraph.applyHighlight(state.search, state.selected, null);
  });
  $('#zoom-in').addEventListener('click', () => overviewGraph.zoom(1.2));
  $('#zoom-out').addEventListener('click', () => overviewGraph.zoom(1 / 1.2));
  $('#zoom-fit').addEventListener('click', () => overviewGraph.fit());
  $('#zoom-reset').addEventListener('click', () => overviewGraph.reset());
  $('#impact-fit').addEventListener('click', () => impactGraph.fit());
  $('#focus-depth').addEventListener('change', event => { state.focusDepth = Number(event.target.value) || 1; renderOverviewGraph(); });
  $('#focus-clear').addEventListener('click', () => { state.focus = null; renderOverviewGraph(); });

  $('#overview-graph').addEventListener('dblclick', event => {
    const node = event.target.closest('.node');
    if (!node || node.dataset.id.startsWith('cluster:')) return;
    state.focus = node.dataset.id;
    renderOverviewGraph();
  });
  $('#overview-graph').addEventListener('keydown', event => {
    const node = event.target.closest('.node');
    if (!node || event.key.toLowerCase() !== 'f' || node.dataset.id.startsWith('cluster:')) return;
    state.focus = node.dataset.id;
    renderOverviewGraph();
    overviewGraph.focusNode(node.dataset.id);
  });

  // Help popover
  const help = $('#help-popover');
  const helpToggle = $('#help-toggle');
  function setHelp(open) {
    help.classList.toggle('visible', open);
    helpToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) $('#help-close').focus({ preventScroll: true }); else helpToggle.focus({ preventScroll: true });
  }
  helpToggle.addEventListener('click', () => setHelp(!help.classList.contains('visible')));
  $('#help-close').addEventListener('click', () => setHelp(false));
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if (help.classList.contains('visible')) { setHelp(false); return; }
    if (!workspace.classList.contains('inspector-closed')) closeInspector();
  });

  // --- status rendering ----------------------------------------------------------

  function renderStatus(status) {
    state.status = status;
    state.project = status.project;
    $('#project-input').value = state.project;
    $('#stat-project').textContent = status.project_name;
    $$('.stat.skeleton').forEach(stat => stat.classList.remove('skeleton'));
    const repoName = $('#repo-name'); if (repoName) repoName.textContent = status.project_name || status.project;
    const repoChip = $('#repo-chip'); if (repoChip) repoChip.title = status.project || '';
    const modelText = (status.model && status.model.description) || (status.ai_configured ? 'configured' : 'not configured');
    $('#stat-ai').textContent = modelText.length > 34 ? modelText.slice(0, 33) + '…' : modelText;
    $('#stat-ai').title = modelText;
    $('#stat-ai').className = `stat-value small ${status.ai_configured ? 'good' : 'warn'}`;
    $('#stat-adrs').textContent = status.adr_count;
    $('#stat-valid').textContent = !status.initialized ? 'Not initialized' : status.valid ? 'Pass' : `${status.issues.length} issue${status.issues.length === 1 ? '' : 's'}`;
    $('#stat-valid').className = `stat-value small ${status.valid ? 'good' : status.initialized ? 'bad' : 'warn'}`;
    const counts = { fresh: 0, aging: 0, 'sources-changed': 0, unknown: 0 };
    (status.records || []).forEach(record => { counts[freshnessOf(record)] += 1; });
    $('#stat-fresh').textContent = status.adr_count ? `${counts.fresh} fresh · ${counts.aging} aging · ${counts['sources-changed']} changed · ${counts.unknown} unknown` : '—';
    $('#stat-fresh').className = `stat-value small ${counts['sources-changed'] ? 'bad' : counts.unknown === status.adr_count && status.adr_count ? 'warn' : 'good'}`;

    const modelReady = Boolean(status.ai_configured);
    const modelInfo = status.model || {};
    const description = $('#model-description');
    if (description) description.textContent = modelInfo.description || (modelReady ? 'configured' : 'not configured');
    const notice = $('#ai-notice');
    if (notice) notice.classList.toggle('ok', modelReady);
    const select = $('#model-provider');
    if (select) {
      const available = new Set(modelInfo.available || []);
      [...select.options].forEach(option => {
        if (option.value === 'none') return;
        option.disabled = !available.has(option.value);
        option.textContent = option.textContent.replace(/ — not found on PATH$/, '') + (available.has(option.value) ? '' : ' — not found on PATH');
      });
      if (modelInfo.provider && [...select.options].some(option => option.value === modelInfo.provider)) select.value = modelInfo.provider;
      else if (available.size) select.value = [...available][0];
      else select.value = 'none';
    }
    const baselineButton = $('[data-action="baseline"]');
    if (baselineButton) {
      const lock = status.adr_count > 0 || !modelReady;
      baselineButton.dataset.locked = lock ? 'true' : 'false';
      baselineButton.disabled = lock;
      baselineButton.title = !modelReady ? 'Set DECINAV_AI_CMD before starting the server' : status.adr_count > 0 ? 'Baseline already exists' : '';
    }
    ['[data-action="ingest"]', '#run-impact', '#run-query'].forEach(selector => {
      const button = $(selector);
      if (!button) return;
      button.dataset.locked = modelReady ? 'false' : 'true';
      button.disabled = !modelReady;
      button.title = modelReady ? '' : 'Set DECINAV_AI_CMD before starting the server; see Setup & validate';
    });
    const loadLast = $('#load-last-impact');
    loadLast.disabled = !status.last_impact_available;
    loadLast.dataset.locked = status.last_impact_available ? 'false' : 'true';
    $('#impact-hint').textContent = status.last_impact_available ? 'A saved impact result is available.' : 'No saved result yet. Run `decinav impact <prd> --save` or the /decinav-impact skill to enable "Load last impact".';

    if (state.focus && !(status.graph?.nodes || []).some(node => node.id === state.focus)) state.focus = null;
    renderFilterChips();
    renderOverviewGraph();
    renderRecords(status);
    renderTimeline(status);
    renderIssues(status);
  }

  function renderRecords(status) {
    const records = $('#record-list');
    if (!status.records.length) { records.innerHTML = '<div class="empty">No ADRs yet. Open Setup &amp; validate to initialize this repository.</div>'; return; }
    records.innerHTML = status.records.map(record => `
      <button class="record-card ${escapeHTML(record.domain)}" type="button" data-id="${escapeHTML(record.id)}">
        <div class="record-top"><span class="record-id">${escapeHTML(record.id)}</span>${chip(record.status, `status-${record.status}`)}</div>
        <div class="record-title">${escapeHTML(record.title)}</div>
        <div class="chips">${chip(record.domain)}${chip(`${FRESHNESS_SYMBOL[freshnessOf(record)]} ${FRESHNESS_LABEL[freshnessOf(record)]}`, freshnessOf(record))}${chip(`builds on ${record.depends_on.length}`)}${chip(`replaces ${record.supersedes.length}`)}</div>
      </button>`).join('');
    $$('.record-card', records).forEach(card => card.addEventListener('click', () => { switchView('overview'); selectNode(card.dataset.id, card); }));
  }

  function renderTimeline(status) {
    const list = $('#timeline-list');
    const records = [...(status.records || [])].sort((a, b) => (a.date || '').localeCompare(b.date || '') || a.id.localeCompare(b.id));
    if (!records.length) { list.innerHTML = '<li><div class="empty">No decisions recorded yet.</div></li>'; return; }
    const supersededBy = new Map();
    records.forEach(record => (record.supersedes || []).forEach(item => { if (!supersededBy.has(item.id)) supersededBy.set(item.id, []); supersededBy.get(item.id).push(record.id); }));
    list.innerHTML = records.map(record => {
      const supersedes = (record.supersedes || []).map(item => `<span class="arrow">⟵ replaces</span> ${escapeHTML(item.id)}`).join(' · ');
      const later = (supersededBy.get(record.id) || []).map(id => `<span class="arrow">⟶ replaced by</span> ${escapeHTML(id)}`).join(' · ');
      const links = [supersedes, later].filter(Boolean).join(' · ');
      return `<li><button class="timeline-card" type="button" data-id="${escapeHTML(record.id)}"><div class="timeline-date">${escapeHTML(record.date || '')} · ${escapeHTML(record.id)}</div><div class="timeline-title">${escapeHTML(record.title)}</div><div class="chips">${chip(record.status, `status-${record.status}`)}${chip(record.domain)}${chip(`${FRESHNESS_SYMBOL[freshnessOf(record)]} ${FRESHNESS_LABEL[freshnessOf(record)]}`, freshnessOf(record))}</div>${links ? `<div class="timeline-links">${links}</div>` : ''}</button></li>`;
    }).join('');
    $$('.timeline-card', list).forEach(card => card.addEventListener('click', () => { switchView('overview'); selectNode(card.dataset.id, card); }));
  }

  function renderIssues(status) {
    const issues = $('#issue-list');
    if (!status.initialized) issues.innerHTML = '<div class="empty">Repository is not initialized.</div>';
    else if (!status.issues.length) issues.innerHTML = '<div class="issue ok">All ADR schema and reference checks pass. This does not verify that decisions match the code; see freshness per ADR.</div>';
    else issues.innerHTML = status.issues.map(issue => `<div class="issue"><strong>${escapeHTML(issue.code)}</strong>${issue.path ? ` · ${escapeHTML(issue.path)}` : ''}<br>${escapeHTML(issue.message)}</div>`).join('');
  }

  async function loadStatus() {
    const status = await api('status');
    renderStatus(status);
    return status;
  }

  function renderList(selector, items, formatter, emptyText) {
    const node = $(selector);
    node.innerHTML = items.length ? items.map(item => `<li>${formatter(item)}</li>`).join('') : `<li>${escapeHTML(emptyText)}</li>`;
  }

  function renderImpact(data, meta) {
    renderList('#affected-list', data.directly_affected_adrs || [], item => `<strong>${escapeHTML(item.id)}</strong> — ${escapeHTML(item.reason_for_change)}`, 'None identified.');
    renderList('#prerequisite-list', data.prerequisite_adrs || [], item => `<strong>${escapeHTML(item.id)}</strong> — ${escapeHTML(item.reason_required)}`, 'None identified.');
    renderList('#risk-list', data.architectural_risks || [], escapeHTML, 'No contradictions identified.');
    renderList('#proposed-list', data.proposed_new_adrs || [], escapeHTML, 'No new decisions proposed.');
    $('#impact-meta').textContent = meta;
    $('#impact-results').classList.add('visible');
    const graph = data.graph || { nodes: [], edges: [] };
    const nodes = graph.nodes.map(node => ({ ...node, kind: 'adr' }));
    // Large repositories: show only the impacted neighbourhood so the overlay stays readable.
    if (nodes.length > CLUSTER_THRESHOLD) {
      const keep = new Set(nodes.filter(node => ['affected', 'prerequisite', 'proposed'].includes(node.class)).map(node => node.id));
      graph.edges.forEach(edge => { if (keep.has(edge.source) || keep.has(edge.target)) { keep.add(edge.source); keep.add(edge.target); } });
      const subset = nodes.filter(node => keep.has(node.id));
      $('#impact-meta').textContent = `${meta} Showing the ${subset.length} impacted and adjacent decisions of ${nodes.length}.`;
      impactGraph.render({ nodes: subset, edges: graph.edges.filter(edge => keep.has(edge.source) && keep.has(edge.target)) }, { records: state.status?.records || [], emptyMessage: 'No decisions to display.' });
    } else impactGraph.render({ nodes, edges: graph.edges }, { records: state.status?.records || [], emptyMessage: 'No decisions to display.' });
    renderLegend($('#graph-legend'), true);
  }

  // --- navigation and actions ------------------------------------------------------

  function switchView(view) {
    $$('.nav-button').forEach(item => item.classList.toggle('active', item.dataset.view === view));
    $$('.view').forEach(item => item.classList.toggle('active', item.id === `view-${view}`));
    if (view === 'overview' && overviewGraph.svg) overviewGraph.fit();
    if (view === 'impact' && impactGraph.svg) impactGraph.fit();
  }
  $$('.nav-button').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));

  $('#load-project').addEventListener('click', event => withBusy(event.currentTarget, async () => {
    state.project = $('#project-input').value.trim();
    state.focus = null; state.selected = null; state.viewMode = 'auto'; state.expandedDomains.clear(); state.revealed.clear();
    closeInspector();
    await loadStatus();
    toast('Project loaded.');
  }).catch(() => {}));
  $('#refresh-status').addEventListener('click', event => withBusy(event.currentTarget, async () => { await loadStatus(); toast('Architecture refreshed.'); }).catch(() => {}));
  $('#theme-toggle')?.addEventListener('click', () => { const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'; applyTheme(next); storeTheme(next); });
  $('#switch-project')?.addEventListener('click', event => {
    const panel = $('#project-switch');
    const open = panel.hidden;
    panel.hidden = !open;
    event.currentTarget.setAttribute('aria-expanded', String(open));
    if (open) { $('#project-input').value = state.project; $('#project-input').focus(); }
  });
  $('#copy-path')?.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(state.project); toast('Repository path copied.'); }
    catch (error) { toast('Could not copy; the path is in the Switch… box.', true); }
  });

  $('#prd-file').addEventListener('change', async event => {
    const file = event.target.files[0];
    if (!file) return;
    $('#prd-name').value = file.name;
    $('#prd-text').value = await file.text();
    $('#impact-hint').textContent = `${Math.ceil(file.size / 1024)} KiB loaded`;
  });

  $('#run-impact').addEventListener('click', event => withBusy(event.currentTarget, async () => {
    const data = await api('impact', { prd_name: $('#prd-name').value, prd_text: $('#prd-text').value });
    renderImpact(data, `Analyzed ${$('#prd-name').value || 'pasted PRD'} just now through the server's AI command.`);
    toast(data.message);
  }).catch(() => {}));

  $('#load-last-impact').addEventListener('click', event => withBusy(event.currentTarget, async () => {
    const data = await api('impact/last');
    renderImpact(data, `Saved result for ${data.prd || 'PRD'} · generated ${data.generated_at || 'unknown'} at commit ${String(data.commit || 'unknown').slice(0, 10)}.`);
    toast('Loaded the last saved impact result.');
  }).catch(() => {}));

  $('#run-query').addEventListener('click', event => withBusy(event.currentTarget, async () => {
    const data = await api('query', { question: $('#question').value });
    $('#query-answer').innerHTML = renderMarkdown(data.answer);
    $('#query-results').classList.add('visible');
    toast(data.message);
  }).catch(() => {}));
  $('#question').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); $('#run-query').click(); } });
  $$('.sample-question').forEach(button => button.addEventListener('click', () => { $('#question').value = button.dataset.question; $('#question').focus(); }));

  $('#save-model')?.addEventListener('click', event => withBusy(event.currentTarget, async () => {
    const data = await api('setup', { provider: $('#model-provider').value });
    renderStatus(data.status);
    toast(data.message);
  }).catch(() => {}));

  $$('[data-action]').forEach(button => button.addEventListener('click', event => withBusy(event.currentTarget, async () => {
    const action = event.currentTarget.dataset.action;
    const payload = action === 'ingest' ? { folder: $('#ingest-folder').value } : {};
    const data = await api(action, payload);
    if (data.status) renderStatus(data.status); else await loadStatus();
    toast(data.message);
  }).catch(() => {})));

  window.addEventListener('resize', () => { if (overviewGraph.svg) overviewGraph.fit(); });
  window.archUI = { state, overviewGraph, impactGraph, renderOverviewGraph };   // for measurement in the console

  $('#project-input').value = state.project;
  loadStatus().catch(error => toast(error.message, true));
})();
