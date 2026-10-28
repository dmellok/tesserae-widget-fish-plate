// Fish Plate: the fishes of the waters around the panel as a natural-history plate.
//
// server.py hands over the species rows (name, scientific name, count, latest record,
// a picture as a data URI with its credit) and the plate's words; this file lays them
// out on paper. Two layouts: the stacked plate (fishes one above another, a name line
// under each, a scale bar at the foot) and the hero (one fish large inside a double
// ruled frame, the next two small in the lower corners). The paper colour is literal,
// as a printed page is; the ink is near-black, pure black on a panel without colour.
//
// Nothing is fetched here and nothing animates: the render is idempotent.

const PAPER = { cream: '#f3ead6', white: '#ffffff', aged: '#e9dcc0' };
// A fish row needs about this much height before the plate drops a species rather than
// shrink every fish to a thumbnail; the foot and head take about a quarter of the cell.
const ROW_MIN_PX = 100;
const HEAD_SHARE = 0.17;
const FOOT_SHARE = 0.1;

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function num(n) {
  const v = Number(n) || 0;
  return v.toLocaleString('en-US');
}

/* How many fishes the stacked plate shows: the user's count, cut to what the cell can
   hold at a readable size, and one at sm. */
function rowsFor(d, size, w, h) {
  const n = d.species.length;
  if (size === 'sm') return Math.min(1, n);
  const avail = h * (1 - HEAD_SHARE - (d.credits ? FOOT_SHARE : FOOT_SHARE * 0.6));
  const fit = Math.max(1, Math.floor(avail / ROW_MIN_PX));
  return Math.max(1, Math.min(n, fit));
}

function picture(s, cls) {
  const p = s.pic;
  if (!p || !p.src) return `<div class="${cls} fp-nopic"><i class="ph ph-fish" aria-hidden="true"></i></div>`;
  return `<div class="${cls} ${p.kind === 'painting' ? 'is-painting' : 'is-photo'}"><img src="${p.src}" alt="${esc(s.name)}" decoding="sync"></div>`;
}

function nameLine(s) {
  const sci = s.sci && s.sci !== s.name ? ` <span class="fp-dot">·</span> <span class="fp-sci">${esc(s.sci)}</span>` : '';
  return `<span class="fp-common">${esc(s.name)}</span>${sci}`;
}

function creditsLine(d, shown) {
  if (!d.credits) return '';
  const photos = shown.filter((s) => s.pic && s.pic.kind === 'photo');
  const paintings = shown.filter((s) => s.pic && s.pic.kind === 'painting');
  const bits = [];
  if (photos.length && !paintings.length) {
    const who = photos.map((s) => String(s.pic.credit || '').replace(/^Photo by /, '').replace(/, iNaturalist$/, ''));
    const list = who.length > 1 ? `${who.slice(0, -1).join(', ')} and ${who[who.length - 1]}` : who[0];
    bits.push(`Photograph${who.length > 1 ? 's' : ''} by ${list}, iNaturalist`);
  } else {
    for (const s of shown) if (s.pic && s.pic.credit) bits.push(`${s.name}: ${s.pic.credit}`);
  }
  return bits.join('; ');
}

/* The head: the title on one line, the plate number top right where the cell is wide
   enough and in the subtitle line where it is not (a 600 px portrait cell, an md cell). */
function head(d, size, w) {
  const corner = size !== 'sm' && w >= 700;
  const bits = [d.subtitle, corner ? '' : d.plate].filter(Boolean);
  const sub = size === 'sm' || !bits.length ? '' : `<div class="fp-sub">${bits.map(esc).join(' · ')}</div>`;
  const plate = corner || size === 'sm' ? `<div class="fp-plate">${esc(d.plate || '')}</div>` : '';
  return `<header class="fp-head${corner || size === 'sm' ? ' has-corner' : ''}">
    ${plate}
    <h1 class="fp-title">${esc(d.title || 'Fishes')}</h1>${sub}
  </header>`;
}

function foot(d, shown, size) {
  if (size === 'sm') return '';
  const notes = [...(d.status || []), ...(d.notices || [])].map(esc).join(' · ');
  const credits = creditsLine(d, shown);
  const small = [credits, notes].filter(Boolean).join(' · ');
  return `<footer class="fp-foot">
    <div class="fp-scale" aria-hidden="true"><span class="fp-rule"></span><span class="fp-scale-txt">${esc(d.foot || '')}</span><span class="fp-rule"></span></div>
    ${small ? `<div class="fp-credits">${small}</div>` : ''}
  </footer>`;
}

/* A landscape cell that would leave each stacked fish under two fifths of the height
   puts the fishes side by side instead: two or three across, four as two rows of two,
   five as a row of three over a row of two. Portrait cells and sm keep the stack. */
function sideBySide(d, size, w, h) {
  if (size === 'sm' || w <= h) return 0;
  const n = Math.min(d.species.length, 5);
  if (n < 2) return 0;
  const avail = h * (1 - HEAD_SHARE - FOOT_SHARE);
  return avail / n < 0.4 * h ? n : 0;
}

/* The row arrangement for a wide cell. A single row is kept when it fills the height
   (two fishes, or a very wide cell); when its pictures, each limited to a column's
   width, would fill less than two thirds of the room, the fishes go in two rows and
   each row's pictures are sized by the height it has: three as two over one (the lone
   one centred), four as two over two, five as three over two. */
const PHOTO_ASPECT = 5 / 3;

function rowPlan(shown, w, h) {
  const n = shown.length;
  const cq = Math.min(w, h);
  const gapX = Math.min(36, Math.max(8, 0.03 * cq));
  const bodyW = w - 2 * Math.min(56, Math.max(10, 0.05 * cq));
  const avail = h * (1 - HEAD_SHARE - FOOT_SHARE);
  const nameFont = Math.max(12, Math.min(26, 0.026 * cq));
  const caption = 2.4 * nameFont;
  const aspect = (s) => (s.pic && s.pic.kind === 'painting' && Number(s.pic.aspect) > 0 ? Number(s.pic.aspect) : PHOTO_ASPECT);
  const plan = (cols, rows) => {
    const cw = (bodyW - gapX * (cols - 1)) / cols;
    const pich = Math.max(40, Math.floor(avail / rows - caption - (rows > 1 ? 12 : 0)));
    const fill = shown.reduce((sum, s) => sum + Math.min(pich, cw / aspect(s)), 0) / (n * pich);
    return { cols, rows, cw: Math.floor(cw), pich, fill };
  };
  const single = plan(n, 1);
  if (n <= 2 || single.fill >= 0.667) return single;
  return plan(n === 5 ? 3 : 2, 2);
}

function stacked(d, size, w, h) {
  const across = sideBySide(d, size, w, h);
  if (across) {
    const shown = d.species.slice(0, across);
    const plan = rowPlan(shown, w, h);
    const n = shown.length;
    // five fishes sit on a six-track grid (each spanning two) so the second row of two
    // centres under the three; three in two rows put the lone fish centred, column wide
    const six = plan.rows === 2 && n === 5;
    const tracks = six ? 6 : plan.cols;
    const cells = shown
      .map((s, i) => {
        let style = '';
        if (six) style = i >= 3 ? ` style="grid-column:${(i - 3) * 2 + 2} / span 2"` : ' style="grid-column:span 2"';
        else if (plan.rows === 2 && n === 3 && i === 2) style = ` style="grid-column:1 / span 2;width:${plan.cw}px;justify-self:center"`;
        const name = `<span class="fp-common">${esc(s.name)}</span>${s.sci && s.sci !== s.name ? `<span class="fp-sci">${esc(s.sci)}</span>` : ''}`;
        return `<figure class="fp-fish"${style}>${picture(s, 'fp-pic')}<figcaption class="fp-name fp-name-2">${name}</figcaption></figure>`;
      })
      .join('');
    return `${head(d, size, w)}<main class="fp-row c${plan.cols}" style="--tracks:${tracks};--rows:${plan.rows};--pich:${plan.pich}px">${cells}</main>${foot(d, shown, size)}`;
  }
  const n = rowsFor(d, size, w, h);
  const shown = d.species.slice(0, n);
  const rows = shown
    .map(
      (s) => `<figure class="fp-fish">${picture(s, 'fp-pic')}<figcaption class="fp-name">${nameLine(s)}</figcaption></figure>`,
    )
    .join('');
  return `${head(d, size, w)}<main class="fp-stack n${n}" style="--n:${n}">${rows}</main>${foot(d, shown, size)}`;
}

function hero(d, size) {
  const [first, ...rest] = d.species;
  const corners = size === 'sm' ? [] : rest.slice(0, 2);
  const shown = [first, ...corners];
  const cornerFish = (s, side) =>
    s
      ? `<figure class="fp-cfish ${side}">${picture(s, 'fp-cpic')}<figcaption>${esc(s.name)}</figcaption></figure>`
      : `<div class="fp-cfish ${side} empty"></div>`;
  const meta =
    size === 'sm'
      ? ''
      : `<div class="fp-hmeta"><div class="fp-hwaters">${esc(d.waters || d.area || '')}</div><div class="fp-hcount">${num(first.count)} record${first.count === 1 ? '' : 's'}${first.last ? ` · last record ${esc(first.last)}` : ''}</div></div>`;
  const notes = [...(d.status || []), ...(d.notices || [])].map(esc).join(' · ');
  const credits = size === 'sm' ? '' : [creditsLine(d, shown), notes].filter(Boolean).join(' · ');
  return `<div class="fp-frame">
    <i class="fp-cnr tl"></i><i class="fp-cnr tr"></i><i class="fp-cnr bl"></i><i class="fp-cnr br"></i>
    <div class="fp-hplate">${esc(d.plate || '')}</div>
    ${picture(first, 'fp-hpic')}
    <h1 class="fp-hname">${esc(first.name)}</h1>
    ${first.sci && first.sci !== first.name ? `<div class="fp-hsci">${esc(first.sci)}</div>` : ''}
    <div class="fp-hfoot">${cornerFish(corners[0], 'l')}${meta}${cornerFish(corners[1], 'r')}</div>
    ${credits ? `<div class="fp-hcredits">${credits}</div>` : ''}
  </div>`;
}

function message(d, icon, title, text) {
  return `<div class="fp-msg">
    <i class="ph ${icon}" aria-hidden="true"></i>
    <div class="fp-msg-t">${esc(title)}</div>
    <div class="fp-msg-b">${esc(text)}</div>
  </div>`;
}

export default async function render(shadow, ctx) {
  const d = { ...((ctx && ctx.data) || {}) };
  d.species = Array.isArray(d.species) ? d.species : [];
  const size = (ctx && ctx.cell && ctx.cell.size) || 'md';
  const w = (ctx && ctx.cell && ctx.cell.w) || 640;
  const h = (ctx && ctx.cell && ctx.cell.h) || 400;
  const paper = PAPER[d.paper] || PAPER.cream;
  const layout = d.layout === 'hero' ? 'hero' : 'stack';
  let body;
  if (!d.state) body = message(d, 'ph-fish', d.title || 'Fish Plate', 'No data from the server yet.');
  else if (d.state === 'error') body = message(d, 'ph-warning-circle', d.title || 'Fish Plate', d.error || 'Something went wrong.');
  else if (d.state === 'empty' || !d.species.length) body = message(d, 'ph-fish', d.title || 'Fish Plate', d.error || 'No fishes recorded here yet.');
  else if (layout === 'hero') body = hero(d, size);
  else body = stacked(d, size, w, h);
  shadow.innerHTML = `
    <link rel="stylesheet" href="/static/style/spectra-widgets.css">
    <link rel="stylesheet" href="/static/icons/phosphor/regular/style.css">
    <link rel="stylesheet" href="/plugins/fish_plate/client.css">
    <div class="fp size-${esc(size)} lay-${layout} lv-${esc(d.panel || 'colour')}${w > h ? ' wide' : ' tall'}" data-widget="fish_plate" style="--paper:${paper}">${body}</div>`;
}
