// Vanilla JS port of the behaviour of `page-mascot` (MIT, (c) 2026 Kamran Ahmed,
// https://github.com/nilbuild/page-mascot). The original is a React component; this app has no React.
// Each character is two 3x3 sprite sheets: nine head directions and nine expressions.

const DIRECTIONS = ['up-left', 'up', 'up-right', 'left', 'center', 'right', 'down-left', 'down', 'down-right'];
const REACTIONS = ['blink', 'heart', 'sparkle', 'surprised', 'wink', 'bashful', 'sleepy', 'dizzy', 'delighted'];
// Clockwise from the right, matching atan2 with y pointing down.
const CLOCKWISE = ['right', 'down-right', 'down', 'down-left', 'left', 'up-left', 'up', 'up-right'];
const SECTOR = (Math.PI * 2) / CLOCKWISE.length;
const HYSTERESIS = 0.12;
const DEAD_ZONE = 70;
const PAYOFFS = ['heart', 'sparkle', 'delighted'];
const BOOP_PAYOFF = 120;
const BOOP_END = 560;
const SQUASH_MS = 420;
const DIZZY_AFTER = 4;
const DIZZY_WINDOW = 1600;
const DIZZY_END = 1100;
const SQUASH = [
  { transform: 'scale(1, 1)', easing: 'ease-in' },
  { transform: 'scale(1.10, 0.86)', offset: 0.18, easing: 'ease-out' },
  { transform: 'scale(0.95, 1.08)', offset: 0.45, easing: 'ease-in-out' },
  { transform: 'scale(1.03, 0.97)', offset: 0.72, easing: 'ease-in-out' },
  { transform: 'scale(1, 1)' },
];

// background-size 300% makes each cell a clean 0/50/100% step on both axes.
const cellPos = (i) => `${(i % 3) * 50}% ${Math.floor(i / 3) * 50}%`;
const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a));

// Fills `host` with a clickable mascot that follows the pointer. Returns { react(name, ms) }.
export function mountMascot(host, { name, label = 'mascot' }) {
  const button = Object.assign(document.createElement('button'), { type: 'button', className: 'mascot' });
  button.setAttribute('aria-label', `Boop the ${label}`);
  const squash = Object.assign(document.createElement('span'), { className: 'squash' });
  const dirLayer = Object.assign(document.createElement('span'), { className: 'layer' });
  const reactLayer = Object.assign(document.createElement('span'), { className: 'layer' });
  dirLayer.style.backgroundImage = `url(/mascots/${name}-directions.webp)`;
  reactLayer.style.backgroundImage = `url(/mascots/${name}-reactions.webp)`;
  squash.append(dirLayer, reactLayer);
  button.append(squash);
  host.replaceChildren(button);

  const show = (direction, reaction) => {
    dirLayer.style.backgroundPosition = cellPos(DIRECTIONS.indexOf(direction));
    dirLayer.style.opacity = reaction ? 0 : 1;
    reactLayer.style.backgroundPosition = cellPos(REACTIONS.indexOf(reaction ?? 'blink'));
    reactLayer.style.opacity = reaction ? 1 : 0;
  };
  let direction = 'center';
  let reaction = null;
  show(direction, reaction);

  let sector = -1;
  if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
    let pointer = null;
    const aim = () => {
      if (!pointer) return;
      const box = button.getBoundingClientRect();
      const dx = pointer.x - (box.left + box.width / 2);
      const dy = pointer.y - (box.top + box.height / 2);
      if (Math.hypot(dx, dy) < DEAD_ZONE) {
        sector = -1;
        direction = 'center';
      } else {
        const angle = Math.atan2(dy, dx);
        // Hold the current sector until the pointer is well past its edge.
        if (sector !== -1 && Math.abs(wrap(angle - sector * SECTOR)) < SECTOR / 2 + HYSTERESIS) return;
        sector = (Math.round(angle / SECTOR) + CLOCKWISE.length) % CLOCKWISE.length;
        direction = CLOCKWISE[sector];
      }
      show(direction, reaction);
    };
    window.addEventListener('pointermove', (e) => { pointer = { x: e.clientX, y: e.clientY }; aim(); }, { passive: true });
    window.addEventListener('scroll', aim, { passive: true });
  }

  let timers = [];
  const later = (ms, next) => timers.push(setTimeout(() => { reaction = next; show(direction, reaction); }, ms));
  const boops = { count: 0, at: 0 };

  // Show a reaction cell for `ms`, then go back to tracking.
  const react = (which, ms = BOOP_END) => {
    timers.forEach(clearTimeout);
    timers = [];
    reaction = which;
    show(direction, reaction);
    later(ms, null);
  };

  button.onclick = () => {
    timers.forEach(clearTimeout);
    timers = [];
    const now = Date.now();
    boops.count = now - boops.at < DIZZY_WINDOW ? boops.count + 1 : 1;
    boops.at = now;
    if (boops.count >= DIZZY_AFTER) {
      boops.count = 0;
      reaction = 'dizzy';
      show(direction, reaction);
      later(DIZZY_END, null);
    } else {
      reaction = 'blink';
      show(direction, reaction);
      later(BOOP_PAYOFF, PAYOFFS[(boops.count - 1) % PAYOFFS.length]);
      later(BOOP_END, null);
    }
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    squash.animate(SQUASH, { duration: SQUASH_MS, easing: 'linear' });
  };

  // Point the head somewhere without a pointer (idle glances). Forgets the pointer's sector so tracking resumes cleanly.
  const look = (d) => { sector = -1; direction = d; show(direction, reaction); };

  return { react, look };
}
