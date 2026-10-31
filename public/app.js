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

// Changing a site you already built: the same four agents, but with revision personas. You pick who to start with and the
// rest of the line follows (CEO -> Designer -> Developer -> Tester); the Tester then offers to push and redeploy.
const REVISE = {
  ceo: { persona: 'ceo_revise', label: 'CEO', tools: ['confirm_changes'], next: 'designer', up: 'Up next: design changes' },
  designer: { persona: 'designer_revise', label: 'Designer', tools: ['confirm_design_changes'], next: 'developer', up: 'Up next: applying the changes' },
  developer: { persona: 'developer_revise', label: 'Developer', tools: ['confirm_code_changes'], next: 'tester', up: 'Up next: test and deploy' },
  tester: { persona: 'tester_revise', label: 'Tester', tools: ['confirm_redeploy', 'confirm_deploy', 'skip_deploy'], next: null },
};
const REVISE_KICKOFF = 'The user came back to change a website the team already built. Introduce yourself briefly and ask what they want to change.';
let revising = null; // { siteId, title, brief, files, deployment, notes, entry } while a revision meeting is running

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
    for (const m of [...Object.values(hero), cutuMascot]) {
      if (idle && Math.random() < 0.6) m.look(pick(GLANCES));
      if (Math.random() < 0.18) m.react(pick(MOODS), 800);
    }
  }, 1400);
}

let context = '';
let handoff = null;
let building = null;
let lastSite = null;
let lastFollowError = '';

const flowOf = (agent) => (revising ? REVISE[agent] : FLOW[agent]);
const toolsOf = (f) => f.tools || [f.tool];

async function runAgent(agent) {
  const f = flowOf(agent);
  handoff = null;
  setAgent(agent, 'Connecting...');
  const kickoff = revising ? (agent === revising.entry ? REVISE_KICKOFF : KICKOFF) : (agent === 'ceo' ? undefined : KICKOFF);
  try {
    live = await startLive(f.persona || agent, micStream, {
      onSpeaking: (on) => { speaking = on; setAgent(agent, on ? 'Speaking' : 'Listening', on); },
      onUserText: (t) => caption('You', t),
      onAgentText: (t) => caption(f.label, t),
      onToolCall: (name, args) => {
        if (!toolsOf(f).includes(name)) return;
        handoff = { name, args };
        if (args.summary) {
          context += `\n[${f.label}] ${args.summary}`;
          if (revising) revising.notes.push(`[${f.label}] ${args.summary}`);
        }
        if (agent === 'developer') building = revising ? runRevisionBuild() : runBuild(context);
        if (f.next) setAgent(f.next, f.up);
      },
      onFarewellDone: () => finish(agent),
      onClose: () => !handoff && setAgent(agent, 'Disconnected'),
    }, context, kickoff);
  } catch (e) {
    setAgent(agent, 'Could not connect');
    console.error(e);
  }
}

// Wait until the farewell audio has played out, then switch agents.
function finish(agent) {
  if (!handoff) return;
  if (speaking) return void setTimeout(() => finish(agent), 300);
  const f = flowOf(agent);
  const { name, args } = handoff;
  live?.stop(); live = null;
  handoff = null;
  if (agent === 'developer') return void finishBuild();
  if (agent === 'tester') return void finishTester(name, args);
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
    lastFollowError = err.message;
  }
  return final;
}

const onBuildEvent = (e) => {
  if (e.site && e.files && !e.final) { lastSite = e.site; showSite(e.site, e.files); }
  if (e.screenshot) showShot(e.screenshot);
};
function runBuild(ctx) {
  return follow('/api/build', { context: ctx }, onBuildEvent);
}
// Same site id: the server loads the saved files, applies the changes, tests, and saves the new version.
function runRevisionBuild() {
  const notes = revising.notes.join('\n');
  return follow('/api/build', { context: notes, site_id: revising.siteId, changes: notes }, onBuildEvent);
}

async function finishBuild() {
  const final = await building;
  if (!final || final.error) {
    setAgent('developer', 'Build failed');
    $('captions').textContent = `Developer: ${final?.message || lastFollowError || 'the build failed'}`;
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

function finishTester(name, args) {
  if (name === 'skip_deploy') {
    setAgent('tester', 'Changes saved, not deployed');
    const back = el('button', 'Back to Your Code', { className: 'primary', onclick: () => { $('leave').click(); location.hash = '#/code'; } });
    const row = el('div', '', { className: 'row' });
    row.append(closeBtn(), back);
    return showCard(el('h3', 'Changes saved'), el('p', 'Your updated site is saved in Your Code. You can deploy it any time from the Deploy page.'), row);
  }
  if (name === 'confirm_redeploy') return showRedeployConfirm();
  return showConfirm(args, lastSite?.id); // confirm_deploy: a site that was never deployed gets the normal first deployment
}

// Nothing is pushed or redeployed until the user clicks the confirm button.
function showRedeployConfirm() {
  const d = revising?.deployment;
  if (!d || !revising) return showResult({ error: true, message: 'This site has not been deployed before, so there is no repository to update.' }, true);
  setAgent('tester', 'Waiting for your confirmation');
  const go = el('button', 'Push and redeploy', { className: 'primary' });
  const cancel = el('button', 'Cancel');
  const row = el('div', '', { className: 'row' });
  row.append(cancel, go);
  showCard(
    el('h3', 'Confirm redeploy'),
    el('p', `Push these changes to the GitHub repository "${d.repo_name}" and redeploy it on Vercel?`),
    ...(d.visibility === 'public' ? [el('p', 'This repository is public: the changes will be visible to anyone.', { className: 'warn' })] : []),
    row,
  );
  cancel.onclick = () => { $('dialog').hidden = true; setAgent('tester', 'Redeploy cancelled'); };
  go.onclick = async () => {
    $('dialog').hidden = true;
    const summary = revising.notes.map((n) => n.replace(/^\[[^\]]*\]\s*/, '')).join('; ').slice(0, 200);
    const final = await follow('/api/redeploy', { site_id: revising.siteId, summary, confirmed: true });
    showResult(final, true);
    if (final && !final.error) revising.deployment = { ...d, live_url: final.live_url, repo_url: final.repo_url };
  };
}

function showResult(f, again = false) {
  const close = el('button', 'Close', { onclick: () => ($('dialog').hidden = true) });
  if (!f || f.error) {
    showCard(el('h3', again ? 'Redeploy failed' : 'Deployment failed'), el('p', f?.message || 'Something went wrong.'), close);
    return;
  }
  const link = (label, url, prefix) => (url.startsWith(prefix)
    ? el('a', `${label}: ${url}`, { href: url, target: '_blank', rel: 'noopener' })
    : el('p', `${label}: (unexpected URL)`));
  showCard(
    el('h3', again ? 'Your changes are live' : 'Your site is live'),
    link('Live site', f.live_url, 'https://'),
    link('GitHub repo', f.repo_url, 'https://github.com/'),
    ...(f.commit_url && f.commit_url.startsWith('https://github.com/') ? [link('Commit', f.commit_url, 'https://github.com/')] : []),
    ...(again && f.changed === false ? [el('p', 'GitHub already had exactly these files, so no new commit was needed.')] : []),
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

async function getMic() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    return true;
  } catch {
    return false;
  }
}

function openRoom() {
  $('landing').hidden = true;
  wave.stop();
  $('room').hidden = false;
  context = '';
  building = null;
  lastSite = null;
  revising = null;
  for (const agent of Object.keys(CHARACTER)) setAgent(agent, 'Waiting'); // nothing left over from an earlier meeting
  $('captions').textContent = '';
  lastWho = '';
  $('work').hidden = true;
  $('tab-tester').hidden = true;
  $('dialog').hidden = true;
}

$('join').onclick = async () => {
  stopCutu();
  $('error').hidden = true;
  if (!(await getMic())) {
    $('error').textContent = 'Microphone access is needed to talk to the team. Allow it in your browser and try again.';
    $('error').hidden = false;
    return;
  }
  openRoom();
  runAgent('ceo');
};

$('leave').onclick = () => {
  handoff = null;
  revising = null;
  live?.stop(); live = null;
  micStream?.getTracks().forEach((t) => t.stop());
  micStream = null;
  document.querySelectorAll('.tile').forEach((t) => t.classList.remove('speaking'));
  $('room').hidden = true;
  $('landing').hidden = false;
  wave.start();
  if (!$('view-code').hidden) loadCode(); // a change you just made should show up in the list
  if (!$('view-deploy').hidden) loadDeploy();
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
  return apiAs(path, CLIENT_ID);
}

async function apiAs(path, key) {
  const r = await fetch(path, { headers: { 'X-Client-Id': key } });
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
    el('button', 'Make changes', { className: 'btn-ghost accent', onclick: () => chooseAgent(s) }),
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

// ---- Workspace key: the browser's anonymous id, shown so it can be carried to another browser or device ----
const KEY_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const maskKey = (k) => k.replace(/[0-9a-f]/g, '\u2022');

function showKeyDialog() {
  let revealed = false;
  const shown = el('span', maskKey(CLIENT_ID));
  const reveal = el('button', 'Show', {
    onclick: () => { revealed = !revealed; shown.textContent = revealed ? CLIENT_ID : maskKey(CLIENT_ID); reveal.textContent = revealed ? 'Hide' : 'Show'; },
  });
  const copy = el('button', 'Copy', {
    onclick: async () => {
      try { await navigator.clipboard.writeText(CLIENT_ID); copy.textContent = 'Copied'; } catch { copy.textContent = 'Copy failed'; }
      setTimeout(() => (copy.textContent = 'Copy'), 1500);
    },
  });
  const box = el('div', '', { className: 'keybox' });
  box.append(shown, reveal, copy);

  const input = el('input', '', { id: 'key-input', placeholder: 'Paste a workspace key', autocomplete: 'off', spellcheck: false });
  const status = el('p', '', { className: 'key-status', role: 'status' });
  const find = el('button', 'Find my sites');
  const sw = el('button', 'Switch to these sites', { className: 'primary' });
  sw.disabled = true;
  let candidate = '';
  input.oninput = () => { sw.disabled = true; candidate = ''; status.textContent = ''; };

  find.onclick = async () => {
    const key = input.value.trim().toLowerCase();
    if (!KEY_RE.test(key)) return void (status.textContent = 'That does not look like a workspace key. Copy the whole key and try again.');
    if (key === CLIENT_ID) return void (status.textContent = 'That is the key of this workspace already.');
    find.disabled = true;
    status.textContent = 'Looking...';
    try {
      const { sites } = await apiAs('/api/sites', key); // only switches to a key that really has sites, so a typo cannot land you in an empty workspace
      if (!sites.length) {
        status.textContent = 'No sites were found for that key. Check that you copied all of it.';
      } else {
        candidate = key;
        sw.disabled = false;
        status.textContent = `Found ${sites.length} site${sites.length === 1 ? '' : 's'}; the latest is "${sites[0].title}". Switching shows them here. Your current workspace stays saved: copy its key above first if you want to come back to it.`;
      }
    } catch (e) {
      status.textContent = e.message;
    }
    find.disabled = false;
  };
  sw.onclick = () => {
    try { localStorage.setItem('jm_client', candidate); } catch { return void (status.textContent = 'This browser blocks saving site data, so the key cannot be switched here.'); }
    location.reload();
  };

  const findRow = el('div', '', { className: 'row' });
  findRow.append(find);
  const row = el('div', '', { className: 'row' });
  row.append(closeBtn(), sw);
  showCard(
    el('h3', 'Your workspace key'),
    el('p', 'This key is how this browser finds your saved sites. Paste it into another browser or device to open the same sites there.'),
    box,
    el('p', 'Treat it like a password: anyone who has it can see, change and deploy your sites. Do not share it or post it online.', { className: 'warn' }),
    el('h4', 'Open sites from another browser or device'),
    input, findRow, status, row,
  );
}
$('open-key').onclick = showKeyDialog;

// ---- Make changes: choose who to talk to, then the agents continue down the line with that site ----
const REVISE_CHOICES = [
  ['ceo', 'Talk to the CEO', 'New or bigger changes. The CEO writes them down, then the Designer, Developer and Tester carry on.'],
  ['designer', 'Talk to the Designer', 'Look and feel: colours, fonts, layout. Then the Developer applies it and the Tester checks it.'],
  ['developer', 'Talk to the Developer', 'Code or behaviour changes. The Developer applies them and the Tester checks it.'],
];

function chooseAgent(site) {
  const choices = REVISE_CHOICES.map(([agent, title, text]) => {
    const b = el('button', '', { className: 'choice', onclick: () => startRevision(site.site_id, agent) });
    b.append(el('strong', title), el('span', text));
    return b;
  });
  showCard(el('h3', 'Who should handle your changes?'), el('p', site.title, { className: 'choice-site' }), ...choices, el('div', '', { className: 'row' }));
  $('card').lastChild.append(el('button', 'Cancel', { onclick: () => ($('dialog').hidden = true) }));
}

function reviseContext(r) {
  const d = r.deployment;
  return [
    `[Existing site] "${r.title}" with the files: ${r.files.map((f) => f.path).join(', ')}`,
    r.brief ? `[Original brief] ${r.brief.slice(0, 800)}` : '',
    d ? `[Deployed before] yes, repository ${d.repo_name} (${d.visibility}), live at ${d.live_url}` : '[Deployed before] no, it was never deployed',
  ].filter(Boolean).join('\n');
}

async function startRevision(siteId, entry) {
  stopCutu();
  $('dialog').hidden = true;
  let site;
  try {
    site = await api(`/api/sites/${siteId}`);
  } catch (e) {
    return showCard(el('h3', 'Could not open that site'), el('p', e.message), closeBtn());
  }
  if (!(await getMic())) {
    return showCard(el('h3', 'Microphone needed'), el('p', 'Allow the microphone in your browser, then press Make changes again.'), closeBtn());
  }
  openRoom();
  revising = { siteId, title: site.title, brief: site.brief || '', files: site.files, deployment: site.deployment, notes: [], entry };
  lastSite = { id: siteId, url: `/sites/${siteId}/` };
  showSite(lastSite, site.files);
  context = reviseContext(revising);
  runAgent(entry);
}

// ---- Cutu: the guide bot on the About page. Voice only, with its own Gemini key on the server. ----
const cutuMascot = mountMascot($('cutu-avatar'), { name: 'cube', label: 'Cutu' });
let cutuSession = null; // { live, mic } while a conversation is running
let cutuAttempt = 0; // bumped by every start and every stop, so a slow connection that finished after a cancel can be dropped
let cutuMood = null;
let cutuLastWho = '';

function cutuLine(who, text) {
  const chat = $('cutu-chat');
  chat.hidden = false;
  if (who !== cutuLastWho || !chat.lastChild) {
    const p = el('p', '');
    p.append(el('b', `${who}: `), el('span', ''));
    chat.append(p);
    while (chat.children.length > 8) chat.firstChild.remove();
    cutuLastWho = who;
  }
  chat.lastChild.lastChild.textContent += text;
  chat.scrollTop = chat.scrollHeight;
}

function stopCutu(message = '') {
  cutuAttempt++;
  clearInterval(cutuMood);
  const s = cutuSession;
  cutuSession = null;
  s?.live.stop();
  s?.mic.getTracks().forEach((t) => t.stop());
  const btn = $('cutu-start');
  btn.textContent = 'Start conversation';
  btn.classList.remove('end');
  btn.disabled = false;
  $('cutu-status').textContent = message;
}

async function startCutu() {
  const btn = $('cutu-start');
  const status = $('cutu-status');
  const attempt = ++cutuAttempt;
  btn.disabled = true;
  status.textContent = 'Asking for your microphone...';
  let mic;
  try {
    mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch {
    btn.disabled = false;
    status.textContent = 'Microphone access is needed to talk with Cutu. Allow it in your browser and try again.';
    return;
  }
  if (attempt !== cutuAttempt) { mic.getTracks().forEach((t) => t.stop()); return; } // cancelled while the permission prompt was open
  status.textContent = 'Connecting...';
  $('cutu-chat').replaceChildren();
  cutuLastWho = '';
  try {
    const live = await startLive('cutu', mic, {
      onSpeaking: (on) => {
        if (attempt !== cutuAttempt) return; // a cancelled attempt must not touch the page
        status.textContent = on ? 'Cutu is speaking...' : 'Listening. Ask anything about Join the Meeting.';
        clearInterval(cutuMood);
        if (on) cutuMood = setInterval(() => cutuMascot.react(pick(['wink', 'sparkle', 'delighted']), 700), 1600);
      },
      onUserText: (t) => attempt === cutuAttempt && cutuLine('You', t),
      onAgentText: (t) => attempt === cutuAttempt && cutuLine('Cutu', t),
      onToolCall() {},
      onFarewellDone() {},
      onClose: () => { if (cutuSession) stopCutu('The conversation ended. Press Start conversation to talk again.'); },
    }, '', 'A visitor just pressed Start conversation. Greet them in one sentence and offer to explain how Join the Meeting works.');
    if (attempt !== cutuAttempt) { live.stop(); mic.getTracks().forEach((t) => t.stop()); return; } // cancelled while connecting
    cutuSession = { live, mic };
    btn.textContent = 'End conversation';
    btn.classList.add('end');
    btn.disabled = false;
  } catch (e) {
    mic.getTracks().forEach((t) => t.stop());
    if (attempt !== cutuAttempt) return;
    btn.disabled = false;
    status.textContent = /not configured/i.test(e.message) ? 'Cutu is not available right now.' : 'Could not connect to Cutu. Please try again.';
  }
}
$('cutu-start').onclick = () => (cutuSession ? stopCutu('Conversation ended.') : startCutu());

// ---- Fluid nav indicator: a glass pill that stretches, squashes and settles as it glides between links ----
const navLinks = document.querySelector('.links');
const blob = el('span', '', { className: 'blob' });
blob.setAttribute('aria-hidden', 'true');
navLinks.prepend(blob);
navLinks.classList.add('fluid');
let blobAt = null;     // where the pill currently is: { x, w }
let blobTarget = null; // the link it is heading for

const activeLink = () => navLinks.querySelector('a.active');

function moveBlob(link, animate = true) {
  blobTarget = link;
  if (!link) { blob.style.opacity = '0'; blobAt = null; return; }
  const box = navLinks.getBoundingClientRect();
  const r = link.getBoundingClientRect();
  const to = { x: r.left - box.left, w: r.width };
  const cur = blob.getBoundingClientRect();
  const from = blobAt && { x: cur.left - box.left, w: cur.width }; // where the pill visibly is, even mid-flight
  blob.style.opacity = '1';
  blob.style.transform = `translateX(${to.x}px)`;
  blob.style.width = `${to.w}px`;
  blobAt = to;
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!from || !animate || still || (from.x === to.x && from.w === to.w)) return;
  // Stretch toward the destination mid-flight and flatten slightly, then spring into place.
  const d = to.x - from.x;
  const stretch = Math.min(0.7, Math.abs(d) / 260);
  blob.animate([
    { transform: `translateX(${from.x}px) scale(1, 1)`, width: `${from.w}px` },
    { transform: `translateX(${from.x + d * 0.5}px) scale(1, ${1 - stretch * 0.25})`, width: `${Math.max(from.w, to.w) + Math.abs(d) * 0.45}px`, offset: 0.45 },
    { transform: `translateX(${to.x}px) scale(1, 1)`, width: `${to.w}px` },
  ], { duration: 560, easing: 'cubic-bezier(.34, 1.36, .5, 1)' });
}

navLinks.querySelectorAll('a').forEach((a) => {
  a.addEventListener('mouseenter', () => moveBlob(a));
  a.addEventListener('focus', () => moveBlob(a));
});
navLinks.addEventListener('mouseleave', () => moveBlob(activeLink()));
navLinks.addEventListener('focusout', (e) => { if (!navLinks.contains(e.relatedTarget)) moveBlob(activeLink()); });
new ResizeObserver(() => moveBlob(blobTarget, false)).observe(navLinks); // font load, rotation, window resize

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
  if (name !== 'about') stopCutu();
  moveBlob(activeLink());
  window.scrollTo(0, 0);
  if (name === 'code') loadCode();
  if (name === 'deploy') loadDeploy();
}
window.addEventListener('hashchange', route);
route();

window.sendText = (t) => live?.sendText(t); // typed fallback / testing
