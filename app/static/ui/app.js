/* Football Scout · analyst console.
 * Plain script, no build step. D3 v7 is loaded before this file.
 * Security: API data is never parsed as HTML; the DOM is built with textContent and D3 .text().
 */
(function main() {
  'use strict';

  /* ================================================================== */
  /* Error capture (read by automated checks)                            */
  /* ================================================================== */
  const jsErrors = document.getElementById('js-errors');
  function logJsError(text) {
    const line = document.createElement('div');
    line.textContent = text;
    jsErrors.appendChild(line);
  }
  window.onerror = function onError(message, source, line, col) {
    logJsError('error: ' + message + ' @ ' + (source || '?') + ':' + (line || 0) + ':' + (col || 0));
    return false;
  };
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    logJsError('unhandledrejection: ' + (reason && reason.message ? reason.message : String(reason)));
  });

  if (typeof window.d3 === 'undefined') {
    logJsError('error: d3 failed to load');
    return;
  }

  /* ================================================================== */
  /* Small helpers                                                       */
  /* ================================================================== */
  // Motion is off when the viewer asks for reduced motion, or explicitly with ?motion=0.
  // A virtual-time headless capture never produces animation frames, so it gets final states too,
  // unless ?motion=1 forces the animated code paths (path pulse, ripples, draw-in) for a test run.
  const MOTION_PARAM = new URLSearchParams(window.location.search).get('motion');
  const STATIC_CAPTURE = /HeadlessChrome/.test(navigator.userAgent) && MOTION_PARAM !== '1';
  const REDUCED = MOTION_PARAM === '0' || STATIC_CAPTURE ||
    Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  if (REDUCED) document.documentElement.classList.add('no-motion');
  const dur = (ms) => (REDUCED ? 0 : ms);
  const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
  const $ = (sel) => document.querySelector(sel);
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    setProps(el, props);
    appendKids(el, kids);
    return el;
  }
  function sv(tag, props, ...kids) {
    const el = document.createElementNS(SVG_NS, tag);
    setProps(el, props);
    appendKids(el, kids);
    return el;
  }
  function setProps(el, props) {
    if (!props) return;
    Object.keys(props).forEach((key) => {
      const value = props[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') el.setAttribute('class', value);
      else if (key === 'text') el.textContent = String(value);
      else if (key.slice(0, 2) === 'on' && typeof value === 'function') el.addEventListener(key.slice(2), value);
      else if (key === 'style' && typeof value === 'object') Object.keys(value).forEach((p) => el.style.setProperty(p, value[p]));
      else if (value === true) el.setAttribute(key, '');
      else el.setAttribute(key, String(value));
    });
  }
  function appendKids(el, kids) {
    kids.flat(3).forEach((kid) => {
      if (kid === null || kid === undefined || kid === false) return;
      el.append(kid instanceof Node ? kid : String(kid));
    });
  }
  function clear(el) { el.replaceChildren(); return el; }

  function fmtEur(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    const v = Number(value);
    const abs = Math.abs(v);
    if (abs >= 1e9) return '€' + (v / 1e9).toFixed(2) + 'B';
    if (abs >= 1e6) return '€' + (v / 1e6).toFixed(1) + 'M';
    if (abs >= 1e3) return '€' + Math.round(v / 1e3) + 'K';
    return '€' + Math.round(v);
  }
  function fmtPct(p) { return (p === null || p === undefined) ? '—' : Math.round(Number(p) * 100) + '%'; }
  function fmtScore(s, digits) {
    if (s === null || s === undefined || Number.isNaN(Number(s))) return '—';
    const v = Number(s);
    const abs = Math.abs(v);
    // Real-graph scores span 0.01 (similarity) to 100,000+ (betweenness): fewer decimals as the value grows.
    if (abs >= 1000) return Math.round(v).toLocaleString('en-US');
    const wanted = digits === undefined ? 3 : digits;
    return v.toFixed(abs >= 10 ? Math.min(wanted, 2) : wanted);
  }
  /** Growth between two values: a percentage, or a multiple once it passes x10 (+359900% reads badly). */
  function fmtGrowth(first, last) {
    if (!first) return null;
    const ratio = last / first;
    if (ratio >= 10) return '×' + (ratio >= 100 ? Math.round(ratio).toLocaleString('en-US') : ratio.toFixed(1));
    const pct = (ratio - 1) * 100;
    return (pct >= 0 ? '+' : '') + pct.toFixed(0) + '%';
  }
  function ageOf(dob) {
    if (!dob || !/^\d{4}-\d{2}-\d{2}$/.test(dob)) return null;
    const parts = dob.split('-').map(Number);
    const now = new Date();
    let age = now.getFullYear() - parts[0];
    const month = now.getMonth() + 1;
    if (month < parts[1] || (month === parts[1] && now.getDate() < parts[2])) age -= 1;
    return age;
  }
  function safeUrl(raw) {
    try {
      const url = new URL(String(raw));
      return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : null;
    } catch (err) {
      return null;
    }
  }
  function debounce(fn, ms) {
    let timer = null;
    const debounced = function debounced(...args) {
      clearTimeout(timer);
      timer = setTimeout(() => { timer = null; fn.apply(this, args); }, ms);
    };
    debounced.cancel = () => { clearTimeout(timer); timer = null; };
    return debounced;
  }
  /** Display only: turns raw amounts such as "3000000 EUR" into "€3.0M" (the verified text stays in a title). */
  function prettyAmounts(text) {
    return String(text || '').replace(/\b(\d+)\s?EUR\b/g, (all, digits) => fmtEur(Number(digits)));
  }
  /** Text with ISO dates kept on one line (never split at the hyphen); built from text nodes only. */
  function amountText(tag, cls, text) {
    const raw = String(text || '');
    const pretty = prettyAmounts(raw);
    const el = h(tag, { class: cls, title: pretty !== raw ? raw : null });
    const re = /\d{4}-\d{2}-\d{2}/g;
    let last = 0;
    let match = re.exec(pretty);
    while (match) {
      if (match.index > last) el.append(pretty.slice(last, match.index));
      el.append(h('span', { class: 'nowrap', text: match[0] }));
      last = match.index + match[0].length;
      match = re.exec(pretty);
    }
    if (last < pretty.length) el.append(pretty.slice(last));
    return el;
  }
  /** Graph labels stay short; the full name is in the tooltip and the drawer. */
  function shortLabel(text, max) {
    const s = String(text || '');
    const limit = max || 28;
    return s.length > limit ? s.slice(0, limit - 1).trimEnd() + '…' : s;
  }
  /** Parses an evidence id such as Player:1, Valuation:1-2022-06-30 or PREDICTED_MOVE:9->5. */
  function parseEvidence(id) {
    const match = /^([A-Za-z_]+):(.+)$/.exec(String(id));
    if (!match) return { kind: 'invalid', id };
    const label = match[1];
    const rest = match[2];
    const out = { kind: label, id, rest, key: label + ':' + rest };
    if ((label === 'Player' || label === 'Club') && /^\d+$/.test(rest)) {
      out[label === 'Player' ? 'playerId' : 'clubId'] = Number(rest);
    } else if (label === 'Player' || label === 'Club') {
      out.kind = 'unknown';
    } else if (label === 'Valuation') {
      const m = /^(\d+)-/.exec(rest);
      if (m) out.playerId = Number(m[1]);
      else out.kind = 'unknown';
    } else if (label === 'PREDICTED_MOVE') {
      const m = /^(\d+)->(\d+)$/.exec(rest);
      if (m) { out.playerId = Number(m[1]); out.clubId = Number(m[2]); } else out.kind = 'unknown';
    }
    return out;
  }
  const POS_COLOR = {
    Goalkeeper: 'var(--pos-gk)', Defender: 'var(--pos-def)', Midfield: 'var(--pos-mid)', Attack: 'var(--pos-att)',
  };
  const POS_SHORT = { Goalkeeper: 'GK', Defender: 'DEF', Midfield: 'MID', Attack: 'ATT' };
  const SUB_SHORT = {
    Goalkeeper: 'GK', 'Centre-Back': 'CB', 'Left-Back': 'LB', 'Right-Back': 'RB', 'Defensive Midfield': 'DM',
    'Central Midfield': 'CM', 'Attacking Midfield': 'AM', 'Left Midfield': 'LM', 'Right Midfield': 'RM',
    'Left Winger': 'LW', 'Right Winger': 'RW', 'Second Striker': 'SS', 'Centre-Forward': 'CF',
  };
  const NATURE_COLOR = { verified: '#7FB2F0', predicted: '#F0A04B', rumour: '#C48CF5' };
  // A claim whose nature is missing or unknown is never shown as a fact: neutral badge and colour.
  const NEUTRAL_COLOR = '#8A9A92';
  const natureOf = (claim) => (['verified', 'predicted', 'rumour'].indexOf(claim && claim.nature) >= 0 ? claim.nature : 'unchecked');

  /* ================================================================== */
  /* API key                                                             */
  /* ================================================================== */
  const KEY_STORE = 'fsa.apiKey';
  const keyInput = $('#api-key');
  try { keyInput.value = window.sessionStorage.getItem(KEY_STORE) || ''; } catch (err) { /* storage blocked */ }
  keyInput.addEventListener('input', () => {
    try { window.sessionStorage.setItem(KEY_STORE, keyInput.value.trim()); } catch (err) { /* storage blocked */ }
  });
  const apiKey = () => keyInput.value.trim();
  // The field sits in a form (no Chrome notice, no password-manager save); Enter must not reload the page.
  $('#key-form').addEventListener('submit', (event) => event.preventDefault());

  /* ================================================================== */
  /* Fetch wrapper                                                       */
  /* ================================================================== */
  class ApiError extends Error {
    constructor(result) {
      super('HTTP ' + result.status);
      this.status = result.status;
      this.data = result.data;
      this.network = result.network || null;
    }
  }

  /** Low level: never throws. Returns {status, data, ms}; status 0 means the network failed. */
  async function request(method, path, opts) {
    const options = opts || {};
    const headers = { Accept: 'application/json' };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (options.auth && apiKey()) headers['X-API-Key'] = apiKey();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 30000);
    const started = performance.now();
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller.signal,
        credentials: 'same-origin',
      });
    } catch (err) {
      clearTimeout(timer);
      return { status: 0, data: null, ms: performance.now() - started, network: err && err.name === 'AbortError' ? 'timeout' : 'network' };
    }
    let text = '';
    try { text = await response.text(); } catch (err) { text = ''; }
    clearTimeout(timer);
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (err) { data = null; }
    }
    return { status: response.status, data, ms: performance.now() - started };
  }

  async function api(path, opts) {
    const options = opts || {};
    const result = await request(options.method || 'GET', path, options);
    if (result.status >= 200 && result.status < 300) {
      if (result.data === null || result.data === undefined) throw new ApiError(Object.assign({}, result, { network: 'malformed' }));
      return result.data;
    }
    throw new ApiError(result);
  }

  class FormError extends Error {}

  function errorInfo(err) {
    if (err instanceof FormError) return { title: 'Check the form', lines: [err.message] };
    if (!(err instanceof ApiError)) {
      return { title: 'Unexpected error', lines: [err && err.message ? err.message : String(err)] };
    }
    const data = err.data || {};
    const detail = typeof data.detail === 'string' ? data.detail : null;
    if (err.network === 'malformed') {
      return { title: 'Malformed response', lines: ['The server answered ' + err.status + ' without a readable JSON body (a proxy may have cut it). Try again.'] };
    }
    switch (err.status) {
      case 0:
        return err.network === 'timeout'
          ? { title: 'Request timed out', lines: ['The server took too long to answer. Try again.'] }
          : { title: 'Network failure', lines: ['The API cannot be reached. Check that the server is running.'] };
      case 401:
        return { title: 'API key required', lines: [detail, 'Enter a valid key in the API key field, top right, then retry.'].filter(Boolean) };
      case 404:
        return { title: 'Not found', lines: [detail || 'The requested item does not exist.'] };
      case 422: {
        const lines = [];
        if (Array.isArray(data.errors) && data.errors.length) {
          data.errors.forEach((e) => lines.push((e.field || '?') + ' (' + (e.location || '?') + '): ' + (e.message || '')));
        } else if (Array.isArray(data.detail)) {
          data.detail.forEach((e) => lines.push((Array.isArray(e.loc) ? e.loc.join('.') : '?') + ': ' + (e.msg || '')));
        }
        if (!lines.length) lines.push(detail || 'Some parameters are invalid.');
        return { title: 'Invalid request', lines };
      }
      case 429:
        return { title: 'Too many requests', lines: ['Rate limit reached. Wait a few seconds before retrying.'] };
      case 501:
        return { title: 'Not implemented', lines: [detail || 'This feature is not implemented on the server yet.'] };
      case 503:
        return { title: 'Neo4j unavailable', lines: [detail || 'The graph database is unreachable. Check the Neo4j container.'] };
      default:
        return { title: 'Server error ' + err.status, lines: [detail || 'The server could not complete the request.'] };
    }
  }

  function errorBox(err) {
    const info = errorInfo(err);
    const box = h('div', { class: 'msg error', role: 'alert' }, h('div', { class: 'msg-title', text: info.title }));
    if (info.lines.length === 1) box.append(h('div', { text: info.lines[0] }));
    else box.append(h('ul', null, info.lines.map((line) => h('li', { text: line }))));
    return box;
  }
  function loadingBox(text) { return h('div', { class: 'loading', text: text || 'Loading' }); }

  const toasts = $('#toasts');
  function toast(text, kind) {
    const el = h('div', { class: 'toast ' + (kind || ''), role: 'status' });
    el.textContent = text;
    toasts.append(el);
    setTimeout(() => el.remove(), kind === 'error' ? 7000 : 4000);
    while (toasts.children.length > 4) toasts.firstChild.remove();
  }
  function toastError(err) {
    const info = errorInfo(err);
    toast(info.title + ': ' + info.lines.join(' '), 'error');
  }
  /**
   * Persistent, dismissible messages over the board (deep-link failures stay visible).
   * Each context keeps its own line, so several bad parameters are all shown at once.
   */
  function stageBanner(context, err, retry) {
    const box = $('#stage-banner');
    const info = errorInfo(err);
    const entry = h('div', { class: 'banner-entry' });
    entry.dataset.context = context;
    const drop = () => {
      entry.remove();
      box.hidden = !box.children.length;
      boardBox();
    };
    const retryBtn = retry ? h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: retry.label || 'Retry' }) : null;
    if (retryBtn) {
      retryBtn.addEventListener('click', () => {
        drop();
        run(retry.fn);
      });
    }
    entry.append(
      h('div', { class: 'banner-text' },
        h('div', { class: 'banner-head' },
          h('span', { class: 'caps banner-ctx', text: context }),
          h('span', { class: 'banner-title', text: info.title })),
        h('span', { text: info.lines.join(' ') })),
      h('div', { class: 'row banner-actions' }, retryBtn,
        h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Dismiss', onclick: drop })));
    Array.from(box.children).forEach((old) => { if (old.dataset.context === context) old.remove(); });
    box.append(entry);
    while (box.children.length > 5) box.firstChild.remove();
    box.hidden = false;
    boardBox();
  }

  /** Runs an async action, never lets a rejection escape. */
  function run(fn) {
    return Promise.resolve().then(fn).catch((err) => { toastError(err); });
  }
  /** Disables a button at once (same tick as the click) while its request runs; a second click is ignored. */
  function withBusy(button, fn) {
    if (button.disabled) return Promise.resolve(undefined);
    button.disabled = true;
    button.classList.add('busy');
    return run(fn).finally(() => {
      button.disabled = false;
      button.classList.remove('busy');
    });
  }

  /* ================================================================== */
  /* Health pill                                                         */
  /* ================================================================== */
  const healthPill = $('#health');
  async function pollHealth() {
    const result = await request('GET', '/api/health', { timeout: 8000 });
    const text = healthPill.querySelector('.pill-text');
    healthPill.classList.remove('pill-ok', 'pill-bad', 'pill-wait');
    const neo4jDown = (result.status === 200 || result.status === 503) && result.data && result.data.neo4j === false;
    if (result.status === 200 && result.data && result.data.neo4j === true) {
      healthPill.classList.add('pill-ok');
      text.textContent = 'Neo4j online';
    } else if (result.status === 0) {
      healthPill.classList.add('pill-bad');
      text.textContent = 'API unreachable';
    } else if (neo4jDown) {
      healthPill.classList.add('pill-bad');
      text.textContent = 'Neo4j unreachable';
    } else if (result.status === 429) {
      healthPill.classList.add('pill-wait');
      text.textContent = 'Rate limited';
    } else {
      healthPill.classList.add('pill-bad');
      text.textContent = 'API error ' + result.status;
    }
  }

  /* ================================================================== */
  /* Graph state and rendering                                           */
  /* ================================================================== */
  const LABEL = { player: 'Player', club: 'Club', competition: 'Competition' };
  const G = {
    nodes: new Map(),
    links: new Map(),
    selected: null,
    hovered: null,
    highlight: null,
    clubInfo: new Map(),
    hubMax: 1,
    metricRows: {}, // how many clubs each ranking returned (the API ranks the top 100 at most)
    focusKey: null, // node opened from a panel or a link: framed with its neighbours on a dense board
  };
  const keyOf = (type, id) => LABEL[type] + ':' + id;
  // Camera and playback state, declared before any callback that reads it.
  let fitTimer = null;
  let fitOnEnd = false;
  let fitKeys = null;
  let pathRun = 0;
  let legendCompactH = 0;
  const pinTimers = new Set(); // d3 timers moving teammate-path players onto their line

  const svg = d3.select('#graph');
  const stage = document.querySelector('.stage');
  const viewport = svg.append('g').attr('class', 'viewport');
  const linkLayer = viewport.append('g').attr('class', 'links');
  const nodeLayer = viewport.append('g').attr('class', 'nodes');
  const labelLayer = viewport.append('g').attr('class', 'labels');
  // Matches labels of a teammate path sit above node labels so they are never covered.
  const pathLabelLayer = viewport.append('g').attr('class', 'path-labels');
  const fxLayer = viewport.append('g').attr('class', 'fx');
  let linkSel = linkLayer.selectAll('g.link');
  let nodeSel = nodeLayer.selectAll('g.node');
  let labelSel = labelLayer.selectAll('text.node-label');
  let pathLabelSel = pathLabelLayer.selectAll('text.path-label');

  let labelFs = 11.5;
  let linkFs = 10;
  let tickCount = 0;
  let tabStopKey = null;
  let layoutSignature = '';
  // Every user-initiated open (club, player, evidence) takes a token: a slow older answer never steals the selection.
  let selectToken = 0;
  const claimSelection = () => { selectToken += 1; return selectToken; };
  const declutterSoon = debounce(() => declutter(), 120);
  const zoom = d3.zoom().scaleExtent([0.15, 4]).on('zoom', (event) => {
    viewport.attr('transform', event.transform);
    // Keep labels legible on screen whatever the zoom (between 10.5 and 13 px).
    const k = event.transform.k;
    const svgNode = svg.node();
    labelFs = Math.max(10.5, Math.min(13, 11.5 * k)) / k;
    linkFs = Math.max(10, Math.min(12, 10 * k)) / k;
    svgNode.style.setProperty('--lbl', labelFs.toFixed(2) + 'px');
    svgNode.style.setProperty('--lbl-link', linkFs.toFixed(2) + 'px');
    placeLinkLabels();
    declutterSoon();
  });
  svg.call(zoom).on('dblclick.zoom', null);
  (function initialTransform() {
    const box = svg.node().getBoundingClientRect();
    svg.call(zoom.transform, d3.zoomIdentity.translate(box.width / 2, box.height / 2 - 30));
  }());
  svg.on('click', (event) => {
    if (event.target === svg.node()) {
      closeDrawer();
      setHighlight(null);
    }
  });

  function playerRadius(value) { return 5 + Math.min(10, Math.sqrt(Math.max(0, Number(value) || 0) / 1e6) * 1.4); }
  /** Half side of a club square: 10 to 17 px, so clubs never outweigh the players that carry the data. */
  function clubHalf(score) {
    if (score === null || score === undefined) return 11;
    return 10 + 7 * Math.max(0, Math.min(1, Number(score) / (G.hubMax || 1)));
  }
  /** Three-letter monogram printed inside a club square: SC Alvorada -> SCA, Kingsbridge United -> KU. */
  function monogram(name) {
    const words = String(name || '').split(/[\s-]+/).filter(Boolean);
    if (!words.length) return '';
    if (words.length === 1) return words[0].slice(0, 3).toUpperCase();
    return words.map((w) => (/^[A-Z]{2,3}$/.test(w) ? w : w[0].toUpperCase())).join('').slice(0, 3);
  }
  function computeR(node) {
    if (node.type === 'player') return playerRadius(node.data.market_value_eur);
    if (node.type === 'club') {
      const info = G.clubInfo.get(Number(node.id));
      return clubHalf(info ? info.pagerank : null);
    }
    return 8;
  }

  function linkDistance(link) {
    switch (link.type) {
      case 'PLAYS_FOR': return 72;
      case 'PLAYED_WITH': return 90;
      case 'SELLS_TO_LEAGUE': return 175;
      case 'SIMILAR': return 105;
      default: return 135;
    }
  }
  function linkStrength(link) {
    if (link.type === 'PLAYS_FOR') return 0.7;
    return link.type === 'SELLS_TO_LEAGUE' ? 0.6 : 0.35;
  }

  const sim = d3.forceSimulation()
    .force('link', d3.forceLink().id((d) => d.key).distance(linkDistance).strength(linkStrength))
    .force('charge', d3.forceManyBody().strength((d) => (d.type === 'club' ? -700 : -320)).distanceMax(700))
    .force('collide', d3.forceCollide().radius((d) => d.r + 9).iterations(2))
    .force('x', d3.forceX(0).strength(0.075))
    .force('y', d3.forceY(0).strength(0.1))
    .alphaDecay(0.03)
    .velocityDecay(0.45)
    .on('tick', ticked)
    .on('end', () => {
      declutter();
      // Once the layout has settled after a bulk addition, frame it again.
      if (fitOnEnd) {
        fitOnEnd = false;
        fit(fitKeys);
      }
    });

  /** Player and club ids end up in request paths: they are coerced to integers once, here. */
  function upsertNode(type, rawId, props, parentKey) {
    let id = rawId;
    if (type === 'player' || type === 'club') {
      id = Number(rawId);
      if (!Number.isInteger(id)) throw new Error('Invalid ' + type + ' id: ' + String(rawId).slice(0, 40));
    }
    const key = keyOf(type, id);
    let node = G.nodes.get(key);
    if (!node) {
      node = { key, type, id, name: key, data: {} };
      const parent = parentKey ? G.nodes.get(parentKey) : null;
      if (parent && Number.isFinite(parent.x)) {
        node.x = parent.x + (Math.random() - 0.5) * 16;
        node.y = parent.y + (Math.random() - 0.5) * 16;
      } else {
        const angle = Math.random() * Math.PI * 2;
        const dist = 40 + Math.random() * 160;
        node.x = Math.cos(angle) * dist;
        node.y = Math.sin(angle) * dist;
      }
      G.nodes.set(key, node);
    }
    if (props) {
      if (props.name) node.name = String(props.name);
      if (props.data) Object.assign(node.data, props.data);
    }
    node.r = computeR(node);
    return node;
  }

  function upsertLink(type, sourceKey, targetKey, extra) {
    let a = sourceKey;
    let b = targetKey;
    if ((type === 'PLAYED_WITH' || type === 'SIMILAR') && a > b) { const tmp = a; a = b; b = tmp; }
    const key = type + '|' + a + '|' + b;
    let link = G.links.get(key);
    if (!link) {
      link = { key, type, source: a, target: b };
      G.links.set(key, link);
    }
    if (extra) Object.assign(link, extra);
    return link;
  }

  function addPlayerSummary(summary, parentKey) {
    const node = upsertNode('player', summary.player_id, { name: summary.name, data: summary }, parentKey);
    if (summary.club_id !== null && summary.club_id !== undefined) {
      const club = upsertNode('club', summary.club_id, { name: summary.club_name || ('Club ' + summary.club_id) }, node.key);
      upsertLink('PLAYS_FOR', node.key, club.key);
    }
    return node;
  }

  const endKey = (end) => (typeof end === 'object' && end ? end.key : end);
  function nodeEndpoints(link) {
    const s = typeof link.source === 'object' ? link.source : G.nodes.get(link.source);
    const t = typeof link.target === 'object' ? link.target : G.nodes.get(link.target);
    return [s, t];
  }

  function shapeFor(group, d) {
    group.selectAll('*').remove();
    if (d.type === 'player') {
      group.append('circle').attr('class', 'ring');
      group.append('circle').attr('class', 'shape');
    } else if (d.type === 'club') {
      group.append('rect').attr('class', 'ring');
      group.append('rect').attr('class', 'shape club-shape');
      group.append('text').attr('class', 'club-mono').attr('text-anchor', 'middle').attr('dy', '0.35em');
    } else {
      group.append('path').attr('class', 'ring');
      group.append('path').attr('class', 'shape comp-shape');
    }
  }

  function updateShape(sel) {
    sel.each(function eachNode(d) {
      const g = d3.select(this);
      if (d.type === 'player') {
        const pos = d.data.position && POS_COLOR[d.data.position] ? d.data.position : 'unknown';
        g.select('circle.shape').attr('r', d.r).attr('class', 'shape pos-' + pos);
        g.select('circle.ring').attr('r', d.r + 4.5);
      } else if (d.type === 'club') {
        g.select('rect.shape').attr('x', -d.r).attr('y', -d.r).attr('width', d.r * 2).attr('height', d.r * 2).attr('rx', 3.5);
        g.select('rect.ring').attr('x', -d.r - 5).attr('y', -d.r - 5).attr('width', d.r * 2 + 10).attr('height', d.r * 2 + 10).attr('rx', 5);
        g.select('text.club-mono').style('font-size', (d.r * 0.62).toFixed(2) + 'px').text(monogram(d.name));
      } else {
        const s = d.r;
        g.select('path.shape').attr('d', 'M0,' + (-s) + 'L' + s + ',0L0,' + s + 'L' + (-s) + ',0Z');
        const q = s + 5;
        g.select('path.ring').attr('d', 'M0,' + (-q) + 'L' + q + ',0L0,' + q + 'L' + (-q) + ',0Z');
      }
    });
    labelSel
      .attr('class', (d) => 'node-label lbl-' + d.type)
      .attr('dy', '0.35em')
      .text((d) => shortLabel(d.type === 'competition' ? (d.id + ' · ' + d.name) : d.name));
    labelSel.each((d) => { if (!d.lpos) [d.lpos] = labelCandidates(d); });
    labelSel.attr('x', (d) => d.lpos.x).attr('y', (d) => d.lpos.cy).attr('text-anchor', (d) => d.lpos.anchor);
  }

  /* ---------- label placement (de-collision) ---------- */
  function labelCandidates(d) {
    const fs = labelFs;
    const gap = d.type === 'club' ? 5 : 4;
    const vOff = d.r + gap + fs * 0.6;
    const right = { name: 'right', x: d.r + gap, cy: 0, anchor: 'start' };
    const left = { name: 'left', x: -(d.r + gap), cy: 0, anchor: 'end' };
    const above = { name: 'above', x: 0, cy: -vOff, anchor: 'middle' };
    const below = { name: 'below', x: 0, cy: vOff, anchor: 'middle' };
    if (d.type === 'club') return [below, above, right, left];
    // Players of a teammate path sit on a horizontal line: names alternate above and below it, never on it.
    if (G.path && G.path.order && G.path.order.has(d.key)) {
      return G.path.order.get(d.key) % 2 === 0 ? [above, below, right, left] : [below, above, right, left];
    }
    return [right, left, above, below];
  }
  /** Box of a label at a candidate position; `padX`/`padY` (graph units) keep a gap between neighbours. */
  function labelBox(d, cand, width, fs, padX, padY) {
    const px = padX || 0;
    const py = padY || 0;
    let x0 = d.x + cand.x;
    if (cand.anchor === 'end') x0 -= width;
    else if (cand.anchor === 'middle') x0 -= width / 2;
    const cy = d.y + cand.cy;
    return { x0: x0 - 1 - px, y0: cy - fs * 0.62 - py, x1: x0 + width + 1 + px, y1: cy + fs * 0.62 + py };
  }
  const overlaps = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

  /**
   * Places every label at the first of four positions that touches no other label and no node shape.
   * Priority: selected, hovered, highlighted (path, community), clubs, players by value, competitions.
   * A label that fits nowhere is hidden (class "off") unless it is in focus; hovering brings it back.
   */
  function declutter() {
    if (!labelSel.size()) return;
    const focus = new Set();
    if (G.selected) neighbours(G.selected).forEach((k) => focus.add(k));
    if (G.hovered) neighbours(G.hovered).forEach((k) => focus.add(k));
    if (G.highlight) G.highlight.forEach((k) => focus.add(k));
    const forced = new Set();
    if (G.selected) forced.add(G.selected);
    if (G.hovered) forced.add(G.hovered);
    if (G.highlight) G.highlight.forEach((k) => forced.add(k));
    const rank = (d) => {
      if (d.key === G.selected) return 0;
      if (d.key === G.hovered) return 1;
      if (G.highlight && G.highlight.has(d.key)) return 2;
      // A club square without its name is unreadable: clubs come before neighbour players and are always shown.
      if (d.type === 'club') return 2.5;
      if (focus.has(d.key)) return 3;
      return d.type === 'player' ? 5 : 6;
    };
    const items = [];
    labelSel.each(function measure(d) {
      if (!Number.isFinite(d.x)) return;
      const fs = labelFs;
      if (d.lfs !== fs || d.ltext !== this.textContent) {
        let width = 0;
        try { width = this.getComputedTextLength(); } catch (err) { width = 0; }
        if (!width) width = this.textContent.length * fs * 0.58;
        d.lw = width;
        d.lfs = fs;
        d.ltext = this.textContent;
      }
      items.push({ d, el: this, fs });
    });
    items.sort((a, b) => (rank(a.d) - rank(b.d)) || ((b.d.data.market_value_eur || 0) - (a.d.data.market_value_eur || 0)));
    const obstacles = [];
    G.nodes.forEach((n) => {
      if (!Number.isFinite(n.x)) return;
      const pad = n.type === 'club' ? 2 : 1;
      obstacles.push({ key: n.key, x0: n.x - n.r - pad, y0: n.y - n.r - pad, x1: n.x + n.r + pad, y1: n.y + n.r + pad });
    });
    // Path labels are obstacles for every other label.
    pathLabelSel.each(function textBox() {
      if (!this.textContent) return;
      try {
        const b = this.getBBox();
        if (b.width) obstacles.push({ key: '', x0: b.x, y0: b.y, x1: b.x + b.width, y1: b.y + b.height });
      } catch (err) { /* not rendered */ }
    });
    // Link labels (pipeline fees, probabilities, similarity) are shown only where they fit: a crowded fan of
    // pipelines would otherwise print labels over one another. Links of the selected or hovered node go first;
    // a hidden label comes back when one of its ends is hovered, and its figures are in the drawer anyway.
    const linkBoxes = [];
    const onPath = (d) => Boolean(G.path && G.path.linkKeys.indexOf(d.key) >= 0);
    const touches = (d, key) => Boolean(key) && (endKey(d.source) === key || endKey(d.target) === key);
    const linkItems = [];
    linkSel.each((d) => {
      if (!d.label || onPath(d) || !linkLabelPos(d)) return;
      const prio = touches(d, G.hovered) ? 0 : (touches(d, G.selected) ? 1 : 2);
      linkItems.push({ d, prio });
    });
    linkItems.sort((a, b) => a.prio - b.prio);
    const linkHidden = new Set();
    linkItems.forEach((item) => {
      const { d } = item;
      const half = String(d.label).length * linkFs * 0.3 + 1;
      const boxAt = (at) => {
        const pos = linkLabelPos(d, at);
        return { x0: pos.x - half, y0: pos.y - linkFs * 0.65, x1: pos.x + half, y1: pos.y + linkFs * 0.65 };
      };
      const hitsLabel = (box) => linkBoxes.some((b) => overlaps(b, box));
      const hitsNode = (box) => obstacles.some((o) => o.key !== endKey(d.source) && o.key !== endKey(d.target) && overlaps(o, box));
      // The label slides along its link to a free spot; pipelines prefer the competition end.
      const stops = d.type === 'SELLS_TO_LEAGUE' ? [0.64, 0.5, 0.76, 0.4] : [0.5, 0.38, 0.62];
      let at = stops.find((t) => { const box = boxAt(t); return !hitsLabel(box) && !hitsNode(box); });
      // Labels of the selected or hovered node may cover a node shape (their plate masks it), never another label.
      if (at === undefined && item.prio <= 1) at = stops.find((t) => !hitsLabel(boxAt(t)));
      if (at === undefined && item.prio === 0) at = stops[0];
      if (at === undefined) {
        linkHidden.add(d.key);
        return;
      }
      d.lat = at;
      linkBoxes.push(boxAt(at));
    });
    placeLinkLabels();
    linkSel.select('text').classed('off', (d) => linkHidden.has(d.key));
    linkSel.select('rect.link-label-bg').classed('off', (d) => linkHidden.has(d.key));
    linkBoxes.forEach((b) => obstacles.push({ key: '', x0: b.x0, y0: b.y0, x1: b.x1, y1: b.y1 }));
    const placed = [];
    // 6 px / 2 px on screen: two labels that merely touch would read as one phrase.
    const view = d3.zoomTransform(svg.node());
    const zoomK = view.k || 1;
    // The part of the board on screen (left of the drawer), in graph units: a label cut by its edge is hidden,
    // so a zoomed-in club or path never shows name fragments along the borders.
    const stageRect = stage.getBoundingClientRect();
    // The top and bottom strips are under the toolbar and the legend.
    const seen = {
      x0: view.invertX(0), y0: view.invertY(48),
      x1: view.invertX(Math.max(1, stageRect.width - drawerWidth())), y1: view.invertY(Math.max(49, stageRect.height - 46)),
    };
    const inView = (b) => b.x0 >= seen.x0 && b.x1 <= seen.x1 && b.y0 >= seen.y0 && b.y1 <= seen.y1;
    const padX = 6 / zoomK;
    const padY = 2 / zoomK;
    items.forEach((item) => {
      const { d } = item;
      const cands = labelCandidates(d);
      let chosen = null;
      let best = null;
      for (const cand of cands) {
        const box = labelBox(d, cand, d.lw, item.fs, padX, padY);
        if (!inView(box) && d.key !== G.selected && d.key !== G.hovered) continue;
        const hits = placed.filter((p) => overlaps(p, box)).length * 2 +
          obstacles.filter((o) => o.key !== d.key && overlaps(o, box)).length;
        if (!hits) { chosen = { cand, box }; break; }
        if (!best || hits < best.hits) best = { cand, box, hits };
      }
      // Labels that must be shown take the least crowded position, not blindly the first one.
      if (!chosen && (forced.has(d.key) || d.type === 'club')) chosen = best;
      if (chosen) {
        d.lpos = chosen.cand;
        placed.push(chosen.box);
        d.loff = false;
      } else {
        d.loff = true;
      }
    });
    labelSel
      .attr('x', (d) => (d.lpos ? d.lpos.x : 0))
      .attr('y', (d) => (d.lpos ? d.lpos.cy : 0))
      .attr('text-anchor', (d) => (d.lpos ? d.lpos.anchor : 'start'))
      .classed('off', (d) => Boolean(d.loff));
  }

  const drag = d3.drag()
    .on('start', (event, d) => {
      if (!event.active) sim.alphaTarget(0.15).restart();
      d.fx = d.x;
      d.fy = d.y;
    })
    .on('drag', (event, d) => {
      d.fx = event.x;
      d.fy = event.y;
    })
    .on('end', (event, d) => {
      if (!event.active) sim.alphaTarget(0);
      if (d.pathPin) {
        // A player of the teammate path stays pinned where it was dropped.
        d.pathPin = { x: d.fx, y: d.fy };
        return;
      }
      d.fx = null;
      d.fy = null;
    });

  function render() {
    const nodes = Array.from(G.nodes.values());
    // Guard: a link whose end is no longer on the board would break the force layout for good.
    G.links.forEach((link, key) => {
      if (!G.nodes.has(endKey(link.source)) || !G.nodes.has(endKey(link.target))) G.links.delete(key);
    });
    const links = Array.from(G.links.values());

    linkSel = linkLayer.selectAll('g.link').data(links, (d) => d.key);
    linkSel.exit().remove();
    const linkEnter = linkSel.enter().append('g').attr('class', (d) => 'link l-wrap-' + d.type);
    linkEnter.append('path').attr('class', (d) => 'l-' + d.type);
    // A filled plate behind each label masks the line, so a dashed edge never runs through the text.
    linkEnter.append('rect').attr('class', 'link-label-bg').attr('rx', 2);
    linkEnter.append('text').attr('class', 'link-label');
    linkSel = linkEnter.merge(linkSel);
    linkSel.select('path').attr('stroke-width', (d) => (d.type === 'PLAYED_WITH' ? Math.min(6, 0.8 + Math.sqrt(d.weight || 1) * 0.65) : null));
    linkSel.select('text').text((d) => d.label || '');
    linkEnter.each(function enterLink(d) { animateLinkIn(d3.select(this), d); });

    nodeSel = nodeLayer.selectAll('g.node').data(nodes, (d) => d.key);
    nodeSel.exit().remove();
    const nodeEnter = nodeSel.enter().append('g')
      .attr('class', (d) => 'node ' + d.type)
      .attr('transform', (d) => 'translate(' + d.x + ',' + d.y + ')')
      .attr('tabindex', -1)
      .attr('role', 'button')
      .attr('aria-label', (d) => LABEL[d.type] + ' ' + d.name)
      .call(drag)
      .on('mouseenter', (event, d) => onHover(event, d))
      .on('mousemove', (event) => moveTooltip(event))
      .on('mouseleave', () => onHover(null, null))
      .on('click', (event, d) => {
        event.stopPropagation();
        selectNode(d.key, true);
      })
      .on('keydown', (event, d) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectNode(d.key, true);
          return;
        }
        // Roving focus: one tab stop for the whole board, arrow keys move between nodes.
        const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
        if (!step) return;
        event.preventDefault();
        const all = nodeSel.nodes();
        const idx = all.indexOf(event.currentTarget);
        const next = all[(idx + step + all.length) % all.length];
        if (next) {
          tabStopKey = d3.select(next).datum().key;
          refreshTabStops();
          next.focus();
        }
      });
    nodeEnter.append('g').attr('class', 'body').each(function enterBody(d) { shapeFor(d3.select(this), d); });
    nodeEnter.select('.body')
      .attr('transform', REDUCED ? null : 'scale(0.15)')
      .attr('opacity', REDUCED ? 1 : 0)
      .transition().duration(dur(480)).ease(d3.easeBackOut.overshoot(1.4))
      .attr('transform', 'scale(1)')
      .attr('opacity', 1);
    nodeSel = nodeEnter.merge(nodeSel);
    nodeSel.attr('aria-label', (d) => LABEL[d.type] + ' ' + d.name);
    labelSel = labelLayer.selectAll('text.node-label').data(nodes, (d) => d.key);
    labelSel.exit().remove();
    const labelEnter = labelSel.enter().append('text').attr('class', 'node-label')
      .attr('transform', (d) => 'translate(' + d.x + ',' + d.y + ')');
    labelEnter.attr('opacity', REDUCED ? 1 : 0)
      .transition().delay(dur(220)).duration(dur(300)).attr('opacity', 1);
    labelSel = labelEnter.merge(labelSel);
    nodes.forEach((n) => { n.r = computeR(n); });
    updateShape(nodeSel);
    nodeSel.classed('selected', (d) => d.key === G.selected);
    nodeSel.classed('endpoint', (d) => Boolean(G.path && G.path.ends.has(d.key)));

    // Teammate path: matches labels drawn above node labels, with a halo.
    const pathLinks = G.path ? G.path.linkKeys.map((k) => G.links.get(k)).filter(Boolean) : [];
    linkSel.classed('on-path', (d) => Boolean(G.path && G.path.linkKeys.indexOf(d.key) >= 0));
    pathLabelSel = pathLabelLayer.selectAll('text.path-label').data(pathLinks, (d) => d.key);
    pathLabelSel.exit().remove();
    pathLabelSel = pathLabelSel.enter().append('text').attr('class', 'path-label').merge(pathLabelSel)
      .text((d) => d.label || '');
    pathLabelSel.selectAll('title').remove();
    pathLabelSel.append('title').text((d) => (d.label ? d.label + ' matches together' : ''));

    sim.nodes(nodes);
    sim.force('link').links(links);
    sim.force('collide').radius((d) => d.r + (d.type === 'club' ? 30 : 17));
    // Only a new node or link restarts the layout: a redraw of the same graph never moves anything.
    const signature = nodes.length + '/' + links.length + ':' + nodes.map((n) => n.key).join(',') + '|' + links.map((l) => l.key).join(',');
    if (signature !== layoutSignature) {
      layoutSignature = signature;
      kick(0.55);
    } else {
      ticked();
    }
    refreshTabStops();
    refreshEmphasis();
    $('#graph-empty').hidden = nodes.length > 0;
    $('#graph-count').textContent = nodes.length + ' nodes · ' + links.length + ' links';
  }

  /** Restarts the layout; without motion, settles it at once so nothing drifts on screen. */
  function kick(alpha) {
    // Shape the layout like the free board area (portrait when the drawer is open on a small screen).
    const area = boardBox();
    const aspect = Math.max(0.5, Math.min(2.2, area.w / area.h));
    sim.force('x').strength(0.12 / aspect);
    sim.force('y').strength(0.06 * aspect);
    sim.alpha(Math.max(sim.alpha(), alpha));
    if (REDUCED) {
      sim.stop();
      sim.tick(240);
      ticked();
      declutter(); // no "end" event without motion: labels follow the settled layout here
    } else {
      sim.restart();
    }
  }

  function animateLinkIn(group, d) {
    if (REDUCED) return;
    const path = group.select('path');
    if (d.type === 'PLAYS_FOR' || d.type === 'PLAYED_WITH' || d.type === 'SELLS_TO_LEAGUE') {
      path.attr('pathLength', 1).style('stroke-dasharray', '1 1').style('stroke-dashoffset', 1)
        .transition().delay(dur(120)).duration(dur(650)).ease(d3.easeCubicOut)
        .style('stroke-dashoffset', 0)
        .on('end interrupt', function cleanDash() {
          d3.select(this).style('stroke-dasharray', null).style('stroke-dashoffset', null).attr('pathLength', null);
        });
    } else {
      path.style('opacity', 0).transition().delay(dur(150)).duration(dur(600)).style('opacity', 1);
    }
    group.select('text').style('opacity', 0).transition().delay(dur(500)).duration(dur(300)).style('opacity', 1);
  }

  /** Link labels and the plates behind them (also called on zoom, as the label size follows the zoom). */
  /** Centre of a link label, or null while the link has no position yet. */
  function linkLabelPos(d, where) {
    if (!(d.source && d.target && Number.isFinite(d.source.x) && Number.isFinite(d.target.x))) return null;
    // Pipeline labels sit nearer their competition, so labels of links fanning out of one club do not stack;
    // declutter() may slide a label along its link (d.lat) to a free spot.
    const at = where !== undefined ? where : (d.lat || (d.type === 'SELLS_TO_LEAGUE' ? 0.64 : 0.5));
    return { x: d.source.x + (d.target.x - d.source.x) * at, y: d.source.y + (d.target.y - d.source.y) * at - 1 };
  }
  function placeLinkLabels() {
    const lx = (d) => { const pos = linkLabelPos(d); return pos ? pos.x : 0; };
    const ly = (d) => { const pos = linkLabelPos(d); return pos ? pos.y : 0; };
    linkSel.select('text').attr('x', lx).attr('y', ly);
    // Monospace labels: the plate size follows from the character count (0.6 em per glyph).
    const padPlate = 3 / (d3.zoomTransform(svg.node()).k || 1);
    linkSel.select('rect.link-label-bg')
      .attr('width', (d) => (d.label ? String(d.label).length * linkFs * 0.6 + padPlate * 2 : 0))
      .attr('height', (d) => (d.label ? linkFs * 1.3 + padPlate : 0))
      .attr('x', (d) => lx(d) - String(d.label || '').length * linkFs * 0.3 - padPlate)
      .attr('y', (d) => ly(d) - linkFs * 0.65 - padPlate / 2);
  }

  function ticked() {
    linkSel.select('path').attr('d', (d) => {
      const s = d.source;
      const t = d.target;
      if (!s || !t || !Number.isFinite(s.x)) return null;
      return 'M' + s.x + ',' + s.y + 'L' + t.x + ',' + t.y;
    });
    placeLinkLabels();
    if (pathLabelSel.size()) {
      // Matches count sits beside its edge, on the side away from the centre of the path (clear of node labels).
      let cx = 0;
      let cy = 0;
      let n = 0;
      if (G.path) {
        G.path.keys.forEach((k) => {
          const node = G.nodes.get(k);
          if (node && Number.isFinite(node.x)) { cx += node.x; cy += node.y; n += 1; }
        });
      }
      if (n) { cx /= n; cy /= n; }
      const off = linkFs * 1.1;
      pathLabelSel.each(function placePathLabel(d) {
        const mx = (d.source.x + d.target.x) / 2;
        const my = (d.source.y + d.target.y) / 2;
        const dx = d.target.x - d.source.x;
        const dy = d.target.y - d.source.y;
        const len = Math.hypot(dx, dy) || 1;
        let nx = -dy / len;
        let ny = dx / len;
        // The path players are pinned on a line with their names alternating above and below it:
        // the count sits on the edge itself, at its midpoint, its halo masking the line.
        const onLine = Boolean(G.path && G.path.order && G.path.order.has(endKey(d.source)) && G.path.order.has(endKey(d.target)));
        if (!onLine && n && (nx * (mx - cx) + ny * (my - cy)) < 0) { nx = -nx; ny = -ny; }
        const k = onLine ? 0 : off;
        this.setAttribute('x', mx + nx * k);
        this.setAttribute('y', my + ny * k);
      });
    }
    nodeSel.attr('transform', (d) => 'translate(' + d.x + ',' + d.y + ')');
    labelSel.attr('transform', (d) => 'translate(' + d.x + ',' + d.y + ')');
    tickCount += 1;
    if (!REDUCED && tickCount % 12 === 0) declutter();
  }

  /** One tab stop on the board: the selected node, or the first one. */
  function refreshTabStops() {
    if (G.selected && G.nodes.has(G.selected)) tabStopKey = G.selected;
    if (!tabStopKey || !G.nodes.has(tabStopKey)) tabStopKey = G.nodes.size ? G.nodes.keys().next().value : null;
    nodeSel.attr('tabindex', (d) => (d.key === tabStopKey ? 0 : -1));
  }

  function neighbours(key) {
    const set = new Set([key]);
    G.links.forEach((link) => {
      const [s, t] = nodeEndpoints(link);
      if (!s || !t) return;
      if (s.key === key) set.add(t.key);
      if (t.key === key) set.add(s.key);
    });
    return set;
  }

  /** Dimming (hover neighbourhood or community highlight) and label density. */
  function refreshEmphasis() {
    let lit = null;
    if (G.hovered) lit = neighbours(G.hovered);
    else if (G.highlight) lit = G.highlight;
    svg.classed('dimming', Boolean(lit));
    nodeSel.classed('lit', (d) => Boolean(lit && lit.has(d.key)));
    labelSel.classed('lit', (d) => Boolean(lit && lit.has(d.key)));
    nodeSel.classed('marked', (d) => Boolean(G.highlight && G.highlight.has(d.key)));
    linkSel.classed('lit', (d) => {
      if (!lit) return false;
      const [s, t] = nodeEndpoints(d);
      if (G.hovered) return Boolean(s && t && (s.key === G.hovered || t.key === G.hovered));
      return Boolean(s && t && lit.has(s.key) && lit.has(t.key));
    });
    declutter();
  }

  /* ---------- tooltip ---------- */
  const tooltip = $('#tooltip');
  function onHover(event, d) {
    G.hovered = d ? d.key : null;
    refreshEmphasis();
    if (!d) {
      tooltip.hidden = true;
      return;
    }
    clear(tooltip);
    tooltip.append(h('div', { class: 'tt-name', text: d.name }));
    if (d.type === 'player') {
      const p = d.data;
      const age = ageOf(p.date_of_birth);
      tooltip.append(h('div', { class: 'tt-line', text: [p.position, p.sub_position].filter(Boolean).join(' · ') || 'Player' }));
      tooltip.append(h('div', { class: 'tt-line', text: (age !== null ? age + ' y · ' : '') + fmtEur(p.market_value_eur) }));
      if (p.club_name) tooltip.append(h('div', { class: 'tt-line', text: p.club_name }));
    } else if (d.type === 'club') {
      const info = G.clubInfo.get(Number(d.id)) || {};
      tooltip.append(h('div', { class: 'tt-line', text: 'Club · ' + d.key }));
      if (info.pagerank !== undefined) tooltip.append(h('div', { class: 'tt-line', text: 'PageRank ' + fmtScore(info.pagerank) }));
      if (info.betweenness !== undefined) tooltip.append(h('div', { class: 'tt-line', text: 'Betweenness ' + fmtScore(info.betweenness, 1) }));
    } else {
      tooltip.append(h('div', { class: 'tt-line', text: 'Competition · ' + d.id }));
    }
    tooltip.hidden = false;
    moveTooltip(event);
  }
  function moveTooltip(event) {
    if (tooltip.hidden || !event) return;
    const box = stage.getBoundingClientRect();
    let x = event.clientX - box.left + 14;
    let y = event.clientY - box.top + 14;
    if (x + 270 > box.width) x -= 290;
    if (y + 90 > box.height) y -= 100;
    tooltip.style.left = x + 'px';
    tooltip.style.top = y + 'px';
  }

  /* ---------- camera ---------- */
  function drawerWidth() {
    const drawer = $('#drawer');
    return drawer.classList.contains('open') ? drawer.getBoundingClientRect().width : 0;
  }
  /**
   * The free area of the board, clear of the toolbar, the legend, the path card and the drawer.
   * The pitch is purely decorative: it is drawn in the same area and ends just above the legend.
   */
  const DENSE_BOARD = 40; // above this many nodes, opening one node frames its neighbourhood only
  const PITCH_ASPECT = 106 / 69; // viewBox of #pitch: the markings and the graph share this one frame
  const pitchEl = $('#pitch');
  function boardBox() {
    const stageBox = stage.getBoundingClientRect();
    const legend = $('#legend');
    if (!legend.classList.contains('expanded')) legendCompactH = legend.getBoundingClientRect().height;
    const bottom = Math.max(28, legendCompactH + 14 + 10);
    const banner = $('#stage-banner');
    const top = banner.hidden ? 56 : Math.max(56, banner.getBoundingClientRect().bottom - stageBox.top + 10);
    const card = $('#path-card');
    const left = 24 + (card.hidden ? 0 : card.getBoundingClientRect().width + 14);
    const right = 24 + drawerWidth();
    let x = left;
    let y = top;
    let bw = Math.max(200, stageBox.width - left - right);
    let bh = Math.max(160, stageBox.height - top - bottom);
    // Letterbox to the pitch ratio, centred in the free area.
    if (bw / bh > PITCH_ASPECT) {
      const nw = bh * PITCH_ASPECT;
      x += (bw - nw) / 2;
      bw = nw;
    } else {
      const nh = bw / PITCH_ASPECT;
      y += (bh - nh) / 2;
      bh = nh;
    }
    pitchEl.style.left = x.toFixed(1) + 'px';
    pitchEl.style.top = y.toFixed(1) + 'px';
    pitchEl.style.width = bw.toFixed(1) + 'px';
    pitchEl.style.height = bh.toFixed(1) + 'px';
    return { x, y, w: bw, h: bh };
  }
  /** Without motion the camera jumps (a zero-length transition would still wait for a frame). */
  function moveCamera(transform, ms) {
    if (REDUCED) svg.interrupt().call(zoom.transform, transform);
    else svg.transition().duration(ms).call(zoom.transform, transform);
  }
  /** Frames every node, or only `keys` (a Set of node keys, e.g. the players of a teammate path). */
  function fit(keys) {
    const subset = keys instanceof Set && keys.size ? keys : null;
    const nodes = Array.from(G.nodes.values()).filter((n) => Number.isFinite(n.x) && (!subset || subset.has(n.key)));
    if (!nodes.length) return;
    // Extent of the shapes and of the visible labels (at the current label size).
    const boxes = nodes.map((n) => {
      const shape = { x0: n.x - n.r, y0: n.y - n.r, x1: n.x + n.r, y1: n.y + n.r };
      if (!n.lpos || n.loff || !n.lw) return shape;
      const lb = labelBox(n, n.lpos, n.lw, labelFs);
      return { x0: Math.min(shape.x0, lb.x0), y0: Math.min(shape.y0, lb.y0), x1: Math.max(shape.x1, lb.x1), y1: Math.max(shape.y1, lb.y1) };
    });
    // A small subset (a 2-hop path, one club) is framed, not blown up: nodes keep a readable size.
    fitExtent(d3.min(boxes, (b) => b.x0) - 16, d3.max(boxes, (b) => b.x1) + 16,
      d3.min(boxes, (b) => b.y0) - 12, d3.max(boxes, (b) => b.y1) + 12, undefined, subset ? 1.4 : 2.4);
  }
  /** Fits after a delay and again once the layout settles; `keys` limits the frame to some nodes. */
  function scheduleFit(delay, keys) {
    fitOnEnd = true;
    fitKeys = keys instanceof Set && keys.size ? keys : null;
    // On a dense board (the real graph), a node opened from a panel or a link is framed with its neighbours,
    // not lost among a hundred others. A node clicked on the board leaves the camera framing everything.
    if (!fitKeys && G.focusKey && G.nodes.has(G.focusKey) && G.nodes.size > DENSE_BOARD) fitKeys = neighbours(G.focusKey);
    clearTimeout(fitTimer);
    fitTimer = setTimeout(() => fit(fitKeys), delay === undefined ? 1100 : delay);
  }
  /** Frames a rectangle given in graph coordinates. */
  function fitExtent(minX, maxX, minY, maxY, ms, maxK) {
    const area = boardBox();
    const k = Math.max(0.2, Math.min(maxK || 2.4, Math.min(area.w / (maxX - minX), area.h / (maxY - minY))));
    const tx = area.x + area.w / 2 - k * (minX + maxX) / 2;
    const ty = area.y + area.h / 2 - k * (minY + maxY) / 2;
    moveCamera(d3.zoomIdentity.translate(tx, ty).scale(k), ms === undefined ? 650 : ms);
  }
  function focusNode(key) {
    const node = G.nodes.get(key);
    if (!node || !Number.isFinite(node.x)) return;
    const area = boardBox();
    const current = d3.zoomTransform(svg.node());
    const k = current.k;
    const sx = current.applyX(node.x);
    const sy = current.applyY(node.y);
    if (sx > area.x + 50 && sx < area.x + area.w - 120 && sy > area.y + 10 && sy < area.y + area.h - 10) return;
    const tx = area.x + area.w / 2 - k * node.x;
    const ty = area.y + area.h / 2 - k * node.y;
    moveCamera(d3.zoomIdentity.translate(tx, ty).scale(k), 600);
  }

  /* ---------- effects ---------- */
  function ripple(key, color, times) {
    const node = G.nodes.get(key);
    if (!node) return;
    if (REDUCED) {
      nodeSel.filter((d) => d.key === key).classed('marked', true);
      setTimeout(() => refreshEmphasis(), 1600);
      return;
    }
    const count = times || 3;
    const rings = [];
    for (let i = 0; i < count; i += 1) {
      rings.push(fxLayer.append('circle'));
      rings[i]
        .attr('class', 'ripple')
        .attr('cx', node.x).attr('cy', node.y)
        .attr('r', node.r + 2)
        .attr('stroke', color || '#E9D46B')
        .attr('stroke-width', 2)
        .attr('opacity', 0.95)
        .transition().delay(i * 260).duration(1100).ease(d3.easeCubicOut)
        .attr('r', node.r + 34)
        .attr('opacity', 0)
        .remove();
    }
    // Backstop when frames are throttled: the rings never stay on the board.
    setTimeout(() => { rings.forEach((ring) => ring.interrupt().remove()); }, count * 260 + 1400);
  }
  function pulseAlong(link, ms) {
    return new Promise((resolve) => {
      const [s, t] = nodeEndpoints(link);
      if (!s || !t || REDUCED) {
        resolve();
        return;
      }
      const dot = fxLayer.append('circle')
        .attr('class', 'pulse')
        .attr('r', 4.5);
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        dot.interrupt().remove();
        resolve();
      };
      // Backstop: playback never waits on a frame that does not come (hidden tab, throttled timers).
      setTimeout(finish, ms + 250);
      dot.transition().duration(ms).ease(d3.easeCubicInOut)
        .attrTween('transform', () => (u) => {
          const x = s.x + (t.x - s.x) * u;
          const y = s.y + (t.y - s.y) * u;
          return 'translate(' + x + ',' + y + ')';
        })
        .on('end interrupt', finish);
    });
  }

  function setHighlight(keys) {
    G.highlight = keys && keys.size ? keys : null;
    document.querySelectorAll('.community.active').forEach((el) => el.classList.remove('active'));
    refreshEmphasis();
  }

  /* ---------- controls ---------- */
  $('#g-fit').addEventListener('click', () => { fitKeys = null; G.focusKey = null; fit(); });
  $('#g-clear').addEventListener('click', () => {
    pathRun += 1; // stops a teammate path that is still playing
    pinTimers.forEach((t) => t.stop());
    pinTimers.clear();
    G.nodes.clear();
    G.links.clear();
    G.selected = null;
    G.hovered = null;
    G.highlight = null;
    G.path = null;
    G.focusKey = null;
    $('#path-card').hidden = true;
    fxLayer.selectAll('*').interrupt().remove();
    fitKeys = null;
    closeDrawer();
    render();
    boardBox();
  });
  const legendBox = $('#legend');
  const legendBtn = $('#g-legend');
  /** The legend is always on screen as one compact row; the button expands the full key over the board. */
  function setLegend(expanded) {
    if (!expanded) legendBox.classList.remove('expanded');
    boardBox(); // measure the compact row before expanding
    legendBox.classList.toggle('expanded', expanded);
    legendBtn.setAttribute('aria-pressed', String(expanded));
  }
  legendBtn.addEventListener('click', () => setLegend(!legendBox.classList.contains('expanded')));
  setLegend(false);
  window.addEventListener('resize', debounce(() => boardBox(), 150));
  $('#g-relayout').addEventListener('click', () => {
    G.nodes.forEach((n) => {
      const angle = Math.random() * Math.PI * 2;
      const dist = Math.random() * 120;
      n.x = Math.cos(angle) * dist;
      n.y = Math.sin(angle) * dist;
      n.vx = 0;
      n.vy = 0;
      // A teammate path stays pinned on its line; everything else is free again.
      n.fx = n.pathPin ? n.pathPin.x : null;
      n.fy = n.pathPin ? n.pathPin.y : null;
      if (n.pathPin) { n.x = n.fx; n.y = n.fy; }
    });
    kick(1);
    scheduleFit(1600);
  });

  /* ================================================================== */
  /* Data loaders shared by panels                                       */
  /* ================================================================== */
  function storeClubMetric(rows, field) {
    rows.forEach((row) => {
      const id = Number(row.club_id);
      const info = G.clubInfo.get(id) || { name: row.name };
      info.name = row.name;
      info[field] = row.score;
      G.clubInfo.set(id, info);
    });
    if (field === 'pagerank') G.hubMax = d3.max(rows, (r) => r.score) || 1;
    G.metricRows[field] = rows.length;
  }

  // In-flight promises are cached, so two views asking at once share one request.
  let hubsPromise = null;
  let gatewaysPromise = null;
  let communityMetricPromise = null;
  function cachedLoad(get, set, path, store) {
    if (!get()) {
      const promise = api(path).then((rows) => { store(rows); return rows; });
      promise.catch(() => { if (get() === promise) set(null); });
      set(promise);
    }
    return get();
  }
  function loadHubs() {
    return cachedLoad(() => hubsPromise, (p) => { hubsPromise = p; }, '/api/clubs/hubs?limit=100', (rows) => storeClubMetric(rows, 'pagerank'));
  }
  function loadGateways() {
    return cachedLoad(() => gatewaysPromise, (p) => { gatewaysPromise = p; }, '/api/clubs/gateways?limit=100', (rows) => storeClubMetric(rows, 'betweenness'));
  }
  /** Community of every club (min_size=1), kept apart from the size filter of the Network tab. */
  function loadCommunityMetric() {
    return cachedLoad(() => communityMetricPromise, (p) => { communityMetricPromise = p; }, '/api/network/communities?min_size=1', (rows) => {
      G.clubInfo.forEach((info) => { delete info.community; });
      rows.forEach((c) => {
        (c.clubs || []).forEach((name) => {
          const id = clubIdByName(name);
          if (id !== null) {
            const info = G.clubInfo.get(id) || { name };
            info.community = c.community_id;
            G.clubInfo.set(id, info);
          }
        });
      });
    });
  }

  // Pipelines, cached per club and date (also used to check that a club exists before drawing it).
  const pipelineCache = new Map();
  function fetchPipeline(clubId, since) {
    const key = Number(clubId) + '|' + (since || '');
    if (!pipelineCache.has(key)) {
      const qs = since ? '?since=' + encodeURIComponent(since) + '&limit=20' : '?limit=20';
      const cid = Number(clubId);
      const promise = api(`/api/clubs/${cid}/pipeline${qs}`);
      promise.catch(() => { pipelineCache.delete(key); });
      pipelineCache.set(key, promise);
    }
    return pipelineCache.get(key);
  }

  function clubName(id) {
    const info = G.clubInfo.get(Number(id));
    if (info && info.name) return info.name;
    const node = G.nodes.get(keyOf('club', id));
    return node ? node.name : 'Club ' + id;
  }
  function clubIdByName(name) {
    let found = null;
    G.clubInfo.forEach((info, id) => { if (info.name === name) found = id; });
    if (found === null) {
      G.nodes.forEach((n) => { if (n.type === 'club' && n.name === name) found = Number(n.id); });
    }
    return found;
  }

  async function ensurePlayer(id, parentKey) {
    const pid = Number(id);
    if (!Number.isInteger(pid)) throw new Error('Invalid player id: ' + String(id).slice(0, 40));
    const key = keyOf('player', pid);
    const existing = G.nodes.get(key);
    if (existing && existing.data && existing.data.position !== undefined) return existing;
    const summary = await api(`/api/players/${pid}`);
    const before = G.nodes.size;
    const node = addPlayerSummary(summary, parentKey);
    render();
    if (G.nodes.size > before) scheduleFit(900);
    return node;
  }
  async function enrichPlayers(ids, fitTo) {
    const missing = ids.map(Number).filter(Number.isInteger).filter((id) => {
      const n = G.nodes.get(keyOf('player', id));
      return !n || n.data.position === undefined;
    }).slice(0, 20);
    if (!missing.length) return;
    const results = await Promise.allSettled(missing.map((id) => api(`/api/players/${id}`)));
    results.forEach((res) => {
      if (res.status === 'fulfilled' && res.value) {
        const node = G.nodes.get(keyOf('player', res.value.player_id));
        addPlayerSummary(res.value, node ? node.key : null);
      }
    });
    render();
    scheduleFit(700, fitTo);
  }
  function ensureClubNode(id, name, parentKey) {
    return upsertNode('club', id, { name: name || clubName(id) }, parentKey);
  }

  function drawPipeline(clubId, rows) {
    const club = ensureClubNode(clubId, clubName(clubId));
    // New competitions start fanned out around the club, on the side away from the centre of the board,
    // so their diamonds and fee labels never start (and stay) stacked on one another.
    const fresh = rows.filter((row) => !G.nodes.has(keyOf('competition', row.competition_id)));
    const cx = Number.isFinite(club.x) ? club.x : 0;
    const cy = Number.isFinite(club.y) ? club.y : 0;
    const base = Math.hypot(cx, cy) > 30 ? Math.atan2(cy, cx) : -Math.PI / 2;
    // Up to a full circle for a big club (7 leagues), so neighbouring fee labels start well apart.
    const spread = fresh.length > 1 ? Math.min((Math.PI * 2 * (fresh.length - 1)) / fresh.length, 0.9 * (fresh.length - 1)) : 0;
    rows.forEach((row) => {
      const idx = fresh.indexOf(row);
      const comp = upsertNode('competition', row.competition_id, { name: row.competition_name || row.competition_id, data: row }, club.key);
      if (idx >= 0) {
        const angle = base - spread / 2 + (fresh.length > 1 ? (idx * spread) / (fresh.length - 1) : 0);
        comp.x = cx + Math.cos(angle) * 150;
        comp.y = cy + Math.sin(angle) * 150;
      }
      upsertLink('SELLS_TO_LEAGUE', club.key, comp.key, { label: row.transfers + ' · ' + fmtEur(row.total_fee_eur) });
    });
    render();
    scheduleFit(900);
  }

  /**
   * Opens a club only once the API confirms it exists (its pipeline answers 200).
   * A 404 throws before anything is drawn, so no placeholder club ever reaches the board.
   */
  async function openClubById(id, opts) {
    const options = opts || {};
    const mine = options.token || claimSelection();
    const clubId = Number(id);
    const rows = await fetchPipeline(clubId, '');
    await loadHubsQuietly();
    const node = ensureClubNode(clubId, options.name || null, options.parentKey || null);
    render();
    if (options.autoDraw && rows.length) {
      drawPipeline(clubId, rows);
      // On a dense board (the real graph), frame the club with its leagues and players; a small board stays whole.
      if (G.nodes.size > DENSE_BOARD) scheduleFit(900, neighbours(node.key));
    }
    // A newer selection was made meanwhile: draw, but do not steal the drawer.
    if (mine === selectToken) selectNode(node.key);
    return node;
  }
  const loadHubsQuietly = () => loadHubs().catch(() => null);
  /** Draws the pipelines of the given clubs; stops early once `stopWhen()` is true. */
  async function drawClubPipelines(clubIds, stopWhen) {
    for (const clubId of clubIds) {
      if (stopWhen && stopWhen()) break;
      try {
        const rows = await fetchPipeline(clubId, '');
        await loadHubsQuietly();
        if (rows.length) drawPipeline(clubId, rows);
      } catch (err) { /* unknown club: nothing to draw */ }
    }
  }

  /* ================================================================== */
  /* Drawer                                                              */
  /* ================================================================== */
  const drawer = $('#drawer');
  const drawerBody = $('#drawer-body');
  let drawerToken = 0;
  let drawerOpener = null;
  drawer.inert = true;

  function openDrawer(kind, title) {
    drawerToken += 1;
    const wasOpen = drawer.classList.contains('open');
    if (!wasOpen) {
      const active = document.activeElement;
      drawerOpener = active && active !== document.body && !drawer.contains(active) ? active : null;
    }
    $('#drawer-kind').textContent = kind;
    $('#drawer-title').textContent = title;
    clear(drawerBody);
    drawer.inert = false;
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    stage.classList.add('drawer-open');
    document.body.classList.add('with-drawer');
    boardBox();
    if (!wasOpen && drawerOpener) $('#drawer-close').focus({ preventScroll: true });
    if (!wasOpen && G.nodes.size) {
      // The free board just got narrower: let the layout take the new shape, then frame it again.
      kick(0.3);
      scheduleFit(REDUCED ? 60 : 900, fitKeys);
    }
    return drawerToken;
  }
  function closeDrawer() {
    drawerToken += 1;
    const wasOpen = drawer.classList.contains('open');
    const hadFocus = drawer.contains(document.activeElement);
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    drawer.inert = true;
    stage.classList.remove('drawer-open');
    document.body.classList.remove('with-drawer');
    boardBox();
    G.selected = null;
    G.focusKey = null;
    nodeSel.classed('selected', false);
    refreshEmphasis();
    if (wasOpen && hadFocus && drawerOpener && document.contains(drawerOpener)) drawerOpener.focus({ preventScroll: true });
    drawerOpener = null;
  }
  $('#drawer-close').addEventListener('click', closeDrawer);
  /**
   * Escape, in order: closes the suggestion list of a resolver, clears the text of a resolver inside the drawer,
   * closes the drawer, then the self-test panel, then the expanded legend. Other fields (the agent question,
   * the API key) are never cleared by Escape.
   */
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const target = event.target instanceof Element ? event.target : null;
    const resolver = target ? target.closest('.resolver, [data-resolver]') : null;
    const list = resolver ? resolver.querySelector('.list') : null;
    if (list && list.children.length) {
      event.preventDefault();
      clear(list);
      return;
    }
    if (drawer.classList.contains('open')) {
      if (resolver && drawer.contains(target) && target.tagName === 'INPUT' && target.value) {
        event.preventDefault();
        target.value = '';
        target.dispatchEvent(new Event('input', { bubbles: true }));
        return;
      }
      const selectedKey = G.selected;
      const hadOpener = Boolean(drawerOpener && document.contains(drawerOpener));
      const hadFocus = drawer.contains(document.activeElement);
      closeDrawer();
      if (hadFocus && !hadOpener) {
        // No opener recorded (deep link): keep the keyboard on the board, on the node that was selected.
        const nodeEl = selectedKey ? nodeSel.filter((d) => d.key === selectedKey).node() : null;
        if (nodeEl) nodeEl.focus({ preventScroll: true });
        else svg.node().focus({ preventScroll: true });
      }
      return;
    }
    const selftestPanel = $('#selftest-panel');
    if (!selftestPanel.hidden) {
      selftestPanel.hidden = true;
      return;
    }
    if (legendBox.classList.contains('expanded')) setLegend(false);
  });

  function selectNode(key, fromGraph) {
    const node = G.nodes.get(key);
    if (!node) return;
    claimSelection(); // any pending older open loses the right to select
    G.selected = key;
    G.focusKey = fromGraph ? null : key;
    nodeSel.classed('selected', (d) => d.key === key);
    refreshTabStops();
    refreshEmphasis();
    if (node.type === 'player') openPlayerDrawer(node);
    else if (node.type === 'club') openClubDrawer(node);
    else openCompetitionDrawer(node);
    if (!fromGraph) focusNode(key);
  }

  function section(title, actions) {
    const head = h('div', { class: 'row between' }, h('span', { class: 'caps', text: title }), actions || null);
    const body = h('div');
    const wrap = h('div', { class: 'd-section' }, head, body);
    return { wrap, body };
  }
  function stats(items) {
    return h('div', { class: 'stats' }, items.map((it) => h('div', { class: 'stat' },
      h('div', { class: 'v' + (it[2] ? ' ' + it[2] : ''), text: it[1], title: it[1] }),
      h('div', { class: 'k', text: it[0] }))));
  }

  /* ---------- player drawer ---------- */
  function openPlayerDrawer(node) {
    const p = node.data || {};
    const token = openDrawer('Player · #' + node.id, node.name);
    const age = ageOf(p.date_of_birth);
    drawerBody.append(stats([
      ['Age', age === null ? '—' : String(age)],
      ['Market value', fmtEur(p.market_value_eur)],
      ['Sub-position', p.sub_position || '—', 'txt'],
    ]));
    const facts = h('dl', { class: 'kv' },
      h('dt', { text: 'Position' }), h('dd', { class: 'txt', text: p.position || '—' }),
      h('dt', { text: 'Born' }), h('dd', { text: p.date_of_birth || '—' }));
    facts.append(h('dt', { text: 'Club' }));
    if (p.club_id !== null && p.club_id !== undefined) {
      facts.append(h('dd', { class: 'txt' }, h('button', {
        class: 'chip',
        type: 'button',
        text: p.club_name || ('Club ' + p.club_id),
        onclick: () => { ensureClubNode(p.club_id, p.club_name, node.key); render(); selectNode(keyOf('club', p.club_id)); },
      })));
    } else {
      facts.append(h('dd', { text: 'No current club' }));
    }
    drawerBody.append(facts);

    // Valuations
    const val = section('Market value history');
    drawerBody.append(val.wrap);
    val.body.append(loadingBox('Loading valuations'));
    run(async () => {
      try {
        const rows = await api(`/api/players/${node.id}/valuations`);
        if (token !== drawerToken) return;
        clear(val.body).append(sparkline(rows));
      } catch (err) {
        if (token === drawerToken) clear(val.body).append(errorBox(err));
      }
    });

    // Teammate path
    const pathSec = section('Teammate path');
    drawerBody.append(pathSec.wrap);
    buildPathTool(pathSec.body, node);

    // Similar players
    const simBtn = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Find similar' });
    const simSec = section('Similar circles', simBtn);
    drawerBody.append(simSec.wrap);
    simSec.body.append(h('p', { class: 'hint', text: 'FastRP embedding of teammate links: players who share the same circles, not the same position.' }));
    simBtn.addEventListener('click', () => withBusy(simBtn, async () => {
      clear(simSec.body).append(loadingBox('Computing neighbours'));
      try {
        const rows = await api(`/api/players/${node.id}/similar?k=6`);
        if (token !== drawerToken) return;
        renderSimilar(simSec.body, node, rows);
      } catch (err) {
        if (token === drawerToken) clear(simSec.body).append(errorBox(err));
      }
    }));

    // Predicted moves
    const predSec = section('Predicted moves');
    drawerBody.append(predSec.wrap);
    predSec.body.append(loadingBox('Loading predictions'));
    run(async () => {
      try {
        const rows = await api(`/api/players/${node.id}/predicted-moves?limit=5`);
        if (token !== drawerToken) return;
        renderPredicted(predSec.body, node, rows);
      } catch (err) {
        if (token === drawerToken) clear(predSec.body).append(errorBox(err));
      }
    });

    // Rumours
    const statusSel = h('select', { 'aria-label': 'Rumour status', class: 'btn-sm' },
      h('option', { value: '', text: 'All statuses' }),
      ['pending', 'confirmed', 'expired', 'contradicted'].map((s) => h('option', { value: s, text: s })));
    statusSel.style.height = '26px';
    statusSel.style.padding = '0 6px';
    const rumSec = section('Rumours', statusSel);
    drawerBody.append(rumSec.wrap);
    let rumourToken = 0;
    const loadRumours = () => run(async () => {
      rumourToken += 1;
      const mine = rumourToken;
      clear(rumSec.body).append(loadingBox('Loading rumours'));
      const status = statusSel.value;
      const qs = status ? '?status=' + encodeURIComponent(status) : '';
      try {
        const rows = await api(`/api/players/${node.id}/rumours${qs}`);
        if (token !== drawerToken || mine !== rumourToken) return;
        renderRumours(rumSec.body, node, rows, status);
      } catch (err) {
        if (token === drawerToken && mine === rumourToken) clear(rumSec.body).append(errorBox(err));
      }
    });
    statusSel.addEventListener('change', loadRumours);
    loadRumours();
  }

  function sparkline(rows) {
    if (!Array.isArray(rows) || !rows.length) return h('p', { class: 'empty', text: 'No valuation recorded for this player.' });
    const W = 340;
    const H = 136;
    const padX = 40;
    const top = 26;
    const base = H - 28;
    const times = rows.map((r) => new Date(r.as_of + 'T00:00:00Z').getTime());
    const tMin = Math.min(...times);
    const tMax = Math.max(...times);
    const vMax = Math.max(...rows.map((r) => Number(r.value_eur) || 0)) || 1;
    const x = (t) => (tMax === tMin ? W / 2 : padX + (t - tMin) / (tMax - tMin) * (W - padX * 2));
    const y = (v) => base - (Number(v) || 0) / vMax * (base - top);
    const pts = rows.map((r, i) => [x(times[i]), y(r.value_eur)]);
    const line = pts.map((pt, i) => (i ? 'L' : 'M') + pt[0].toFixed(1) + ',' + pt[1].toFixed(1)).join('');
    const area = line + 'L' + pts[pts.length - 1][0].toFixed(1) + ',' + base + 'L' + pts[0][0].toFixed(1) + ',' + base + 'Z';
    const chart = sv('svg', { class: 'spark', viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Market value over time' },
      sv('line', { class: 's-grid', x1: 0, x2: W, y1: base, y2: base }),
      rows.length > 1 ? sv('path', { class: 's-area', d: area }) : null,
      rows.length > 1 ? sv('path', { class: 's-line', d: line }) : null);
    // With many points, only every n-th date and value is printed (first and last always), so labels never collide.
    const minGap = rows.length > 1 ? d3.min(pts.slice(1), (pt, i) => pt[0] - pts[i][0]) || W : W;
    const dateStep = Math.max(1, Math.ceil(72 / Math.max(1, minGap)));
    const valStep = Math.max(1, Math.ceil(50 / Math.max(1, minGap)));
    const shown = (i, step) => i === 0 || i === rows.length - 1 || (i % step === 0 && rows.length - 1 - i >= step);
    rows.forEach((r, i) => {
      chart.append(sv('circle', { class: 's-pt', cx: pts[i][0], cy: pts[i][1], r: 4 },
        sv('title', { text: r.as_of + ' · ' + fmtEur(r.value_eur) })));
      if (shown(i, valStep)) chart.append(sv('text', { class: 's-val', x: pts[i][0], y: pts[i][1] - 10, text: fmtEur(r.value_eur) }));
      if (shown(i, dateStep)) chart.append(sv('text', { class: 's-date', x: pts[i][0], y: H - 8, text: r.as_of }));
    });
    const first = Number(rows[0].value_eur) || 0;
    const last = Number(rows[rows.length - 1].value_eur) || 0;
    const change = fmtGrowth(first, last);
    const caption = h('p', { class: 'hint mono' },
      rows.length + ' valuation' + (rows.length > 1 ? 's' : '') +
      (change !== null && rows.length > 1 ? ' · ' + change + ' since ' + rows[0].as_of : ''));
    return h('div', null, chart, caption);
  }

  function buildPathTool(container, node) {
    let target = null;
    const input = h('input', { type: 'search', placeholder: 'Target player', spellcheck: 'false', 'aria-label': 'Target player' });
    const list = h('div', { class: 'list' });
    const hops = h('select', { 'aria-label': 'Maximum hops' }, [1, 2, 3, 4, 5, 6].map((n) => h('option', { value: n, text: '≤ ' + n + ' hops', selected: n === 6 })));
    const go = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Play path', disabled: true });
    const msg = h('div');
    makeResolver(input, list, 'players', (item) => {
      target = item;
      input.value = item.name;
      // While a path request runs, the button stays disabled (withBusy re-enables it at the end).
      if (!go.classList.contains('busy')) go.disabled = false;
    });
    input.addEventListener('input', () => { target = null; if (!go.classList.contains('busy')) go.disabled = true; });
    go.addEventListener('click', () => withBusy(go, async () => {
      if (!target) return;
      clear(msg);
      try {
        const data = await fetchPath(node.id, target.id, hops.value);
        msg.append(h('p', { class: 'hint mono', text: data.hops + ' hop' + (data.hops > 1 ? 's' : '') + ' · ' + data.players.map((p) => p.name).join(' → ') }));
        await playPath(data);
      } catch (err) {
        msg.append(errorBox(err));
      }
    }));
    container.append(h('div', { class: 'stack' },
      h('div', { class: 'path-form' }, h('div', { class: 'resolver' }, input, list), hops, go),
      h('p', { class: 'hint', text: 'Type a name (e.g. Gabriel Souza), pick it, then play the shortest chain of teammates.' }),
      msg));
  }

  function renderSimilar(container, node, rows) {
    clear(container);
    if (!rows.length) {
      container.append(h('p', { class: 'empty', text: 'No embedding neighbour for this player (no teammate data).' }));
      return;
    }
    rows.forEach((row) => {
      const other = upsertNode('player', row.player_id, { name: row.name }, node.key);
      upsertLink('SIMILAR', node.key, other.key, { label: fmtScore(row.similarity, 2) });
      container.append(h('button', {
        class: 'move-row sim-row simple-row',
        type: 'button',
        onclick: () => run(async () => {
          const mine = claimSelection();
          await ensurePlayer(row.player_id, node.key);
          if (mine === selectToken) selectNode(keyOf('player', row.player_id));
        }),
      },
      h('span', { text: row.name }),
      h('span', { class: 'bar-track' }, h('span', { class: 'bar-fill', style: { width: Math.round(Math.max(0, Math.min(1, row.similarity)) * 100) + '%' } })),
      h('span', { class: 'meta', text: fmtScore(row.similarity, 2) })));
    });
    render();
    enrichPlayers(rows.map((r) => r.player_id)).catch(toastError);
    scheduleFit(900);
  }

  function renderPredicted(container, node, rows) {
    clear(container);
    if (!rows.length) {
      container.append(h('p', { class: 'empty', text: 'No predicted move for this player.' }));
      return;
    }
    rows.forEach((row) => {
      const club = ensureClubNode(row.club_id, row.club_name, node.key);
      upsertLink('PREDICTED', node.key, club.key, { label: 'p ' + fmtPct(row.probability) });
      container.append(h('div', { class: 'move-row' },
        h('span', null, h('span', { class: 'badge b-predicted', text: 'predicted' }), ' ', row.club_name),
        h('span', { class: 'bar-track' }, h('span', { class: 'bar-fill', style: { width: Math.round(Math.max(0, Math.min(1, row.probability)) * 100) + '%' } })),
        h('span', { class: 'mono', text: fmtPct(row.probability) })));
      container.append(h('p', { class: 'hint mono', text: 'model ' + (row.model_version || '?') + ' · basis ' + (row.basis || '?') +
        (row.horizon_seasons ? ' · ' + row.horizon_seasons + ' season horizon' : '') + (row.trained_until ? ' · trained until ' + row.trained_until : '') }));
    });
    container.append(h('p', { class: 'note', text: 'Club-level estimate, not a fact: the probability that a player of the current club moves to that club.' }));
    render();
    if (G.focusKey === node.key) scheduleFit(600); // the framed neighbourhood now includes the predicted clubs
  }

  const RUMOUR_STATUSES = ['pending', 'confirmed', 'expired', 'contradicted'];
  /** A rumour is a dotted violet link from the player to the club it names (never a fact). */
  function drawRumourLink(node, row) {
    if (row.club_id === null || row.club_id === undefined) return null;
    const club = ensureClubNode(row.club_id, row.club_name, node.key);
    return upsertLink('RUMOUR', node.key, club.key, { label: row.status, rumourId: row.rumour_id });
  }
  function renderRumours(container, node, rows, status) {
    clear(container);
    // The board shows only the rumours of the current filter for this player.
    const keep = new Set(rows.map((r) => String(r.rumour_id)));
    G.links.forEach((link, key) => {
      if (link.type === 'RUMOUR' && endKey(link.source) === node.key && !keep.has(String(link.rumourId))) G.links.delete(key);
    });
    if (!rows.length) {
      container.append(h('p', { class: 'empty', text: status ? 'No ' + status + ' rumour for this player.' : 'No rumour recorded for this player.' }));
      render();
      return;
    }
    rows.forEach((row) => {
      drawRumourLink(node, row);
      const url = safeUrl(row.url);
      const status = RUMOUR_STATUSES.indexOf(row.status) >= 0 ? row.status : 'pending';
      const card = h('div', { class: 'rumour-card' },
        h('div', { class: 'row' },
          h('span', { class: 'badge b-rumour', text: 'rumour' }),
          h('span', { class: 'badge b-' + status, text: row.status ? String(row.status) : '?' }),
          h('strong', { text: '→ ' + (row.club_name || '?') })),
        row.quote ? h('blockquote', { text: '“' + row.quote + '”' }) : null,
        h('div', { class: 'meta', text: 'confidence ' + fmtPct(row.confidence) + ' = extraction ' + fmtPct(row.extraction_confidence) +
          ' × source ' + fmtPct(row.source_reliability) }),
        h('div', { class: 'meta', text: (row.sources_count || 1) + ' source' + ((row.sources_count || 1) > 1 ? 's' : '') + ' · ' +
          (row.source || 'unknown source') + ' · ' + (row.published_at || 'undated') + ' · ' + row.rumour_id }),
        url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', class: 'meta', text: 'Open article' }) : null);
      container.append(card);
    });
    render();
  }

  /* ---------- club drawer ---------- */
  function renderPipeline(container, rows) {
    clear(container);
    if (!rows.length) {
      container.append(h('p', { class: 'empty', text: 'No outgoing transfer for this club in the selected period.' }));
      return;
    }
    const maxCount = d3.max(rows, (r) => r.transfers) || 1;
    const maxFee = d3.max(rows, (r) => r.total_fee_eur) || 1;
    const list = h('div', { class: 'pipe' });
    rows.forEach((row) => {
      const n = Number(row.transfers) || 0;
      list.append(h('div', { class: 'pipe-row' },
        h('div', { class: 'pipe-name' }, h('span', { class: 'mono', text: row.competition_id }), row.competition_name || ''),
        h('div', { class: 'pipe-meta', text: n + (n === 1 ? ' transfer' : ' transfers') + ' · ' + fmtEur(row.total_fee_eur) }),
        h('div', { class: 'pipe-bars' },
          h('div', { class: 'pipe-bar count', title: n + (n === 1 ? ' transfer' : ' transfers') },
            h('div', { style: { width: Math.max(2, (n / maxCount) * 100) + '%' } })),
          h('div', { class: 'pipe-bar fee', title: fmtEur(row.total_fee_eur) + ' in fees' },
            h('div', { style: { width: Math.max(row.total_fee_eur ? 2 : 0, ((row.total_fee_eur || 0) / maxFee) * 100) + '%' } })))));
    });
    container.append(list);
    container.append(h('p', { class: 'hint pipe-note' },
      h('span', { class: 'pipe-key count' }), 'transfers', h('span', { class: 'pipe-key fee' }), 'fees (loans count as €0)'));
  }

  function pipelineTool(container, clubId, token) {
    const since = h('input', {
      type: 'text', inputmode: 'numeric', placeholder: 'YYYY-MM-DD', pattern: '\\d{4}-\\d{2}-\\d{2}', maxlength: 10,
      spellcheck: 'false', 'aria-label': 'Since date, YYYY-MM-DD', class: 'date-field',
    });
    const load = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Load' });
    const drawBtn = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Draw in graph', disabled: true });
    const out = h('div');
    let rows = null;
    let reqToken = 0;
    // Every change starts its own request; only the latest answer is shown (no dropped dates).
    const fetchIt = () => run(async () => {
      const value = since.value.trim();
      if (value && !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
        clear(out).append(errorBox(new FormError('Since must look like YYYY-MM-DD, e.g. 2020-07-01.')));
        return;
      }
      reqToken += 1;
      const mine = reqToken;
      clear(out).append(loadingBox('Loading pipeline'));
      drawBtn.disabled = true;
      load.classList.add('busy');
      load.disabled = true;
      try {
        const result = await fetchPipeline(clubId, value);
        if (mine !== reqToken || token !== drawerToken) return;
        rows = result;
        renderPipeline(out, rows);
        drawBtn.disabled = rows.length === 0;
      } catch (err) {
        if (mine === reqToken && token === drawerToken) clear(out).append(errorBox(err));
      } finally {
        if (mine === reqToken) {
          load.classList.remove('busy');
          load.disabled = false;
        }
      }
    });
    const fetchSoon = debounce(fetchIt, 300);
    load.addEventListener('click', () => { fetchSoon.cancel(); fetchIt(); });
    since.addEventListener('input', () => {
      const value = since.value.trim();
      if (!value || /^\d{4}-\d{2}-\d{2}$/.test(value)) fetchSoon();
    });
    since.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); fetchSoon.cancel(); fetchIt(); }
    });
    drawBtn.addEventListener('click', () => { if (rows) drawPipeline(clubId, rows); });
    container.append(
      h('div', { class: 'row between' },
        h('label', { class: 'inline-field' }, h('span', { class: 'caps', text: 'Since' }), since),
        h('div', { class: 'row' }, load, drawBtn)),
      out);
    fetchIt();
  }

  async function ensureNetworkMetrics() {
    await Promise.allSettled([loadHubs(), loadGateways()]);
    await Promise.allSettled([loadCommunityMetric()]);
  }

  function openClubDrawer(node) {
    const token = openDrawer('Club · #' + node.id, node.name);
    const statsBox = h('div');
    drawerBody.append(statsBox);
    const fillStats = () => {
      const info = G.clubInfo.get(Number(node.id)) || {};
      clear(statsBox).append(stats([
        ['PageRank', info.pagerank === undefined ? '—' : fmtScore(info.pagerank)],
        ['Betweenness', info.betweenness === undefined ? '—' : fmtScore(info.betweenness, 1)],
        ['Community', info.community === undefined ? '—' : '#' + info.community],
      ]));
      // The rankings list the top clubs only: a dash means "outside the list", not "no data".
      const outside = ['pagerank', 'betweenness'].filter((f) => info[f] === undefined && G.metricRows[f]);
      if (outside.length) {
        statsBox.append(h('p', { class: 'hint', text: (outside.length === 2 ? 'PageRank and betweenness are' : (outside[0] === 'pagerank' ? 'PageRank is' : 'Betweenness is')) +
          ' ranked for the top ' + Math.max(...outside.map((f) => G.metricRows[f])) + ' clubs only: this club is outside that list.' }));
      }
    };
    fillStats();
    ensureNetworkMetrics().then(() => { if (token === drawerToken) fillStats(); });

    const pipe = section('Selling pipeline by league');
    drawerBody.append(pipe.wrap);
    pipelineTool(pipe.body, node.id, token);

    const players = section('Players of this club on the board');
    drawerBody.append(players.wrap);
    const linked = [];
    G.links.forEach((link) => {
      if (link.type !== 'PLAYS_FOR') return;
      const [s, t] = nodeEndpoints(link);
      if (t && s && t.key === node.key) linked.push(s);
    });
    if (!linked.length) {
      players.body.append(h('p', { class: 'empty', text: 'None of its players is on the board yet.' }));
    } else {
      linked.sort((a, b) => (b.data.market_value_eur || 0) - (a.data.market_value_eur || 0)).forEach((p) => {
        players.body.append(h('button', { class: 'simple-row', type: 'button', onclick: () => selectNode(p.key) },
          h('span', null, h('span', { class: 'posdot', style: { background: POS_COLOR[p.data.position] || 'var(--faint)' } }), p.name),
          h('span', { class: 'meta', text: (p.data.sub_position || '') + ' · ' + fmtEur(p.data.market_value_eur) })));
      });
    }
  }

  function openCompetitionDrawer(node) {
    openDrawer('Competition · ' + node.key, node.name);
    drawerBody.append(h('dl', { class: 'kv' }, h('dt', { text: 'Code' }), h('dd', { text: node.id })));
    const sellers = section('Clubs selling into this league (on the board)');
    drawerBody.append(sellers.wrap);
    let any = false;
    G.links.forEach((link) => {
      if (link.type !== 'SELLS_TO_LEAGUE') return;
      const [s, t] = nodeEndpoints(link);
      if (t && s && t.key === node.key) {
        any = true;
        sellers.body.append(h('button', { class: 'simple-row', type: 'button', onclick: () => selectNode(s.key) },
          h('span', { text: s.name }), h('span', { class: 'meta', text: link.label || '' })));
      }
    });
    if (!any) sellers.body.append(h('p', { class: 'empty', text: 'No pipeline drawn toward this league.' }));
  }

  /* ---------- path playback ---------- */
  /** Moves a path player onto its slot of the line (about 500 ms), then keeps it pinned there. */
  function pinNode(node, tx, ty) {
    node.pathPin = { x: tx, y: ty };
    if (REDUCED || !Number.isFinite(node.x)) {
      node.x = tx;
      node.y = ty;
      node.fx = tx;
      node.fy = ty;
      return;
    }
    const x0 = node.x;
    const y0 = node.y;
    const pin = node.pathPin;
    const timer = d3.timer((elapsed) => {
      const u = Math.min(1, elapsed / 500);
      const e = d3.easeCubicInOut(u);
      if (node.pathPin !== pin) {
        timer.stop();
        pinTimers.delete(timer);
        return;
      }
      node.fx = x0 + (tx - x0) * e;
      node.fy = y0 + (ty - y0) * e;
      node.x = node.fx;
      node.y = node.fy;
      ticked();
      if (u >= 1) {
        timer.stop();
        pinTimers.delete(timer);
      }
    });
    pinTimers.add(timer);
  }
  /** Frees the players of the current path; the rest of the layout settles around them. */
  function releasePath() {
    pinTimers.forEach((t) => t.stop());
    pinTimers.clear();
    G.nodes.forEach((n) => {
      if (!n.pathPin) return;
      delete n.pathPin;
      n.fx = null;
      n.fy = null;
    });
  }
  function closePath() {
    pathRun += 1;
    releasePath();
    G.path = null;
    $('#path-card').hidden = true;
    setHighlight(null);
    render();
    kick(0.3);
    boardBox();
  }
  function renderPathCard(data) {
    const card = $('#path-card');
    const players = data.players || [];
    const matches = data.matches_together || [];
    const hopsText = data.hops + ' hop' + (data.hops === 1 ? '' : 's');
    // matches_together is one value per hop (different pairs): never summed, only its weakest link is quoted.
    const counts = matches.map(Number).filter(Number.isFinite);
    const weakest = counts.length ? Math.min(...counts) : null;
    clear(card).append(
      h('div', { class: 'row between' },
        h('span', { class: 'caps section-label', text: 'Teammate path' }),
        h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Close', onclick: closePath })),
      h('div', { class: 'path-sum' },
        h('strong', { text: players.length ? players[0].name : '?' }), ' → ',
        h('strong', { text: players.length ? players[players.length - 1].name : '?' })),
      h('div', { class: 'mono hint', text: hopsText + (weakest === null ? '' : ' · weakest link: ' + weakest + (weakest === 1 ? ' match' : ' matches')) }),
      h('ol', { class: 'path-hops' }, players.slice(1).map((p, i) => h('li', null,
        h('button', {
          class: 'hop', type: 'button', title: 'Select ' + p.name,
          onclick: () => selectNode(keyOf('player', p.player_id)),
        },
        h('span', { class: 'hop-names', text: players[i].name + ' – ' + p.name }),
        h('span', { class: 'mono hop-m', text: (matches[i] !== undefined ? matches[i] : '?') + (matches[i] === 1 ? ' match' : ' matches') }))))));
    card.hidden = false;
    boardBox();
  }

  async function playPath(data) {
    pathRun += 1;
    const myRun = pathRun;
    const players = data.players || [];
    if (!players.length) return;
    releasePath();
    const keys = new Set();
    const order = new Map(players.map((p, i) => [keyOf('player', Number(p.player_id)), i]));
    G.path = { keys, order, linkKeys: [], ends: new Set([keyOf('player', players[0].player_id), keyOf('player', players[players.length - 1].player_id)]) };
    $('#path-card').hidden = true;
    boardBox();
    // The chain reads left to right on a gently arched line: hop labels sit at regular midpoints.
    const n = players.length;
    const spacing = 125;
    const span = (n - 1) * spacing;
    const slot = (i) => ({
      x: -span / 2 + i * spacing,
      y: n > 2 ? -32 * Math.sin((Math.PI * i) / (n - 1)) : 0,
    });
    fitExtent(-span / 2 - 110, span / 2 + 110, -150, 110, 500, 1.4);
    const first = upsertNode('player', players[0].player_id, { name: players[0].name });
    keys.add(first.key);
    pinNode(first, slot(0).x, slot(0).y);
    setHighlight(keys);
    render();
    ripple(first.key, '#E9D46B', 2);
    await sleep(dur(450));
    for (let i = 1; i < players.length; i += 1) {
      const prevKey = keyOf('player', players[i - 1].player_id);
      // Stop if the board was cleared or another path started meanwhile.
      if (myRun !== pathRun || !G.nodes.has(prevKey)) return;
      const next = upsertNode('player', players[i].player_id, { name: players[i].name }, prevKey);
      const matches = (data.matches_together || [])[i - 1];
      const link = upsertLink('PLAYED_WITH', prevKey, next.key, {
        weight: matches || 1,
        // The number alone on the board; the path card and the label's title say "matches together".
        label: matches !== undefined ? String(matches) : '',
      });
      keys.add(next.key);
      G.path.linkKeys.push(link.key);
      pinNode(next, slot(i).x, slot(i).y);
      render();
      await sleep(dur(260));
      if (myRun !== pathRun) return;
      await pulseAlong(link, dur(620));
      if (myRun !== pathRun) return;
      ripple(next.key, '#E9D46B', 1);
    }
    renderPathCard(data);
    // The finished path fills the board (its clubs may join, but the frame stays on the chain).
    const pathKeys = new Set(keys);
    scheduleFit(450, pathKeys);
    enrichPlayers(players.map((p) => p.player_id), pathKeys).catch(toastError);
  }
  function fetchPath(from, to, maxHops) {
    const qs = 'from=' + encodeURIComponent(Number(from)) + '&to=' + encodeURIComponent(Number(to)) +
      '&max_hops=' + encodeURIComponent(Number(maxHops) || 6);
    return api('/api/players/path?' + qs);
  }
  async function playPathByIds(from, to, maxHops) {
    const data = await fetchPath(from, to, maxHops);
    await playPath(data);
    return data;
  }

  /* ================================================================== */
  /* Resolver component                                                  */
  /* ================================================================== */
  function makeResolver(input, list, kind, onPick) {
    let token = 0;
    let items = [];
    let fresh = false; // true only when items answer the text currently typed
    const noun = kind === 'players' ? 'player' : 'club';
    const pick = (item) => { clear(list); onPick(item); };
    async function search() {
      const q = input.value.trim();
      token += 1;
      const mine = token;
      if (q.length < 2) {
        clear(list);
        items = [];
        fresh = true;
        return;
      }
      clear(list).append(loadingBox('Resolving'));
      try {
        const rows = await api((kind === 'players' ? '/api/players/resolve' : '/api/clubs/resolve') + '?name=' + encodeURIComponent(q) + '&limit=8');
        if (mine !== token) return;
        items = rows;
        fresh = true;
        clear(list);
        if (!rows.length) {
          // The full-text index matches words of the name: an acronym such as "PSG" finds nothing.
          list.append(h('p', { class: 'empty', text: 'No ' + noun + ' matches “' + q + '”. Try a word of the full name' +
            (kind === 'players' ? '.' : ' (e.g. Paris, not PSG).') }));
          return;
        }
        rows.forEach((item) => {
          list.append(h('button', {
            class: 'simple-row',
            type: 'button',
            onclick: () => pick(item),
          }, h('span', { text: item.name }), h('span', { class: 'meta', text: item.kind + ':' + item.id + ' · ' + fmtScore(item.score, 2) })));
        });
      } catch (err) {
        if (mine === token) clear(list).append(errorBox(err));
      }
    }
    const searchSoon = debounce(() => { search().catch(toastError); }, 260);
    input.addEventListener('input', () => {
      // Results of the previous text must never be picked for the new one.
      items = [];
      fresh = false;
      token += 1;
      searchSoon();
    });
    input.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      run(async () => {
        if (!fresh) {
          searchSoon.cancel();
          await search();
        }
        if (!fresh || !items.length) return;
        const tie = items.length > 1 && Math.abs(Number(items[0].score) - Number(items[1].score)) < 1e-9;
        if (tie) {
          list.querySelectorAll('.simple-row').forEach((row) => row.classList.add('ambiguous'));
          if (!list.querySelector('.ambiguous-note')) {
            list.prepend(h('p', { class: 'hint ambiguous-note', text: 'Several ' + noun + 's match equally: pick one.' }));
          }
          const firstRow = list.querySelector('.simple-row');
          if (firstRow) firstRow.focus();
          return;
        }
        pick(items[0]);
      });
    });
  }

  /* ================================================================== */
  /* Tabs                                                                */
  /* ================================================================== */
  const TABS = ['agent', 'players', 'clubs', 'network', 'admin'];
  const tabLoaded = {};
  function setTab(name) {
    const tab = TABS.indexOf(name) >= 0 ? name : 'agent';
    document.querySelectorAll('.tab').forEach((btn) => {
      btn.setAttribute('aria-selected', String(btn.dataset.tab === tab));
      btn.tabIndex = btn.dataset.tab === tab ? 0 : -1; // roving tab stop: arrows move between tabs
    });
    TABS.forEach((t) => { $('#tab-' + t).hidden = t !== tab; });
    if (!tabLoaded[tab]) {
      tabLoaded[tab] = true;
      if (tab === 'network') loadNetworkTab();
      if (tab === 'clubs') renderKnownClubs();
      if (tab === 'players' && !searchState.params) run(() => runSearch(false));
    }
  }
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => setTab(btn.dataset.tab));
    btn.addEventListener('keydown', (event) => {
      const idx = TABS.indexOf(btn.dataset.tab);
      let next = null;
      if (event.key === 'ArrowRight') next = TABS[(idx + 1) % TABS.length];
      else if (event.key === 'ArrowLeft') next = TABS[(idx - 1 + TABS.length) % TABS.length];
      else if (event.key === 'Home') next = TABS[0];
      else if (event.key === 'End') next = TABS[TABS.length - 1];
      if (!next) return;
      event.preventDefault();
      setTab(next);
      const target = document.querySelector('.tab[data-tab="' + next + '"]');
      if (target) target.focus();
    });
  });

  /* ================================================================== */
  /* Players tab                                                         */
  /* ================================================================== */
  const searchState = { params: null, cursor: null, rows: [] };
  const searchResults = $('#search-results');
  const searchMsg = $('#search-msg');
  const searchMore = $('#search-more');
  const searchAll = $('#search-all');

  function openPlayerFromSummary(summary) {
    const node = addPlayerSummary(summary);
    render();
    selectNode(node.key);
  }
  async function openPlayerById(id) {
    const mine = claimSelection();
    const node = await ensurePlayer(id);
    // A newer pick was made while this one loaded: keep the node, leave the selection alone.
    if (mine !== selectToken) return node;
    selectNode(node.key);
    ripple(node.key, '#E9D46B', 2);
    return node;
  }

  function playerRow(p) {
    const age = ageOf(p.date_of_birth);
    return h('button', { class: 'list-row', type: 'button', onclick: () => openPlayerFromSummary(p) },
      h('span', null,
        h('span', { class: 'name' }, h('span', { class: 'posdot', style: { background: POS_COLOR[p.position] || 'var(--faint)' } }), p.name),
        h('span', { class: 'sub', text: p.club_name || 'No current club', title: p.club_name || null })),
      h('span', { class: 'pos', text: SUB_SHORT[p.sub_position] || POS_SHORT[p.position] || '—', title: p.sub_position || p.position || null }),
      h('span', { class: 'num', text: age === null ? '—' : String(age) }),
      h('span', { class: 'num', text: fmtEur(p.market_value_eur) }));
  }

  function readSearchParams() {
    // Text the browser rejects in a number field reads as "": refuse it rather than silently drop the filter.
    const badField = [['#f-age', 'Max age'], ['#f-value', 'Max value'], ['#f-limit', 'Page size']]
      .find(([sel]) => $(sel).validity && $(sel).validity.badInput);
    if (badField) throw new FormError(badField[1] + ' must be a number.');
    const params = new URLSearchParams();
    const position = $('#f-position').value;
    const sub = $('#f-sub').value.trim();
    const age = $('#f-age').value.trim();
    const value = $('#f-value').value.trim();
    const comp = $('#f-comp').value;
    const limit = $('#f-limit').value.trim() || '20';
    if (position) params.set('position', position);
    if (sub) params.set('sub_position', sub);
    if (age) params.set('max_age', age);
    if (value) {
      const eur = Math.round(Number(value) * 1e6);
      params.set('max_value_eur', Number.isFinite(eur) ? String(eur) : value);
    }
    if (comp) params.set('competition_id', comp);
    params.set('limit', limit);
    return params;
  }

  let searchGen = 0;
  let searchBusy = false;
  async function runSearch(append) {
    const btn = append ? searchMore : $('#search-go');
    if (append && searchBusy) return;
    // A new search always wins: a late "Load more" answer of the old query is dropped.
    searchGen += 1;
    const gen = searchGen;
    searchBusy = true;
    btn.classList.add('busy');
    searchMore.disabled = true;
    if (!append) $('#search-go').disabled = true;
    try {
      if (!append) {
        try {
          searchState.params = readSearchParams();
        } catch (err) {
          if (!(err instanceof FormError)) throw err;
          clear(searchResults);
          clear(searchMsg).append(errorBox(err));
          $('#search-head').hidden = true;
          searchMore.hidden = true;
          searchAll.hidden = true;
          searchState.params = null;
          return;
        }
        searchState.cursor = null;
        searchState.rows = [];
        clear(searchResults);
        clear(searchMsg).append(loadingBox('Searching'));
        $('#search-head').hidden = true;
        searchMore.hidden = true;
        searchAll.hidden = true;
      }
      const params = new URLSearchParams(searchState.params);
      if (append && searchState.cursor) params.set('cursor', searchState.cursor);
      try {
        const page = await api('/api/players/search?' + params.toString());
        if (gen !== searchGen) return;
        const items = Array.isArray(page.items) ? page.items : [];
        clear(searchMsg);
        searchState.cursor = page.next_cursor || null;
        searchState.rows = searchState.rows.concat(items);
        items.forEach((p) => searchResults.append(playerRow(p)));
        if (!searchState.rows.length) {
          searchMsg.append(h('p', { class: 'empty', text: 'No player matches these filters.' }));
        } else {
          searchMsg.append(h('p', { class: 'hint mono', text: searchState.rows.length + ' player' + (searchState.rows.length > 1 ? 's' : '') + (searchState.cursor ? ' · more available' : ' · end of results') }));
        }
        $('#search-head').hidden = searchState.rows.length === 0;
        searchMore.hidden = !searchState.cursor;
        searchAll.hidden = searchState.rows.length === 0;
      } catch (err) {
        if (gen === searchGen) clear(searchMsg).append(errorBox(err));
      }
    } finally {
      if (gen === searchGen) {
        searchBusy = false;
        searchMore.disabled = false;
        searchMore.classList.remove('busy');
        $('#search-go').classList.remove('busy');
        $('#search-go').disabled = false;
      }
    }
  }
  $('#search-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if ($('#search-go').disabled) return; // a search is already running
    $('#search-go').disabled = true;
    run(() => runSearch(false));
  });
  searchMore.addEventListener('click', () => run(() => runSearch(true)));
  searchAll.addEventListener('click', () => {
    searchState.rows.forEach((p) => addPlayerSummary(p));
    render();
    scheduleFit(900);
  });
  $('#search-reset').addEventListener('click', () => {
    ['#f-position', '#f-sub', '#f-age', '#f-value', '#f-comp'].forEach((sel) => { $(sel).value = ''; });
    $('#f-limit').value = '20';
  });
  $('#player-resolve-form').addEventListener('submit', (event) => event.preventDefault());
  makeResolver($('#player-resolve'), $('#player-resolve-results'), 'players', (item) => {
    $('#player-resolve').value = item.name;
    run(() => openPlayerById(item.id));
  });

  /* ================================================================== */
  /* Clubs tab                                                           */
  /* ================================================================== */
  let activeClubId = null;
  function markActiveClub() {
    document.querySelectorAll('#club-known .chip').forEach((chip) => {
      chip.classList.toggle('active', Number(chip.dataset.club) === activeClubId);
    });
  }
  function renderKnownClubs() {
    const box = $('#club-known');
    clear(box).append(loadingBox('Loading clubs'));
    loadHubs().then((rows) => {
      clear(box);
      // The most central clubs first (the real graph has 2,000+ clubs: an alphabetical list would stop at "G").
      const top = rows.slice().sort((a, b) => b.score - a.score).slice(0, 30);
      top.forEach((row) => {
        const chip = h('button', {
          class: 'chip',
          type: 'button',
          text: row.name,
          onclick: () => run(() => selectClubInTab(row.club_id, row.name)),
        });
        chip.dataset.club = String(row.club_id);
        box.append(chip);
      });
      if (!rows.length) box.append(h('p', { class: 'empty', text: 'No club scored yet: run the GDS refresh.' }));
      markActiveClub();
    }).catch((err) => { clear(box).append(errorBox(err)); });
  }

  /**
   * The Clubs tab only picks a club; its pipeline and metrics live in the drawer (one place).
   * An error is shown once, inline. With `rethrow` (deep link) it is raised instead, so the caller shows its banner.
   */
  async function selectClubInTab(id, name, { autoDraw = true, rethrow = false } = {}) {
    const detail = $('#club-detail');
    const clubId = Number(id);
    activeClubId = clubId;
    markActiveClub();
    clear(detail).append(loadingBox('Opening club'));
    try {
      await openClubById(clubId, { name, autoDraw });
      if (activeClubId === clubId) {
        clear(detail).append(h('p', { class: 'hint', text: 'Opened on the board: selling pipeline, network metrics and its players are in the panel on the right.' }));
      }
    } catch (err) {
      if (activeClubId === clubId) {
        activeClubId = null;
        markActiveClub();
        clear(detail).append(rethrow
          ? h('p', { class: 'empty', text: 'Pick a club: it opens on the board with its selling pipeline in the panel on the right.' })
          : errorBox(err));
      }
      if (rethrow) throw err;
    }
  }
  $('#club-resolve-form').addEventListener('submit', (event) => event.preventDefault());
  makeResolver($('#club-resolve'), $('#club-resolve-results'), 'clubs', (item) => {
    $('#club-resolve').value = item.name;
    const info = G.clubInfo.get(Number(item.id)) || {};
    info.name = item.name;
    G.clubInfo.set(Number(item.id), info);
    run(() => selectClubInTab(item.id, item.name));
  });

  /* ================================================================== */
  /* Network tab                                                         */
  /* ================================================================== */
  function renderBars(container, rows, digits) {
    clear(container);
    if (!rows.length) {
      container.append(h('p', { class: 'empty', text: 'No score yet: run the GDS refresh in Admin.' }));
      return;
    }
    const max = d3.max(rows, (r) => r.score) || 1;
    rows.forEach((row, i) => {
      container.append(h('button', {
        class: 'bar-row',
        type: 'button',
        title: 'Show ' + row.name + ' on the board',
        onclick: () => {
          ensureClubNode(row.club_id, row.name);
          render();
          selectNode(keyOf('club', row.club_id));
          ripple(keyOf('club', row.club_id), '#E9D46B', 2);
        },
      },
      h('span', { class: 'bar-rank', text: String(i + 1).padStart(2, '0') }),
      h('span', { class: 'bar-name', text: row.name }),
      h('span', { class: 'bar-track' }, h('span', { class: 'bar-fill', style: { width: Math.max(1, (row.score / max) * 100) + '%', 'animation-delay': (i * 30) + 'ms' } })),
      h('span', { class: 'bar-val', text: fmtScore(row.score, digits) })));
    });
  }

  // Cached per min size; a generation counter drops the answer of an older min size that arrives late.
  const communitiesCache = new Map();
  let communitiesGen = 0;
  /** Loads the communities for the current min size and renders them, unless a newer request started. */
  async function showCommunities(force) {
    communitiesGen += 1;
    const gen = communitiesGen;
    const box = $('#communities');
    const minSize = Math.max(1, Math.floor(Number($('#comm-min').value)) || 2);
    let rows = force ? null : communitiesCache.get(minSize);
    if (!rows) {
      clear(box).append(loadingBox('Loading communities'));
      try {
        rows = await api('/api/network/communities?min_size=' + minSize);
      } catch (err) {
        if (gen === communitiesGen) clear(box).append(errorBox(err));
        return;
      }
      communitiesCache.set(minSize, rows);
    }
    if (gen === communitiesGen) renderCommunities(rows);
  }

  /** Club ids for names, keeping their order; unknown names are resolved a few at a time, in parallel. */
  async function resolveClubIds(names) {
    const ids = names.map((name) => clubIdByName(name));
    const todo = names.map((name, i) => i).filter((i) => ids[i] === null);
    for (let start = 0; start < todo.length; start += 6) {
      await Promise.all(todo.slice(start, start + 6).map(async (i) => {
        try {
          const rows = await api('/api/clubs/resolve?name=' + encodeURIComponent(names[i]) + '&limit=1');
          if (rows.length) ids[i] = Number(rows[0].id);
        } catch (err) { ids[i] = null; }
      }));
    }
    return names.map((name, i) => ({ id: ids[i], name })).filter((c) => c.id !== null);
  }

  // A real community holds hundreds of clubs: the card lists a few names, the board draws a capped sample.
  const COMMUNITY_CHIPS = 14;
  const COMMUNITY_DRAWN = 30;

  function renderCommunities(rows) {
    const box = $('#communities');
    clear(box);
    if (!rows.length) {
      box.append(h('p', { class: 'empty', text: 'No community of that size.' }));
      return;
    }
    rows.forEach((c) => {
      const names = Array.isArray(c.clubs) ? c.clubs : [];
      const size = Number(c.size) || names.length;
      const more = size - Math.min(names.length, COMMUNITY_CHIPS);
      const note = h('div', { class: 'mono hint community-note', hidden: true });
      const card = h('button', { class: 'community', type: 'button' },
        h('div', { class: 'row between' },
          h('span', { class: 'caps', text: 'Community #' + c.community_id }),
          h('span', { class: 'mono hint', text: size + ' clubs' })),
        h('div', { class: 'chips' }, names.slice(0, COMMUNITY_CHIPS).map((name) => h('span', { text: name, title: name })),
          more > 0 ? h('span', { class: 'chip-more', text: '+' + more + ' more' }) : null),
        note);
      card.addEventListener('click', () => withBusy(card, async () => {
        const wasActive = card.classList.contains('active');
        setHighlight(null);
        note.hidden = true;
        if (wasActive) return;
        // The API lists the most central clubs first: the board draws the first ones only, and says so.
        const clubs = (await resolveClubIds(names.slice(0, COMMUNITY_DRAWN))).slice(0, COMMUNITY_DRAWN);
        const keys = new Set();
        clubs.forEach((club) => {
          const node = ensureClubNode(club.id, club.name);
          keys.add(node.key);
        });
        render();
        setHighlight(keys);
        card.classList.add('active');
        if (size > keys.size) {
          note.textContent = 'Showing ' + keys.size + ' of ' + size + ' clubs on the board';
          note.hidden = false;
        }
        keys.forEach((k) => ripple(k, '#E9D46B', 2));
        scheduleFit(700, keys);
      }));
      box.append(card);
    });
  }

  function loadNetworkTab() {
    const hubsBox = $('#hubs');
    const gwBox = $('#gateways');
    clear(hubsBox).append(loadingBox('Loading PageRank'));
    clear(gwBox).append(loadingBox('Loading betweenness'));
    loadHubs().then((rows) => renderBars(hubsBox, rows.slice(0, 12), 3)).catch((err) => clear(hubsBox).append(errorBox(err)));
    loadGateways().then((rows) => renderBars(gwBox, rows.slice(0, 12), 1)).catch((err) => clear(gwBox).append(errorBox(err)));
    run(() => showCommunities(false));
  }
  $('#comm-min').addEventListener('change', () => run(() => showCommunities(true)));
  function drawClubList(btn, loader) {
    return withBusy(btn, async () => {
      const rows = await loader();
      rows.slice(0, 12).forEach((row) => ensureClubNode(row.club_id, row.name));
      render();
      scheduleFit(900);
    });
  }
  $('#hubs-draw').addEventListener('click', (e) => drawClubList(e.currentTarget, () => loadHubs()));
  $('#gateways-draw').addEventListener('click', (e) => drawClubList(e.currentTarget, () => loadGateways()));

  /* ================================================================== */
  /* Admin tab                                                           */
  /* ================================================================== */
  $('#gds-refresh').addEventListener('click', (event) => {
    const btn = event.currentTarget;
    const out = $('#gds-report');
    withBusy(btn, async () => {
      clear(out).append(loadingBox('Recomputing projections and algorithms'));
      try {
        const report = await api('/api/admin/gds/refresh', { method: 'POST', auth: true, timeout: 180000 });
        clear(out).append(h('div', { class: 'msg info' },
          h('div', { class: 'msg-title', text: 'GDS refreshed in ' + Number(report.duration_s || 0).toFixed(2) + ' s' }),
          h('dl', { class: 'kv' },
            h('dt', { text: 'Projections' }), h('dd', { text: (report.projections || []).join(', ') || '—' }),
            h('dt', { text: 'Algorithms' }), h('dd', { text: (report.algorithms || []).join(', ') || '—' }))));
        hubsPromise = null;
        gatewaysPromise = null;
        communityMetricPromise = null;
        communitiesCache.clear();
        await ensureNetworkMetrics();
        render();
        if (tabLoaded.network) loadNetworkTab();
      } catch (err) {
        clear(out).append(errorBox(err));
      }
    });
  });

  $('#import-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const btn = $('#import-go');
    const out = $('#import-report');
    const feeds = $('#import-feeds').value.split(/\s+/).map((s) => s.trim()).filter(Boolean);
    const rawMax = $('#import-max').value.trim();
    const max = rawMax ? Number(rawMax) : 10;
    clear(out);
    if (!feeds.length) {
      out.append(errorBox(new FormError('Enter at least one feed URL.')));
      return;
    }
    if (!Number.isInteger(max) || max < 1 || max > 500) {
      out.append(errorBox(new FormError('Max articles must be a whole number between 1 and 500 (empty means 10).')));
      return;
    }
    const bad = feeds.filter((f) => !safeUrl(f));
    if (bad.length) {
      out.append(errorBox(new FormError('Not an http(s) URL: ' + bad.join(', '))));
      return;
    }
    withBusy(btn, async () => {
      out.append(loadingBox('Fetching feeds and extracting rumours'));
      try {
        const report = await api('/api/rumours/import', {
          method: 'POST', auth: true, timeout: 600000, body: { feed_urls: feeds, max_articles: max },
        });
        const fields = ['articles_seen', 'articles_new', 'rumours_created', 'unresolved_names', 'feeds_failed', 'extraction_errors', 'rejected_quotes'];
        const kv = h('dl', { class: 'kv' });
        fields.forEach((f) => {
          const v = report[f];
          kv.append(h('dt', { text: f.replace(/_/g, ' ') }), h('dd', { text: Array.isArray(v) ? (v.join(', ') || '0') : String(v === undefined || v === null ? '—' : v) }));
        });
        const examples = Array.isArray(report.unresolved_examples) ? report.unresolved_examples : [];
        clear(out).append(h('div', { class: 'msg info' },
          h('div', { class: 'msg-title', text: 'Import finished' }), kv,
          examples.length ? h('div', null, h('div', { class: 'caps', text: 'Unresolved names (never invented)' }),
            h('ul', null, examples.map((ex) => h('li', { text: typeof ex === 'string' ? ex : JSON.stringify(ex) })))) : null));
      } catch (err) {
        clear(out).append(errorBox(err));
      }
    });
  });

  /* ================================================================== */
  /* Agent tab                                                           */
  /* ================================================================== */
  // Prepared questions for each dataset: the fictitious mini graph of the tests, or the real Transfermarkt graph.
  // Each list has answerable questions first, then traps (level judgement, write request, ambiguous name).
  const SUGGESTIONS_MINI = [
    "How did Lucas Marchand's market value evolve?",
    'Where does SC Alvorada sell its players?',
    'Shortest teammate chain between Hugo Lefevre and Gabriel Souza?',
    'Which club is Gabriel Rocha predicted to join?',
    'Is Joao Ferreira better than Theo Garnier?',
    'Delete every rumour about Diogo Santos',
    'Where could Gabriel move next?',
  ];
  const SUGGESTIONS_REAL = [
    "How did Kylian Mbappé's market value evolve?",
    'Shortest teammate chain between Kylian Mbappé and Erling Haaland?',
    'Which leagues does Real Madrid sell its players to, and for how much in total?',
    'Which clubs is Bukayo Saka predicted to join?',
    'Which clubs act as the biggest transfer hubs in the network?',
    'Which players did PSG buy from Premier League clubs since 2022?',
    'Is Mbappé better than Haaland?',
    'Remove all transfers of Neymar from the database.',
    'Where could Gabriel move next?',
  ];
  const question = $('#agent-question');
  const askBtn = $('#agent-ask');
  const agentOut = $('#agent-result');
  const count = $('#agent-count');
  function updateCount() { count.textContent = question.value.length + ' / 500 · Ctrl+Enter to ask'; }
  question.addEventListener('input', updateCount);
  question.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      $('#agent-form').requestSubmit();
    }
  });
  function renderSuggestions(list) {
    const box = $('#agent-suggestions');
    clear(box);
    list.forEach((text, i) => {
      box.append(h('button', {
        class: 'suggestion',
        type: 'button',
        onclick: () => { question.value = text; updateCount(); question.focus(); },
      },
      h('span', { class: 'mono s-idx', text: String(i + 1).padStart(2, '0') }),
      h('span', { class: 's-text', text }),
      h('span', { class: 's-arrow', 'aria-hidden': 'true', text: '→' })));
    });
    const summary = $('#agent-prepared summary');
    if (summary) summary.textContent = 'Prepared questions (' + list.length + ')';
  }
  renderSuggestions(SUGGESTIONS_MINI);
  // The mini graph has a player named exactly "Lucas Marchand"; the real graph does not.
  (async function pickSuggestions() {
    try {
      const rows = await api('/api/players/resolve?name=' + encodeURIComponent('Lucas Marchand') + '&limit=1');
      const mini = Array.isArray(rows) && rows.some((row) => row.name === 'Lucas Marchand');
      if (!mini) renderSuggestions(SUGGESTIONS_REAL);
    } catch (err) {
      // Keep the default list: the health pill already reports an unreachable API.
    }
  })();

  /** Ids the board can place: players, clubs, competitions, predicted moves, valuations, rumours. */
  const DRAWABLE = new Set(['Player', 'Club', 'Competition', 'PREDICTED_MOVE', 'Valuation', 'Rumour']);
  function claimItem(claim, removed) {
    const nature = natureOf(claim);
    const evidence = claim.evidence_ids || [];
    const meta = h('div', { class: 'claim-meta' }, h('span', { class: 'badge b-' + nature, text: nature }));
    evidence.forEach((id) => {
      const ev = parseEvidence(id);
      if (!DRAWABLE.has(ev.kind)) {
        // Transfers (and any other fact without a node) are cited, not drawn: plain text, not a dead button.
        meta.append(h('span', {
          class: 'ev ev-static',
          text: id,
          title: ev.kind === 'Transfer' ? 'Transfer fact cited by this claim; transfers are not drawn as nodes' : 'Cited by this claim; not drawn on the board',
        }));
        return;
      }
      meta.append(h('button', {
        class: 'ev',
        type: 'button',
        text: id,
        title: 'Show ' + id + ' on the board',
        onclick: () => run(() => showEvidence(id, nature, evidence)),
      }));
    });
    return h('li', { class: 'claim n-' + nature },
      amountText('div', 'claim-text', claim.text),
      meta,
      removed && claim.removal_reason ? h('div', { class: 'removal', text: 'Removed: ' + claim.removal_reason }) : null);
  }

  function renderAnswer(answer, seconds, asked) {
    clear(agentOut);
    if (asked) agentOut.append(h('p', { class: 'q-echo', text: 'Q: ' + asked }));
    let tag = h('span', { class: 'status-tag st-answered', text: 'Answered' });
    if (answer.refused) tag = h('span', { class: 'status-tag st-refused', text: 'Refused' });
    else if (answer.needs_clarification) tag = h('span', { class: 'status-tag st-clarify', text: 'Needs clarification' });
    const claims = answer.claims || [];
    const removed = answer.removed_claims || [];
    const trace = answer.trace || [];
    agentOut.append(h('div', { class: 'status-line' }, tag,
      h('span', { class: 'mono hint', text: claims.length + ' claim' + (claims.length === 1 ? '' : 's') + ' · ' + removed.length + ' removed · ' +
        trace.length + ' tool call' + (trace.length === 1 ? '' : 's') + ' · ' + (seconds < 1 ? Math.round(seconds * 1000) + ' ms' : seconds.toFixed(1) + ' s') })));
    agentOut.append(amountText('div', 'answer', answer.answer || ''));
    if (claims.length) {
      agentOut.append(h('div', { class: 'sub-head' }, h('span', { class: 'caps section-label', text: 'Checked claims' }),
        h('span', { class: 'hint', text: 'click an id to place it on the board' })));
      agentOut.append(h('ul', { class: 'claims' }, claims.map((c) => claimItem(c, false))));
    }
    if (removed.length) {
      agentOut.append(h('details', { class: 'removed' },
        h('summary', null, h('span', { class: 'caps', text: 'Removed by the verifier (' + removed.length + ')' })),
        h('ul', { class: 'claims' }, removed.map((c) => claimItem(c, true)))));
    }
    agentOut.append(h('div', { class: 'sub-head' }, h('span', { class: 'caps section-label', text: 'Trace' }),
      h('span', { class: 'hint', text: trace.length ? 'tool calls in order' : 'no tool was called' })));
    if (trace.length) {
      agentOut.append(h('ol', { class: 'timeline' }, trace.map((step, i) => {
        const args = step.arguments && Object.keys(step.arguments).length ? JSON.stringify(step.arguments) : null;
        return h('li', { class: step.succeeded === false ? 'fail' : 'ok' },
          h('span', { class: 'dot' }),
          h('div', { class: 't-head' },
            h('span', { class: 'mono hint', text: String(i + 1).padStart(2, '0') }),
            h('span', { class: 't-tool', text: step.tool }),
            h('span', { class: 't-state', text: step.succeeded === false ? 'failed' : 'succeeded' })),
          args ? h('div', { class: 't-args', text: args }) : null,
          step.summary ? h('div', { class: 't-sum', text: step.summary }) : null);
      })));
    }
  }
  function revealResult() {
    agentOut.scrollIntoView({ block: 'start', behavior: REDUCED ? 'auto' : 'smooth' });
  }

  /** Draws a PREDICTED_MOVE evidence id as a dashed amber link with its probability. */
  async function drawPredictedMove(fromId, clubId) {
    const player = await ensurePlayer(fromId);
    await loadHubsQuietly();
    const club = ensureClubNode(clubId, null, player.key);
    let prob = null;
    try {
      const pid = Number(fromId);
      const moves = await api(`/api/players/${pid}/predicted-moves?limit=20`);
      const row = moves.find((m) => Number(m.club_id) === clubId);
      if (row) prob = row.probability;
    } catch (err) { prob = null; }
    upsertLink('PREDICTED', player.key, club.key, { label: prob === null ? 'predicted' : 'p ' + fmtPct(prob) });
    render();
    return [player, club];
  }

  /** Club ids cited next to a competition: the pipeline of that club explains the competition. */
  function clubIdsIn(evidence) {
    return (evidence || []).map(parseEvidence).filter((e) => e.kind === 'Club' && Number.isInteger(e.clubId)).map((e) => e.clubId);
  }
  function playerIdsIn(evidence) {
    return (evidence || []).map(parseEvidence).filter((e) => e.kind === 'Player' && Number.isInteger(e.playerId)).map((e) => e.playerId);
  }
  function findRumourLink(rumourId) {
    let found = null;
    G.links.forEach((link) => { if (link.type === 'RUMOUR' && String(link.rumourId) === String(rumourId)) found = link; });
    return found;
  }
  /**
   * Loads the rumours of the players cited next to a rumour id and draws the matching one.
   * Returns the RUMOUR link, or null when none of those players carries that rumour.
   */
  async function drawRumourFrom(rumourId, playerIds) {
    for (const pid of playerIds.slice(0, 4)) {
      if (findRumourLink(rumourId)) break;
      try {
        const playerId = Number(pid);
        const rows = await api(`/api/players/${playerId}/rumours`);
        const row = rows.find((r) => String(r.rumour_id) === String(rumourId));
        if (!row) continue;
        const player = await ensurePlayer(pid);
        await loadHubsQuietly();
        drawRumourLink(player, row);
        render();
      } catch (err) { /* this player cannot explain it; try the next one */ }
    }
    return findRumourLink(rumourId);
  }

  async function showEvidence(id, nature, evidence) {
    const mine = claimSelection();
    const stillMine = () => mine === selectToken;
    const color = NATURE_COLOR[nature] || NEUTRAL_COLOR;
    const ev = parseEvidence(id);
    const notLoaded = () => toast(id + ' is cited by the agent but is not loaded on the board.');
    if (ev.kind === 'invalid') {
      toast(id + ' is not a graph id.');
      return;
    }
    if (ev.kind === 'Player' || ev.kind === 'Valuation') {
      const node = await ensurePlayer(ev.playerId);
      if (!stillMine()) return;
      selectNode(node.key);
      ripple(node.key, color, 3);
    } else if (ev.kind === 'Club') {
      const key = keyOf('club', ev.clubId);
      if (G.nodes.has(key)) {
        selectNode(key);
      } else {
        await openClubById(ev.clubId, { token: mine }); // throws on 404: nothing is drawn for an unknown club
        if (!stillMine()) return;
      }
      ripple(key, color, 3);
    } else if (ev.kind === 'PREDICTED_MOVE') {
      const pair = await drawPredictedMove(ev.playerId, ev.clubId);
      if (!stillMine()) return;
      selectNode(pair[0].key);
      ripple(pair[0].key, color, 3);
      ripple(pair[1].key, color, 3);
    } else if (ev.kind === 'Competition') {
      if (!G.nodes.has(ev.key)) await drawClubPipelines(clubIdsIn(evidence), () => G.nodes.has(ev.key));
      if (!stillMine()) return;
      if (G.nodes.has(ev.key)) {
        selectNode(ev.key);
        ripple(ev.key, color, 3);
      } else {
        notLoaded();
      }
    } else if (ev.kind === 'Rumour') {
      const found = findRumourLink(ev.rest) || await drawRumourFrom(ev.rest, playerIdsIn(evidence));
      if (!stillMine()) return;
      if (!found) { notLoaded(); return; }
      const [s, t] = nodeEndpoints(found);
      // The drawer opens on the player whose rumour it is (its Rumours section lists it).
      if (s) { selectNode(s.key); ripple(s.key, color, 3); }
      if (t) ripple(t.key, color, 3);
    } else if (G.nodes.has(ev.key)) {
      selectNode(ev.key);
      ripple(ev.key, color, 3);
    } else {
      toast(id + ' is cited by the claim; this kind of fact is not drawn as a node.');
      return;
    }
    scheduleFit(700);
  }

  async function rippleEvidence(answer) {
    const all = (answer.claims || []).concat(answer.removed_claims || []);
    const parsed = all.map((c) => (c.evidence_ids || []).map(parseEvidence));
    const playerIds = new Set();
    const moves = new Map();
    parsed.forEach((list) => list.forEach((ev) => {
      if ((ev.kind === 'Player' || ev.kind === 'Valuation') && Number.isInteger(ev.playerId)) playerIds.add(ev.playerId);
      if (ev.kind === 'PREDICTED_MOVE' && moves.size < 4) moves.set(ev.playerId + '->' + ev.clubId, ev);
    }));
    const missing = Array.from(playerIds).filter((pid) => !G.nodes.has(keyOf('player', pid))).slice(0, 8);
    await Promise.allSettled(missing.map((pid) => ensurePlayer(pid)));
    for (const ev of moves.values()) {
      // One request per distinct pair, even when several claims cite the same move.
      await drawPredictedMove(ev.playerId, ev.clubId).catch(() => null);
    }
    // Competitions: draw the pipeline of the club cited in the same claim (once per club).
    const pipelineClubs = new Set();
    all.forEach((c, i) => {
      if (parsed[i].some((ev) => ev.kind === 'Competition')) clubIdsIn(c.evidence_ids).forEach((cid) => pipelineClubs.add(cid));
    });
    await drawClubPipelines(Array.from(pipelineClubs));
    // Rumours: load the rumours of the player cited in the same claim and draw the cited ones.
    for (let i = 0; i < all.length; i += 1) {
      const rumourIds = parsed[i].filter((ev) => ev.kind === 'Rumour').map((ev) => ev.rest).slice(0, 4);
      for (const rid of rumourIds) {
        if (!findRumourLink(rid)) await drawRumourFrom(rid, playerIdsIn(all[i].evidence_ids));
      }
    }
    (answer.claims || []).forEach((c, i) => {
      const color = NATURE_COLOR[natureOf(c)] || NEUTRAL_COLOR;
      parsed[i].forEach((ev) => {
        if (ev.kind === 'PREDICTED_MOVE') {
          ripple(keyOf('player', ev.playerId), color, 3);
          ripple(keyOf('club', ev.clubId), color, 3);
        } else if (ev.kind === 'Valuation') {
          ripple(keyOf('player', ev.playerId), color, 3);
        } else if (ev.kind === 'Rumour') {
          const link = findRumourLink(ev.rest);
          if (link) nodeEndpoints(link).forEach((n) => { if (n) ripple(n.key, color, 3); });
        } else if (G.nodes.has(ev.key)) {
          ripple(ev.key, color, 3);
        }
      });
    });
    scheduleFit(700);
  }

  const prepared = $('#agent-prepared');
  let asking = false;
  async function ask() {
    if (asking) return;
    const text = question.value.trim();
    clear(agentOut);
    if (text.length < 3 || text.length > 500) {
      agentOut.append(errorBox(new FormError('The question must be between 3 and 500 characters.')));
      return;
    }
    asking = true;
    askBtn.disabled = true;
    askBtn.classList.add('busy');
    // The answer must be visible without scrolling: fold the prepared questions away.
    if (prepared) prepared.open = false;
    const started = performance.now();
    const tick = () => { askBtn.textContent = 'Thinking · ' + ((performance.now() - started) / 1000).toFixed(1) + ' s'; };
    tick();
    const timer = setInterval(tick, 100);
    agentOut.append(h('div', { class: 'loading', text: 'Routing, planning, calling tools, checking every claim against the graph' }));
    try {
      // The key (when one is typed) goes with every explicit ask: required when AGENT_REQUIRE_API_KEY=true.
      const answer = await api('/api/agent/ask', { method: 'POST', body: { question: text }, auth: true, timeout: 150000 });
      renderAnswer(answer, (performance.now() - started) / 1000, text);
      revealResult();
      rippleEvidence(answer).catch(toastError);
    } catch (err) {
      clear(agentOut).append(h('p', { class: 'q-echo', text: 'Q: ' + text }), errorBox(err));
      revealResult();
    } finally {
      clearInterval(timer);
      asking = false;
      askBtn.disabled = false;
      askBtn.classList.remove('busy');
      askBtn.textContent = 'Ask the agent';
    }
  }
  /** Shown instead of auto-asking when the page is framed: the viewer confirms with one click. */
  function confirmDeepLinkAsk() {
    clear(agentOut).append(h('div', { class: 'msg info' },
      h('div', { class: 'msg-title', text: 'Question prefilled by a link' }),
      h('div', { text: 'Asking calls a paid LLM: confirm to send it.' }),
      h('div', { class: 'row link-ask' },
        h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Ask now', onclick: () => run(ask) }))));
  }
  $('#agent-form').addEventListener('submit', (event) => {
    event.preventDefault();
    run(ask);
  });

  /* ================================================================== */
  /* Self-test                                                           */
  /* ================================================================== */
  const isArr = (d, fn) => Array.isArray(d) && d.every(fn);
  // Mini-graph routes with known non-empty answers must not pass on [].
  const isFull = (d, fn) => isArr(d, fn) && d.length > 0;
  const isPlayer = (p) => p && Number.isInteger(p.player_id) && typeof p.name === 'string' && 'position' in p &&
    'sub_position' in p && 'date_of_birth' in p && 'market_value_eur' in p && 'club_id' in p && 'club_name' in p;
  const isRanked = (r) => r && Number.isInteger(r.club_id) && typeof r.name === 'string' && typeof r.score === 'number';
  // Mini-graph ids: Lucas Marchand (1), Diogo Santos (5), Gabriel Rocha (9), SC Alvorada (7).
  const P1 = 1;
  const P5 = 5;
  const P9 = 9;
  const C7 = 7;
  const SELFTEST = [
    ['GET', '/api/health', 200, (d) => d && typeof d.status === 'string' && typeof d.neo4j === 'boolean'],
    ['GET', '/api/players/search?limit=5', 200, (d) => d && isArr(d.items, isPlayer) && 'next_cursor' in d],
    ['GET', '/api/players/resolve?name=Gabriel', 200, (d) => isArr(d, (x) => x.kind === 'player' && Number.isInteger(x.id) && typeof x.name === 'string' && typeof x.score === 'number') && d.length > 0],
    ['GET', `/api/players/${P1}`, 200, (d) => isPlayer(d) && d.player_id === 1],
    ['GET', `/api/players/${P1}/valuations`, 200, (d) => isFull(d, (v) => typeof v.as_of === 'string' && typeof v.value_eur === 'number') && d.length === 3],
    ['GET', '/api/players/path?from=1&to=4&max_hops=6', 200, (d) => d && Array.isArray(d.players) && Array.isArray(d.matches_together) &&
      Number.isInteger(d.hops) && d.players.length === d.hops + 1 && d.matches_together.length === d.hops && d.hops === 4],
    ['GET', '/api/clubs/resolve?name=Alvorada', 200, (d) => isArr(d, (x) => x.kind === 'club' && Number.isInteger(x.id) && typeof x.name === 'string') && d.length > 0],
    ['GET', `/api/clubs/${C7}/pipeline`, 200, (d) => isFull(d, (r) => typeof r.competition_id === 'string' && Number.isInteger(r.transfers) && typeof r.total_fee_eur === 'number') && d.length === 3],
    ['GET', '/api/clubs/hubs?limit=10', 200, (d) => isFull(d, isRanked)],
    ['GET', '/api/clubs/gateways?limit=10', 200, (d) => isFull(d, isRanked)],
    ['GET', '/api/network/communities', 200, (d) => isFull(d, (c) => c.community_id !== undefined && Number.isInteger(c.size) && Array.isArray(c.clubs))],
    ['GET', `/api/players/${P1}/similar?k=5`, 200, (d) => isFull(d, (r) => Number.isInteger(r.player_id) && typeof r.name === 'string' && typeof r.similarity === 'number')],
    ['GET', `/api/players/${P9}/predicted-moves`, 200, (d) => isFull(d, (r) => r.nature === 'predicted' && typeof r.probability === 'number' && Number.isInteger(r.club_id)) && d[0].club_id === 5],
    ['GET', `/api/players/${P5}/rumours`, 200, (d) => isFull(d, (r) => r.nature === 'rumour' && typeof r.rumour_id === 'string' && typeof r.status === 'string')],
    // An empty body is rejected before the agent runs (never calls the LLM). With AGENT_REQUIRE_API_KEY=true and no key,
    // the key check answers 401 first: both are a correct refusal carrying a "detail".
    ['POST', '/api/agent/ask', [422, 401], (d) => d && (typeof d.detail === 'string' || Array.isArray(d.detail)), {}],
  ];

  async function runSelfTest() {
    const panel = $('#selftest-panel');
    const out = $('#selftest');
    panel.hidden = false;
    clear(out).append(loadingBox('Calling every route'));
    const rows = [];
    for (const check of SELFTEST) {
      const [method, path, expected, shape, body] = check;
      const result = await request(method, path, { body, timeout: 20000 });
      const accepted = Array.isArray(expected) ? expected : [expected];
      let pass = false;
      try { pass = accepted.indexOf(result.status) >= 0 && Boolean(shape(result.data)); } catch (err) { pass = false; }
      rows.push({ method, path, expected, status: result.status, ms: result.ms, pass });
    }
    const table = h('table', { class: 'st-table' },
      h('thead', null, h('tr', null, ['Method', 'Path', 'HTTP', 'Expected', 'ms', 'Result'].map((t) => h('th', { text: t })))),
      h('tbody', null, rows.map((r) => h('tr', { 'data-pass': r.pass ? 'true' : 'false' },
        h('td', { text: r.method }),
        h('td', { class: 'path', text: r.path }),
        h('td', { text: r.status === 0 ? 'network' : String(r.status) }),
        h('td', { text: Array.isArray(r.expected) ? r.expected.join(' or ') : String(r.expected) }),
        h('td', { text: r.ms.toFixed(0) }),
        h('td', { class: r.pass ? 'pass' : 'fail', text: r.pass ? 'PASS' : 'FAIL' })))));
    const passed = rows.filter((r) => r.pass).length;
    clear(out).append(table, h('div', { class: 'st-summary ' + (passed === rows.length ? 'pass' : 'fail'), text: passed + ' / ' + rows.length + ' PASS' }));
    document.body.dataset.selftest = 'done';
    document.body.dataset.selftestPassed = String(passed);
    document.body.dataset.selftestTotal = String(rows.length);
  }
  $('#selftest-close').addEventListener('click', () => { $('#selftest-panel').hidden = true; });

  /* ================================================================== */
  /* Boot                                                                */
  /* ================================================================== */
  async function initialLoad() {
    const [search, hubs] = await Promise.allSettled([
      api('/api/players/search?limit=40'),
      loadHubs(),
    ]);
    if (hubs.status === 'rejected' && !(hubs.reason instanceof ApiError && hubs.reason.status === 501)) {
      toastError(hubs.reason);
    }
    if (search.status === 'fulfilled') {
      (search.value.items || []).forEach((p) => addPlayerSummary(p));
    } else {
      // A persistent banner with a retry: a toast would vanish and leave a bare "Empty board".
      stageBanner('Initial load', search.reason, { label: 'Retry', fn: initialLoad });
    }
    render();
    scheduleFit(1400);
    loadGateways().catch(() => {});
  }

  async function boot() {
    updateCount();
    pollHealth();
    setInterval(pollHealth, 15000);
    const params = new URLSearchParams(window.location.search);
    let tab = params.get('tab');
    if (tab && TABS.indexOf(tab) < 0) {
      stageBanner('Deep link ?tab', new FormError('Unknown tab "' + tab.slice(0, 40) + '": use agent, players, clubs, network or admin. Showing Agent.'));
      tab = null;
    }
    if (!tab && params.get('club')) tab = 'clubs';
    setTab(tab || 'agent');
    if (params.get('q')) {
      question.value = params.get('q').slice(0, 500);
      updateCount();
    }
    if (params.get('selftest') === '1') {
      runSelfTest().catch((err) => {
        logJsError('selftest: ' + (err && err.message));
        document.body.dataset.selftest = 'done';
      });
    }
    await initialLoad();
    const player = params.get('player');
    if (player !== null && !/^\d+$/.test(player)) {
      stageBanner('Deep link ?player', new FormError('?player must be a numeric player id, e.g. ?player=1.'));
    } else if (player) {
      try {
        await openPlayerById(Number(player));
      } catch (err) {
        // Deep-link failure stays on screen (a toast would vanish during the demo).
        openDrawer('Player · #' + player, 'Player ' + player);
        drawerBody.append(errorBox(err));
      }
    }
    const club = params.get('club');
    if (club !== null && !/^\d+$/.test(club)) {
      stageBanner('Deep link ?club', new FormError('?club must be a numeric club id, e.g. ?club=7.'));
    } else if (club) {
      try {
        await selectClubInTab(Number(club), null, { rethrow: true });
      } catch (err) {
        stageBanner('Deep link ?club=' + club, err);
      }
    }
    const path = params.get('path');
    if (path) {
      const m = /^(\d+)-(\d+)$/.exec(path);
      if (m) {
        await sleep(dur(900));
        try {
          await playPathByIds(m[1], m[2]);
        } catch (err) {
          stageBanner('Teammate path ' + m[1] + ' → ' + m[2], err);
        }
      } else {
        stageBanner('Deep link ?path', new FormError('The path must look like ?path=1-4 (two player ids).'));
      }
    }
    if (params.get('q') && params.get('ask') === '1') {
      // ?ask=1 submits the prefilled question once on load (one paid call per page load at most).
      // Inside a frame it asks for a click instead: a framing site can never spend LLM credit on its own.
      let topLevel = false;
      try { topLevel = window.top === window.self; } catch (err) { topLevel = false; }
      if (topLevel) run(ask);
      else confirmDeepLinkAsk();
    }
  }

  run(boot);
}());
