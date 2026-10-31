import { startLive } from './live.js';
import { mountMascot } from './mascot.js';

const $ = (id) => document.getElementById(id);
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
  if (agent === 'tester') return void showConfirm(args);
  setAgent(agent, 'Done');
  mascots[agent].react('delighted');
  runAgent(f.next);
}

// POST and read a text/event-stream response as parsed JSON events.
async function* sseStream(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
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
  $('card').replaceChildren(...nodes);
  $('dialog').hidden = false;
}
const el = (tag, text, props = {}) => Object.assign(document.createElement(tag), { textContent: text, ...props });

function showConfirm(args) {
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
    const final = await follow('/api/deploy', { site_id: lastSite.id, repo_name: repo, visibility, confirmed: true });
    showResult(final);
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
};
window.sendText = (t) => live?.sendText(t); // typed fallback / testing
