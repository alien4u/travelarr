'use strict';

const $ = (s, el = document) => el.querySelector(s);
const view = $('#view');
const S = { state: null, tab: 'watchlist', libs: null, lib: {}, q: {}, sort: {}, rid: 0, timer: null, dirty: false, path: '',
  pick: null, locked: false };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const dev = () => (S.state && S.state.device) || 'NAS';

// Builds DOM nodes; every string goes in as text, never as HTML.
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'checked' || k === 'disabled' || k === 'selected' || k === 'value') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

async function api(path, opts = {}) {
  const o = { headers: { 'X-Requested-With': 'travelarr' }, ...opts };
  if (o.body && typeof o.body !== 'string') {
    o.body = JSON.stringify(o.body);
    o.headers['Content-Type'] = 'application/json';
  }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) {
    const e = new Error((data && data.error) || `HTTP ${r.status}`);
    e.code = data && data.code;
    e.retryAfter = Number(r.headers.get('Retry-After')) || 0;
    if (r.status === 401 && e.code === 'signin' && !S.locked) renderLogin();
    throw e;
  }
  return data;
}
const post = (p, body) => api(p, { method: 'POST', body: body || {} });

function fmtBytes(n) {
  if (n == null) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i >= 3 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
}
function fmtTime(s) {
  if (!s) return '';
  return new Date(s).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}
function fmtAdded(sec) {
  const d = new Date(sec * 1000);
  const year = d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric';
  return `Added ${d.toLocaleDateString([], { month: 'short', day: 'numeric', year })}`;
}
function fmtEta(sec) {
  if (!sec || !isFinite(sec)) return '';
  if (sec < 90) return `${Math.round(sec)} s left`;
  if (sec < 5400) return `${Math.round(sec / 60)} min left`;
  return `${(sec / 3600).toFixed(1)} h left`;
}
const fmtRate = n => `${fmtBytes(n || 0)}/s`;
const pad = n => String(n ?? 0).padStart(2, '0');
function jobTitle(j) {
  if (j.kind === 'episode') return `${j.show_title} S${pad(j.season)}E${pad(j.episode)} · ${j.title}`;
  return j.year ? `${j.title} (${j.year})` : j.title;
}

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}

function errorPanel(e, retry) {
  const needsSetup = e.code === 'not_ready' || e.code === 'auth';
  return h('div', { class: 'panel' },
    h('h2', {}, needsSetup ? 'Plex is not available' : 'Something went wrong'),
    h('p', { class: 'muted' }, e.message),
    h('div', { class: 'bar' },
      retry && h('button', { class: 'btn primary', onclick: retry }, 'Try again'),
      h('button', { class: 'btn', onclick: () => setTab('settings') }, 'Settings')));
}

function thumbUrl(t, w = 240, hh = 360) {
  if (!t) return null;
  if (t.startsWith('/library/')) return `/api/thumb?path=${encodeURIComponent(t)}&w=${w}&h=${hh}`;
  if (t.startsWith('https://')) return t;
  return null;
}
function poster(item, w, hh) {
  const p = h('div', { class: 'poster' }, h('div', { class: 'ph' }, item.title));
  const url = thumbUrl(item.thumb, w, hh);
  if (url) {
    const img = h('img', { loading: 'lazy', alt: '', src: url });
    img.onerror = () => img.remove();
    p.append(img);
  }
  return p;
}

function statusOf(st) {
  return ({ done: [`On ${dev()}`, ''], queued: ['Queued', 'busy'], downloading: ['Copying', 'busy'],
    failed: ['Failed', 'bad'] })[st] || [st, 'dim'];
}
function badgeFor(item) {
  const n = item.copy;
  if (!n) return null;
  if (typeof n === 'string') {
    const [txt, cls] = statusOf(n);
    return h('span', { class: `badge ${cls}` }, txt);
  }
  if (n.active) return h('span', { class: 'badge busy' }, `${n.done} + ${n.active} copying`);
  if (n.done) return h('span', { class: 'badge' }, `${n.done} on ${dev()}`);
  if (n.failed) return h('span', { class: 'badge bad' }, 'Failed');
  return null;
}

function card(item, recent) {
  const p = poster(item);
  const b = badgeFor(item);
  if (b) p.append(b);
  let sub = item.year || '';
  if (item.type === 'show' && item.episodes != null) sub = `${item.unwatched} unwatched of ${item.episodes}`;
  if (item.type === 'movie' && (item.size || recent)) {
    sub = [recent ? item.addedAt && fmtAdded(item.addedAt) : item.year, item.size && fmtBytes(item.size)]
      .filter(Boolean).join(' · ');
  }
  if (item.inLibrary === false) sub = 'Not in your library';
  p.append(h('span', { class: 'tick' }));
  const el = h('button', { class: 'card', type: 'button', onclick: e => cardClick(item, el, e) },
    p, h('div', { class: 't' }, item.title), h('div', { class: 's' }, sub));
  el.dataset.rk = item.ratingKey || '';
  el.item = item;
  return el;
}

// One selection at a time, for the tab it was started on. S.pick.busy locks it
// while its copy request runs.
const MAX_PICK = 100;  // the server takes at most 200 titles per request
const pickable = i => !!i.ratingKey && (i.type === 'show' || !i.copy || i.copy === 'failed');
const picking = () => S.pick && S.pick.tab === S.tab ? S.pick : null;

function whyNot(i) {
  if (!i.ratingKey) return 'Not in your Plex library';
  return ({ done: `Already on ${dev()}`, queued: 'Already queued', downloading: 'Copying now' })[i.copy] || statusOf(i.copy)[0];
}

function cardClick(item, el, e) {
  const p = picking();
  if (!p) return item.ratingKey ? openItem(item.ratingKey) : toast('Not in your Plex library, nothing to copy');
  if (p.busy) return;
  if (!pickable(item)) return toast(whyNot(item));
  const cards = [...view.querySelectorAll('.card')];
  const from = e.shiftKey && p.last ? cards.findIndex(c => c.dataset.rk === p.last) : -1;
  if (from >= 0) {
    const to = cards.indexOf(el);
    const range = cards.slice(Math.min(from, to), Math.max(from, to) + 1).map(c => c.item).filter(pickable);
    if (!addPicks(range)) toast(`Select at most ${MAX_PICK} titles at a time`);
  } else if (p.items.has(item.ratingKey)) {
    p.items.delete(item.ratingKey);
  } else if (!addPicks([item])) {
    toast(`Select at most ${MAX_PICK} titles at a time`);
  }
  p.last = item.ratingKey;
  paintPick();
}

// Adds what fits under the cap; false when something did not fit.
function addPicks(items) {
  const p = S.pick;
  for (const i of items) {
    if (p.items.has(i.ratingKey)) continue;
    if (p.items.size >= MAX_PICK) return false;
    p.items.set(i.ratingKey, i);
  }
  return true;
}

// After a load: refresh the selected titles that came back and drop those that
// can no longer be copied. A complete list (the Watchlist) also drops the ones
// that are not in it any more.
function reconcile(items, complete) {
  const p = picking();
  if (!p || p.busy) return;
  const seen = new Map(items.filter(i => i.ratingKey).map(i => [i.ratingKey, i]));
  let dropped = 0;
  for (const rk of [...p.items.keys()]) {
    const now = seen.get(rk);
    if (now && pickable(now)) p.items.set(rk, now);
    else if (now || complete) { p.items.delete(rk); dropped++; }
  }
  if (dropped) toast(dropped === 1 ? '1 selected title is no longer available and was unselected'
    : `${dropped} selected titles are no longer available and were unselected`);
}

function pickToggle() {
  return h('button', {
    class: 'btn small pick-toggle', type: 'button',
    onclick: () => {
      const p = picking();
      if (p && p.busy) return;
      S.pick = p ? null : { tab: S.tab, items: new Map(), last: null, mode: 'unwatched', busy: false };
      paintPick();
    },
  }, 'Select');
}

function pickSummary(p) {
  const all = [...p.items.values()];
  const movies = all.filter(i => i.type !== 'show');
  const shows = all.length - movies.length;
  if (!all.length) return 'No titles selected';
  const parts = [];
  if (movies.length) {
    const bytes = movies.reduce((a, i) => a + (i.size || 0), 0);
    const some = movies.some(i => i.size == null) ? 'at least ' : '';
    parts.push(`${movies.length} movie${movies.length === 1 ? '' : 's'} · ${some}${fmtBytes(bytes)}`);
  }
  if (shows) parts.push(`${shows} show${shows === 1 ? '' : 's'}`);
  return parts.join(' + ');
}

// Repaints the Select button, the cards and the action bar from S.pick.
function paintPick() {
  const p = picking();
  const busy = !!(p && p.busy);
  for (const t of view.querySelectorAll('.pick-toggle')) {
    t.classList.toggle('on', !!p);
    t.setAttribute('aria-pressed', String(!!p));
    t.disabled = busy;
  }
  for (const s of view.querySelectorAll('.lib-select')) s.disabled = busy;
  for (const c of view.querySelectorAll('.card')) {
    const on = !!p && p.items.has(c.dataset.rk);
    c.classList.toggle('picking', !!p);
    c.classList.toggle('picked', on);
    c.classList.toggle('off', !!p && !pickable(c.item));
    if (p) c.setAttribute('aria-pressed', String(on));
    else c.removeAttribute('aria-pressed');
  }
  const slot = $('.pick-slot', view);
  if (!slot) return;
  if (!p) return slot.replaceChildren();
  const shows = [...p.items.values()].some(i => i.type === 'show');
  const mode = shows ? h('div', { class: 'seg', role: 'group', 'aria-label': 'Episodes of selected shows' },
    [['unwatched', 'Unwatched episodes'], ['all', 'All episodes']].map(([v, label]) => h('button', {
      type: 'button', class: v === p.mode ? 'on' : null, 'aria-pressed': String(v === p.mode), disabled: busy,
      onclick: () => { p.mode = v; paintPick(); },
    }, label))) : null;
  const n = p.items.size;
  slot.replaceChildren(h('div', { class: 'files-actions' },
    h('div', { class: 'grow small' }, pickSummary(p)), mode,
    h('button', { class: 'btn small', disabled: busy, onclick: selectShown }, 'Select all shown'),
    h('button', { class: 'btn small', disabled: busy || !n, onclick: () => { p.items.clear(); p.last = null; paintPick(); } }, 'Clear'),
    h('button', { class: 'btn small', disabled: busy, onclick: () => { S.pick = null; paintPick(); } }, 'Cancel'),
    h('button', { class: 'btn primary', disabled: busy || !n, onclick: pickCopy },
      busy ? 'Queuing...' : n ? `Copy ${n} to ${dev()}` : 'Select titles to copy')));
}

function selectShown() {
  const items = [...view.querySelectorAll('.card')].map(c => c.item).filter(pickable);
  if (!addPicks(items)) toast(`Selected the first ${MAX_PICK}; copy them, then select the rest`);
  paintPick();
}

async function pickCopy() {
  const p = picking();
  if (!p || p.busy || !p.items.size) return;
  p.busy = true;
  paintPick();
  const r = await queue([...p.items.keys()], p.mode, p.tab === 'watchlist' ? 'watchlist' : 'browse');
  p.busy = false;
  if (S.pick !== p) return;  // tab switched or a new selection started: only the toast applies
  if (r) { S.pick = null; render(); }
  else paintPick();
}

async function refreshState() {
  if (S.locked) return;
  try {
    const prev = S.state;
    S.state = await api('/api/state');
    paintPill();
    if (prev && (prev.signedIn !== S.state.signedIn || prev.serverId !== S.state.serverId)) render();
  } catch (e) {
    if (e.code !== 'signin') $('#pill').replaceChildren(h('span', { class: 'dot bad' }), 'travelarr unreachable');
  }
}

function paintPill() {
  const st = S.state;
  let dot = 'bad', text = 'Not signed in';
  if (st.signedIn) {
    if (st.conn) { dot = st.conn.kind === 'relay' ? 'bad' : 'ok'; text = `${st.conn.name} · ${st.conn.kind}`; }
    else if (st.connError) { text = 'Plex unreachable'; }
    else { dot = ''; text = st.serverName || 'Plex'; }
  }
  if (st.current) dot = 'busy';
  if (st.disk) text += ` · ${fmtBytes(st.disk.free)} free`;
  else text += ' · media folder missing';
  $('#pill').replaceChildren(h('span', { class: `dot ${dot}` }), text);
  const t = st.totals || {};
  const q = ((t.queued || {}).count || 0) + ((t.downloading || {}).count || 0);
  $('#qcount').textContent = q ? String(q) : '';
}

function setTab(tab) {
  if (tab !== S.tab) S.pick = null;
  S.tab = tab;
  if (location.hash !== '#' + tab) history.replaceState(null, '', '#' + tab);
  render();
}

function render() {
  S.rid++;
  clearInterval(S.timer);
  if (S.locked) return;
  for (const b of document.querySelectorAll('#tabs button')) b.classList.toggle('on', b.dataset.tab === S.tab);
  const st = S.state;
  $('#tabs').hidden = !(st && st.signedIn && st.serverId);
  if (!st) return;
  if (!st.signedIn) return renderSignIn();
  if (!st.serverId) return renderServers();
  ({ watchlist: renderWatchlist, movies: () => renderLibrary('movie'), tv: () => renderLibrary('show'),
     queue: renderQueue, history: renderHistory, files: renderFiles, settings: renderSettings }[S.tab] || renderWatchlist)();
}

$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-tab]');
  if (b) setTab(b.dataset.tab);
});
$('#pill').addEventListener('click', () => setTab('settings'));

// A full-page overlay, so a tab still loading underneath cannot paint over it.
function renderLogin() {
  S.locked = true;
  S.rid++;
  S.dirty = false;
  clearInterval(S.timer);
  closeSheet();
  view.replaceChildren();
  $('#tabs').hidden = true;
  const old = $('#login');
  if (old) old.remove();
  const user = h('input', { type: 'text', name: 'username', autocomplete: 'username', autocapitalize: 'none',
    spellcheck: 'false', required: true });
  const pw = h('input', { type: 'password', name: 'password', autocomplete: 'current-password', required: true });
  const msg = h('p', { class: 'small warn', role: 'alert' });
  const btn = h('button', { class: 'btn primary', type: 'submit' }, 'Sign in');
  const form = h('form', {
    class: 'login panel', onsubmit: async e => {
      e.preventDefault();
      btn.disabled = true;
      msg.textContent = '';
      try {
        await post('/api/login', { user: user.value, password: pw.value });
        S.locked = false;
        wrap.remove();
        await refreshState();
        render();
        return;
      } catch (err) {
        msg.textContent = err.code === 'throttled'
          ? `Too many wrong passwords. Try again in ${Math.max(1, Math.ceil(err.retryAfter / 60))} min.`
          : err.code === 'wrong' ? 'Wrong user name or password.' : err.message;
        pw.value = '';
        pw.focus();
      }
      btn.disabled = false;
    },
  },
    h('div', { class: 'login-brand' }, h('span', { class: 'logo' }), 'travelarr'),
    h('div', { class: 'field' }, h('label', {}, 'User name'), user),
    h('div', { class: 'field' }, h('label', {}, 'Password'), pw),
    btn, msg);
  const wrap = h('div', { id: 'login', class: 'login-wrap' }, form);
  document.body.append(wrap);
  user.focus();
}

function renderSignIn() {
  const msg = h('p', { class: 'small' });
  const btn = h('button', { class: 'btn primary', onclick: () => signIn(btn, msg) }, 'Sign in with Plex');
  view.replaceChildren(h('div', { class: 'setup panel' },
    h('h2', {}, 'Connect travelarr to your Plex'),
    h('p', {}, 'Sign in with the Plex account that owns your home server. travelarr reads your libraries and watchlist, and downloads only the files you choose.'),
    btn, msg));
}

async function signIn(btn, msg) {
  const win = window.open('about:blank', '_blank');
  btn.disabled = true;
  try {
    const { url } = await post('/api/auth/start');
    if (win) { win.location = url; msg.textContent = 'Approve travelarr in the Plex tab, then come back here.'; }
    else msg.replaceChildren('Pop-ups are blocked. ', h('a', { href: url, target: '_blank', rel: 'noopener' }, 'Open the Plex sign-in page'));
    const t0 = Date.now();
    while (Date.now() - t0 < 10 * 60 * 1000) {
      await sleep(2000);
      const r = await post('/api/auth/check');
      if (r.signedIn) {
        try { if (win) win.close(); } catch { /* ignore */ }
        await refreshState();
        return render();
      }
    }
    msg.textContent = 'Timed out waiting for Plex. Try again.';
  } catch (e) {
    try { if (win) win.close(); } catch { /* ignore */ }
    msg.textContent = e.message;
  }
  btn.disabled = false;
}

async function renderServers() {
  const rid = S.rid;
  view.replaceChildren(h('div', { class: 'spinner' }));
  let servers;
  try { ({ servers } = await api('/api/servers')); }
  catch (e) { return view.replaceChildren(errorPanel(e, renderServers)); }
  if (rid !== S.rid) return;
  const st = S.state;
  let chosen = (servers.find(s => s.id === st.serverId) || servers.find(s => s.owned) || servers[0] || {}).id;
  const url = h('input', { type: 'url', placeholder: 'https://192-168-1-10.<id>.plex.direct:32400', value: st.preferredUrl || '' });
  const relay = h('input', { type: 'checkbox', checked: !!st.allowRelay });
  const msg = h('p', { class: 'small' });
  const save = h('button', { class: 'btn primary' }, 'Save and connect');
  save.onclick = async () => {
    const s = servers.find(x => x.id === chosen);
    if (!s) return;
    save.disabled = true;
    msg.textContent = 'Connecting…';
    try {
      const r = await post('/api/server', { id: s.id, name: s.name, preferredUrl: url.value, allowRelay: relay.checked });
      toast(`Connected to ${r.conn.name} (${r.conn.kind})`);
      await refreshState();
      S.libs = null;
      setTab('watchlist');
    } catch (e) {
      msg.textContent = e.message;
      await refreshState();
    }
    save.disabled = false;
  };
  view.replaceChildren(h('div', { class: 'setup panel' },
    h('h2', {}, 'Choose your Plex server'),
    servers.length ? null : h('p', {}, 'No Plex servers found on this account.'),
    servers.map(s => {
      const r = h('input', { type: 'radio', name: 'srv', checked: s.id === chosen });
      r.onchange = () => { chosen = s.id; };
      return h('label', { class: 'radio' }, r, h('div', {},
        h('div', { style: 'font-weight:600' }, s.name),
        h('div', { class: 'small muted' }, [s.owned ? 'Your server' : 'Shared with you', s.online ? 'online' : 'offline'].join(' · '))));
    }),
    h('div', { class: 'field' }, h('label', {}, 'Fixed server address (optional)'), url,
      h('div', { class: 'small muted' }, 'Leave empty and travelarr uses the LAN address at home and Plex Remote Access when away. Set it to use a VPN address instead.')),
    h('label', { class: 'check small' }, relay, 'Allow Plex Relay as a last resort (very slow)'),
    h('div', { class: 'bar', style: 'justify-content:center;margin-top:14px' }, save), msg));
}

async function renderWatchlist() {
  const rid = S.rid;
  view.replaceChildren(h('div', { class: 'spinner' }));
  let items;
  try { ({ items } = await api('/api/watchlist')); }
  catch (e) { if (rid === S.rid) view.replaceChildren(errorPanel(e, render)); return; }
  if (rid !== S.rid) return;
  reconcile(items, true);
  const copyable = items.filter(i => i.ratingKey && !(typeof i.copy === 'string' && i.copy !== 'failed'));
  const copyAll = h('button', { class: 'btn small primary', disabled: !copyable.length }, 'Copy all (unwatched)');
  copyAll.onclick = async () => {
    if (!confirm(`Copy ${copyable.length} watchlist title(s)? Shows copy their unwatched episodes.`)) return;
    await queue(copyable.map(i => i.ratingKey), 'unwatched', 'watchlist');
    render();
  };
  view.replaceChildren(
    h('div', { class: 'bar' },
      h('div', { class: 'grow muted small' }, `${items.length} on your Plex watchlist. Add titles from any Plex app, then refresh.`),
      h('button', { class: 'btn small', onclick: render }, 'Refresh'), items.length ? pickToggle() : null, copyAll),
    items.length ? h('div', { class: 'grid' }, items.map(i => card(i)))
      : h('div', { class: 'empty' }, 'Your watchlist is empty.'),
    h('div', { class: 'pick-slot' }));
  paintPick();
}

async function renderLibrary(type) {
  const rid = S.rid;
  view.replaceChildren(h('div', { class: 'spinner' }));
  try { if (!S.libs) S.libs = (await api('/api/libraries')).libraries; }
  catch (e) { if (rid === S.rid) view.replaceChildren(errorPanel(e, render)); return; }
  if (rid !== S.rid) return;
  const libs = S.libs.filter(l => l.type === type);
  if (!libs.length) return view.replaceChildren(h('div', { class: 'empty' }, `No ${type === 'movie' ? 'movie' : 'TV'} libraries on this server.`));
  const key = libs.some(l => l.key === S.lib[type]) ? S.lib[type] : libs[0].key;
  S.lib[type] = key;
  const sel = libs.length > 1 ? h('select', {
    class: 'lib-select',
    onchange: e => {
      S.lib[type] = e.target.value;
      const p = picking();
      if (p) { p.items.clear(); p.last = null; }  // never keep titles from a library that is not on screen
      render();
    },
  }, libs.map(l => h('option', { value: l.key, selected: l.key === key }, l.title))) : null;
  const search = h('input', { type: 'search', placeholder: `Search ${libs.find(l => l.key === key).title}`, value: S.q[type] || '' });
  const sort = S.sort[type] || 'title';
  const order = h('div', { class: 'seg', role: 'group', 'aria-label': 'Order' },
    [['title', 'A-Z'], ['recent', 'Recently added']].map(([v, label]) => h('button', {
      type: 'button', class: v === sort ? 'on' : null, 'aria-pressed': String(v === sort),
      onclick: () => { if (v !== sort) { S.sort[type] = v; render(); } },
    }, label)));
  const grid = h('div', { class: 'grid' });
  const more = h('div', { class: 'more' });
  let start = 0, debounce;
  const size = 60;
  // Everything below belongs to this render (rid): a later render, such as a
  // new order, makes its timers and responses stale.
  async function load(reset) {
    if (rid !== S.rid) return;
    if (reset) { start = 0; grid.replaceChildren(); }
    more.replaceChildren(h('div', { class: 'spinner' }));
    const q = S.q[type] || '';
    let d;
    try { d = await api(`/api/library/${key}?start=${start}&size=${size}&sort=${sort}${q ? `&q=${encodeURIComponent(q)}` : ''}`); }
    catch (e) { if (rid === S.rid) more.replaceChildren(errorPanel(e, () => load(reset))); return; }
    if (rid !== S.rid || (S.q[type] || '') !== q) return;
    reconcile(d.items, false);
    d.items.forEach(i => grid.append(card(i, sort === 'recent')));
    paintPick();
    start += d.items.length;
    more.replaceChildren();
    const hasMore = d.total == null ? d.items.length === size : start < d.total;
    if (hasMore) more.append(h('button', { class: 'btn', onclick: () => load(false) }, 'Load more'));
    if (!grid.children.length) more.append(h('div', { class: 'empty' }, q ? 'No matches.' : 'This library is empty.'));
  }
  search.oninput = () => {
    S.q[type] = search.value.trim();  // kept at once, so switching the order keeps what was typed
    clearTimeout(debounce);
    debounce = setTimeout(() => load(true), 350);
  };
  view.replaceChildren(h('div', { class: 'bar' }, sel, order, pickToggle(), h('div', { class: 'grow' }, search)),
    grid, more, h('div', { class: 'pick-slot' }));
  paintPick();
  load(true);
}

async function queue(keys, mode, source, only, mediaVersion) {
  try {
    const r = await post('/api/queue', {
      keys, mode, source: source || 'browse', only, mediaVersion, serverId: S.state && S.state.serverId,
    });
    const parts = [];
    if (r.added) parts.push(`Queued ${r.added} file(s), ${fmtBytes(r.bytes)}`);
    if (r.skipped) parts.push(`${r.skipped} already on ${dev()} or queued`);
    if (!r.added && !r.skipped) parts.push('Nothing to copy (all watched?)');
    toast(parts.join(' · '));
    if (r.warning) setTimeout(() => toast(r.warning), 3600);
    S.dirty = true;
    refreshState();
    return r;
  } catch (e) { toast(e.message); return null; }
}

function openSheet(...nodes) {
  $('#sheet-body').replaceChildren(...nodes.flat(Infinity).filter(n => n != null && n !== false));
  $('#sheet').hidden = false;
  document.body.style.overflow = 'hidden';
}
function closeSheet() {
  $('#sheet').hidden = true;
  document.body.style.overflow = '';
  if (S.dirty && ['watchlist', 'movies', 'tv'].includes(S.tab)) { S.dirty = false; render(); }
}
$('#sheet').addEventListener('click', e => { if (e.target.closest('[data-close]')) closeSheet(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#sheet').hidden) closeSheet(); });

function removedToast(results) {
  if (results.busy) return toast(`${results.busy === 1 ? 'It is' : `${results.busy} files are`} being finished (a copy or a re-sync); try again in a moment`);
  if (results.deferred) return toast(`Removed from ${dev()}. Some old files could not be deleted yet; they will be retried`);
  const busy = results.cancelling || 0;
  if (!busy) return toast(`Removed from ${dev()}`);
  const n = (results.removed || 0) + (results.cancelled || 0);
  toast(`Removed ${n} file(s). ${busy === 1 ? '1 copying file was' : `${busy} copying files were`} asked to stop; `
    + `if ${busy === 1 ? 'it' : 'they'} had already finished, ${busy === 1 ? 'it is' : 'they are'} kept, remove again.`);
}

// version: the version choice for this title page and the copy it starts (the setting when unset)
async function openItem(rk, version) {
  openSheet(h('div', { class: 'spinner' }));
  let d;
  try { d = await api(`/api/item/${rk}${version ? `?mediaVersion=${encodeURIComponent(version)}` : ''}`); }
  catch (e) { return openSheet(errorPanel(e, () => openItem(rk, version))); }
  d.mediaVersion = version || S.state.mediaVersion;
  const meta = [d.year, d.size ? fmtBytes(d.size) : null,
    d.type === 'show' ? `${d.episodes} episodes, ${d.unwatched} unwatched` : null].filter(Boolean).join(' · ');
  const head = h('div', { class: 'detail-head' }, poster(d, 220, 330),
    h('div', {}, h('h1', {}, d.title), h('div', { class: 'muted small' }, meta),
      h('p', { class: 'summary' }, d.summary || '')));
  const pick = h('select', { onchange: () => openItem(rk, pick.value) },
    ...MEDIA_VERSIONS.map(([v, text]) => h('option', { value: v, selected: v === d.mediaVersion }, text)));
  const versions = h('label', { class: 'small muted' }, 'Versions to copy ', pick);
  openSheet(head, versions, d.type === 'show' ? showBody(d) : videoBody(d), historyBlock(d.history));
}

function videoBody(d) {
  const st = d.copy;
  const actions = h('div', { class: 'sticky-actions' });
  const active = (d.history || []).filter(j => j.status === 'queued' || j.status === 'downloading');
  if (!st || st === 'failed') {
    actions.append(h('button', {
      class: 'btn primary', onclick: async () => { await queue([d.ratingKey], 'all', 'browse', undefined, d.mediaVersion); openItem(d.ratingKey, d.mediaVersion); },
    }, `Copy to ${dev()} · ${fmtBytes(d.size)}`));
  } else {
    actions.append(h('span', { class: `chip ${st}` }, statusOf(st)[0]));
    if (active.length) actions.append(h('button', {
      class: 'btn small', onclick: async () => { for (const j of active) await post(`/api/jobs/${j.id}/cancel`); S.dirty = true; openItem(d.ratingKey); },
    }, 'Cancel'));
  }
  const resync = resyncButton(d);  // null until the title is on the device; append(null) would show "null"
  if (resync) actions.append(resync);
  if (st === 'done') actions.append(h('button', {
    class: 'btn small danger', onclick: async () => {
      if (!confirm(`Delete "${d.title}" from ${dev()}?`)) return;
      const r = await post('/api/remove', { ratingKeys: [d.ratingKey], serverId: S.state.serverId });
      S.dirty = true; removedToast(r.results); openItem(d.ratingKey);
    },
  }, `Remove from ${dev()}`));
  return h('div', {},
    (d.parts || []).map(p => h('div', { class: 'mono muted' }, `${dev()}: ${p.dest}`)),
    actions);
}

function showBody(d) {
  const eligible = e => !e.copy || e.copy === 'failed';
  const eps = d.seasons.flatMap(s => s.episodes);
  const sel = new Set(eps.filter(e => eligible(e) && !e.watched).map(e => e.ratingKey));
  const boxes = new Map();
  const copyBtn = h('button', { class: 'btn primary' });
  const refresh = () => {
    let bytes = 0;
    for (const e of eps) if (sel.has(e.ratingKey)) bytes += e.size || 0;
    for (const [rk, cb] of boxes) cb.checked = sel.has(rk);
    copyBtn.textContent = sel.size ? `Copy ${sel.size} episode(s) · ${fmtBytes(bytes)}` : 'Select episodes';
    copyBtn.disabled = !sel.size;
  };
  copyBtn.onclick = async () => {
    copyBtn.disabled = true;
    await queue([d.ratingKey], 'all', 'browse', [...sel], d.mediaVersion);
    openItem(d.ratingKey, d.mediaVersion);
  };
  const pick = fn => () => { sel.clear(); eps.filter(e => eligible(e) && fn(e)).forEach(e => sel.add(e.ratingKey)); refresh(); };
  const onDevice = eps.filter(e => e.copy === 'done').length;
  const busy = eps.filter(e => e.copy === 'queued' || e.copy === 'downloading').length;

  const seasons = d.seasons.map(s => {
    const sEl = s.episodes.filter(eligible);
    const scb = h('input', { type: 'checkbox', disabled: !sEl.length });
    scb.onchange = () => { sEl.forEach(e => scb.checked ? sel.add(e.ratingKey) : sel.delete(e.ratingKey)); refresh(); };
    const done = s.episodes.filter(e => e.copy === 'done').length;
    const sTitle = s.title || `Season ${s.index}`;
    return h('div', { class: 'season' },
      h('label', { class: 'season-h' }, scb, sTitle,
        h('span', { class: 'muted small grow' }, `${s.episodes.length} ep${done ? ` · ${done} on ${dev()}` : ''}`)),
      s.episodes.map(e => {
        const cb = h('input', { type: 'checkbox', disabled: !eligible(e) });
        cb.onchange = () => { cb.checked ? sel.add(e.ratingKey) : sel.delete(e.ratingKey); refresh(); };
        boxes.set(e.ratingKey, cb);
        return h('label', { class: `ep${e.watched ? ' watched' : ''}` }, cb,
          h('span', { class: 'n' }, `E${pad(e.index)}`),
          h('span', { class: 'et' }, e.title),
          e.copy ? h('span', { class: `chip ${e.copy}` }, statusOf(e.copy)[0])
            : e.watched ? h('span', { class: 'chip' }, 'Watched') : null,
          h('span', { class: 'sz' }, fmtBytes(e.size)));
      }));
  });

  const removeBtn = onDevice || busy ? h('button', {
    class: 'btn small danger', onclick: async () => {
      if (!confirm(`Delete all ${onDevice} episode(s) of "${d.title}" from ${dev()}${busy ? ' and cancel the queued ones' : ''}?`)) return;
      const r = await post('/api/remove', { showKeys: [d.ratingKey], serverId: S.state.serverId });
      S.dirty = true; removedToast(r.results); openItem(d.ratingKey);
    },
  }, `Remove show from ${dev()}`) : null;

  refresh();
  return h('div', {},
    h('div', { class: 'bar' },
      h('span', { class: 'muted small grow' }, [onDevice && `${onDevice} on ${dev()}`, busy && `${busy} queued`].filter(Boolean).join(' · ') || `Nothing on ${dev()} yet`),
      h('button', { class: 'btn small', onclick: pick(e => !e.watched) }, 'Unwatched'),
      h('button', { class: 'btn small', onclick: pick(() => true) }, 'All'),
      h('button', { class: 'btn small', onclick: pick(() => false) }, 'None')),
    seasons,
    h('div', { class: 'sticky-actions' }, copyBtn, resyncButton(d), removeBtn));
}

/* the title's folder on the NAS, re-synced like that folder in Local Files */
function resyncButton(d) {
  return d.nasFolder ? h('button', {
    class: 'btn small', onclick: () => openResync(d.nasFolder, () => openItem(d.ratingKey)),
  }, 'Re-sync from Plex') : null;
}

const RESYNC_ACT = ['upgraded', 'changed', 'absent'];

function resyncLine(i) {
  if (i.video) {
    const name = i.kind === 'upgraded' ? ` (${i.video.to.file})` : '';
    return `new version: ${fmtBytes(i.video.from.size)} -> ${fmtBytes(i.video.to.size)}${name}`;
  }
  return (i.reason || i.kind).replace('the NAS', dev()) + (i.watched ? ' · watched' : '');
}

async function openResync(path, back = closeSheet) {
  const head = h('div', { class: 'detail-head' }, h('div', {},
    h('h1', {}, `Re-sync ${path ? path.split('/').pop() : `everything on ${dev()}`}`),
    h('div', { class: 'muted small' }, `Compares the videos on ${dev()} with Plex.`)));
  const backBtn = h('button', { class: 'btn small', onclick: () => back() }, 'Back');
  openSheet(head, h('div', { class: 'spinner' }), h('p', { class: 'muted small' }, 'Checking...'));
  let d;
  try { d = await post('/api/resync/check', { path }); }
  catch (e) { return openSheet(head, errorPanel(e, () => openResync(path, back)), backBtn); }
  if (!d.checked && !d.items.length) {
    return openSheet(head, h('div', { class: 'empty' }, 'No videos from Plex in this folder.'), backBtn);
  }
  const act = d.items.filter(i => RESYNC_ACT.includes(i.kind));
  const rest = d.items.filter(i => !RESYNC_ACT.includes(i.kind));
  const sel = new Set(act.filter(i => !(i.kind === 'absent' && i.watched)).map(i => i.ratingKey));
  const btn = h('button', { class: 'btn primary' });
  const subs = h('input', { type: 'checkbox', checked: !act.length && !!d.unchanged });
  const paint = () => {
    const bytes = act.filter(i => sel.has(i.ratingKey)).reduce((a, i) => a + (i.videoBytes || 0), 0);
    btn.textContent = `Re-sync${bytes ? ` · ${fmtBytes(bytes)} of video` : ''}`;
    btn.disabled = !sel.size && !subs.checked;
  };
  subs.onchange = paint;
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = 'Queuing...';
    try {
      const r = await post('/api/resync/apply', { path, serverId: d.serverId, ratingKeys: [...sel], subtitles: subs.checked });
      const parts = [r.upgrades && `${r.upgrades} new version(s)`, r.redownloads && `${r.redownloads} to copy again`,
        r.copies && `${r.copies} to copy`, r.refreshes && `subtitles of ${r.refreshes}`].filter(Boolean);
      toast(parts.length ? `Queued ${parts.join(', ')}${r.videoBytes ? ` · ${fmtBytes(r.videoBytes)}` : ''}`
        : 'Nothing to sync any more');
      if (r.warning) setTimeout(() => toast(r.warning), 3600);
      S.dirty = true;
      refreshState();
      back();
    } catch (e) { toast(e.message); paint(); }
  };
  const rows = act.map(i => {
    const cb = h('input', { type: 'checkbox', checked: sel.has(i.ratingKey) });
    cb.onchange = () => { cb.checked ? sel.add(i.ratingKey) : sel.delete(i.ratingKey); paint(); };
    return h('label', { class: 'ep rs' }, cb, h('span', { class: 'et' }, h('b', {}, i.label), ` · ${resyncLine(i)}`));
  });
  const others = rest.map(i => h('div', { class: 'ep rs muted' }, h('span', { class: 'n' }),
    h('span', { class: 'et' }, h('b', {}, i.label), ` · ${resyncLine(i)}`)));
  paint();
  openSheet(head,
    h('p', { class: 'muted small' }, [`${d.checked} checked`, act.length && `${act.length} video(s) to update`,
      d.unchanged && `${d.unchanged} up to date`,
      d.unverified && `${d.unverified} matched by name and size only (Plex did not send their bytes)`,
      rest.length && `${rest.length} not synced`].filter(Boolean).join(' · ')),
    act.length ? rows : h('div', { class: 'empty' }, rest.length ? 'No video needs an update.' : 'Every video is up to date.'),
    d.unchanged ? h('label', { class: 'ep rs' }, subs,
      h('span', { class: 'et' }, `Fetch subtitles again for the ${d.unchanged} up-to-date video(s)`)) : null,
    others.length ? [h('h3', {}, 'Not synced'), others] : null,
    h('div', { class: 'sticky-actions' }, btn, backBtn));
}

function historyBlock(jobs) {
  if (!jobs || !jobs.length) return null;
  return h('div', { class: 'panel', style: 'margin-top:18px' }, h('h2', {}, 'History'),
    h('div', { class: 'list' }, jobs.slice().reverse().slice(0, 15).map(j => h('div', { class: 'small' },
      h('span', { class: `chip ${j.status}` }, j.status), ' ', jobTitle(j), ' ',
      h('span', { class: 'muted' }, fmtTime(j.removed_at || j.finished_at || j.queued_at))))));
}

function renderQueue() {
  const rid = S.rid;
  const box = h('div', {}, h('div', { class: 'spinner' }));
  view.replaceChildren(box);
  const tick = async () => {
    if (rid !== S.rid) return clearInterval(S.timer);
    let d;
    try { d = await api('/api/jobs?view=active'); } catch (e) { box.replaceChildren(errorPanel(e, render)); return; }
    if (rid !== S.rid) return;
    paintQueue(box, d);
  };
  tick();
  S.timer = setInterval(tick, 1500);
}

function paintQueue(box, d) {
  const cur = d.current;
  const queued = d.jobs.filter(j => j.status === 'queued');
  const failed = d.jobs.filter(j => j.status === 'failed');
  const left = queued.reduce((a, j) => a + (j.size - j.bytes_done), 0) + (cur ? cur.size - cur.bytes : 0);
  const nodes = [h('div', { class: 'bar' },
    h('div', { class: 'grow muted small' }, (queued.length || cur)
      ? [`${queued.length + (cur ? 1 : 0)} file(s) to go`, fmtBytes(left), cur ? fmtRate(cur.speed) : null]
        .filter(Boolean).join(' · ') : 'Nothing queued.'),
    h('button', {
      class: 'btn small', onclick: async () => { await post('/api/settings', { paused: !d.paused }); refreshState(); },
    }, d.paused ? 'Resume' : 'Pause'))];
  if (d.paused) nodes.push(h('div', { class: 'panel warn' }, 'Downloads are paused.'));
  if (d.workerError) nodes.push(h('div', { class: 'panel' }, h('b', {}, 'Waiting: '), d.workerError));
  if (cur) {
    const pct = cur.size ? Math.min(100, (100 * cur.bytes) / cur.size) : 0;
    const done = `${fmtBytes(cur.bytes)} of ${fmtBytes(cur.size)}`;
    const line = {
      connecting: ['Connecting', fmtRate(cur.speed)],
      retrying: [`Connection lost, retrying in ${cur.retry_in ?? 0} s (attempt ${cur.attempt} of ${cur.max_attempts})`,
        fmtRate(cur.speed)],
      finishing: ['Finishing: subtitles', fmtRate(cur.speed)],
      verifying: ['Verifying the file already downloaded', null],
    }[cur.state] || [done, fmtRate(cur.speed), fmtEta(cur.eta)];
    nodes.push(h('div', { class: 'panel' },
      h('div', { style: 'font-weight:600' }, cur.title),
      h('div', { class: 'progress' }, h('div', { style: `width:${pct.toFixed(1)}%` })),
      h('div', { class: 'bar small muted', style: 'margin:0' },
        h('span', { class: 'grow' }, line.filter(Boolean).join(' · ')),
        h('button', { class: 'btn small', onclick: () => post(`/api/jobs/${cur.id}/cancel`) }, 'Cancel')),
      cur.state === 'retrying' && cur.error ? h('div', { class: 'small muted' }, cur.error) : null));
  }
  if (failed.length) {
    nodes.push(h('h3', {}, 'Failed'));
    nodes.push(h('div', { class: 'list' }, failed.map(j => jobRow(j))));
  }
  if (queued.length) {
    nodes.push(h('h3', {}, 'Up next'));
    nodes.push(h('div', { class: 'list' }, queued.slice(0, 300).map(j => jobRow(j))));
  }
  box.replaceChildren(...nodes);
}

function jobRow(j) {
  const act = async (what) => {
    try {
      const r = await post(`/api/jobs/${j.id}/${what}`);
      if (what === 'remove') removedToast({ [r.result]: 1 });
      S.dirty = true; refreshState(); render();
    } catch (e) { toast(e.message); }
  };
  const actions = [];
  if ((j.status === 'queued' || j.status === 'downloading') && !j.phase) actions.push(h('button', { class: 'btn small', onclick: () => act('cancel') }, 'Cancel'));
  if (j.status === 'failed' || j.status === 'cancelled') actions.push(h('button', { class: 'btn small', onclick: () => act('retry') }, 'Retry'));
  if (j.status === 'done' || j.status === 'failed' || (j.status === 'cancelled' && ['redownload', 'recover'].includes(j.mode))) actions.push(h('button', {
    class: 'btn small danger', onclick: () => { if (confirm(`Delete "${jobTitle(j)}" from ${dev()}?`)) act('remove'); },
  }, 'Remove'));
  const when = j.status === 'removed' ? `Removed ${fmtTime(j.removed_at)}`
    : j.finished_at ? `${j.status === 'done' ? 'Copied' : 'Ended'} ${fmtTime(j.finished_at)}` : `Queued ${fmtTime(j.queued_at)}`;
  const subs = j.subs_total ? `${j.subs_done}/${j.subs_total} subtitles` : null;
  return h('div', { class: 'row' },
    h('div', { class: 'main' },
      h('div', { class: 'title' }, jobTitle(j)),
      h('div', { class: 'small muted' }, h('span', { class: `chip ${j.status}` }, j.phase && j.status !== 'done' ? 'finishing' : j.status), ' ',
        [{ upgrade: 'new version', redownload: 'copy again', refresh: 'subtitles only' }[j.mode],
          fmtBytes(j.size), when, subs, j.source === 'watchlist' ? 'from watchlist' : null].filter(Boolean).join(' · ')),
      j.waiting ? h('div', { class: 'small warn' }, 'Waiting: this job belongs to another Plex server; select it again in Settings or remove the job') : null,
      j.error ? h('div', { class: 'small warn' }, j.error) : null,
      j.note && j.status !== 'done' ? h('div', { class: 'small muted' }, j.note) : null,
      h('div', { class: 'mono muted' }, j.dest_rel)),
    h('div', { class: 'actions' }, actions));
}

async function renderHistory() {
  await refreshState();
  const rid = S.rid;
  const list = h('div', { class: 'list' });
  const more = h('div', { class: 'more' });
  const search = h('input', { type: 'search', placeholder: 'Search history', value: S.q.history || '' });
  const t = (S.state.totals || {}).done || { count: 0, bytes: 0 };
  let offset = 0, debounce;
  async function load(reset) {
    if (reset) { offset = 0; list.replaceChildren(); }
    const q = S.q.history || '';
    let d;
    try { d = await api(`/api/jobs?view=history&limit=100&offset=${offset}${q ? `&q=${encodeURIComponent(q)}` : ''}`); }
    catch (e) { list.replaceChildren(errorPanel(e, render)); return; }
    if (rid !== S.rid || (S.q.history || '') !== q) return;
    d.jobs.forEach(j => list.append(jobRow(j)));
    offset += d.jobs.length;
    more.replaceChildren(d.jobs.length === 100 ? h('button', { class: 'btn', onclick: () => load(false) }, 'Load more') : '');
    if (!list.children.length) list.append(h('div', { class: 'empty' }, q ? 'No matches.' : 'Nothing copied yet.'));
  }
  search.oninput = () => { clearTimeout(debounce); debounce = setTimeout(() => { S.q.history = search.value.trim(); load(true); }, 350); };
  view.replaceChildren(
    h('div', { class: 'bar' }, h('div', { class: 'grow' }, search)),
    h('p', { class: 'muted small' }, `${t.count} file(s) currently on ${dev()} · ${fmtBytes(t.bytes)}`),
    list, more);
  load(true);
}

const MAX_SELECT = 500;  // the delete API's limit per request

function totalOf(entries) {
  return entries.reduce((a, e) => ({ size: a.size + e.size, files: a.files + e.files, complete: a.complete && e.complete }),
    { size: 0, files: 0, complete: true });
}
// A total that is only a lower bound always says so.
const amount = t => `${t.complete ? '' : 'at least '}${fmtBytes(t.size)}`;
const fileCount = t => `${t.files}${t.complete ? '' : '+'} file${t.files === 1 && t.complete ? '' : 's'}`;

function fileRow(e, boxes, toggle) {
  let cb = null;
  if (e.deletable) {
    cb = h('input', { type: 'checkbox', 'aria-label': `Select ${e.name}` });
    cb.onchange = () => toggle(e, cb.checked);
    boxes.set(e.path, cb);
  }
  const name = e.dir
    ? h('button', { class: 'linkish', type: 'button', onclick: () => { S.path = e.path; render(); } }, e.name)
    : e.name;
  const meta = !e.dir ? fmtBytes(e.size)
    : !e.complete && !e.size && !e.files ? 'size not counted' : `${amount(e)} · ${fileCount(e)}`;
  return h('div', { class: 'row' }, cb,
    h('span', { class: `ico ${e.dir ? 'dir' : 'file'}` }),
    h('div', { class: 'main' },
      h('div', { class: 'title' }, name),
      h('div', { class: 'small muted' }, meta,
        e.partial ? [' ', h('span', { class: 'chip' }, 'partial download')] : null,
        e.link ? [' ', h('span', { class: 'chip' }, 'link')] : null)));
}

async function renderFiles() {
  const rid = S.rid;
  const path = S.path;
  view.replaceChildren(h('div', { class: 'spinner' }));
  let d;
  try { d = await api(`/api/files?path=${encodeURIComponent(path)}`); }
  catch (e) {
    if (rid !== S.rid) return;
    if (e.code === 'missing' && path) { S.path = ''; return render(); }
    return view.replaceChildren(errorPanel(e, render));
  }
  if (rid !== S.rid) return;
  const entries = [...d.entries];
  const sel = new Map();
  const boxes = new Map();
  const summary = h('p', { class: 'muted small' });
  const list = h('div', { class: 'list' });
  const more = h('div', { class: 'more' });
  const delBtn = h('button', { class: 'btn danger' });

  const refresh = () => {
    for (const [p, cb] of boxes) cb.checked = sel.has(p);
    const t = totalOf([...sel.values()]);
    delBtn.textContent = sel.size ? `Delete ${sel.size} selected · ${amount(t)}` : 'Select items to delete';
    delBtn.disabled = !sel.size;
    const all = totalOf(entries);
    const free = d.disk ? ` · ${fmtBytes(d.disk.free)} free of ${fmtBytes(d.disk.total)}` : '';
    summary.textContent = (entries.length < d.total
      ? `Showing ${entries.length} of ${d.total} items · ${amount(all)} in those`
      : `${d.total} item${d.total === 1 ? '' : 's'} · ${amount(all)}`) + free;
  };
  const toggle = (e, on) => {
    if (on && !sel.has(e.path) && sel.size >= MAX_SELECT) toast(`Select at most ${MAX_SELECT} at a time`);
    else if (on) sel.set(e.path, e);
    else sel.delete(e.path);
    refresh();
  };
  const pickAll = () => {
    const all = entries.filter(e => e.deletable);
    sel.clear();
    all.slice(0, MAX_SELECT).forEach(e => sel.set(e.path, e));
    if (all.length > MAX_SELECT) toast(`Selected the first ${MAX_SELECT}; delete them, then select the rest`);
    refresh();
  };
  const paintMore = () => more.replaceChildren(entries.length < d.total
    ? h('button', { class: 'btn', onclick: loadMore }, 'Load more') : '');
  async function loadMore() {
    more.replaceChildren(h('div', { class: 'spinner' }));
    let p;
    try { p = await api(`/api/files?path=${encodeURIComponent(path)}&offset=${entries.length}&token=${d.token}`); }
    catch (e) {
      if (rid !== S.rid) return;
      if (e.code === 'changed') { toast('The folder changed, reloaded'); return render(); }
      if (e.code === 'missing') { S.path = ''; return render(); }
      return more.replaceChildren(errorPanel(e, loadMore));
    }
    if (rid !== S.rid) return;
    entries.push(...p.entries);
    d.disk = p.disk;
    p.entries.forEach(e => list.append(fileRow(e, boxes, toggle)));
    paintMore();
    refresh();
  }
  delBtn.onclick = async () => {
    const items = [...sel.values()];
    const t = totalOf(items);
    const what = items.length === 1 ? `"${items[0].name}"` : `${items.length} items`;
    if (!confirm(`Delete ${what} from ${dev()}? ${amount(t)} in ${fileCount(t)}. `
      + 'Queued downloads inside are cancelled. This cannot be undone.')) return;
    delBtn.disabled = true;
    try {
      const r = await post('/api/files/delete', { paths: items.map(e => e.path) });
      toast(r.errors.length ? `Deleted ${r.deleted} of ${items.length}. ${r.errors[0].path}: ${r.errors[0].error}`
        : `Deleted ${r.deleted} item(s) · ${fmtBytes(r.bytes)} of data`);
    } catch (e) {
      toast(e.message);
      if (e.code !== 'missing') { delBtn.disabled = false; return; }
    }
    S.dirty = true;
    refreshState();
    render();
  };

  const parts = path ? path.split('/') : [];
  const crumbs = h('nav', { class: 'crumbs grow', 'aria-label': 'Folder' },
    parts.length ? h('button', { class: 'linkish', type: 'button', onclick: () => { S.path = ''; render(); } }, 'Media')
      : h('span', {}, 'Media'),
    parts.map((p, i) => [h('span', { class: 'muted' }, '›'), i === parts.length - 1 ? h('span', {}, p)
      : h('button', {
        class: 'linkish', type: 'button', onclick: () => { S.path = parts.slice(0, i + 1).join('/'); render(); },
      }, p)]));
  entries.forEach(e => list.append(fileRow(e, boxes, toggle)));
  if (!entries.length) list.append(h('div', { class: 'empty' }, 'This folder is empty.'));
  const deletable = entries.some(e => e.deletable);
  refresh();
  paintMore();
  view.replaceChildren(...[
    h('div', { class: 'bar' }, crumbs,
      h('button', { class: 'btn small', onclick: () => openResync(path) }, 'Re-sync from Plex'),
      h('button', { class: 'btn small', onclick: render }, 'Refresh')),
    summary,
    parts.length ? null : h('p', { class: 'muted small' }, 'Library folders cannot be deleted here. Open one to delete what is inside.'),
    list, more,
    deletable ? h('div', { class: 'files-actions' },
      h('button', { class: 'btn small', onclick: pickAll }, 'All'),
      h('button', { class: 'btn small', onclick: () => { sel.clear(); refresh(); } }, 'None'),
      h('span', { class: 'grow' }), delBtn) : null,
  ].filter(Boolean));
}

const MEDIA_VERSIONS = [
  ['all', 'All versions'],
  ['original', 'Original (largest)'],
  ['smallest', 'Smallest'],
  ['2160', 'Best up to 4K'],
  ['1080', 'Best up to 1080p'],
  ['720', 'Best up to 720p'],
  ['sd', 'Best up to SD'],
];

function renderSettings() {
  const st = S.state;
  const reserve = h('input', { type: 'number', min: '0', step: '1', value: st.reserveGb, style: 'width:110px' });
  const version = h('select', {
    onchange: async () => { await post('/api/settings', { mediaVersion: version.value }); toast('Saved'); refreshState(); },
  }, ...MEDIA_VERSIONS.map(([v, text]) => h('option', { value: v, selected: v === st.mediaVersion }, text)));
  const connLine = st.conn
    ? `Connected via ${st.conn.kind}: ${st.conn.url}`
    : st.connError ? `Not connected: ${st.connError}` : 'Not connected yet';
  view.replaceChildren(
    h('div', { class: 'panel' }, h('h2', {}, 'Plex account'),
      h('p', {}, st.user ? `Signed in as ${st.user}` : 'Signed in'),
      h('button', {
        class: 'btn danger small', onclick: async () => {
          if (!confirm('Sign out of Plex? Your copy history stays.')) return;
          await post('/api/auth/signout'); await refreshState(); render();
        },
      }, 'Sign out')),
    authPanel(),
    h('div', { class: 'panel' }, h('h2', {}, 'Server'),
      h('p', {}, st.serverName || ''), h('p', { class: `small ${st.conn ? 'muted' : 'warn'}` }, connLine),
      st.conn && st.conn.kind === 'relay' ? h('p', { class: 'small warn' }, 'Plex Relay is very slow. Fix Remote Access or set a VPN address.') : null,
      h('div', { class: 'bar' },
        h('button', {
          class: 'btn small', onclick: async () => {
            try { const r = await post('/api/connect'); toast(`Connected via ${r.conn.kind}`); }
            catch (e) { toast(e.message); }
            await refreshState(); render();
          },
        }, 'Reconnect'),
        h('button', { class: 'btn small', onclick: () => { S.libs = null; renderServers(); } }, 'Change server or address'))),
    h('div', { class: 'panel' }, h('h2', {}, 'Downloads'),
      h('div', { class: 'bar' },
        h('button', {
          class: 'btn small', onclick: async () => { await post('/api/settings', { paused: !st.paused }); await refreshState(); render(); },
        }, st.paused ? 'Resume downloads' : 'Pause downloads')),
      h('div', { class: 'field' }, h('label', {}, `Always keep this much free on ${dev()} (GB)`),
        h('div', { class: 'bar', style: 'margin:0' }, reserve, h('button', {
          class: 'btn small', onclick: async () => { await post('/api/settings', { reserveGb: reserve.value }); toast('Saved'); refreshState(); },
        }, 'Save'))),
      h('div', { class: 'field' }, h('label', {}, 'When Plex has several versions of a title, copy'), version,
        h('p', { class: 'muted small' },
          'Any choice but All versions copies one version of each movie or episode; with a maximum, the best one ',
          'at or under it, else the smallest. Use Plex\'s Optimize to make smaller versions; one is saved under ',
          'the original\'s name, not in a Plex Versions folder. A title page can choose differently for one copy.'))),
    h('div', { class: 'panel' }, h('h2', {}, 'Storage'),
      h('p', {}, `Device name: ${st.device}`,
        h('span', { class: 'muted small' }, ' (the Unraid server name or host name; set TRAVELARR_NAME to change it)')),
      h('p', { class: 'mono' }, st.mediaRoot),
      st.disk ? h('p', {}, `${fmtBytes(st.disk.free)} free of ${fmtBytes(st.disk.total)}`)
        : h('p', { class: 'warn' }, 'The media folder is missing. Map the media share to it.')),
    h('p', { class: 'muted small' }, `travelarr ${st.version}`));
}

function authPanel() {
  const st = S.state;
  const panel = h('div', { class: 'panel' }, h('h2', {}, 'Authentication'));
  const msg = h('p', { class: 'small warn', role: 'alert' });
  const input = (type, ac, value) => h('input', { type, autocomplete: ac, value: value || '', required: true });
  const field = (label, el) => h('div', { class: 'field' }, h('label', {}, label), el);
  const submit = (fields, label, cls, body, done) => h('form', {
    class: 'auth-form', onsubmit: async e => {
      e.preventDefault();
      msg.textContent = '';
      try { await post('/api/security', body()); toast(done); await refreshState(); render(); }
      catch (err) { if (err.code !== 'signin') msg.textContent = err.message; }
    },
  }, ...fields, h('button', { class: `btn small ${cls}`, type: 'submit' }, label));
  if (!st.authEnabled) {
    const user = input('text', 'username', 'admin'), pw = input('password', 'new-password'), rep = input('password', 'new-password');
    panel.append(h('p', { class: 'small warn' }, 'Off: anyone on your network can open travelarr and use your Plex sign-in.'),
      submit([field('User name', user), field('Password (8 or more characters)', pw), field('Repeat password', rep)],
        'Require authentication', 'primary',
        () => ({ action: 'enable', user: user.value, password: pw.value, repeat: rep.value }), 'Authentication is on'),
      msg);
    return panel;
  }
  const box = h('div');
  const show = form => { msg.textContent = ''; box.replaceChildren(form); };
  const cur = () => input('password', 'current-password');
  const changeForm = () => {
    const c = cur(), pw = input('password', 'new-password'), rep = input('password', 'new-password');
    return submit([field('Current password', c), field('New password (8 or more characters)', pw), field('Repeat new password', rep)],
      'Change password', 'primary', () => ({ action: 'change', current: c.value, password: pw.value, repeat: rep.value }),
      'Password changed; other browsers were signed out');
  };
  const offForm = () => {
    const c = cur();
    return submit([field('Current password', c)], 'Turn off authentication', 'danger',
      () => ({ action: 'disable', current: c.value }), 'Authentication is off');
  };
  panel.append(h('p', {}, `Required. User name: ${st.authUser}`),
    h('div', { class: 'bar' },
      h('button', { class: 'btn small', onclick: () => show(changeForm()) }, 'Change password'),
      h('button', { class: 'btn small', onclick: () => show(offForm()) }, 'Turn off'),
      h('button', {
        class: 'btn small', onclick: async () => { await post('/api/logout'); renderLogin(); },
      }, 'Sign out')),
    box, msg);
  return panel;
}

(async function init() {
  const tab = location.hash.slice(1);
  if (['watchlist', 'movies', 'tv', 'queue', 'history', 'files', 'settings'].includes(tab)) S.tab = tab;
  try {
    const s = await api('/api/session');
    if (s.authRequired && !s.signedIn) renderLogin();
  } catch { /* refreshState reports it */ }
  await refreshState();
  if (!S.locked) render();
  setInterval(refreshState, 5000);
})();
