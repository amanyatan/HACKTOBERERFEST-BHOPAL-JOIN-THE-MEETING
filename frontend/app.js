import { startLive } from './live.js';
import { mountMascot } from './mascot.js';
import { mountGradientWave } from './gradient-wave.js';

const $ = (id) => document.getElementById(id);
const wave = mountGradientWave($('wave')); // landing background; paused while in the room
wave.start();
let micStream = null;
let live = null;
let speaking = false;

// setAgent('designer', 'Picking colours', true): status line + speaking ring on a tile.
window.setAgent = (agent, status, speaking = false) => {
  const tile = document.querySelector(`.tile[data-agent="${agent}"]`);
  tile.querySelector('.status').textContent = status;
  tile.classList.toggle('speaking', speaking);
};

// Each agent: label for captions, who is next, and the tool that ends its part.
const FLOW = {
  ceo: { label: 'CEO', tool: 'confirm_requirements', next: 'designer', up: 'Up next: design questions' },
  designer: { label: 'Designer', tool: 'confirm_design', next: 'developer', up: 'Up next: tech stack' },
  developer: { label: 'Developer', tool: 'confirm_stack', next: 'tester', up: 'Up next: testing and deploy' },
  tester: { label: 'Tester', tool: 'confirm_deploy', next: null },
};
const KICKOFF = 'You are now speaking with the user. Introduce yourself briefly and ask your first question.';
const LABEL = Object.fromEntries(Object.entries(FLOW).map(([k, v]) => [k, v.label]));

// Characters from page-mascot (MIT): see THIRD_PARTY.md.
const CHARACTER = { ceo: 'kamran', designer: 'ballerina', developer: 'scout', tester: 'cap' };
const mascots = Object.fromEntries(Object.entries(CHARACTER).map(([agent, name]) => [
  agent,
  mountMascot(document.querySelector(`.tile[data-agent="${agent}"] .avatar`), { name, label: LABEL[agent] }),
]));

// Landing hero: the same four characters. They look around and react on their own, and the pointer takes over while it moves.
const hero = Object.fromEntries(Object.entries(CHARACTER).map(([agent, name]) => [
  agent,
  mountMascot(document.querySelector(`.hero-mascot[data-agent="${agent}"] .hero-avatar`), { name, label: LABEL[agent] }),
]));
let lastPointer = 0;
window.addEventListener('pointermove', () => (lastPointer = Date.now()), { passive: true });
const GLANCES = ['up-left', 'up', 'up-right', 'left', 'right', 'down-left', 'down', 'down-right', 'center'];
const MOODS = ['sparkle', 'heart', 'wink', 'delighted', 'blink', 'surprised'];
const pick = (list) => list[Math.floor(Math.random() * list.length)];
if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
  setInterval(() => {
    if ($('landing').hidden) return;
    const idle = Date.now() - lastPointer > 2500;
    for (const m of Object.values(hero)) {
      if (idle && Math.random() < 0.6) m.look(pick(GLANCES));
      if (Math.random() < 0.18) m.react(pick(MOODS), 800);
    }
  }, 1400);
}

let context = '';
let handoff = null;
let building = null;
let lastSite = null;

async function runAgent(agent) {
  const f = FLOW[agent];
  handoff = null;
  setAgent(agent, 'Connecting...');
  try {
    live = await startLive(agent, micStream, {
      onSpeaking: (on) => { speaking = on; setAgent(agent, on ? 'Speaking' : 'Listening', on); },
      onUserText: (t) => caption('You', t),
      onAgentText: (t) => caption(f.label, t),
      onToolCall: (name, args) => {
        if (name !== f.tool) return;
        handoff = args;
        context += `\n[${f.label}] ${args.summary || JSON.stringify(args)}`;
        if (agent === 'developer') building = runBuild(context);
        if (f.next) setAgent(f.next, f.up);
      },
      onFarewellDone: () => finish(agent),
      onClose: () => !handoff && setAgent(agent, 'Disconnected'),
    }, context, agent === 'ceo' ? undefined : KICKOFF);
  } catch (e) {
    setAgent(agent, 'Could not connect');
    console.error(e);
  }
}

// Wait until the farewell audio has played out, then switch agents.
function finish(agent) {
  if (!handoff) return;
  if (speaking) return void setTimeout(() => finish(agent), 300);
  const f = FLOW[agent];
  const args = handoff;
  live?.stop(); live = null;
  handoff = null;
  if (agent === 'developer') return void finishBuild();
  if (agent === 'tester') return void showConfirm(args, lastSite?.id);
  setAgent(agent, 'Done');
  mascots[agent].react('delighted');
  runAgent(f.next);
}

// POST and read a text/event-stream response as parsed JSON events.
async function* sseStream(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Client-Id': CLIENT_ID }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json()).detail || 'request failed');
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (chunk.startsWith('data: ')) yield JSON.parse(chunk.slice(6));
    }
  }
}

// Shows each backend step on the tiles; resolves to the final event (or null on failure).
async function follow(url, body, onEvent) {
  let final = null;
  try {
    for await (const e of sseStream(url, body)) {
      document.querySelectorAll('.tile').forEach((t) => t.classList.toggle('speaking', t.dataset.agent === e.agent && !e.final));
      setAgent(e.agent, e.status, !e.final);
      if (e.message) { $('captions').textContent = `${LABEL[e.agent]}: ${e.message}`; lastWho = ''; }
      onEvent?.(e);
      if (e.final) final = e;
    }
  } catch (err) {
    console.error(err);
  }
  return final;
}

function runBuild(ctx) {
  return follow('/api/build', { context: ctx }, (e) => {
    if (e.site && e.files && !e.final) { lastSite = e.site; showSite(e.site, e.files); }
    if (e.screenshot) showShot(e.screenshot);
  });
}

async function finishBuild() {
  const final = await building;
  if (!final || final.error) {
    setAgent('developer', 'Build failed');
    return;
  }
  lastSite = final.site;
  showSite(final.site, final.files);
  setAgent('developer', `Built ${final.files.length} files`);
  if (final.saved === false) $('captions').textContent = 'Heads up: this site could not be saved to Your Code.';
  context += final.passed === null
    ? '\n[Automated test could not run]'
    : `\n[Automated test: ${final.passed ? 'passed' : 'NOT fully passed, remaining issues: ' + final.issues.join('; ')} after ${final.rounds} fix round(s)]`;
  runAgent('tester');
}

function showSite(site, files) {
  $('work').hidden = false;
  $('preview').src = `${site.url}?v=${Date.now()}`;
  $('open').href = site.url;
  $('file').replaceChildren(...files.map((f, i) => Object.assign(document.createElement('option'), { value: i, textContent: f.path })));
  const show = () => ($('src').textContent = files[$('file').value].content);
  $('file').onchange = show;
  show();
}

function showShot(b64) {
  $('shot').src = `data:image/jpeg;base64,${b64}`;
  $('tab-tester').hidden = false;
}

const PANES = { preview: 'preview', code: 'code', tester: 'tester-view' };
document.querySelectorAll('#work nav button').forEach((b) => (b.onclick = () => {
  document.querySelectorAll('#work nav button').forEach((x) => x.classList.toggle('on', x === b));
  Object.entries(PANES).forEach(([tab, id]) => ($(id).hidden = tab !== b.dataset.tab));
}));

// Nothing is created on GitHub or Vercel until the user clicks the confirm button.
function showCard(...nodes) {
  $('card').classList.remove('wide');
  $('card').replaceChildren(...nodes);
  $('dialog').hidden = false;
}
const el = (tag, text, props = {}) => Object.assign(document.createElement(tag), { textContent: text, ...props });

function showConfirm(args, siteId, onEvent) {
  if (!siteId) return showResult({ error: true, message: 'There is no saved site to deploy yet.' });
  const repo = String(args.repo_name || '').trim();
  const visibility = args.visibility === 'public' ? 'public' : 'private';
  setAgent('tester', 'Waiting for your confirmation');
  const go = el('button', 'Create and deploy', { className: 'primary' });
  const cancel = el('button', 'Cancel');
  const row = el('div', '', { className: 'row' });
  row.append(cancel, go);
  showCard(
    el('h3', 'Confirm deployment'),
    el('p', `Create a ${visibility.toUpperCase()} GitHub repository named "${repo}" and publish the site on Vercel?`),
    ...(visibility === 'public' ? [el('p', 'A public repository can be seen by anyone.', { className: 'warn' })] : []),
    row,
  );
  cancel.onclick = () => { $('dialog').hidden = true; setAgent('tester', 'Deployment cancelled'); };
  go.onclick = async () => {
    $('dialog').hidden = true;
    const final = await follow('/api/deploy', { site_id: siteId, repo_name: repo, visibility, confirmed: true }, onEvent);
    showResult(final);
    if (!$('view-deploy').hidden) loadDeploy();
  };
}

function showResult(f) {
  const close = el('button', 'Close', { onclick: () => ($('dialog').hidden = true) });
  if (!f || f.error) {
    showCard(el('h3', 'Deployment failed'), el('p', f?.message || 'Something went wrong.'), close);
    return;
  }
  const link = (label, url, prefix) => (url.startsWith(prefix)
    ? el('a', `${label}: ${url}`, { href: url, target: '_blank', rel: 'noopener' })
    : el('p', `${label}: (unexpected URL)`));
  showCard(
    el('h3', 'Your site is live'),
    link('Live site', f.live_url, 'https://'),
    link('GitHub repo', f.repo_url, 'https://github.com/'),
    ...(f.warning ? [el('p', f.warning, { className: 'warn' })] : []),
    ...(f.saved === false ? [el('p', 'This deployment could not be saved to your history.', { className: 'warn' })] : []),
    close,
  );
}

let lastWho = '';
function caption(who, text) {
  const c = $('captions');
  if (who !== lastWho) { c.textContent = `${who}: `; lastWho = who; }
  c.textContent += text;
}

$('join').onclick = async () => {
  $('error').hidden = true;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch {
    $('error').textContent = 'Microphone access is needed to talk to the team. Allow it in your browser and try again.';
    $('error').hidden = false;
    return;
  }
  $('landing').hidden = true;
  wave.stop();
  $('room').hidden = false;
  context = '';
  building = null;
  lastSite = null;
  $('work').hidden = true;
  $('tab-tester').hidden = true;
  $('dialog').hidden = true;
  runAgent('ceo');
};

$('leave').onclick = () => {
  handoff = null;
  live?.stop(); live = null;
  micStream?.getTracks().forEach((t) => t.stop());
  micStream = null;
  document.querySelectorAll('.tile').forEach((t) => t.classList.remove('speaking'));
  $('room').hidden = true;
  $('landing').hidden = false;
  wave.start();
};

// ---- Anonymous client id: scopes the saved sites and deployments to this browser (no accounts) ----
const CLIENT_ID = (() => {
  try {
    let id = localStorage.getItem('jm_client');
    if (!/^[0-9a-f-]{36}$/.test(id || '')) { id = crypto.randomUUID(); localStorage.setItem('jm_client', id); }
    return id;
  } catch {
    return crypto.randomUUID(); // storage blocked: works for this visit only
  }
})();

async function api(path) {
  const r = await fetch(path, { headers: { 'X-Client-Id': CLIENT_ID } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `Request failed (${r.status})`);
  return body;
}

const fmtDate = (iso) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
const closeBtn = () => el('button', 'Close', { onclick: () => ($('dialog').hidden = true) });
const option = (value, text) => Object.assign(document.createElement('option'), { value, textContent: text });
const emptyCard = (text) => { const c = el('div', '', { className: 'glass card' }); c.append(el('p', text)); return c; };

function siteCard(s) {
  const card = el('div', '', { className: 'glass card item' });
  const verdict = s.passed === true ? ['ok', 'Tested: passed'] : s.passed === false ? ['bad', `${s.issues.length} issue(s) left`] : ['unk', 'Not tested'];
  const meta = el('div', '', { className: 'meta' });
  meta.append(el('span', fmtDate(s.created_at)), el('span', verdict[1], { className: `badge ${verdict[0]}` }));
  const info = el('div');
  info.append(el('h3', s.title), meta);
  const actions = el('div', '', { className: 'actions' });
  actions.append(
    el('button', 'View code', { className: 'btn-ghost', onclick: () => viewCode(s.site_id) }),
    el('button', 'Preview', { className: 'btn-ghost', onclick: () => previewSite(s.site_id) }),
    el('button', 'Deploy', { className: 'btn-ghost', onclick: () => { pendingSite = s.site_id; location.hash = '#/deploy'; } }),
  );
  card.append(info, actions);
  return card;
}

function deployCard(d) {
  const card = el('div', '', { className: 'glass card item' });
  const meta = el('div', '', { className: 'meta' });
  meta.append(el('span', fmtDate(d.created_at)), el('span', d.visibility, { className: 'badge unk' }));
  const info = el('div');
  info.append(el('h3', d.repo_name), meta);
  const actions = el('div', '', { className: 'actions' });
  if (d.live_url.startsWith('https://')) actions.append(el('a', 'Live site', { className: 'btn-ghost', href: d.live_url, target: '_blank', rel: 'noopener' }));
  if (d.repo_url.startsWith('https://github.com/')) actions.append(el('a', 'GitHub', { className: 'btn-ghost', href: d.repo_url, target: '_blank', rel: 'noopener' }));
  card.append(info, actions);
  return card;
}

async function viewCode(id) {
  try {
    const site = await api(`/api/sites/${id}`);
    const pre = el('pre', '');
    const pick = el('select');
    site.files.forEach((f, i) => pick.append(option(i, f.path)));
    const show = () => (pre.textContent = site.files[pick.value].content);
    pick.onchange = show;
    show();
    const copy = el('button', 'Copy file', {
      onclick: async () => {
        try { await navigator.clipboard.writeText(site.files[pick.value].content); copy.textContent = 'Copied'; } catch { copy.textContent = 'Copy failed'; }
        setTimeout(() => (copy.textContent = 'Copy file'), 1500);
      },
    });
    const row = el('div', '', { className: 'row' });
    row.append(copy, closeBtn());
    showCard(el('h3', site.title), pick, pre, row);
    $('card').classList.add('wide');
  } catch (e) {
    showCard(el('h3', 'Could not load the code'), el('p', e.message), closeBtn());
  }
}

async function previewSite(id) {
  const w = window.open('', '_blank'); // opened first so the click permission is not lost while we wait
  try {
    await api(`/api/sites/${id}`); // also restores the files on the server if it was restarted
    if (w) w.location.href = `/sites/${id}/`; else window.open(`/sites/${id}/`, '_blank');
  } catch (e) {
    w?.close();
    showCard(el('h3', 'Could not open the preview'), el('p', e.message), closeBtn());
  }
}

async function loadCode() {
  const box = $('code-list');
  box.replaceChildren(el('p', 'Loading...', { className: 'note' }));
  try {
    const { sites } = await api('/api/sites');
    box.replaceChildren(...(sites.length ? sites.map(siteCard) : [emptyCard('No sites yet. Join a meeting and the first site you build will appear here.')]));
  } catch (e) {
    box.replaceChildren(emptyCard(e.message));
  }
}

let pendingSite = null; // set by "Deploy" on a Your Code card
async function loadDeploy() {
  const select = $('deploy-site');
  const list = $('deploy-list');
  list.replaceChildren(el('p', 'Loading...', { className: 'note' }));
  const [sites, deployments] = await Promise.allSettled([api('/api/sites'), api('/api/deployments')]);
  if (sites.status === 'fulfilled') {
    const rows = sites.value.sites;
    select.replaceChildren(...(rows.length ? rows.map((s) => option(s.site_id, `${s.title} (${fmtDate(s.created_at)})`)) : [option('', 'No saved sites yet')]));
    if (pendingSite) { select.value = pendingSite; pendingSite = null; }
  } else {
    select.replaceChildren(option('', 'Saved sites unavailable'));
  }
  if (deployments.status === 'fulfilled') {
    const rows = deployments.value.deployments;
    list.replaceChildren(...(rows.length ? rows.map(deployCard) : [emptyCard('No deployments yet.')]));
  } else {
    list.replaceChildren(emptyCard(deployments.reason.message));
  }
}

const REPO_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$/;
$('deploy-form').onsubmit = (e) => {
  e.preventDefault();
  const status = $('deploy-status');
  const site = $('deploy-site').value;
  const repo = $('deploy-repo').value.trim();
  if (!site) return void (status.textContent = 'Pick a saved site first.');
  if (!REPO_RE.test(repo) || repo.endsWith('.git')) return void (status.textContent = 'Use letters, numbers, dots, dashes or underscores (max 60), starting with a letter or number.');
  status.textContent = '';
  showConfirm({ repo_name: repo, visibility: $('deploy-vis').value }, site, (ev) => { status.textContent = `${ev.status}${ev.message ? ': ' + ev.message : ''}`; });
};

// ---- Router: #/ (home), #/about, #/code, #/deploy ----
const ROUTES = ['home', 'about', 'code', 'deploy'];
function route() {
  const hash = location.hash.replace(/^#\/?/, '');
  const name = ROUTES.includes(hash) ? hash : 'home';
  ROUTES.forEach((n) => ($(`view-${n}`).hidden = n !== name));
  document.querySelectorAll('.links a').forEach((a) => {
    a.classList.toggle('active', a.dataset.route === name);
    if (a.dataset.route === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  window.scrollTo(0, 0);
  if (name === 'code') loadCode();
  if (name === 'deploy') loadDeploy();
}
window.addEventListener('hashchange', route);
route();

window.sendText = (t) => live?.sendText(t); // typed fallback / testing
