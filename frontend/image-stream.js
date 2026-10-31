// Image-stream corridor, ported from a React/TypeScript component to a plain ES module.
// Two rails of cards ride from far behind the screen toward the viewer. Perspective alone makes each card
// grow and sweep outward from the vanishing point. Every length is in `cqw` (percent of the container's
// width), so the corridor keeps its proportions at any size.
//
// 1. Depth is authored as apparent size, geometrically, so consecutive cards keep a constant size ratio.
// 2. The rails open hard early and then hold (`fan` > 1), so the ribbon bends once instead of a straight cone.
// 3. Neither end of the loop is on screen: cards die past the container edge and are born across the axis
//    (`railBirth` < 0), so the centre is always covered.
//
// mountImageStream(container, { items, cards, speed, axis, path }) where each item is
// { background, face: { sheet, cell } } and `face` crops one cell of a 3x3 sprite sheet.

const PATH = {
  perspective: 30,
  cardWidth: 18,
  cardHeight: 25,
  cardRadius: 0.4,
  birthHeight: 2.6,
  exitHeight: 46,
  railBirth: -11,
  railExit: 44,
  fan: 3.3,
  turnBirth: 6,
  turnExit: 28,
  stops: 24,
};

let instance = 0;

// Sample the path once so the CSS keyframes trace the real curve.
function keyframes(dir, name, p) {
  const steps = [];
  for (let s = 0; s <= p.stops; s++) {
    const u = s / p.stops;
    const scale = (p.birthHeight / p.cardHeight) * Math.pow(p.exitHeight / p.birthHeight, u);
    const z = p.perspective * (1 - 1 / scale);
    const rail = p.railExit - (p.railExit - p.railBirth) * Math.pow(1 - u, p.fan);
    const turn = p.turnBirth + (p.turnExit - p.turnBirth) * u;
    steps.push(
      `${(u * 100).toFixed(2)}%{transform:translate3d(${(dir * rail).toFixed(2)}cqw,0,${z.toFixed(2)}cqw) rotateY(${(-dir * turn).toFixed(2)}deg)}`,
    );
  }
  return `@keyframes ${name}{${steps.join('')}}`;
}

export function mountImageStream(container, { items, cards = 9, speed = 18, axis = 55, path } = {}) {
  const p = { ...PATH, ...path };
  const id = ++instance;
  const right = `ish-r-${id}`;
  const left = `ish-l-${id}`;
  const cardClass = `ish-c-${id}`;

  container.style.containerType = 'inline-size';

  // Pausing (not disabling) under reduced motion keeps the corridor whole: each card is already
  // dropped mid-flight by its negative delay, so it freezes as a finished still.
  const style = document.createElement('style');
  style.textContent = `${keyframes(1, right, p)}${keyframes(-1, left, p)}@media(prefers-reduced-motion:reduce){.${cardClass}{animation-play-state:paused}}`;

  const scene = document.createElement('div');
  scene.setAttribute('aria-hidden', 'true');
  Object.assign(scene.style, {
    position: 'absolute', inset: '0', pointerEvents: 'none',
    perspective: `${p.perspective}cqw`, perspectiveOrigin: `50% ${axis}%`,
  });
  const world = document.createElement('div');
  Object.assign(world.style, { position: 'absolute', inset: '0', transformStyle: 'preserve-3d' });

  for (const name of [right, left]) {
    for (let i = 0; i < cards; i++) {
      // Both rails walk the same sequence, so the left side mirrors the right at every depth.
      const item = items[i % Math.max(items.length, 1)];
      const card = document.createElement('div');
      card.className = `${cardClass} ish-card`;
      Object.assign(card.style, {
        position: 'absolute', overflow: 'hidden', left: '50%', top: `${axis}%`,
        width: `${p.cardWidth}cqw`, height: `${p.cardHeight}cqw`,
        marginLeft: `${-p.cardWidth / 2}cqw`, marginTop: `${-p.cardHeight / 2}cqw`,
        borderRadius: `${p.cardRadius}cqw`,
        animation: `${name} ${speed}s linear infinite`,
        animationDelay: `${-(i * speed) / cards}s`, // negative delay: the corridor is full on the first frame
        backfaceVisibility: 'hidden',
        background: item?.background ?? '#fff',
      });
      if (item?.face) {
        // A square window onto one cell of the 3x3 sheet, centred in the portrait card.
        const { sheet, cell } = item.face;
        const face = document.createElement('div');
        Object.assign(face.style, {
          position: 'absolute', left: '0', right: '0', top: '50%', aspectRatio: '1', transform: 'translateY(-50%)',
          backgroundImage: `url(${sheet})`, backgroundSize: '300% 300%', backgroundRepeat: 'no-repeat',
          backgroundPosition: `${(cell % 3) * 50}% ${Math.floor(cell / 3) * 50}%`,
        });
        card.append(face);
      }
      world.append(card);
    }
  }

  scene.append(world);
  container.append(style, scene);
  return { destroy() { style.remove(); scene.remove(); } };
}
