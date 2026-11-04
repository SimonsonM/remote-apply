const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat();
let dashboard = null;
let filter = 'all';
let visibleLimit = 30;

function setText(id, value) { $(id).textContent = value; }

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('themeIcon').textContent = theme === 'dark' ? '☼' : '☾';
  $('themeToggle').setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`);
  $('themeToggle').title = `Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`;
  try { localStorage.setItem('ra-theme', theme); } catch (_) {}
  if (dashboard) { renderPipeline(dashboard); renderActivity(dashboard); }
}

const SVG = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs = {}, parent) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.append(el);
  return el;
}
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const shade = (color, pct, to = 'black') => `color-mix(in srgb, ${color} ${pct}%, ${to})`;

function columns3d(container, items, color, { height = 190, gradId = 'col' } = {}) {
  // Isometric 3D columns: lit front, shaded left side, top cap, on a receding floor grid.
  container.replaceChildren();
  const W = 560, H = height, base = H - 22, top = 22, dx = -9, dy = -7;
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart-3d', preserveAspectRatio: 'none' }, container);
  const defs = svgEl('defs', {}, svg);
  const grad = svgEl('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
  svgEl('stop', { offset: '0%', 'stop-color': color }, grad);
  svgEl('stop', { offset: '100%', 'stop-color': shade(color, 55) }, grad);
  const max = Math.max(1, ...items.map(i => i.count));
  const n = items.length, slot = (W - 30) / n, w = Math.min(n > 10 ? 22 : 40, slot * 0.55);
  svgEl('path', { d: `M20 ${base} L${W - 10} ${base} L${W - 10 + dx} ${base + dy} L${20 + dx} ${base + dy} Z`, class: 'floor' }, svg);
  for (let g = 1; g <= 3; g++) {
    const y = base - (base - top) * g / 3;
    svgEl('line', { x1: 20 + dx, y1: y + dy, x2: W - 10 + dx, y2: y + dy, class: 'gridline' }, svg);
  }
  items.forEach((item, i) => {
    const h = item.count ? Math.max(4, (base - top) * item.count / max) : 2;
    const x = 20 + i * slot + (slot - w) / 2, y = base - h;
    const g = svgEl('g', { class: 'bar3d', tabindex: 0, 'aria-label': `${item.label}: ${item.count}` }, svg);
    svgEl('title', {}, g).textContent = `${item.label}: ${item.count}`;
    svgEl('path', { d: `M${x} ${y} L${x + dx} ${y + dy} L${x + dx} ${base + dy} L${x} ${base} Z`, fill: shade(color, 45) }, g);
    svgEl('rect', { x, y, width: w, height: h, fill: `url(#${gradId})` }, g);
    svgEl('path', { d: `M${x} ${y} L${x + dx} ${y + dy} L${x + w + dx} ${y + dy} L${x + w} ${y} Z`, fill: shade(color, 70, 'white') }, g);
    if (item.count) svgEl('text', { x: x + w / 2 + dx / 2, y: y + dy - 5, class: 'bar-value' }, g).textContent = item.count;
    if (item.axis) svgEl('text', { x: x + w / 2, y: base + 16, class: 'bar-axis' }, g).textContent = item.axis;
  });
}

function renderActivity(data) {
  const items = data.activity.map(item => {
    const label = new Date(`${item.date}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    return { label, count: item.count };
  });
  columns3d($('activityChart'), items, cssVar('--accent') || '#86b7ff', { gradId: 'barFront' });
  setText('activityStart', items[0]?.label || '—');
  setText('activityEnd', items.at(-1)?.label || 'Today');
}

function renderPipeline(data) {
  // Tilted 3D donut: outer walls on the near half, inner walls seen through the hole, lit ring on top.
  const c = data.status_counts;
  const groups = [
    ['Manual leads', c.manual || 0, '--amber'],
    ['Needs you', c.needs_you || 0, '--blue'],
    ['Verify submission', c.unconfirmed || 0, '--purple'],
    ['Applied', c.applied || 0, '--teal'],
    ['Ready / CAPTCHA', (c.staged || 0) + (c.captcha || 0), '--accent'],
  ];
  // Broken out like a "pie of pie": the untouched backlog dwarfs everything being worked, so it sits under the wheel.
  const backlog = (c.new || 0) + (c.ready || 0);
  const talking = (c.interview || 0) + (c.offer || 0);
  if (talking) groups.push(['Interview / offer', talking, '--teal']);
  const total = groups.reduce((sum, [, count]) => sum + count, 0);
  setText('donutTotal', number.format(total));
  const host = $('pipelineDonut');
  host.querySelector('svg')?.remove();
  // Stepped 3D donut: every section stands a little taller than the one before it.
  const cx = 120, cy = 96, rx = 108, ry = 56, ir = 0.5, floor = 18, step = 7;
  const svg = svgEl('svg', { viewBox: '0 -6 240 196', class: 'donut-3d' });
  host.prepend(svg);
  svgEl('ellipse', { cx, cy: cy + floor + 10, rx: rx * 0.95, ry: ry * 0.7, class: 'donut-shadow' }, svg);
  const P = (t, r = 1, dz = 0) => [cx + rx * r * Math.cos(t), cy + ry * r * Math.sin(t) + dz];
  const fmt = ([x, y]) => `${x.toFixed(2)} ${y.toFixed(2)}`;
  const slices = [];
  let a = -Math.PI / 2;
  for (const [label, count, variable] of groups) {
    if (!count || !total) continue;
    const b = a + count / total * Math.PI * 2;
    slices.push({ label, count, color: cssVar(variable), a0: a, a1: b, lift: 0 });
    a = b;
  }
  if (!slices.length) slices.push({ label: 'None', count: 0, color: cssVar('--line'), a0: 0, a1: Math.PI * 2, lift: 0 });
  [...slices].sort((p, q) => q.count - p.count).forEach((sl, i) => { sl.lift = i * step; });   // biggest sits lowest, each smaller one a step higher
  const clip = (a0, a1, lo, hi) => {   // overlap of [a0,a1] with [lo,hi] (+2πk)
    const out = [];
    for (const k of [-2, 0, 2]) {
      const s = Math.max(a0, lo + k * Math.PI), e = Math.min(a1, hi + k * Math.PI);
      if (e > s) out.push([s, e]);
    }
    return out;
  };
  const arc = (t0, t1, r, dz, sweep) => `A ${rx * r} ${ry * r} 0 ${Math.abs(t1 - t0) > Math.PI ? 1 : 0} ${sweep} ${fmt(P(t1, r, dz))}`;
  const wall = (s, e, r, top, bottom, fill, parent) =>
    svgEl('path', { d: `M ${fmt(P(s, r, top))} ${arc(s, e, r, top, 1)} L ${fmt(P(e, r, bottom))} ${arc(e, s, r, bottom, 0)} Z`, fill }, parent);
  const cut = (t, top, bottom, fill, parent) =>
    svgEl('path', { d: `M ${fmt(P(t, 1, top))} L ${fmt(P(t, ir, top))} L ${fmt(P(t, ir, bottom))} L ${fmt(P(t, 1, bottom))} Z`, fill }, parent);
  // paint far sections first so nearer, taller ones overlap them
  const order = [...slices].sort((p, q) => Math.sin((p.a0 + p.a1) / 2) - Math.sin((q.a0 + q.a1) / 2));
  for (const sl of order) {
    const top = -sl.lift, bottom = floor;
    const g = svgEl('g', { class: 'slice', tabindex: 0 }, svg);
    svgEl('title', {}, g).textContent = `${sl.label}: ${number.format(sl.count)}`;
    for (const [s, e] of clip(sl.a0, sl.a1, Math.PI, 2 * Math.PI)) wall(s, e, ir, top, bottom, shade(sl.color, 36), g);  // inner wall seen through the hole
    const full = sl.a1 - sl.a0 >= Math.PI * 2 - 1e-6;
    if (!full) {
      cut(sl.a0, top, bottom, shade(sl.color, 48), g);   // exposed radial faces where the steps change height
      cut(sl.a1, top, bottom, shade(sl.color, 48), g);
    }
    for (const [s, e] of clip(sl.a0, sl.a1, 0, Math.PI)) wall(s, e, 1, top, bottom, shade(sl.color, 60), g);       // outer wall, near half
    const a1 = full ? sl.a1 - 1e-4 : sl.a1;
    svgEl('path', { d: `M ${fmt(P(sl.a0, 1, top))} ${arc(sl.a0, a1, 1, top, 1)} L ${fmt(P(a1, ir, top))} ${arc(a1, sl.a0, ir, top, 0)} Z`,
                    fill: sl.color, class: 'slice-top' }, g);
    const mid = (sl.a0 + sl.a1) / 2;
    g.style.setProperty('--lift-x', `${(Math.cos(mid) * 5).toFixed(1)}px`);
    g.style.setProperty('--lift-y', `${(Math.sin(mid) * 3 - 4).toFixed(1)}px`);
  }
  host.setAttribute('aria-label', `${number.format(total)} active jobs: ${groups.map(([label, count]) => `${label} ${count}`).join(', ')}`);
  const legend = $('pipelineLegend');
  legend.replaceChildren();
  for (const [label, count, variable] of groups) {
    if (!count && label === 'Interview / offer') continue;
    const row = document.createElement('div');
    row.className = 'legend-row';
    const dot = document.createElement('span');
    dot.className = 'legend-dot';
    dot.style.background = `var(${variable})`;
    const name = document.createElement('span');
    name.textContent = label;
    const value = document.createElement('strong');
    value.textContent = number.format(count);
    row.append(dot, name, value);
    legend.append(row);
  }
  const back = document.createElement('div');
  back.className = 'backlog';
  const bar = document.createElement('div');
  bar.className = 'backlog-bar bar3d-h';
  bar.style.width = `${Math.min(100, 30 + 70 * backlog / Math.max(backlog, total * 4))}%`;
  const txt = document.createElement('span');
  txt.innerHTML = '';
  txt.append(Object.assign(document.createElement('strong'), { textContent: number.format(backlog) }), ' in the backlog, not yet worked (kept out of the wheel)');
  back.append(bar, txt);
  legend.append(back);
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function rateTable(rows, title) {
  const box = el('div', 'rate-table');
  box.append(el('h3', '', title));
  const max = Math.max(1, ...rows.map(r => r.response_pct));
  for (const r of rows) {
    const row = el('div', 'rate-row');
    const bar = el('div', 'rate-bar');
    const fill = el('i', 'bar3d-h');
    fill.style.width = `${Math.max(2, r.response_pct / max * 100)}%`;
    bar.append(fill);
    row.append(el('span', 'rate-label', r.label), bar, el('strong', '', `${r.response_pct.toFixed(1)}%`), el('small', '', `${r.applied} sent`));
    box.append(row);
  }
  return box;
}

function renderInsights(data) {
  const ins = data.insights;
  const host = $('insightsBody');
  host.replaceChildren();
  if (!ins) return;
  const kpis = el('div', 'insight-kpis');
  const kpi = (label, value, note) => { const k = el('div', 'outcome'); k.append(el('span', '', label), el('strong', '', value), el('small', '', note)); kpis.append(k); };
  kpi('Median days to a rejection', ins.median_days_to_rejection ?? '—', ins.fastest_rejection_days != null ? `fastest: ${ins.fastest_rejection_days} days` : 'no rejections yet');
  kpi('Silent 14+ days', number.format(ins.silent_14d), `${ins.silent_14d_pct}% of open applications`);
  const talks = ins.by_level.reduce((n, r) => n + r.interviews, 0);
  const pool = [...ins.by_level].filter(r => r.applied >= 10);
  const pick = talks ? pool.sort((a, b) => b.interviews / b.applied - a.interviews / a.applied)[0]
                     : pool.sort((a, b) => b.rejected / b.applied - a.rejected / a.applied)[0];
  kpi(talks ? 'Best interview rate' : 'Most rejected level', pick ? pick.label : '—',
      pick ? (talks ? `${pick.interviews} interviews from ${pick.applied}` : `${(100 * pick.rejected / pick.applied).toFixed(1)}% rejected`) : 'need 10+ applications');
  kpis.lastChild.classList.add('text-kpi');
  kpi('Days from posting to applying', ins.median_days_posted_to_applied ?? '—',
      ins.applied_within_2_days_pct != null ? `${ins.applied_within_2_days_pct}% sent within 48 hours` : 'no posting dates yet');
  host.append(kpis);
  // funnel
  const funnel = el('div', 'funnel');
  const fmax = Math.max(1, ...ins.funnel.map(f => f.count));
  ins.funnel.forEach((f, i) => {
    const step = el('div', 'funnel-step');
    const bar = el('div', 'funnel-bar bar3d-h');
    bar.style.width = `${Math.max(4, Math.sqrt(f.count / fmax) * 100)}%`;
    bar.style.setProperty('--i', i);
    bar.append(el('span', '', number.format(f.count)));
    const prev = i ? ins.funnel[i - 1].count : 0;
    step.append(el('span', 'funnel-label', f.label), bar, el('small', '', i && prev ? `${(100 * f.count / prev).toFixed(1)}% of previous` : ''));
    funnel.append(step);
  });
  host.append(funnel);
  const grid = el('div', 'rate-grid');
  grid.append(rateTable(ins.top_titles || [], 'Most applied-for roles (response rate)'), rateTable(ins.by_level, 'Response rate by seniority'), rateTable(ins.by_source, 'By application system'),
              rateTable(ins.by_fit, 'By match score'), rateTable(ins.by_mode, 'Remote vs local'),
              rateTable(ins.by_resume || [], 'By resume version'));
  host.append(grid);
  if ((ins.keyword_gaps || []).length) {
    const gaps = el('div', 'rate-table gaps');
    gaps.append(el('h3', '', 'Phrases your postings use that your resume never does (add only what is true for you)'));
    const chips = el('div', 'gap-chips');
    for (const g of ins.keyword_gaps) chips.append(el('span', 'geo-chip', `${g.phrase} · ${g.pct}%`));
    gaps.append(chips);
    host.append(gaps);
  }
  const foot = el('div', 'insight-foot');
  const wd = el('div', 'weekday');
  wd.append(el('h3', '', 'Applications by weekday'));
  const wrow = el('div', 'weekday-chart');
  columns3d(wrow, ins.weekdays.map(w => ({ label: w.day, axis: w.day, count: w.count })), cssVar('--purple') || '#b9a4e3', { height: 150, gradId: 'wkFront' });
  wd.append(wrow);
  const rej = el('div', 'rejecters');
  rej.append(el('h3', '', 'Most rejections from'));
  for (const r of ins.top_rejecters) {
    const row = el('div', 'legend-row');
    row.append(el('span', '', r.company), el('strong', '', r.count));
    rej.append(row);
  }
  foot.append(wd, rej);
  host.append(foot);
}

function renderOutreach(data) {
  const host = $('outreachBody');
  host.replaceChildren();
  const items = data.outreach || [];
  setText('sideOutreachCount', number.format(items.length));
  if (!items.length) {
    host.append(el('p', 'muted', 'No drafts waiting. They are written after each nightly run: a note for applications with a 70+ match from the last 3 days, and a follow-up once an application has been quiet for its follow-up date.'));
    return;
  }
  const list = el('div', 'threat-list');
  for (const it of items) {
    const card = el('article', `threat-card outreach ${it.kind}`);
    const head = el('div', 'threat-head');
    head.append(el('span', 'threat-level', it.kind === 'recruiter' ? 'INTRO NOTE' : 'FOLLOW-UP'),
                el('strong', '', `${it.company} · ${it.title}`), el('small', '', `applied ${it.applied_at || '—'} · fit ${it.fit}`));
    const body = el('pre', 'draft-text', it.text);
    const actions = el('div', 'auth-badges');
    const btn = (label, fn, cls = 'badge info') => { const b = el('button', cls, label); b.type = 'button'; b.addEventListener('click', fn); actions.append(b); return b; };
    const copy = btn('Copy text', async () => { try { await navigator.clipboard.writeText(it.text); copy.textContent = 'Copied'; } catch (_) { copy.textContent = 'Select and copy'; } });
    for (const [label, href] of [['Find the recruiter', it.recruiter], ['Find the hiring manager', it.hiring_manager], ['Job posting', it.job_url]]) {
      const a = el('a', 'badge info', label); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; actions.append(a);
    }
    const mark = (status) => async () => {
      try { await fetch(`/api/outreach/${it.id}/${status}`, { method: 'POST' }); card.remove(); } catch (_) {}
    };
    btn('Mark sent', mark('sent'), 'badge ok');
    btn('Skip', mark('skipped'), 'badge none');
    card.append(head, body, actions);
    list.append(card);
  }
  host.append(list);
}

function renderThreats(data) {
  const t = data.threats;
  const host = $('threatsBody');
  host.replaceChildren();
  if (!t || !t.scanned) {
    host.append(el('p', 'muted', 'The scam scanner has not run yet. It runs with the hourly mail sort.'));
    return;
  }
  setText('sideThreatCount', number.format(t.high + t.suspicious));
  const kpis = el('div', 'insight-kpis threat-kpis');
  const kpi = (label, value, note, cls = '') => { const k = el('div', `outcome ${cls}`); k.append(el('span', '', label), el('strong', '', value), el('small', '', note)); kpis.append(k); };
  kpi('Messages scanned', number.format(t.scanned), 'job-related mail, inbox + folders');
  kpi('High risk', number.format(t.high), 'score 60+', t.high ? 'danger' : '');
  kpi('Suspicious', number.format(t.suspicious), 'score 30–59', t.suspicious ? 'warn' : '');
  kpi('Passed SPF + DKIM', `${t.scanned ? (100 * t.auth_pass / t.scanned).toFixed(1) : 0}%`, `${number.format(t.auth_weak)} with weak or failed auth`);
  host.append(kpis);
  if (t.countries.length) {
    const geo = el('div', 'geo-strip');
    geo.append(el('span', 'geo-title', 'Sending networks by country'));
    for (const c of t.countries) geo.append(el('span', 'geo-chip', `${c.country} · ${c.count}`));
    host.append(geo);
  }
  const list = el('div', 'threat-list');
  if (!t.items.length) list.append(el('p', 'muted', 'Nothing suspicious found. Every scanned message passed.'));
  for (const it of t.items) {
    const card = el('article', `threat-card ${it.level}`);
    const head = el('div', 'threat-head');
    head.append(el('span', `threat-level ${it.level}`, `${it.level === 'high' ? 'HIGH' : 'SUSPICIOUS'} · ${it.score}`),
                el('strong', '', it.from_addr || it.from_domain), el('small', '', (it.received_at || '').slice(0, 10)));
    const subj = el('div', 'threat-subject', it.subject || '(no subject)');
    const auth = el('div', 'auth-badges');
    for (const [k, v] of [['SPF', it.spf], ['DKIM', it.dkim], ['DMARC', it.dmarc]]) {
      auth.append(el('span', `badge ${v === 'pass' ? 'ok' : v ? 'bad' : 'none'}`, `${k} ${v || '—'}`));
    }
    if (it.ip) auth.append(el('span', 'badge info', `${it.ip}${it.ip_country ? ' · ' + it.ip_country : ''}${it.ip_org ? ' · ' + it.ip_org : ''}`));
    if (it.domain_age_days != null) auth.append(el('span', `badge ${it.domain_age_days < 365 ? 'bad' : 'info'}`, `domain ${it.domain_age_days} days old`));
    const why = el('ul', 'threat-reasons');
    for (const r of it.reasons) why.append(el('li', '', r));
    card.append(head, subj, auth, why);
    list.append(card);
  }
  host.append(list);
}

function jobCard(job) {
  const card = document.createElement('article');
  card.className = 'job-card';
  const main = document.createElement('div');
  main.className = 'job-main';
  const topline = document.createElement('div');
  topline.className = 'job-topline';
  const company = document.createElement('span');
  company.className = 'job-company';
  company.textContent = job.company || 'Unknown company';
  const fit = document.createElement('span');
  fit.className = 'job-fit';
  fit.textContent = `${number.format(job.fit)} fit`;
  topline.append(company, fit);
  const title = document.createElement('h3');
  title.className = 'job-title';
  title.textContent = job.title || 'Untitled role';
  const meta = document.createElement('div');
  meta.className = 'job-meta';
  for (const value of [job.location, job.salary, job.source]) {
    if (!value) continue;
    const span = document.createElement('span');
    span.textContent = value;
    meta.append(span);
  }
  const note = document.createElement('p');
  note.className = 'job-note';
  const strong = document.createElement('strong');
  strong.textContent = job.action_label;
  note.append(strong, document.createTextNode(` — ${job.guidance}`));
  if (job.blockers) note.append(document.createTextNode(` · ${job.blockers} open blocker${job.blockers === 1 ? '' : 's'}`));
  main.append(topline, title, meta, note);
  if (job.notes && job.notes.trim()) {
    const details = document.createElement('details');
    details.className = 'job-details';
    const summary = document.createElement('summary');
    summary.textContent = 'Show application note';
    const explanation = document.createElement('p');
    explanation.textContent = job.notes;
    details.append(summary, explanation);
    main.append(details);
  }
  const side = document.createElement('div');
  side.className = 'job-side';
  const status = document.createElement('span');
  status.className = `job-status ${job.status}`;
  status.textContent = {staged:'Ready to submit',captcha:'CAPTCHA',needs_you:'Needs you',unconfirmed:'Verify',manual:'Manual'}[job.status] || job.status;
  const link = document.createElement('a');
  link.className = 'job-link';
  if (job.link) {
    link.href = job.link;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'Open application ↗';
    link.setAttribute('aria-label', `Open application for ${job.title || 'job'} at ${job.company || 'employer'} in a new tab`);
  } else {
    link.classList.add('disabled');
    link.textContent = 'No link available';
    link.setAttribute('aria-disabled', 'true');
  }
  side.append(status, link);
  card.append(main, side);
  return card;
}

function renderJobs() {
  if (!dashboard) return;
  const query = $('searchInput').value.trim().toLocaleLowerCase();
  const jobs = dashboard.jobs.filter(job =>
    (filter === 'all' || job.status === filter) &&
    (!query || [job.company, job.title, job.location, job.source].some(value => (value || '').toLocaleLowerCase().includes(query))));
  const list = $('jobList');
  const fragment = document.createDocumentFragment();
  for (const job of jobs.slice(0, visibleLimit)) fragment.append(jobCard(job));
  list.replaceChildren(fragment);
  $('emptyState').hidden = jobs.length !== 0;
  $('loadMoreButton').hidden = jobs.length <= visibleLimit;
  setText('resultsText', `Showing ${number.format(Math.min(visibleLimit, jobs.length))} of ${number.format(jobs.length)} matching jobs`);
}

function renderOutcomes(data) {
  const o = data.outcomes || {};
  const n = (v) => number.format(v || 0);
  const pct = (v) => `${(v || 0).toFixed(1)}%`;
  setText('outSubmitted', n(o.submitted));
  setText('outRejected', n(o.rejected));
  setText('outRejectedNote', `${pct(o.rejected_pct)} of applied · ${n(o.rejected_week)} this week`);
  setText('outResponse', pct(o.response_pct));
  setText('outResponseNote', `${n(o.responded)} heard back`);
  setText('outInterviews', n(o.interviews));
  setText('outInterviewsNote', `${pct(o.interview_pct)} of applied`);
  setText('outAwaiting', n(o.awaiting));
  setText('outAwaitingNote', `${pct(o.awaiting_pct)} · ${n(o.no_reply_30d)} silent 30+ days`);
  const bar = $('outcomeBar');
  bar.replaceChildren();
  for (const [value, color] of [[o.rejected_pct, '--danger'], [o.interview_pct, '--teal'], [o.awaiting_pct, '--faint']]) {
    const seg = document.createElement('div');
    seg.style.width = `${value || 0}%`;
    seg.style.background = `var(${color})`;
    bar.append(seg);
  }
}

function render(data) {
  dashboard = data;
  const c = data.status_counts;
  setText('metricAction', number.format(data.totals.action));
  setText('metricApplied', number.format(data.totals.applied));
  setText('metricInterviews', number.format(data.totals.interviews + data.totals.offers));
  setText('metricTracked', number.format(data.totals.tracked));
  setText('sideActionCount', number.format(data.totals.action));
  setText('queueCount', number.format(data.totals.action));
  for (const [id, value] of Object.entries({filterAllCount:data.jobs.length,filterStagedCount:c.staged || 0,
    filterCaptchaCount:c.captcha || 0,filterNeedsCount:c.needs_you || 0,
    filterUnconfirmedCount:c.unconfirmed || 0,filterManualCount:c.manual || 0})) setText(id, number.format(value));
  setText('dataStatus', data.stale_since
    ? `An application run is using the database right now. Showing data from ${data.stale_since}; refresh after the run finishes.`
    : data.database_found ? 'Connected to your local applications database' : 'No applications database found yet');
  setText('updatedTime', `Updated ${new Date().toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'})}`);
  renderOutcomes(data);
  renderActivity(data);
  renderPipeline(data);
  renderInsights(data);
  renderOutreach(data);
  renderThreats(data);
  renderJobs();
}

async function refresh() {
  $('refreshButton').classList.add('loading');
  $('refreshButton').disabled = true;
  try {
    const response = await fetch('/api/dashboard', {cache:'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    render(data);
    setText('firstName', data.first_name ? `, ${data.first_name}` : '');
  } catch (error) {
    setText('dataStatus', `Could not load the database: ${error.message}`);
    if (!dashboard) setText('resultsText', 'Data unavailable');
  } finally {
    $('refreshButton').classList.remove('loading');
    $('refreshButton').disabled = false;
  }
}

const hour = new Date().getHours();
setText('greeting', hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening');
setTheme(document.documentElement.dataset.theme);
$('themeToggle').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
$('refreshButton').addEventListener('click', refresh);
$('searchInput').addEventListener('input', () => { visibleLimit = 30; renderJobs(); });
$('filters').addEventListener('click', event => {
  const button = event.target.closest('button[data-filter]');
  if (!button) return;
  filter = button.dataset.filter;
  visibleLimit = 30;
  for (const item of $('filters').querySelectorAll('button')) item.classList.toggle('active', item === button);
  renderJobs();
});
$('backToTop').addEventListener('click', () => window.scrollTo({top:0,behavior:'smooth'}));

// Each workspace is its own page; the sidebar switches between them by hash (#activity, #threats, ...).
const PAGES = { 'overview': 'Overview', 'action-queue': 'Action queue', 'insights': 'Market insights', 'outreach': 'Outreach', 'threats': 'Threats' };
function route() {
  const page = PAGES[location.hash.slice(1)] ? location.hash.slice(1) : 'overview';   // #activity now lives on the overview
  document.querySelectorAll('[data-page]').forEach(node => { node.hidden = node.dataset.page !== page; });
  document.querySelectorAll('.nav-link').forEach(a => a.classList.toggle('active', a.getAttribute('href') === `#${page}`));
  setText('crumb', PAGES[page]);
  document.title = `Remote Apply · ${PAGES[page]}`;
  window.scrollTo(0, 0);
}
window.addEventListener('hashchange', route);
route();
$('loadMoreButton').addEventListener('click', () => { visibleLimit += 30; renderJobs(); });
refresh();
