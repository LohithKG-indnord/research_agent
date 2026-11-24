const $ = s => document.querySelector(s);
const form = $('#run-form'), status = $('#status'), log = $('#log');
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const safeUrl = v => { try { const u = new URL(v); return /^https?:$/.test(u.protocol) ? u.href : '#'; } catch { return '#'; } };
const host = v => { try { return new URL(v).hostname.replace(/^www\./, ''); } catch { return 'source'; } };
const LABELS = { problem_fit: 'Has the problem', profile_fit: 'ICP fit', timing: 'Buying signals', role_clarity: 'Buyer visible', evidence_quality: 'Evidence quality' };
// The model cites its numbered sources ("[S4]", "(S3, S6)"); those numbers mean nothing on screen.
const clean = t => String(t ?? '').replace(/\s*[[(]\s*S\d+(?:\s*[-–,;]\s*S?\d+)*\s*[\])]/g, '').replace(/[,;]\s*S\d+(?:\s*[-–,;]\s*S?\d+)*(?=\))/g, '');
const list = items => items?.length ? `<ul>${items.map(x => `<li>${esc(clean(x))}</li>`).join('')}</ul>` : '<p class="mini">None found.</p>';
const FINANCIALS = { revenue: 'Revenue', growth: 'Revenue growth', profit: 'Profit / loss', funding: 'Funding', employees: 'Employees' };
const figure = f => f ? one(f) + (f.others.length ? `<div class="mini differs">Other figures found: ${f.others.map(one).join('; ')}</div>` : '') : '<span class="mini">Unknown</span>';
const one = f => `<b>${esc(f.value)}</b> <span class="mini">${esc(f.period === 'Unknown' ? 'year unknown' : f.period)}${f.basis === 'reported' ? '' : ` · ${esc(f.basis)}`} · ${esc(f.entity)} · <a href="${safeUrl(f.url)}" title="${esc(f.quote)}" target="_blank" rel="noreferrer">${esc(host(f.url))}</a></span>`;
let lastResult = null;
const live = [];
let config = { people: [], max_people: 5 };

fetch('/api/config').then(r => r.json()).then(async c => {
  config = c;
  // Bring back the last result after a reload so it can still be reviewed.
  const latest = await fetch('/api/latest').then(r => r.json());
  if (/^named-v([3-9]|\d{2,})$/.test(latest?.run?.prompt_version || '') &&!live.length && !lastResult && !form.querySelector('button').disabled) { lastResult = latest; renderResult(latest); }
  if (c.product) $('#product').value = c.product;
  if (c.criteria) $('#criteria').value = c.criteria;
  if (!c.ready) status.textContent = 'Add ANTHROPIC_API_KEY to .env and restart the server to run the agent.';
}).catch(() => {});

form.addEventListener('submit', async e => {
  e.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true; live.length = 0; lastResult = null;
  log.replaceChildren(); log.hidden = false; $('#results').hidden = true;
  status.textContent = 'Starting…';
  try {
    const body = Object.fromEntries(new FormData(form));
    const file = $('#product-file').files[0];
    if (file) {
      if (file.size > 10e6) throw new Error('Upload a file under 10 MB.');
      const url = await new Promise((ok, fail) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = () => fail(r.error); r.readAsDataURL(file); });
      body.product_file = { name: file.name, data: url.slice(url.indexOf(',') + 1) };
    }
    const res = await fetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok || !res.body) throw new Error(`Server returned ${res.status}`);
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      const lines = buffer.split('\n'); buffer = lines.pop();
      for (const line of lines) if (line.trim()) handle(JSON.parse(line));
    }
  } catch (err) {
    status.textContent = err.message;
  } finally { button.disabled = false; }
});

function handle(ev) {
  if (ev.type === 'stage') { status.textContent = ev.message; addLog(ev.message, 'stage'); }
  else if (ev.type === 'log') addLog(ev.message);
  else if (ev.type === 'company') { live.push(ev.company); renderCards([...live].sort((a, b) => b.fit_score - a.fit_score)); $('#results-title').textContent = 'Research in progress…'; $('#results-meta').textContent = `${live.length} done so far`; }
  else if (ev.type === 'result') { lastResult = ev.result; renderResult(ev.result); }
  else if (ev.type === 'error') { status.textContent = `Error: ${ev.message}`; addLog(ev.message, 'error'); }
}

function addLog(text, cls = '') {
  const li = document.createElement('li'); li.textContent = text; li.className = cls;
  log.append(li); log.scrollTop = log.scrollHeight;
}

function renderResult(r) {
  $('#results-title').textContent = `${r.counts.researched} compan${r.counts.researched === 1 ? 'y' : 'ies'} assessed`;
  $('#results-meta').textContent = `Ranked by fit score · ${r.run.model_version} · passages reranked with ${r.run.rerank_model} · ${r.run.duration_s}s${r.failed.length ? ` · failed: ${r.failed.map(f => `${f.input_name} (${f.error})`).join(', ')}` : ''}`;
  renderCards(r.companies);
}

const showAll = {}; // company id -> false when the user picked "New leads" (all leads show by default)

// The message is editable; Copy and the email links always use the current text.
// The profile when a provider found one; otherwise a LinkedIn people search the user opens and checks themself.
const profileUrl = p => safeUrl(/^https?:/i.test(p.profile_url) ? p.profile_url : `https://${p.profile_url}`);
const linkedinSearch = (p, c) => `https://www.linkedin.com/search/results/people/?${new URLSearchParams({ keywords: `${p.name} ${c.name.replace(/\(.*?\)/g, '').trim()}` })}`;

function outreach(p, c) {
  if (!p.message) return p.message_error ? `<p class="mini warn">Message not written: ${esc(p.message_error)} Click Accept again to retry.</p>` : '';
  const m = p.message;
  return `<div class="outreach">
    <label>Subject<input class="msg-subject" value="${esc(m.subject)}"/></label>
    <label>Email<textarea class="msg-email" rows="8">${esc(m.email)}</textarea></label>
    <div><button type="button" data-copy="email">Copy email</button>${p.email ? ` <button type="button" data-send="gmail">Open in Gmail</button> <button type="button" data-send="mailto">Open in email app</button>` : ''}</div>
    <label>LinkedIn note <span>under 300 characters</span><textarea class="msg-li" rows="3">${esc(m.linkedin_note)}</textarea></label>
    <div><button type="button" data-copy="li">Copy LinkedIn note</button> ${p.profile_url ? `<a class="button" href="${profileUrl(p)}" target="_blank" rel="noreferrer">Open LinkedIn profile</a>` : `<a class="button" href="${linkedinSearch(p, c)}" target="_blank" rel="noreferrer">Find on LinkedIn</a> <span class="mini">no profile link found; check the result is the right person</span>`}</div>
    ${m.followup ? `<label>LinkedIn follow-up <span>send after they accept</span><textarea class="msg-follow" rows="5">${esc(m.followup)}</textarea></label>
    <div><button type="button" data-copy="follow">Copy follow-up</button></div>` : ''}
    <label>Change something? <span>e.g. "shorter", "mention their hiring"; rewrites the messages (about $0.005)</span><input class="msg-feedback"/></label>
    <div><button type="button" data-rewrite>Rewrite</button> <button type="button" data-outreach="sent" aria-pressed="${p.outreach?.status === 'sent'}">Mark sent</button> <button type="button" data-outreach="skipped" aria-pressed="${p.outreach?.status === 'skipped'}">Skip</button></div>
  </div>`;
}

// One block per accepted lead: their email, LinkedIn note and follow-up, kept out of the leads list.
function messagesPanel(c) {
  const saved = c.people.filter(p => p.saved);
  return `<div class="review"><ul class="messages">${saved.map(p => `<li data-lead-id="${esc(p.id)}"><b>${esc(p.name)}</b> ${esc(p.title)}${p.outreach ? ` <span class="pill">${p.outreach.status === 'sent' ? 'sent' : 'skipped'} ${esc(p.outreach.at.slice(0, 10))}</span>` : ''}${outreach(p, c)}</li>`).join('')}</ul></div>`;
}

async function messageTool(tool) {
  const box = tool.closest('.outreach'), li = tool.closest('li');
  const person = lastResult.companies.flatMap(c => c.people).find(p => p.id === li.dataset.leadId);
  const subject = box.querySelector('.msg-subject').value, body = box.querySelector('.msg-email').value;
  if (tool.dataset.copy) {
    const field = box.querySelector({ li: '.msg-li', follow: '.msg-follow' }[tool.dataset.copy] || '.msg-email');
    let done = 'Copied ✓';
    // If the browser blocks the clipboard, select the text so Ctrl+C still works.
    try { await navigator.clipboard.writeText(tool.dataset.copy === 'email' ? `Subject: ${subject}\n\n${body}` : field.value); }
    catch { field.focus(); field.select(); done = 'Selected: press Ctrl+C'; }
    const label = tool.textContent; tool.textContent = done; setTimeout(() => { tool.textContent = label; }, 2500);
    return;
  }
  const q = new URLSearchParams({ to: person.email, su: subject, body });
  window.open(tool.dataset.send === 'gmail' ? `https://mail.google.com/mail/?view=cm&fs=1&${q}` : `mailto:${encodeURIComponent(person.email)}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`, '_blank', 'noopener');
}

const tabs = {}; // company id -> open tab ("overview", "growth", "financials", "evidence", "leads", "messages")

// Accept / Reject for the company, above the tabs.
// "what to do -> the growth it brings" lines, with the result in bold
const arrows = items => items?.length ? `<ul class="levers">${items.map(g => { const [act, gain] = clean(g).split(/\s*(?:->|→)\s*/); return `<li>${esc(act)}${gain ? ` <b>→ ${esc(gain)}</b>` : ''}</li>`; }).join('')}</ul>` : '<p class="mini">None found.</p>';

// What they could spend on a new offering: a rule of thumb from verified revenue, with the arithmetic shown.
const capacity = b => !b ? '' : `<div class="capacity"><h4>Buying capacity <span class="pill aff-${esc(b.affinity)}">${esc(b.affinity)} affinity to buy</span></h4>
  ${b.tech_budget ? `<dl class="financials"><div><dt>Technology budget</dt><dd>${esc(b.tech_budget)}</dd></div><div><dt>For new offerings</dt><dd><b>${esc(b.new_offering_budget)}</b></dd></div></dl><p class="mini">Estimate. ${esc(b.basis)}</p>` : '<p class="mini">No verified revenue, so no budget estimate.</p>'}
  <p class="mini">Affinity is based on: ${b.reasons.map(esc).join(' · ')}</p></div>`;

const caseStudy = cs => `<div class="case">
  <p><b>${esc(cs.name)}</b>${cs.what_they_do ? ` <span class="mini">· ${esc(cs.what_they_do)}</span>` : ''}</p>
  <p class="gain">${cs.growth_metric && !/revenue/i.test(cs.growth_metric) ? `${esc(cs.growth_metric)} growth (no revenue figures published): ` : ''}${esc(cs.growth)}</p>
  <p>${esc(clean(cs.what_they_did))}</p>
  ${cs.implemented?.length ? `<p><b>What they implemented to grow:</b></p>${list(cs.implemented)}` : ''}
  ${cs.how_product_helps?.length ? `<p><b>How our product gets this company there:</b></p><ul class="levers">${cs.how_product_helps.map(h => { const [did, can] = clean(h).split(/\s*(?:->|→)\s*/); return `<li>${esc(did)}${can ? ` <b>→ ${esc(can)}</b>` : ''}</li>`; }).join('')}</ul>` : ''}
  <p class="mini">“${esc(cs.quote)}” <a href="${safeUrl(cs.url)}" target="_blank" rel="noreferrer">${esc(host(cs.url))} ↗</a></p>
</div>`;

// A decision shows as a badge; the one button left switches it.
const decided = (decision, what) => `<span class="badge ${decision}">${decision === 'accepted' ? '✓' : '✕'} ${what} ${decision}</span>`;

function decisionBar(c) {
  const decision = c.review?.decision;
  const hint = config.people.length ? `Accepting looks up to ${config.max_people} decision makers (C-level, founders, VPs; directors too at 1,000+ staff)` : 'Accept the company to look up leads';
  return `<div class="review">
    ${decision ? `${decided(decision, 'Company')}<button type="button" class="switch" data-decision="${decision === 'accepted' ? 'rejected' : 'accepted'}">${decision === 'accepted' ? 'Reject instead' : 'Accept instead'}</button>`
    : `<button type="button" data-decision="accepted">Accept</button><button type="button" data-decision="rejected">Reject</button>`}
    <span class="mini" role="status">${decision ? '' : hint}</span>
  </div>`;
}

function leadsPanel(c) {
  const decision = c.review?.decision;
  if (decision !== 'accepted') return `<p class="mini">Accept the company to look up leads.</p>`;
  const latest = Math.max(0, ...c.people.map(p => p.batch ?? 1), ...(c.people_lookups || []).map(l => l.batch ?? 1));
  const isNew = p => (p.batch ?? 1) === latest;
  const all = showAll[c.id] ?? true;
  const shown = all ? c.people : c.people.filter(isNew);
  const people = shown.map(p => `<li data-lead-id="${esc(p.id)}" class="${p.saved === false ? 'dropped' : ''}">${typeof p.saved === 'boolean'
      ? `${decided(p.saved ? 'accepted' : 'rejected', '')} <button type="button" class="lead switch" data-person="${esc(p.id)}" data-lead="${p.saved ? 'reject' : 'accept'}">${p.saved ? 'Reject' : 'Accept'}</button>`
      : `<button type="button" class="lead" data-person="${esc(p.id)}" data-lead="accept">Accept</button> <button type="button" class="lead" data-person="${esc(p.id)}" data-lead="reject">Reject</button>`} <b>${esc(p.name)}</b> ${esc(p.title)} ${p.email ? `<a href="mailto:${encodeURIComponent(p.email)}">${esc(p.email)}</a>` : '<span class="mini">no email</span>'} <span class="pill ${p.status === 'verified' ? 'verified' : 'inferred'}">${esc(p.status)}</span>${p.profile_url ? ` <a href="${profileUrl(p)}" target="_blank" rel="noreferrer">${/linkedin\.com/i.test(p.profile_url) ? 'LinkedIn' : 'profile'}</a>` : ` <a href="${linkedinSearch(p, c)}" target="_blank" rel="noreferrer">find on LinkedIn</a>`} <span class="mini">${esc(p.source)}${p.from_run ? ' · from an earlier run' : isNew(p) ? '' : ' · earlier lookup'}</span>${p.saved ? ' <span class="mini">· message in the Messages tab</span>' : ''}</li>`).join('');
  return `<div class="review">
    ${c.people_error ? `<p class="mini warn">Lead lookup failed: ${esc(c.people_error)}</p>` : ''}
    ${decision === 'accepted' ? `<label>Job titles <span>comma-separated; empty uses the buyer titles for your product. Only decision makers are kept: C-level, founders, VPs, and directors at companies with 1,000+ staff.</span><input class="titles" placeholder="${esc((c.buyer_titles || []).join(', ') || 'e.g. Chief Operating Officer, CTO, VP Operations')}"/></label>
      <div>${config.people.map(p => `<button type="button" data-provider="${esc(p)}">Find ${c.people.length ? 'more ' : ''}leads with ${esc(p[0].toUpperCase() + p.slice(1))} (up to ${config.max_people} new people)</button>`).join(' ') || '<p class="mini">Add HUNTER_API_KEY or APOLLO_API_KEY to .env and restart to look up leads.</p>'}</div>
      ${c.people.length ? `<h4>Leads (${c.people.length}, ${c.people.filter(p => p.saved).length} accepted)</h4>
        <label class="inline">Show <select class="show"><option value="all">All leads, including earlier ones (${c.people.length})</option><option value="new"${all ? '' : ' selected'}>New leads from the last lookup (${c.people.filter(isNew).length})</option></select></label>
        <p class="mini">Each lookup returns only people you haven't seen before. Only accepted leads go into the database and the accepted leads export.${c.people_lookups?.at(-1)?.not_senior ? ` The last lookup skipped ${c.people_lookups.at(-1).not_senior} people below decision-maker level.` : ''}</p>
        ${people ? `<ul>${people}</ul>` : '<p class="mini">The last lookup found no new people. Try other job titles, or show all leads.</p>'}` : c.people_lookups?.length ? '<p class="mini">No leads found yet. Try other job titles.</p>' : ''}` : ''}
  </div>`;
}

$('#cards').addEventListener('change', e => {
  if (!e.target.matches('select.show')) return;
  showAll[e.target.closest('.card').dataset.id] = e.target.value === 'all';
  renderCards(lastResult.companies);
});

$('#cards').addEventListener('click', async e => {
  const tool = e.target.closest('[data-copy], [data-send]');
  if (tool) return messageTool(tool);
  const tab = e.target.closest('[data-tab]');
  if (tab) { // switch tabs in place, so the page doesn't jump
    const card = tab.closest('.card');
    tabs[card.dataset.id] = tab.dataset.tab;
    card.querySelectorAll('[data-tab]').forEach(t => t.setAttribute('aria-selected', t === tab));
    card.querySelectorAll('[data-panel]').forEach(p => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
    if (['growth', 'recommend'].includes(tab.dataset.tab)) writeGrowth(card.dataset.id); // written the first time it's opened
    return;
  }
  const again = e.target.closest('[data-growth]');
  if (again) return writeGrowth(again.closest('.card').dataset.id, true);
  const alt = e.target.closest('[data-alt]');
  if (alt) { // research the company the user meant instead
    $('#run-form [name=companies]').value = alt.dataset.alt;
    if ('fresh' in alt.dataset) $('#run-form [name=fresh]').checked = true;
    showMode('research');
    $('#run-form').scrollIntoView({ behavior: 'smooth' });
    return;
  }
  const button = e.target.closest('[data-decision], [data-provider], [data-person], [data-rewrite], [data-outreach], [data-notes], [data-case]');
  if (!button) return;
  const box = button.closest('.card'), say = box.querySelector('[role=status]'), run = lastResult;
  const { decision, provider, person, outreach } = button.dataset, rewrite = 'rewrite' in button.dataset, notes = 'notes' in button.dataset, caseRun = 'case' in button.dataset;
  const li = button.closest('li'), personId = person || li?.dataset.leadId;
  // A button pressed again clears the status.
  const status = outreach && button.getAttribute('aria-pressed') !== 'true' ? outreach : null;
  box.querySelectorAll('button').forEach(b => { b.disabled = true; });
  say.textContent = caseRun ? 'Searching for a case study…' : notes ? 'Updating the results with your notes…' : rewrite ? 'Rewriting the message…' : person ? (button.dataset.lead === 'accept' ? 'Saving and writing a message…' : 'Saving…') : decision === 'accepted' ? (config.people.length ? 'Writing the growth plan and looking up leads…' : 'Writing the growth plan…') : provider ? 'Looking up leads…' : 'Saving…';
  try {
    const url = caseRun ? '/api/case-study' : notes ? '/api/notes' : rewrite ? '/api/message' : outreach ? '/api/outreach' : person ? '/api/save-lead' : provider ? '/api/people' : '/api/review';
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: run.run.run_id, company_id: box.dataset.id, decision, provider, person_id: personId, saved: person ? button.dataset.lead === 'accept' : undefined, titles: box.querySelector('.titles')?.value, feedback: li?.querySelector('.msg-feedback')?.value, status, notes: notes ? box.querySelector('.notes').value : undefined }) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    if (provider || decision === 'accepted') tabs[data.id] = 'leads';
    run.companies[run.companies.findIndex(c => c.id === data.id)] = data;
    if (lastResult === run) renderCards(run.companies);
  } catch (err) {
    say.textContent = err.message;
    box.querySelectorAll('button').forEach(b => { b.disabled = false; });
  }
});

// The growth plan and recommendation are written on demand (one Claude call, about $0.02), so companies nobody pursues never pay for them.
const pendingGrowth = new Set();
async function writeGrowth(id, again = false) {
  const run = lastResult, i = run?.companies.findIndex(x => x.id === id), c = run?.companies[i];
  if (!c || pendingGrowth.has(id) || (!again && (c.growth_written || c.growth_play || c.growth_business?.length || c.growth_error))) return;
  pendingGrowth.add(id); delete c.growth_error; renderCards(run.companies);
  try {
    const res = await fetch('/api/growth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: run.run.run_id, company_id: id, again }) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    run.companies[i] = data;
  } catch (err) { c.growth_error = err.message; }
  pendingGrowth.delete(id);
  if (lastResult === run) renderCards(run.companies);
}

function renderCards(companies) {
  $('#results').hidden = false;
  $('#cards').innerHTML = companies.map((c, i) => {
    const verified = c.evidence.filter(e => e.status === 'verified').length;
    const inferred = c.evidence.length - verified;
    const hasGrowth = c.growth_play || c.growth_business?.length || c.path_to_win?.length;
    const growth = lastResult || hasGrowth; // during a run there's nothing to write it from yet
    const growthWait = c.growth_error ? `<p class="mini warn">Couldn't write the growth plan: ${esc(c.growth_error)}</p><p><button type="button" class="alt-button" data-growth>Try again</button></p>`
      : `<p class="mini">${pendingGrowth.has(c.id) ? 'Writing the growth plan and recommendation… (about $0.02)' : 'Open this tab to write the growth plan and recommendation (about $0.02).'}</p>`;
    const shown = ['overview', 'financials', ...(growth ? ['growth', 'recommend'] : []), ...(lastResult ? ['notes', 'leads', 'messages'] : []), 'evidence']; // no leads tab while a run is in progress
    const tab = shown.includes(tabs[c.id]) ? tabs[c.id] : 'overview';
    const tabButton = (id, label) => `<button type="button" role="tab" data-tab="${id}" aria-selected="${tab === id}">${label}</button>`;
    const panel = (id, html) => `<div data-panel="${id}" ${tab === id ? '' : 'hidden'}>${html}</div>`;
    return `<article class="card" data-id="${esc(c.id)}">
      <header>
        <div class="rank">${i + 1}</div>
        <div class="title"><h3>${esc(c.name)}</h3><a href="${safeUrl(c.website)}" target="_blank" rel="noreferrer">${esc(c.domain)} ↗</a>
          ${c.identity_confidence !== 'high' ? `<span class="pill inferred">${esc(c.identity_confidence)} confidence this is “${esc(c.input_name)}”</span>` : ''}
          ${c.alternatives?.length ? `<p class="mini">Not the company you meant? Research instead: ${c.alternatives.map(a => `<button type="button" class="alt" data-alt="${esc(`${a.name}, ${a.domain}`)}">${esc(a.name)} (${esc(a.domain)})</button>`).join(' ')}</p>` : ''}
          <div class="tags">${[c.industry, c.location, c.size_band].filter(v => v && v !== 'Unknown').map(t => `<span>${esc(t)}</span>`).join('')}</div>
          ${c.reused_from ? `<p class="mini">Reused research from ${esc(new Date(c.reused_from.at).toLocaleDateString())} · no cost · <button type="button" class="alt" data-alt="${esc(c.domain ? `${c.input_name}, ${c.domain}` : c.input_name)}" data-fresh>Research again</button></p>` : ''}</div>
      </header>
      <p class="summary">${esc(c.summary)}</p>
      <div class="verdict wtb-${esc(c.willingness_to_buy)}"><b>Willingness to buy: ${esc(c.willingness_to_buy)}</b>${c.user_notes ? ' <span class="pill">updated with your notes</span>' : ''}<p>${esc(clean(c.verdict))}</p>${c.pitch_angle ? `<p><b>Pitch angle:</b> ${esc(clean(c.pitch_angle))}</p>` : ''}</div>
      ${lastResult ? decisionBar(c) : ''}
      <div class="tabs" role="tablist">
        ${tabButton('overview', 'Fit & signals')}${tabButton('financials', 'Financials')}${growth ? tabButton('growth', 'Growth plan') + tabButton('recommend', 'Recommendation') : ''}${lastResult ? tabButton('notes', c.user_notes ? 'Your notes ✓' : 'Your notes') : ''}
        ${lastResult ? tabButton('leads', `Leads${c.people.length ? ` (${c.people.length})` : ''}`) : ''}
        ${lastResult && c.people.some(p => p.saved) ? tabButton('messages', `Messages (${c.people.filter(p => p.saved).length})`) : ''}
        ${tabButton('evidence', `Evidence (${verified})`)}
      </div>
      ${panel('overview', `<div class="cols">
        <div><div class="fit"><div class="score tier-${esc(c.tier)}"><strong>${c.fit_score}</strong><small>${esc(c.tier)}</small></div><p class="mini">Fit score out of 100: Priority 75+, Qualified 60+, Nurture 40+, below that Disqualify.</p></div>
          <h4>Score breakdown</h4>${Object.entries(c.score_breakdown).map(([k, v]) => `<div class="bar"><span>${LABELS[k] || k}</span><i><b style="width:${v}%"></b></i><em>${v}</em></div>`).join('')}
          <h4>Why they'd buy</h4>${list(c.fit_reasons)}
          <h4>Why they might not</h4>${list(c.objections)}</div>
        <div><h4>Buying signals</h4>${c.buying_signals.length ? `<ul>${c.buying_signals.map(b => `<li>${esc(clean(b.signal))}${b.date && !/unknown/i.test(b.date) ? ` <em>(${esc(b.date)})</em>` : ''}${b.url ? ` <a href="${safeUrl(b.url)}" target="_blank" rel="noreferrer">${esc(host(b.url))}</a>` : ''}</li>`).join('')}</ul>` : '<p class="mini">None found.</p>'}
          <h4>Who to approach</h4>${list(c.buyer_titles?.length ? c.buyer_titles : c.buying_functions)}</div>
      </div>
      <p class="mini">Read ${c.pages_read} pages · ${c.usage.input_tokens + c.usage.output_tokens} tokens${c.usage.usd === null ? '' : ` ($${c.usage.usd})`} · search: ${esc(c.usage.search_provider)} · ${c.meets_min_evidence ? 'meets minimum evidence' : 'below minimum evidence — verify before outreach'}</p>`)}
      ${growth ? panel('growth', `${c.growth_business?.length || c.growth_technical?.length ? `<div class="cols">
          <div><h4>Business growth</h4>${arrows(c.growth_business)}</div>
          <div><h4>Technical growth</h4>${arrows(c.growth_technical)}</div>
        </div>` : hasGrowth ? '<p class="mini">Run this company again to see its business and technical growth ways.</p>' : growthWait}
        <h4>Case study: a similar company that grew</h4>
        ${c.case_study ? caseStudy(c.case_study)
          : c.peers?.length ? `<div class="peers">${c.peers.map(p => `<div><b>${esc(p.name)}</b>${p.what_they_do ? `<p class="mini">${esc(p.what_they_do)}</p>` : ''}<p>${esc(p.what_they_did || p.edge)}</p>${p.growth ? `<p class="gain">${esc(p.growth)}</p>` : ''}<a href="${safeUrl(p.url)}" target="_blank" rel="noreferrer" title="${esc(p.quote)}">${esc(host(p.url))} ↗</a></div>`).join('')}</div>`
          : c.case_study_search ? '<p class="mini">No company doing the same work was found with proof of its growth, so none is shown.</p>'
          : `<p class="mini">Find one company that does the same work as ${esc(c.name)} and grew its revenue, and how our product gets ${esc(c.name)} the same result.</p>`}
        ${lastResult ? `<p><button type="button" class="alt-button" data-case>${c.case_study ? 'Find a different case study' : c.case_study_search ? 'Search again' : 'Find a case study'}</button> <span class="mini">searches the web with Exa · about $0.03–0.05</span></p>` : ''}`) : ''}
      ${growth ? panel('recommend', !hasGrowth ? growthWait : `${c.growth_play ? `<div class="win"><h4>How our product helps them grow</h4><p>${esc(clean(c.growth_play))}</p></div>` : ''}
        ${c.product_solutions?.length ? `<h4>How our platform solves their growth points</h4><table class="solve">${c.product_solutions.map(x => `<tr><td>${esc(clean(x.growth_point))}</td><td>→ ${esc(clean(x.how_product_helps))}</td></tr>`).join('')}</table>`
          : c.growth_levers?.length ? `<h4>How our platform helps</h4>${arrows(c.growth_levers)}` : ''}
        ${c.product_extras?.length ? `<h4>More ways our product can drive growth</h4>${list(c.product_extras)}` : ''}
        <div class="cols">
          <div><h4>How to win them</h4>${list(c.path_to_win)}</div>
          <div>${c.next_steps?.length ? `<h4>Recommended next steps</h4>${list(c.next_steps)}` : ''}
            ${c.similar_companies?.length ? `<h4>Research next</h4><p class="mini">${c.similar_companies.map(esc).join(' · ')}</p><button type="button" class="alt" data-alt="${esc(c.similar_companies.join('\n'))}">Put these in the form</button>` : ''}</div>
        </div>`) : ''}
      ${panel('financials', `<dl class="financials">${Object.entries(FINANCIALS).map(([k, label]) => `<div><dt>${label}</dt><dd>${figure(c.financials[k])}</dd></div>`).join('')}</dl>${capacity(c.buying_capacity)}`)}
      ${panel('evidence', `<p class="mini">${verified} quotes found word for word on the pages${inferred ? `, ${inferred} not found (faded)` : ''}.</p>
        ${c.evidence.map(e => `<blockquote class="${e.status}"><p>“${esc(e.quote)}”</p><footer><span class="pill">${esc(LABELS[e.supports] || e.supports)}</span> <span class="pill ${e.status}">${e.status}</span> ${esc(e.claim)} ${e.url ? `<a href="${safeUrl(e.url)}" target="_blank" rel="noreferrer">${esc(host(e.url))} ↗</a>` : ''}</footer></blockquote>`).join('')}
        ${c.unknowns.length ? `<h4>Couldn't verify</h4>${list(c.unknowns)}` : ''}`)}
      ${lastResult ? panel('notes', `<p class="mini">Know something about ${esc(c.name)} that the web research missed or got wrong — team size, current tools, budget, pain points, a contact, plans? Add it here and the agent re-judges the fit and rewrites the growth plan using it (one small Claude call, about $0.03). Your notes count as first-hand facts.</p>
        <textarea class="notes" maxlength="3000" placeholder="e.g. They run client service on Excel and WhatsApp; the COO told us they want to double advisors without hiring; budget review in March.">${esc(c.user_notes?.text || '')}</textarea>
        <p><button type="button" data-notes>${c.user_notes ? 'Update again with my notes' : 'Update results with my notes'}</button></p>
        ${c.user_notes ? `<p class="mini">Last updated ${esc(new Date(c.user_notes.updated_at).toLocaleString())} · score before your notes: ${c.user_notes.score_before} → now ${c.fit_score}</p>` : ''}`) : ''}
      ${lastResult ? panel('leads', leadsPanel(c)) + panel('messages', messagesPanel(c)) : ''}
    </article>`;
  }).join('');
}

function download(name, text, type) {
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  a.click(); URL.revokeObjectURL(a.href);
}
// A leading quote stops spreadsheets from running text from the web as a formula.
const cell = v => `"${String(v ?? '').replace(/^(?=[=+@\t\r]|-(?!\d))/, "'").replace(/"/g, '""')}"`;
const accepted = r => r.companies.filter(c => c.review?.decision === 'accepted').flatMap(c => c.people.filter(p => p.saved).map(p => ({ c, p })));

// The same rows feed CSV and Excel: [header, ...rows].
function table(what) {
  if (what === 'leads') {
    const keys = ['name', 'title', 'band', 'function', 'email', 'email_status', 'status', 'confidence', 'profile_url', 'source', 'reason', 'checked_at'];
    return [['company', 'domain', 'fit_score', ...keys, 'message_subject', 'message_email', 'linkedin_note', 'linkedin_followup', 'outreach_status', 'outreach_at'],
      ...accepted(lastResult).map(({ c, p }) => [c.name, c.domain, c.fit_score, ...keys.map(k => p[k]), p.message?.subject, p.message?.email, p.message?.linkedin_note, p.message?.followup, p.outreach?.status, p.outreach?.at])];
  }
  const keys = Object.keys(LABELS);
  const head = ['rank', 'name', 'domain', 'fit_score', 'tier', 'willingness_to_buy', ...keys, 'industry', 'location', 'size_band', ...Object.keys(FINANCIALS).flatMap(k => [k, `${k}_period`, `${k}_basis`, `${k}_entity`, `${k}_source`, `${k}_other_figures`]), 'buying_affinity', 'tech_budget_estimate', 'new_offering_budget_estimate', 'verdict', 'pitch_angle', 'growth_business', 'growth_technical', 'growth_play', 'product_solutions', 'product_extras', 'growth_levers', 'case_study', 'path_to_win', 'similar_companies', 'buying_signals', 'buying_functions', 'top_evidence'];
  return [head, ...lastResult.companies.map(c => [c.rank_position, c.name, c.domain, c.fit_score, c.tier, c.willingness_to_buy, ...keys.map(k => c.score_breakdown[k]), c.industry, c.location, c.size_band, ...Object.keys(FINANCIALS).flatMap(k => { const f = c.financials[k]; return f ? [f.value, f.period, f.basis, f.entity, f.url, f.others.map(o => `${o.value} ${o.period} (${o.url})`).join(' | ')] : ['Unknown', '', '', '', '', '']; }), c.buying_capacity?.affinity || '', c.buying_capacity?.tech_budget || '', c.buying_capacity?.new_offering_budget || '', clean(c.verdict), clean(c.pitch_angle), (c.growth_business || []).map(clean).join(' | '), (c.growth_technical || []).map(clean).join(' | '), clean(c.growth_play || ''), (c.product_solutions || []).map(x => `${x.growth_point} -> ${x.how_product_helps}`).join(' | '), (c.product_extras || []).join(' | '), (c.growth_levers || []).map(clean).join(' | '), c.case_study ? `${c.case_study.name}: ${c.case_study.growth}. ${c.case_study.what_they_did} Implemented: ${(c.case_study.implemented || []).join('; ')} (${c.case_study.url})` : (c.peers || []).map(p => `${p.name}: ${p.what_they_did || p.edge} ${p.growth || ''} (${p.url})`).join(' | '), (c.path_to_win || []).map(clean).join(' | '), (c.similar_companies || []).join(' | '), c.buying_signals.map(b => clean(b.signal)).join(' | '), c.buying_functions.join(' | '), c.evidence.filter(e => e.status === 'verified').map(e => `"${e.quote}" (${e.url})`).join(' | ')])];
}

// A readable report to print or save as PDF: one section per company, then its accepted leads and their messages.
function report(r) {
  const li = items => items?.length ? `<ul>${items.map(x => `<li>${esc(clean(x))}</li>`).join('')}</ul>` : '<p class="muted">None found.</p>';
  const companies = r.companies.map(c => {
    const leads = c.people.filter(p => p.saved);
    return `<section>
      <h2>${c.rank_position}. ${esc(c.name)} <span class="score">${c.fit_score} · ${esc(c.tier)}</span></h2>
      <p class="muted">${esc(c.domain || '')} · ${[c.industry, c.location, c.size_band].filter(v => v && v !== 'Unknown').map(esc).join(' · ')}</p>
      <p>${esc(c.summary)}</p>
      <p><b>Willingness to buy: ${esc(c.willingness_to_buy)}.</b> ${esc(clean(c.verdict))}</p>
      ${c.pitch_angle ? `<p><b>Pitch angle:</b> ${esc(clean(c.pitch_angle))}</p>` : ''}
      <h3>Financials</h3><table>${Object.entries(FINANCIALS).map(([k, label]) => `<tr><td>${label}</td><td>${c.financials[k] ? `${esc(c.financials[k].value)} <span class="muted">${esc(c.financials[k].period)} · ${esc(host(c.financials[k].url))}</span>` : '<span class="muted">Unknown</span>'}</td></tr>`).join('')}${c.buying_capacity ? `<tr><td>Affinity to buy</td><td>${esc(c.buying_capacity.affinity)}</td></tr>${c.buying_capacity.new_offering_budget ? `<tr><td>Budget for new offerings</td><td>${esc(c.buying_capacity.new_offering_budget)} <span class="muted">estimate</span></td></tr>` : ''}` : ''}</table>
      ${c.growth_business?.length ? `<div class="two"><div><h3>Business growth</h3>${li(c.growth_business)}</div><div><h3>Technical growth</h3>${li(c.growth_technical)}</div></div>` : ''}
      ${c.growth_play ? `<h3>Recommendation: how our product helps them grow</h3><p>${esc(clean(c.growth_play))}</p>${li((c.product_solutions || []).map(x => `${x.growth_point} → ${x.how_product_helps}`).concat(c.product_extras || []).concat(c.growth_levers || []))}` : ''}
      ${c.case_study ? `<h3>Case study: ${esc(c.case_study.name)}</h3><p><b>${esc(c.case_study.growth)}</b></p><p>${esc(clean(c.case_study.what_they_did))}</p>${c.case_study.implemented?.length ? `<p><b>What they implemented:</b></p>${li(c.case_study.implemented)}` : ''}<p><b>How our product gets this company there:</b></p>${li(c.case_study.how_product_helps)}<p class="muted">“${esc(c.case_study.quote)}” · ${esc(c.case_study.url)}</p>` : ''}
      <div class="two"><div><h3>Why they'd buy</h3>${li(c.fit_reasons)}</div><div><h3>Why they might not</h3>${li(c.objections)}</div></div>
      ${c.path_to_win?.length ? `<h3>How to win them</h3>${li(c.path_to_win)}` : ''}
      ${leads.length ? `<h3>Accepted leads</h3>${leads.map(p => `<div class="lead"><b>${esc(p.name)}</b>, ${esc(p.title || '')}${p.email ? ` · ${esc(p.email)}` : ''}${p.profile_url ? ` · ${esc(p.profile_url)}` : ''}
        ${p.message ? `<p><b>Subject:</b> ${esc(p.message.subject)}</p><p class="pre">${esc(p.message.email)}</p><p><b>LinkedIn note:</b> ${esc(p.message.linkedin_note)}</p>` : ''}</div>`).join('')}` : ''}
    </section>`;
  }).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><title>Account research report</title><style>
    body{font:14px/1.55 Arial,sans-serif;color:#17241e;max-width:820px;margin:32px auto;padding:0 16px}
    h1{font-size:24px;margin:0}h2{font-size:19px;margin:0 0 4px}h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#205c49;margin:16px 0 6px}
    .muted{color:#63716b;font-size:12px}.score{font-size:13px;background:#eef1e6;border-radius:12px;padding:2px 10px;margin-left:6px}
    section{border-top:2px solid #205c49;padding-top:14px;margin-top:26px;break-inside:auto}table{border-collapse:collapse}td{padding:3px 14px 3px 0;vertical-align:top}
    ul{margin:4px 0;padding-left:20px}.two{display:grid;grid-template-columns:1fr 1fr;gap:20px}.lead{border:1px solid #d9ddd3;border-radius:8px;padding:8px 12px;margin:8px 0}.pre{white-space:pre-wrap}
    @media print{body{margin:0}section{break-before:page}section:first-of-type{break-before:auto}}
  </style></head><body><h1>Account research report</h1>
  <p class="muted">${esc(r.campaign?.title || '')} · ${esc(new Date(r.campaign?.generated_at || Date.now()).toLocaleString())} · ${r.companies.length} compan${r.companies.length === 1 ? 'y' : 'ies'}</p>
  <p><b>Product:</b> ${esc(r.campaign?.icp?.product || 'not specified')}</p>${companies}
  <script>addEventListener('load', () => print())</script></body></html>`;
}

$('#dl-go').onclick = async () => {
  if (!lastResult) return;
  const what = $('#dl-what').value, format = $('#dl-format').value, name = what === 'leads' ? 'accepted-leads' : 'company-research';
  if (format === 'json') return download('leads.json', JSON.stringify(lastResult, null, 2), 'application/json');
  if (format === 'pdf') {
    const url = URL.createObjectURL(new Blob([report(lastResult)], { type: 'text/html' }));
    if (!window.open(url)) download('report.html', report(lastResult), 'text/html'); // pop-up blocked: save the report instead
    return;
  }
  const rows = table(what);
  if (rows.length < 2) return alert(what === 'leads' ? 'Accept some leads first.' : 'No companies to export.');
  if (format === 'csv') return download(`${name}.csv`, rows.map(r => r.map(cell).join(',')).join('\n'), 'text/csv');
  const sheets = [{ name: what === 'leads' ? 'Accepted leads' : 'Companies', rows }];
  const res = await fetch('/api/export.xlsx', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sheets }) });
  if (!res.ok) return alert((await res.json().catch(() => ({}))).error || 'Export failed.');
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(await res.blob()), download: `${name}.xlsx` });
  a.click(); URL.revokeObjectURL(a.href);
};
// JSON and the PDF report always cover everything, so "What" only matters for the tables.
$('#dl-format').onchange = () => { $('#dl-what').disabled = ['json', 'pdf'].includes($('#dl-format').value); };

// ---------- Find companies from an ideal customer profile ----------
let found = [], foundFor = '';
document.querySelectorAll('[data-mode]').forEach(b => { b.onclick = () => showMode(b.dataset.mode); });
function showMode(mode) {
  document.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-selected', b.dataset.mode === mode));
  $('#find').hidden = mode !== 'find'; $('#research').hidden = mode !== 'research';
}
const foundLine = c => c.domain ? `${c.name}, ${c.domain}` : c.name;
const picked = () => [...document.querySelectorAll('#found-list input:checked')].map(i => found[+i.value]);
function showFound() {
  $('#found').hidden = !found.length;
  $('#found-list').innerHTML = found.map((c, i) => `<li><input type="checkbox" value="${i}" checked aria-label="Select ${esc(c.name)}">
    <b>${esc(c.name)}${c.domain ? ` <a href="${safeUrl('https://' + c.domain)}" target="_blank" rel="noreferrer">${esc(c.domain)} ↗</a>` : ' <span class="mini">website not confirmed</span>'}</b>
    <p>${esc(c.why)}</p><p class="mini">found on <a href="${safeUrl(c.source)}" target="_blank" rel="noreferrer">${esc(host(c.source))}</a></p></li>`).join('');
  $('#find-go').innerHTML = found.length ? 'Find 5 more <b>→</b>' : 'Find 5 companies <b>→</b>';
  askClear(false); // a changed list closes any open "clear?" question
}
$('#find-go').onclick = async () => {
  const icp = $('#icp').value.trim(), say = $('#find-status'), button = $('#find-go');
  if (icp !== foundFor) { found = []; foundFor = icp; showFound(); } // a new description starts a new list
  button.disabled = true; say.textContent = found.length ? 'Looking for 5 more…' : 'Searching the web for matching companies…';
  try {
    const res = await fetch('/api/discover', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ icp, exclude: found.map(c => c.name), product: $('#product').value }) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    found.push(...data.companies); showFound();
    say.textContent = data.companies.length ? `Found ${data.companies.length}${data.usd ? ` · cost $${data.usd}` : ''}. Untick any you don't want, then research them.` : 'No more companies clearly match. Try a broader description.';
  } catch (err) { say.textContent = err.message; }
  button.disabled = false;
};
$('#found-copy').onclick = async () => {
  const text = picked().map(foundLine).join('\n');
  try { await navigator.clipboard.writeText(text); $('#find-status').textContent = 'Copied. Paste into Companies to research.'; }
  catch { $('#find-status').textContent = text; } // clipboard blocked: show the text so it can be copied by hand
};
// Asked on the page itself: some browsers (like the Claude app's pane) block confirm() pop-ups.
const askClear = show => { $('#clear-confirm').hidden = !show; $('#found-clear').hidden = show; };
$('#found-clear').onclick = () => {
  $('#clear-question').textContent = `Clear all ${found.length} suggestion${found.length === 1 ? '' : 's'}? Getting them back needs a new search.`;
  askClear(true);
};
$('#clear-no').onclick = () => askClear(false);
$('#clear-yes').onclick = () => {
  askClear(false);
  found = []; foundFor = ''; showFound(); $('#find-status').textContent = ''; // the next search starts a fresh list
};
$('#found-research').onclick = () => {
  const lines = picked().map(foundLine);
  if (!lines.length) { $('#find-status').textContent = 'Tick at least one company.'; return; }
  $('#run-form [name=companies]').value = lines.join('\n');
  showMode('research'); $('#run-form [name=companies]').focus();
};
