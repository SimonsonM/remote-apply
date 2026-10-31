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
  if (dashboard) renderPipeline(dashboard);
}

function renderActivity(data) {
  const chart = $('activityChart');
  chart.replaceChildren();
  const max = Math.max(1, ...data.activity.map(item => item.count));
  const dates = [];
  for (const item of data.activity) {
    const date = new Date(`${item.date}T12:00:00`);
    const label = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    dates.push(label);
    const slot = document.createElement('div');
    slot.className = 'bar-slot';
    slot.tabIndex = 0;
    slot.setAttribute('aria-label', `${label}: ${item.count} recorded applications`);
    const bar = document.createElement('div');
    bar.className = 'bar';
    bar.style.height = `${Math.max(3, (item.count / max) * 100)}%`;
    const tooltip = document.createElement('span');
    tooltip.className = 'bar-tooltip';
    tooltip.textContent = `${label}: ${item.count}`;
    slot.append(bar, tooltip);
    chart.append(slot);
  }
  setText('activityStart', dates[0] || '—');
  setText('activityEnd', dates.at(-1) || 'Today');
}

function renderPipeline(data) {
  const c = data.status_counts;
  const groups = [
    ['Manual leads', c.manual || 0, '--amber'],
    ['Needs you', c.needs_you || 0, '--blue'],
    ['Verify submission', c.unconfirmed || 0, '--purple'],
    ['Applied', c.applied || 0, '--teal'],
    ['Ready / CAPTCHA', (c.staged || 0) + (c.captcha || 0), '--accent'],
    ['Other active', (c.new || 0) + (c.ready || 0) + (c.interview || 0) + (c.offer || 0), '--muted'],
  ];
  const total = groups.reduce((sum, [, count]) => sum + count, 0);
  setText('donutTotal', number.format(total));
  const styles = getComputedStyle(document.documentElement);
  const sections = [];
  let offset = 0;
  for (const [, count, variable] of groups) {
    if (!count || !total) continue;
    const next = offset + count / total * 100;
    sections.push(`${styles.getPropertyValue(variable).trim()} ${offset}% ${next}%`);
    offset = next;
  }
  $('pipelineDonut').style.background = sections.length ? `conic-gradient(${sections.join(',')})` : 'var(--line)';
  $('pipelineDonut').setAttribute('aria-label', `${number.format(total)} active jobs: ${groups.map(([label, count]) => `${label} ${count}`).join(', ')}`);
  const legend = $('pipelineLegend');
  legend.replaceChildren();
  for (const [label, count, variable] of groups) {
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
  setText('dataStatus', data.database_found ? 'Connected to your local applications database' : 'No applications database found yet');
  setText('updatedTime', `Updated ${new Date().toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'})}`);
  renderActivity(data);
  renderPipeline(data);
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
$('loadMoreButton').addEventListener('click', () => { visibleLimit += 30; renderJobs(); });
refresh();
