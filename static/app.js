/* ─────────────────────────────────────────────────────
   Denali Health — BD Automation
   Frontend ↔ Backend wiring (vanilla JS, no framework)
   ───────────────────────────────────────────────────── */

const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
  async post(path, body, isForm = false) {
    const opts = { method: 'POST' };
    if (isForm) {
      opts.body = body;
    } else {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body || {});
    }
    const r = await fetch(path, opts);
    if (!r.ok) {
      const txt = await r.text();
      let detail; try { detail = JSON.parse(txt).detail; } catch {}
      throw new Error(detail || `${r.status} ${r.statusText}`);
    }
    return r.json();
  },
};

// ── App state ────────────────────────────────────
const state = {
  category: null,
  subcategory: [],
  opps: [],
  oppId: null,
  opp: null,
  contacts: [],
  primaryContact: null,
  draft: null,
  isEditing: false,
  lastLogId: 0,
  logPollInterval: null,
};

// ── Helpers ──────────────────────────────────────
function $(id) { return document.getElementById(id); }
function show(id) { $(id).classList.remove('hidden'); }
function hide(id) { $(id).classList.add('hidden'); }
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtDate(s) {
  if (!s) return '—';
  const d = new Date(s);
  if (isNaN(d)) return s;
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}


// ── Searchable combobox (replaces the old <select> dropdowns) ──
function makeCombo(containerId, placeholder, onPick) {
  const root = document.getElementById(containerId);
  if (!root) return null;
  root.innerHTML = '';
  root.classList.add('combo');
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = placeholder;
  input.autocomplete = 'off';
  input.className = 'combo-input';
  const panel = document.createElement('div');
  panel.className = 'combo-panel hidden';
  const arrow = document.createElement('span');
  arrow.className = 'combo-arrow';
  arrow.innerHTML = '▼';
  arrow.title = 'Show all';
  arrow.addEventListener('mousedown', (e) => {
    e.preventDefault();
    if (panel.classList.contains('hidden')) {
      input.focus();
      input.select();
      // render() will run via the focus handler
    } else {
      panel.classList.add('hidden');
      root.classList.remove('open');
      input.blur();
    }
  });
  root.appendChild(input); root.appendChild(arrow); root.appendChild(panel);
  let all = [];
  let currentValue = '';
  let highlighted = -1;
  function render(filter) {
    const f = (filter || '').toLowerCase();
    panel.innerHTML = '';
    const matches = all.filter(o => o.label.toLowerCase().includes(f));
    matches.slice(0, 300).forEach((o, idx) => {
      const div = document.createElement('div');
      div.className = 'combo-option' + (idx === highlighted ? ' active' : '');
      div.textContent = o.label;
      div.addEventListener('mousedown', (e) => {
        e.preventDefault();
        input.value = o.label;
        currentValue = o.value;
        panel.classList.add('hidden');
        onPick(o.value, o);
      });
      panel.appendChild(div);
    });
    if (matches.length === 0) {
      const div = document.createElement('div');
      div.className = 'combo-empty';
      div.textContent = all.length === 0 ? 'No items' : 'No matches';
      panel.appendChild(div);
    }
  }
  input.addEventListener('focus', () => { highlighted = -1; render(input.value); panel.classList.remove('hidden'); root.classList.add('open'); });
  input.addEventListener('input', () => { highlighted = -1; render(input.value); panel.classList.remove('hidden'); });
  input.addEventListener('blur',  () => { setTimeout(() => { panel.classList.add('hidden'); root.classList.remove('open'); }, 180); });
  input.addEventListener('keydown', (e) => {
    const opts = panel.querySelectorAll('.combo-option');
    if (e.key === 'Escape') { panel.classList.add('hidden'); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); highlighted = Math.min(opts.length - 1, highlighted + 1); render(input.value); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); highlighted = Math.max(0, highlighted - 1); render(input.value); }
    else if (e.key === 'Enter' && highlighted >= 0 && opts[highlighted]) {
      opts[highlighted].dispatchEvent(new MouseEvent('mousedown'));
    }
  });
  return {
    setOptions(list) {
      all = list || [];
      if (currentValue) {
        const m = all.find(o => o.value === currentValue);
        input.value = m ? m.label : '';
        if (!m) currentValue = '';
      }
    },
    setValue(value) {
      const m = all.find(o => o.value === value);
      currentValue = m ? value : '';
      input.value = m ? m.label : '';
    },
    getValue() { return currentValue; },
  };
}
let categoryCombo = null, subcategoryCombo = null, oppCombo = null;

// ── Multi-select combo (checkboxes; for Subcategory) ──
function makeMultiCombo(containerId, placeholder, onChange) {
  const root = document.getElementById(containerId);
  if (!root) return null;
  root.innerHTML = '';
  root.classList.add('combo');
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = placeholder;
  input.autocomplete = 'off';
  input.className = 'combo-input';
  input.readOnly = false;
  const panel = document.createElement('div');
  panel.className = 'combo-panel hidden';
  const arrow = document.createElement('span');
  arrow.className = 'combo-arrow';
  arrow.innerHTML = '▼';
  arrow.addEventListener('mousedown', (e) => {
    e.preventDefault();
    if (panel.classList.contains('hidden')) { input.focus(); }
    else { panel.classList.add('hidden'); root.classList.remove('open'); input.blur(); }
  });
  root.appendChild(input); root.appendChild(arrow); root.appendChild(panel);

  let all = [];
  let selected = new Set();
  let filter = '';

  function updateInputText() {
    if (selected.size === 0) { input.value = ''; input.placeholder = placeholder; return; }
    const labels = all.filter(o => selected.has(o.value)).map(o => o.label);
    if (labels.length <= 2) input.value = labels.join(', ');
    else input.value = `${labels[0]} + ${labels.length - 1} more`;
  }

  function render() {
    const f = filter.toLowerCase();
    panel.innerHTML = '';
    const matches = all.filter(o => o.label.toLowerCase().includes(f));
    if (matches.length === 0) {
      const d = document.createElement('div');
      d.className = 'combo-empty';
      d.textContent = all.length === 0 ? 'No items' : 'No matches';
      panel.appendChild(d);
      return;
    }
    matches.slice(0, 300).forEach(o => {
      const row = document.createElement('div');
      row.className = 'combo-option';
      row.style.display = 'flex'; row.style.alignItems = 'center'; row.style.gap = '8px';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = selected.has(o.value);
      cb.style.pointerEvents = 'none';
      const label = document.createElement('span'); label.textContent = o.label;
      row.appendChild(cb); row.appendChild(label);
      row.addEventListener('mousedown', (e) => {
        e.preventDefault();
        if (selected.has(o.value)) selected.delete(o.value); else selected.add(o.value);
        cb.checked = selected.has(o.value);
        updateInputText();
        onChange([...selected]);
      });
      panel.appendChild(row);
    });
  }

  input.addEventListener('focus', () => { filter = ''; render(); panel.classList.remove('hidden'); root.classList.add('open'); });
  input.addEventListener('input', () => { filter = input.value; render(); panel.classList.remove('hidden'); });
  input.addEventListener('blur', () => { setTimeout(() => { panel.classList.add('hidden'); root.classList.remove('open'); updateInputText(); }, 180); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Escape') { panel.classList.add('hidden'); input.blur(); } });

  return {
    setOptions(list) { all = list || []; selected = new Set([...selected].filter(v => all.some(o => o.value === v))); updateInputText(); },
    setValues(arr) { selected = new Set(arr || []); updateInputText(); },
    getValues() { return [...selected]; },
  };
}


// ── Activity log ─────────────────────────────────
function logEntry(text, type) {
  const body = $('logBody');
  const now = new Date().toTimeString().slice(0, 8);
  const entry = document.createElement('div');
  entry.className = 'log-entry';
  entry.innerHTML = `<span class="ts">${now}</span><span class="ev ${type || ''}">${escapeHtml(text)}</span>`;
  body.appendChild(entry);
  body.scrollTop = body.scrollHeight;
}

function logFromActivity(row) {
  const md = row.metadata ? JSON.stringify(row.metadata) : '';
  const text = `${row.entity_type}#${row.entity_id ?? '·'} ${row.action} by ${row.actor_type}${row.actor_id ? ':' + row.actor_id : ''} ${md ? '· ' + md : ''}`;
  let type = 'sys';
  if (row.action === 'approved' || row.action === 'csv_uploaded' || row.action === 'contacts_seeded') type = 'ok';
  if (row.action === 'rejected') type = 'warn';
  logEntry(text, type);
}

async function pollLog() {
  try {
    const rows = await API.get(`/api/campaigns/activity?limit=50&since_id=${state.lastLogId}`);
    // newest first; reverse for chronological display
    rows.reverse().forEach(row => {
      if (row.id > state.lastLogId) state.lastLogId = row.id;
      logFromActivity(row);
    });
  } catch (e) {
    console.warn('log poll failed:', e);
  }
}

// ── Opportunity loader ───────────────────────────
async function loadCategories() {
  let data;
  try {
    data = await API.get('/api/leads/categories');
  } catch (err) {
    logEntry(`Could not load categories: ${err.message}`, 'err');
    show('emptyState'); hide('mainShell');
    return;
  }
  const cats = data.categories || [];
  if (cats.length === 0) {
    categoryCombo.setOptions([]);
    show('emptyState'); hide('mainShell');
    logEntry('No categories found in the LeadsCategory Drive folder', 'sys');
    return;
  }
  categoryCombo.setOptions(cats.map(c => ({ value: c.name, label: c.name })));
  const saved = localStorage.getItem('denali.category');
  const chosen = (saved && cats.some(c => c.name === saved)) ? saved : cats[0].name;
  categoryCombo.setValue(chosen);
  state.category = chosen;
  await loadCategoryData();
}

async function loadCategoryData(forceRefresh = false) {
  if (!state.category) return;
  localStorage.setItem('denali.category', state.category);
  oppCombo.setOptions([{ value: '', label: '— loading… —' }]);
  let payload;
  try {
    const path = `/api/leads/category/${encodeURIComponent(state.category)}${forceRefresh ? '?refresh=true' : ''}`;
    payload = await API.get(path);
  } catch (err) {
    logEntry(`Could not load "${state.category}": ${err.message}`, 'err');
    show('emptyState'); hide('mainShell');
    return;
  }
  state.opps = payload.opportunities || [];
  logEntry(`Loaded ${state.opps.length} trials · ${payload.contact_count} contacts for ${state.category}`, 'ok');
  populateSubcategories();
  renderOpportunityDropdown();
}

// ── Subcategory (Conditions filter) ─────────────────
function splitConditions(s) {
  if (!s) return [];
  return String(s).split(/[;|]/).map(x => x.trim()).filter(Boolean);
}

function populateSubcategories() {
  const opts = [];
  const set = new Set();
  state.opps.forEach(o => splitConditions(o.indication).forEach(c => set.add(c)));
  [...set].sort((a, b) => a.localeCompare(b)).forEach(c => opts.push({ value: c, label: c }));
  subcategoryCombo.setOptions(opts);
  let saved = [];
  try { saved = JSON.parse(localStorage.getItem(`denali.subcategory.${state.category}`) || '[]'); } catch {}
  const valid = (saved || []).filter(v => opts.some(o => o.value === v));
  subcategoryCombo.setValues(valid);
  state.subcategory = valid;
}

function filteredOpps() {
  if (!state.subcategory || state.subcategory.length === 0) return state.opps;
  const wanted = new Set(state.subcategory.map(s => s.toLowerCase()));
  return state.opps.filter(o => splitConditions(o.indication).some(c => wanted.has(c.toLowerCase())));
}

function renderOpportunityDropdown() {
  const opps = filteredOpps();
  if (opps.length === 0) {
    oppCombo.setOptions([]);
    show('emptyState'); hide('mainShell');
    return;
  }
  hide('emptyState'); show('mainShell');
  const opts = opps.map(o => {
    const n = o.contacts ? o.contacts.length : 0;
    const who = o.sponsor_name || '(unknown sponsor)';
    const title = (o.trial_title && o.trial_title !== who) ? ` — ${o.trial_title}` : '';
    const label = `${who}${title} · ${n} contact${n === 1 ? '' : 's'}`;
    return { value: String(o.id), label: label.length > 95 ? label.slice(0, 95) + '…' : label };
  });
  oppCombo.setOptions(opts);
  oppCombo.setValue(String(opps[0].id));
  loadOpportunity(opps[0].id);
}

async function softRefreshCategory() {
  // Re-pull the current category so a just-sent lead disappears from the
  // dropdown, but DON'T replace the visible "Approved" draft view.
  if (!state.category) return;
  try {
    const path = `/api/leads/category/${encodeURIComponent(state.category)}?refresh=true`;
    const payload = await API.get(path);
    state.opps = payload.opportunities || [];
    populateSubcategories();
    const prev = oppCombo.getValue();
    const opps = filteredOpps();
    if (opps.length === 0) {
      oppCombo.setOptions([]);
      return;
    }
    const optsNew = opps.map(o => {
      const n = o.contacts ? o.contacts.length : 0;
      const who = o.sponsor_name || '(unknown sponsor)';
      const title = (o.trial_title && o.trial_title !== who) ? ` — ${o.trial_title}` : '';
      const label = `${who}${title} · ${n} contact${n === 1 ? '' : 's'}`;
      return { value: String(o.id), label: label.length > 95 ? label.slice(0, 95) + '…' : label };
    });
    oppCombo.setOptions(optsNew);
    if (optsNew.some(o => o.value === String(prev))) oppCombo.setValue(String(prev));
  } catch (err) {
    // silent — the email already went out; this is just a UX refresh
    console.warn('soft refresh failed:', err);
  }
}

function loadOpportunity(oppId) {
  const opp = state.opps.find(o => String(o.id) === String(oppId));
  if (!opp) return;
  state.oppId = opp.id;
  state.opp = opp;
  state.contacts = opp.contacts || [];
  state.primaryContact = state.contacts.find(c => c.is_primary) || state.contacts[0] || null;

  renderOpportunity(opp);
  renderContact(state.primaryContact);
  renderOtherContacts(state.contacts.filter(c => c !== state.primaryContact));
  resetDraftView();

  if (state.contacts.length === 0) {
    $('contactBody').innerHTML = `<div style="padding:16px;color:var(--amber);font-size:13px;background:var(--amber-light);border:1px solid #fed7aa;border-radius:8px;margin:4px;">
      <strong>No decision-maker contacts on this trial.</strong><br>
      <span style="font-size:12px;color:var(--ink-mid);display:block;margin-top:8px;">Pick another trial from the Opportunity dropdown.</span>
    </div>`;
    $('generateBtn').disabled = true;
    $('generateBtn').title = 'No contact to email on this trial';
  } else {
    $('generateBtn').disabled = false;
    $('generateBtn').title = '';
  }
}

// ── Renderers ────────────────────────────────────
function renderLiveSource(o) {
  const parts = [];
  if (o.source_url) parts.push(['Source URL', o.source_url]);
  if (o.full_text)  parts.push(['Full Text', o.full_text]);
  if (parts.length === 0) return '';
  const rows = parts.map(([k, v]) => {
    if (/^\s*https?:\/\//.test(String(v))) {
      return `<div class="raw-row"><span class="key">${escapeHtml(k)}</span><span class="val"><a href="${escapeHtml(v)}" target="_blank" rel="noopener">${escapeHtml(v)}</a></span></div>`;
    }
    return `<div class="raw-row"><span class="key">${escapeHtml(k)}</span><span class="val fulltext">${escapeHtml(v)}</span></div>`;
  }).join('');
  return `<div class="raw-data"><div class="raw-data-toggle"><span class="arrow">▶</span> Source data · ${parts.length} field${parts.length === 1 ? '' : 's'} from Clinwire</div><div class="raw-data-body">${rows}</div></div>`;
}

function renderOpportunity(o) {
  $('oppDate').textContent = o.category || '';
  const synthTag = o.synthetic ? ' · from leads' : '';
  $('oppBody').innerHTML = `
    <div class="source-tag">
      <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="8" r="6"/></svg>
      ${escapeHtml((o.category || 'Clinwire').toUpperCase())} Feed${synthTag}
    </div>
    <div class="opp-name">${escapeHtml(o.trial_title || '')}</div>
    <div class="opp-sub">${escapeHtml(o.sponsor_name || '')}</div>
    <div class="opp-grid">
      ${field('Phase',            o.phase)}
      ${field('Indication',       o.indication)}
      ${field('Drug',             o.drug)}
      ${field('Geography',        o.geography)}
      ${field('Trial ID',         o.trial_id, true)}
      ${field('Therapeutic Area', o.therapeutic_area, true)}
    </div>
    ${renderLiveSource(o)}
  `;

  // Wire the raw-data toggle (re-attached every render since we rebuild HTML)
  const rd = $('oppBody').querySelector('.raw-data');
  if (rd) {
    rd.querySelector('.raw-data-toggle').addEventListener('click', () => {
      rd.classList.toggle('open');
    });
  }
}

function renderRawData(raw) {
  if (!raw || typeof raw !== 'object' || Object.keys(raw).length === 0) {
    return '';
  }
  // Order keys: show "Full Text" / "Source URL" last because they're long
  const allKeys = Object.keys(raw);
  const longKeys = allKeys.filter(k => /full.?text|source.?url|description|summary/i.test(k));
  const shortKeys = allKeys.filter(k => !longKeys.includes(k));
  const ordered = [...shortKeys, ...longKeys];

  const rows = ordered.map(key => {
    const v = raw[key];
    if (v == null || v === '') return '';
    let valHtml;
    if (/^\s*https?:\/\//.test(String(v))) {
      valHtml = `<a href="${escapeHtml(v)}" target="_blank" rel="noopener">${escapeHtml(v)}</a>`;
    } else if (/full.?text|description|summary/i.test(key) && String(v).length > 200) {
      valHtml = `<span class="val fulltext">${escapeHtml(v)}</span>`;
      return `<div class="raw-row"><span class="key">${escapeHtml(key)}</span>${valHtml}</div>`;
    } else {
      valHtml = escapeHtml(String(v));
    }
    return `<div class="raw-row"><span class="key">${escapeHtml(key)}</span><span class="val">${valHtml}</span></div>`;
  }).join('');

  return `
    <div class="raw-data">
      <div class="raw-data-toggle">
        <span class="arrow">▶</span>
        Source data · ${ordered.length} fields from Clinwire
      </div>
      <div class="raw-data-body">${rows}</div>
    </div>
  `;
}

function field(label, value, full = false) {
  return `
    <div class="opp-field${full ? ' full' : ''}">
      <div class="opp-field-label">${escapeHtml(label)}</div>
      <div class="opp-field-val">${escapeHtml(value || '—')}</div>
    </div>
  `;
}

function renderContact(c) {
  if (!c) {
    $('contactBody').innerHTML = '';
    $('contactConfidence').style.display = 'none';
    $('toEmailInput').value = '';
    return;
  }
  $('contactConfidence').style.display = '';
  // Pre-fill the "To:" override field; highlight + nudge when the lead has none.
  const inp = $('toEmailInput');
  inp.value = c.email || '';
  if (!c.email) {
    inp.style.borderColor = '#f59e0b';
    inp.style.background  = '#fffbeb';
    inp.placeholder       = 'No email on file — type one to send';
  } else {
    inp.style.borderColor = '';
    inp.style.background  = '';
    inp.placeholder       = 'recipient@example.com';
  }
  // "Save typed email back to the Sheet" default: ON when the lead has no email
  // (you're adding a missing one), OFF when overriding an existing one.
  const saveCb = $('saveEmailToSheet');
  if (saveCb) saveCb.checked = !c.email;
  const fullName = c.full_name || ((c.first_name || '') + ' ' + (c.last_name || '')).trim();
  const initials = ((c.first_name || c.full_name || '?')[0] + ((c.last_name || ' ')[0] || ' ')).toUpperCase();
  const score = c.contact_score ?? 0;
  const rank = c.priority_rank ? `Priority #${c.priority_rank}` : 'Decision-maker for outreach';
  const verified = !!c.email_verified || /verif/i.test(c.email_status || '');

  $('contactBody').innerHTML = `
    <div class="contact-row">
      <div class="avatar">${escapeHtml(initials)}</div>
      <div>
        <div class="contact-name">${escapeHtml(fullName)}</div>
        <div class="contact-title">${escapeHtml(c.title || '')}</div>
        <div class="contact-co">${escapeHtml(c.company || state.opp?.sponsor_name || '')}</div>
      </div>
    </div>
    <div class="score-row">
      <div>
        <div class="score-label">Priority Score</div>
        <div style="font-size:12px;color:#15803d;margin-top:3px;">${escapeHtml(rank)}</div>
      </div>
      <div style="text-align:right;">
        <div class="score-val">${score}</div>
        <div class="score-sub">out of 100</div>
      </div>
    </div>
    ${c.notes ? `
      <div class="ai-reasoning">
        <div class="ai-reasoning-label">Outreach notes</div>
        ${escapeHtml(c.notes)}
      </div>
    ` : ''}
    <div class="contact-meta">
      ${c.email ? `<span class="meta-chip">${escapeHtml(c.email)}</span>` : ''}
      ${c.geography ? `<span class="meta-chip">${escapeHtml(c.geography)}</span>` : ''}
      ${c.phone ? `<span class="meta-chip">${escapeHtml(c.phone)}</span>` : ''}
      <span class="meta-chip">${verified ? '✓ ' + escapeHtml(c.email_status || 'verified') : '⚠ ' + escapeHtml(c.email_status || 'unverified')}</span>
      ${c.linkedin_url ? `<a class="meta-chip" href="${escapeHtml(c.linkedin_url)}" target="_blank" rel="noopener">LinkedIn ↗</a>` : ''}
      ${c.apollo_url ? `<a class="meta-chip" href="${escapeHtml(c.apollo_url)}" target="_blank" rel="noopener">Apollo ↗</a>` : ''}
    </div>
  `;
}

function scoreBar(label, val, max) {
  const v = val ?? 0;
  const pct = Math.round((v / max) * 100);
  let color = '#15803d';
  if (pct < 80) color = '#c8720a';
  if (pct < 40) color = '#b91c1c';
  return `
    <div class="score-bar-wrap">
      <div class="score-dim">
        <span class="score-dim-label">${escapeHtml(label)}</span>
        <span class="score-dim-pts">${v} / ${max}</span>
      </div>
      <div class="score-bar-track">
        <div class="score-bar-fill" style="width:${pct}%;background:${color};"></div>
      </div>
    </div>
  `;
}

function renderOtherContacts(others) {
  const el = $('contactListOther');
  if (others.length === 0) { hide('contactListOther'); return; }
  show('contactListOther');
  el.innerHTML = `<div class="contact-list-label">Other contacts (${others.length})</div>` +
    others.map(c => `
      <div class="contact-mini" data-cid="${c.id}">
        <div>
          <div class="contact-mini-name">${escapeHtml((c.first_name || '') + ' ' + (c.last_name || ''))}</div>
          <div class="contact-mini-title">${escapeHtml(c.title || '')}</div>
        </div>
        <div class="contact-mini-score">${c.contact_score ?? '—'}</div>
      </div>
    `).join('');
  el.querySelectorAll('.contact-mini').forEach(el => {
    el.addEventListener('click', () => {
      const cid = el.dataset.cid;
      const newPrimary = state.contacts.find(c => String(c.id) === String(cid));
      if (newPrimary) {
        const old = state.primaryContact;
        state.primaryContact = newPrimary;
        renderContact(newPrimary);
        renderOtherContacts(state.contacts.filter(c => c !== newPrimary));
        resetDraftView();
        logEntry(`Switched primary contact to ${newPrimary.first_name} ${newPrimary.last_name}`, 'sys');
      }
    });
  });
}

// ── Draft view ───────────────────────────────────
function resetDraftView() {
  state.draft = null;
  state.isEditing = false;
  hide('generatingState');
  hide('draftResult');
  hide('approvedState');
  hide('rejectedState');
  hide('approvalBar');
  hide('generateError');
  show('preGenerate');
  $('draftStatus').textContent = 'Not generated';
  $('editBtn').textContent = '✏ Edit';
}

function showLoading() {
  hide('preGenerate');
  hide('draftResult');
  hide('approvedState');
  hide('rejectedState');
  show('generatingState');
  $('draftStatus').textContent = 'Generating...';
  // Cycle a few loading messages while we wait
  const steps = [
    { text: 'Reading trial data...',     sub: 'Parsing opportunity context' },
    { text: 'Matching contact...',       sub: 'Reviewing score breakdown' },
    { text: 'Drafting email...',         sub: 'Calling Claude' },
    { text: 'Evaluating quality flags...', sub: 'Checking word count + personalisation' },
  ];
  let i = 0;
  const iv = setInterval(() => {
    if (i >= steps.length || !$('generatingState').classList.contains('hidden') === false) {
      clearInterval(iv); return;
    }
    $('generatingText').textContent = steps[i].text;
    $('generatingSubtext').textContent = steps[i].sub;
    i++;
  }, 700);
}

function renderDraft(d) {
  state.draft = d;
  hide('generatingState');
  hide('approvedState');
  hide('rejectedState');
  show('draftResult');
  show('approvalBar');
  $('draftStatus').textContent = 'Draft ready';

  $('subjectDisplay').textContent = d.subject_line;
  $('subjectEdit').value = d.subject_line;
  $('bodyDisplay').textContent = d.body_text;
  $('bodyEdit').value = d.body_text;

  const flags = d.quality_flags || [];
  $('qFlags').innerHTML = flags.map(f => {
    let cls = 'flag';
    if (f.startsWith('✓')) cls += ' ok';
    else if (f.startsWith('📌')) cls += ' signal';
    return `<span class="${cls}">${escapeHtml(f)}</span>`;
  }).join('');
}

// ── Action handlers ──────────────────────────────
async function onUpload(e) {
  const f = e.target.files[0];
  if (!f) return;
  if (!f.name.toLowerCase().endsWith('.csv')) {
    logEntry('Upload error: not a CSV file', 'err');
    return;
  }
  const fd = new FormData();
  fd.append('file', f);
  try {
    logEntry(`Uploading ${f.name}...`, 'sys');
    const res = await API.post('/api/opportunities/upload', fd, true);
    logEntry(`Uploaded: ${res.inserted} new, ${res.duplicates} duplicates, ${res.skipped} skipped`, 'ok');
    e.target.value = '';
    await loadCategoryData(true);
  } catch (err) {
    logEntry(`Upload failed: ${err.message}`, 'err');
  }
}

async function onSyncSheets() {
  const btn = $('syncBtn');
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = '↻ Refreshing...';
  try {
    await API.post('/api/leads/refresh', {});
    await loadCategoryData(true);
    logEntry(`Refreshed "${state.category}" from Google Sheet`, 'ok');
  } catch (err) {
    logEntry(`Refresh failed: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function onSeed() {
  if (!state.oppId) return;
  $('seedBtn').disabled = true;
  try {
    const res = await API.post(`/api/contacts/seed-dummy/${state.oppId}?n=3`, {});
    logEntry(`Seeded ${res.count} dummy contacts for opp #${state.oppId}`, 'ok');
    await loadOpportunity(state.oppId);
  } catch (err) {
    logEntry(`Seed failed: ${err.message}`, 'err');
  } finally {
    $('seedBtn').disabled = false;
  }
}

function onToggleAddContact() {
  const form = $('addContactForm');
  if (form.classList.contains('hidden')) {
    form.classList.remove('hidden');
    $('acFirst').focus();
    $('acError').textContent = '';
  } else {
    form.classList.add('hidden');
  }
}

async function onSubmitContact() {
  if (!state.opp) {
    $('acError').textContent = 'Pick an opportunity first';
    return;
  }
  const first = $('acFirst').value.trim();
  const last  = $('acLast').value.trim();
  const email = $('acEmail').value.trim();
  const title = $('acTitle').value.trim();
  if (!first) {
    $('acError').textContent = 'First name is required';
    return;
  }
  const name = (first + ' ' + last).trim();
  $('acSubmit').disabled = true;
  $('acError').textContent = '';
  try {
    await API.post(`/api/leads/category/${encodeURIComponent(state.category)}/contact`, {
      trial_id:       state.opp.trial_id || '',
      company:        state.opp.sponsor_name || '',
      phase:          state.opp.phase || '',
      condition:      state.opp.indication || '',
      name:           name,
      title:          title || null,
      business_email: email || null,
    });
    logEntry(`Added contact ${name} to ${state.opp.sponsor_name || 'this trial'}`, 'ok');
    $('addContactForm').classList.add('hidden');
    ['acFirst','acLast','acEmail','acTitle'].forEach(id => { $(id).value = ''; });
    await loadCategoryData(true);   // refresh so the new contact shows up
  } catch (err) {
    $('acError').textContent = `Failed: ${err.message}`;
  } finally {
    $('acSubmit').disabled = false;
  }
}

async function onGenerate() {
  if (!state.opp || !state.primaryContact) {
    logEntry('Cannot generate: need an opportunity + contact', 'err');
    return;
  }
  hide('generateError');
  showLoading();
  const templateFilename = $('templateSelect').value || null;
  if (templateFilename) {
    logEntry(`Generating with template: ${templateFilename}`, 'sys');
  }
  try {
    const draft = await API.post('/api/drafts/generate', {
      opportunity: state.opp,
      contact: state.primaryContact,
      template_filename: templateFilename,
    });
    renderDraft(draft);
  } catch (err) {
    hide('generatingState');
    show('preGenerate');
    $('generateError').textContent = err.message;
    show('generateError');
    logEntry(`Generation failed: ${err.message}`, 'err');
  }
}

function onToggleEdit() {
  state.isEditing = !state.isEditing;
  $('subjectDisplay').classList.toggle('hidden', state.isEditing);
  $('subjectEdit').classList.toggle('hidden', !state.isEditing);
  $('bodyDisplay').classList.toggle('hidden', state.isEditing);
  $('bodyEdit').classList.toggle('hidden', !state.isEditing);
  $('editBtn').textContent = state.isEditing ? '✓ Done editing' : '✏ Edit';
  if (!state.isEditing) {
    $('subjectDisplay').textContent = $('subjectEdit').value;
    $('bodyDisplay').textContent = $('bodyEdit').value;
  }
}

// ── Send history ─────────────────────────────────
async function loadSendHistory() {
  try {
    const data = await API.get('/api/campaigns/sent-history?limit=50');
    renderSendHistory(data.sends || []);
  } catch (err) {
    console.warn('send-history load failed:', err);
  }
}

function renderSendHistory(sends) {
  const body = $('sendHistoryBody');
  if (!sends || sends.length === 0) {
    body.innerHTML = '<div style="padding:20px;text-align:center;color:var(--ink-soft);font-size:13px;">No emails sent yet — approve a draft with a mailbox selected to see it here.</div>';
    return;
  }
  body.innerHTML = sends.map(s => {
    const t = s.sent_at ? new Date(s.sent_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '—';
    const date = s.sent_at ? new Date(s.sent_at).toLocaleDateString([], {month:'short', day:'numeric'}) : '';
    const overrideTag = s.is_to_overridden ? '<span class="override-tag">TO OVERRIDDEN</span>' : '';
    return `
      <div class="send-row" data-send-id="${s.send_id}">
        <div class="send-time">${escapeHtml(t)}<br><span style="opacity:0.7;font-size:10px;">${escapeHtml(date)}</span></div>
        <div class="send-main">
          <div class="send-subject">${escapeHtml(s.subject || '(no subject)')}</div>
          <div class="send-meta">
            ${escapeHtml(s.from_mailbox_email || '?')}<span class="arrow">→</span>${escapeHtml(s.recipient_email || '?')}${overrideTag}
            <br>
            <span style="opacity:0.7;">${escapeHtml(s.opportunity_title || '')}  ·  <span class="clickable-name" data-email="${escapeHtml(s.recipient_email || '')}">${escapeHtml(s.contact_name || '')}</span></span>
          </div>
        </div>
        <div class="send-status-pill ${escapeHtml(s.send_status || 'queued')}">${escapeHtml((s.send_status || 'queued').toUpperCase())}</div>
      </div>
    `;
  }).join('');

  // Wire row clicks → expand a detail panel inline
  body.querySelectorAll('.send-row').forEach(row => {
    row.addEventListener('click', () => {
      const sendId = parseInt(row.dataset.sendId, 10);
      const send = sends.find(s => s.send_id === sendId);
      if (!send) return;
      // Toggle existing detail panel
      const next = row.nextElementSibling;
      if (next && next.classList.contains('send-detail')) {
        next.remove();
        return;
      }
      const det = document.createElement('div');
      det.className = 'send-detail';
      det.innerHTML = `
        <div><span class="label">Send ID:</span> #${send.send_id} (draft #${send.draft_id})</div>
        <div><span class="label">From:</span> ${escapeHtml(send.from_mailbox_email || '')}</div>
        <div><span class="label">To:</span> ${escapeHtml(send.recipient_email || '')}${send.is_to_overridden ? ' <span class="override-tag">override of stored '+escapeHtml(send.contact_stored_email||'')+'</span>' : ''}</div>
        <div><span class="label">Subject:</span> ${escapeHtml(send.subject || '')}</div>
        <div><span class="label">Sent at:</span> ${escapeHtml(send.sent_at || '—')}</div>
        <div><span class="label">Approved by:</span> ${escapeHtml(send.approved_by || '—')} ${send.approved_at ? '· ' + escapeHtml(send.approved_at) : ''}</div>
        <div><span class="label">Message-ID:</span> ${escapeHtml(send.message_id || '—')}</div>
        <div><span class="label">Opportunity:</span> ${escapeHtml(send.opportunity_title || '')}</div>
        <div><span class="label">Contact:</span> ${escapeHtml(send.contact_name || '')} (${escapeHtml(send.contact_title || '')})</div>
        <div class="body-block">${escapeHtml(send.body || '')}</div>
      `;
      row.parentNode.insertBefore(det, row.nextSibling);
    });
  });
}

async function loadMailboxes() {
  try {
    const data = await API.get('/api/campaigns/mailboxes');
    const sel = $('mailboxSelect');
    while (sel.options.length > 1) sel.remove(1);
    (data.mailboxes || []).forEach(mb => {
      const label = `${mb.display_name} <${mb.email}>${mb.ready ? '' : '  (dry-run)'}`;
      sel.appendChild(new Option(label, mb.email));
    });
    if (data.mailboxes && data.mailboxes.length > 0) {
      // Prefer last-used mailbox if it still exists, else first ready, else first
      const saved = localStorage.getItem('denali_last_mailbox');
      if (saved && Array.from(sel.options).some(o => o.value === saved)) {
        sel.value = saved;
      } else {
        const firstReady = data.mailboxes.find(m => m.ready) || data.mailboxes[0];
        sel.value = firstReady.email;
      }
    }
    // Save on every change
    sel.addEventListener('change', () => {
      localStorage.setItem('denali_last_mailbox', sel.value);
    });
    logEntry(`Loaded ${data.mailboxes ? data.mailboxes.length : 0} mailboxes`, 'sys');
  } catch (err) {
    logEntry(`Mailbox load failed: ${err.message}`, 'err');
  }
}

async function loadTemplates(forceRefresh = false) {
  try {
    const path = forceRefresh ? '/api/campaigns/templates?refresh=true' : '/api/campaigns/templates';
    const data = await API.get(path);
    const sel = $('templateSelect');
    while (sel.options.length > 1) sel.remove(1);
    (data.templates || []).forEach(t => {
      sel.appendChild(new Option(`${t.display_name}  (${t.filename})`, t.filename));
    });
    // Restore last-used template from localStorage if it still exists in the list
    const saved = localStorage.getItem('denali_last_template');
    if (saved && Array.from(sel.options).some(o => o.value === saved)) {
      sel.value = saved;
    }
    // Save on every change
    sel.addEventListener('change', () => {
      localStorage.setItem('denali_last_template', sel.value);
    });
    logEntry(`Loaded ${data.templates ? data.templates.length : 0} email templates`, 'sys');
  } catch (err) {
    logEntry(`Template load failed: ${err.message}`, 'err');
  }
}

async function onApprove() {
  if (!state.draft) return;
  const approver = window.prompt('Approve as (your name)?', 'Maryam');
  if (!approver) return;
  const fromMailbox = $('mailboxSelect').value || null;
  const editedSubject = $('subjectEdit').value !== state.draft.subject_line ? $('subjectEdit').value : null;
  const editedBody    = $('bodyEdit').value    !== state.draft.body_text    ? $('bodyEdit').value    : null;

  // Resolve the recipient. If the user changed it from the contact's stored email,
  // pass it as an override (single send only — doesn't update the contact record).
  const enteredTo  = $('toEmailInput').value.trim();
  const storedTo   = (state.primaryContact && state.primaryContact.email) || '';
  const toOverride = (enteredTo && enteredTo.toLowerCase() !== storedTo.toLowerCase()) ? enteredTo : null;

  // Optional attachments picked from the user's computer (single send only).
  const attachFiles = ($('attachmentInput').files && Array.from($('attachmentInput').files)) || [];

  // Build multipart form data — required so the files can ride along.
  const fd = new FormData();
  fd.append('approved_by', approver);
  if (fromMailbox)   fd.append('from_mailbox', fromMailbox);
  if (toOverride)    fd.append('to_email_override', toOverride);
  if (editedSubject) fd.append('edited_subject', editedSubject);
  if (editedBody)    fd.append('edited_body', editedBody);
  attachFiles.forEach(f => fd.append('attachments', f, f.name));
  if ($('saveEmailToSheet') && $('saveEmailToSheet').checked && toOverride) fd.append('save_recipient_email', '1');

  try {
    const res = await API.post(`/api/drafts/${state.draft.id}/approve`, fd, true);
    const draft = res.draft || res;            // backend now returns { draft, send }
    const send  = res.send || null;
    hide('draftResult');
    hide('approvalBar');
    show('approvedState');
    $('approvedBy').textContent = draft.approved_by;
    $('approvedTime').textContent = new Date(draft.approved_at).toLocaleString();
    $('draftStatus').textContent = send ? (send.dry_run ? 'Approved (dry-run send)' : 'Approved & sent') : 'Approved';
    if (send) {
      const mode = send.dry_run ? 'DRY-RUN' : 'SENT';
      const overrideTag = send.to_overridden ? ' [TO OVERRIDDEN]' : '';
      const attachTag = send.attachment_count ? `  📎 ${send.attachment_count} file(s): ${send.attachment}` : '';
      logEntry(`${mode}: ${send.sent_via} → ${send.to}${overrideTag}${attachTag}  msg-id=${send.message_id}`, 'ok');
      loadSendHistory();   // refresh the history panel so the new row shows up
      softRefreshCategory();   // sent contact disappears from the dropdown
      // Refresh analytics widgets only if that tab is visible.
      const aPage = document.getElementById('page-analytics');
      if (aPage && aPage.classList.contains('active')) {
        loadMetrics(); loadRecentOutreach(); loadMailboxBars(); loadTopRecipients(); loadHistoryFiltered();
      }
    } else {
      logEntry('Draft approved (no mailbox selected — not sent)', 'sys');
    }
    // Reset the attachment picker after a successful approve/send.
    clearAttachment();
  } catch (err) {
    logEntry(`Approve failed: ${err.message}`, 'err');
  }
}

// Clear the attachment file input and hide the "clear" button.
function clearAttachment() {
  const inp = $('attachmentInput');
  if (inp) inp.value = '';
  hide('attachClearBtn');
}

async function onReject() {
  if (!state.draft) return;
  const reason = window.prompt('Reason for rejection?');
  if (!reason) return;
  const rejecter = window.prompt('Rejected by (your name)?', 'Maryam');
  if (!rejecter) return;
  try {
    await API.post(`/api/drafts/${state.draft.id}/reject`, {
      rejected_by: rejecter, rejection_reason: reason,
    });
    hide('draftResult');
    hide('approvalBar');
    show('rejectedState');
    $('draftStatus').textContent = 'Rejected';
  } catch (err) {
    logEntry(`Reject failed: ${err.message}`, 'err');
  }
}

// ── Init ─────────────────────────────────────────
async function _initDenaliApp() {
  $('csvFile').addEventListener('change', onUpload);
  categoryCombo = makeCombo('categoryCombo', 'Type to search categories…', (val) => {
    state.category = val; loadCategoryData();
  });
  subcategoryCombo = makeMultiCombo('subcategoryCombo', 'All conditions (tick to filter)', (vals) => {
    state.subcategory = vals;
    if (state.category) localStorage.setItem(`denali.subcategory.${state.category}`, JSON.stringify(vals));
    renderOpportunityDropdown();
  });
  oppCombo = makeCombo('oppCombo', 'Type to search trials…', (val) => loadOpportunity(val));
  $('syncBtn').addEventListener('click', onSyncSheets);
  $('refreshTemplatesBtn').addEventListener('click', async () => {
    const btn = $('refreshTemplatesBtn');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '⏳';
    try {
      const r = await API.post('/api/campaigns/templates/refresh', {});
      logEntry(`Templates refreshed: ${(r.templates || []).length} available (drive cache cleared)`, 'ok');
      await loadTemplates(true);
    } catch (err) {
      logEntry(`Template refresh failed: ${err.message}`, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  });
  $('seedBtn').addEventListener('click', onSeed);
  $('addContactBtn').addEventListener('click', onToggleAddContact);
  $('acSubmit').addEventListener('click', onSubmitContact);
  $('acCancel').addEventListener('click', onToggleAddContact);
  $('generateBtn').addEventListener('click', onGenerate);
  $('editBtn').addEventListener('click', onToggleEdit);
  $('approveBtn').addEventListener('click', onApprove);
  $('rejectBtn').addEventListener('click', onReject);
  $('regenerateBtn').addEventListener('click', () => { resetDraftView(); onGenerate(); });

  // Attachment picker: reveal the "clear" button once a file is chosen.
  $('attachmentInput').addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length) show('attachClearBtn');
    else hide('attachClearBtn');
  });
  $('attachClearBtn').addEventListener('click', clearAttachment);

  $('refreshHistoryBtn').addEventListener('click', loadSendHistory);

  logEntry('Session started', 'sys');
  await loadMailboxes();
  await loadTemplates();
  await loadCategories();
  await loadSendHistory();
  await pollLog();
  state.logPollInterval = setInterval(pollLog, 2000);
  _phase2Init();
}

// Run immediately if DOM already loaded, otherwise wait for it.
if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', _initDenaliApp);
} else {
  _initDenaliApp();
}


// ──────────────────────────────────────────────────────────────
// Phase 2: metric tiles, per-category strip, Recent Outreach feed,
// filterable Send History, per-contact side panel.
// ──────────────────────────────────────────────────────────────

function _fmt(n) { return n == null ? '—' : String(n); }
function _shortTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const days = Math.floor((now - d) / 86400000);
  if (days <= 7) return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

async function loadMetrics() {
  try {
    const m = await API.get('/api/campaigns/metrics');
    $('mSentToday').textContent      = _fmt(m.sent_today);
    $('mSentThisWeek').textContent   = _fmt(m.sent_this_week);
    $('mSentThisMonth').textContent  = _fmt(m.sent_this_month);
    $('mActiveLeads').textContent    = _fmt(m.active_leads);
    $('mSentTodaySub').textContent     = `of ${m.sent_total} total all-time`;
    $('mSentThisWeekSub').textContent  = `since Monday`;
    $('mSentThisMonthSub').textContent = `since the 1st`;
    $('mActiveLeadsSub').textContent   = m.active_leads == null
      ? 'load a category to populate'
      : 'across loaded categories';
    // Per-category strip
    const strip = $('catStrip');
    strip.innerHTML = '';
    (m.by_category || []).forEach(c => {
      const tile = document.createElement('div');
      tile.className = 'cat-tile';
      tile.innerHTML = `<div class="cn">${escapeHtml(c.name)}</div><div class="cm">${c.sent_this_week} this week · ${c.sent_total} total</div>`;
      tile.addEventListener('click', () => {
        // jump send-history filter to that category
        const sel = $('histCategory');
        if (sel) { sel.value = c.name; loadHistoryFiltered(); }
      });
      strip.appendChild(tile);
    });
  } catch (err) {
    console.warn('metrics failed:', err);
  }
}

async function loadRecentOutreach() {
  try {
    const data = await API.get('/api/campaigns/sent-history?limit=5');
    const list = $('recentList');
    list.innerHTML = '';
    const sends = data.sends || [];
    if (sends.length === 0) {
      $('recentEmpty').classList.remove('hidden');
      return;
    }
    $('recentEmpty').classList.add('hidden');
    sends.forEach(s => {
      const row = document.createElement('div');
      row.className = 'recent-row';
      const status = (s.send_status || 'sent').toUpperCase();
      row.innerHTML = `
        <div class="t">${escapeHtml(_shortTime(s.sent_at))}</div>
        <div>
          <div class="s">${escapeHtml(s.subject || '(no subject)')}</div>
          <div class="m"><span class="clickable-name" data-email="${escapeHtml(s.recipient_email || '')}">${escapeHtml(s.contact_name || s.recipient_email || '—')}</span> · ${escapeHtml(s.sponsor_name || s.opportunity_title || '')} · ${escapeHtml(s.category || '')}</div>
        </div>
        <div class="p"><span class="meta-chip">${escapeHtml(status)}</span></div>
      `;
      row.querySelector('.clickable-name').addEventListener('click', (e) => {
        e.stopPropagation();
        openContactPanel(e.currentTarget.dataset.email);
      });
      list.appendChild(row);
    });
  } catch (err) {
    console.warn('recent outreach failed:', err);
  }
}

// ── Filterable send history ──────────────────────────────
function _populateHistFilters() {
  // Mailboxes
  const mbSel = $('histMailbox');
  if (mbSel && mbSel.options.length <= 1) {
    fetch('/api/campaigns/mailboxes').then(r => r.json()).then(d => {
      (d.mailboxes || []).forEach(m => {
        const o = document.createElement('option');
        o.value = m.email; o.textContent = m.display_name || m.email;
        mbSel.appendChild(o);
      });
    }).catch(() => {});
  }
  // Categories from /api/leads/categories
  const catSel = $('histCategory');
  if (catSel && catSel.options.length <= 1) {
    fetch('/api/leads/categories').then(r => r.json()).then(d => {
      (d.categories || []).forEach(c => {
        const o = document.createElement('option');
        o.value = c.name; o.textContent = c.name;
        catSel.appendChild(o);
      });
    }).catch(() => {});
  }
}

let _histDebounce = null;
function _scheduleHistory() {
  if (_histDebounce) clearTimeout(_histDebounce);
  _histDebounce = setTimeout(loadHistoryFiltered, 250);
}

async function loadHistoryFiltered() {
  const search   = ($('histSearch')?.value || '').trim();
  const category = $('histCategory')?.value || '';
  const mailbox  = $('histMailbox')?.value  || '';
  const status   = $('histStatus')?.value   || '';
  const range    = $('histRange')?.value    || '';
  const params = new URLSearchParams({ limit: '100' });
  if (search)   params.set('search', search);
  if (category) params.set('category', category);
  if (mailbox)  params.set('mailbox', mailbox);
  if (status)   params.set('status', status);
  if (range) {
    const from = new Date(Date.now() - parseInt(range, 10) * 86400000);
    params.set('date_from', from.toISOString().slice(0, 19));
  }
  try {
    const data = await API.get(`/api/campaigns/sent-history?${params}`);
    renderSendHistory(data.sends || []);
    const c = $('histCount');
    if (c) c.textContent = `${data.count || 0} shown`;
  } catch (err) {
    logEntry(`History load failed: ${err.message}`, 'err');
  }
}

// ── Per-contact side panel ──────────────────────────────
async function openContactPanel(email) {
  if (!email) return;
  const panel = $('sidePanel');
  panel.classList.add('open');
  panel.setAttribute('aria-hidden', 'false');
  $('sideName').textContent  = email;
  $('sideTitle').textContent = '…loading…';
  $('sideSent').textContent = '—';
  $('sideReplied').textContent = '—';
  $('sideCats').textContent = '—';
  $('sideTimeline').innerHTML = '';
  try {
    const data = await API.get(`/api/campaigns/contact-history?email=${encodeURIComponent(email)}`);
    $('sideName').textContent  = data.contact_name || email;
    $('sideTitle').textContent = data.contact_title || email;
    const sends = data.sends || [];
    const replied = sends.filter(s => s.send_status === 'replied').length;
    const cats = new Set(sends.map(s => s.category).filter(Boolean));
    $('sideSent').textContent    = sends.length;
    $('sideReplied').textContent = replied;
    $('sideCats').textContent    = cats.size;
    const tl = $('sideTimeline');
    tl.innerHTML = '';
    if (sends.length === 0) {
      tl.innerHTML = '<div style="font-size:13px;color:#6b7280;">No prior sends to this address.</div>';
      return;
    }
    sends.forEach(s => {
      const item = document.createElement('div');
      item.className = 'side-item';
      item.innerHTML = `
        <div class="side-time">${escapeHtml(_shortTime(s.sent_at))} · ${escapeHtml((s.from_mailbox_email || '').split('@')[0] || '?')} → ${escapeHtml(s.recipient_email || '')}</div>
        <div class="side-subj">${escapeHtml(s.subject || '(no subject)')}</div>
        <div class="side-trial">${escapeHtml(s.sponsor_name || s.opportunity_title || '')} · ${escapeHtml(s.category || '')}</div>
      `;
      tl.appendChild(item);
    });
  } catch (err) {
    $('sideTimeline').innerHTML = `<div style="color:#b91c1c;font-size:13px;">Couldn't load: ${escapeHtml(err.message)}</div>`;
  }
}
function closeContactPanel() {
  const p = $('sidePanel');
  p.classList.remove('open');
  p.setAttribute('aria-hidden', 'true');
}

// ── Phase 2 init: hooks + first load + auto-refresh ──────
function _phase2Init() {
  // Close button + Escape
  const sc = $('sideClose'); if (sc) sc.addEventListener('click', closeContactPanel);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeContactPanel(); });
  // Filter listeners
  ['histSearch','histCategory','histMailbox','histStatus','histRange'].forEach(id => {
    const el = $(id); if (!el) return;
    el.addEventListener(id === 'histSearch' ? 'input' : 'change', _scheduleHistory);
  });
  // Analytics widgets only load when the Analytics tab is opened (see switchTab).
  // Pre-populate filter dropdowns so they're ready when user switches tabs.
  _populateHistFilters();
}

// Hook a contact-name click everywhere on the page (event delegation for already-rendered names).
document.addEventListener('click', (e) => {
  const t = e.target.closest('.clickable-name');
  if (t && t.dataset.email) {
    e.preventDefault();
    openContactPanel(t.dataset.email);
  }
});


/* ───────────── Phase 2.1: Analytics tab + widgets ───────────── */

let _analyticsLoaded = false;
let _analyticsTimer  = null;

function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === ('page-' + tab)));
  if (tab === 'analytics') {
    loadAnalytics(true);
    if (!_analyticsTimer) _analyticsTimer = setInterval(() => loadAnalytics(false), 60000);
  } else {
    if (_analyticsTimer) { clearInterval(_analyticsTimer); _analyticsTimer = null; }
  }
}

async function loadAnalytics(includeHistory) {
  try {
    if (typeof loadMetrics         === 'function') await loadMetrics();
    if (typeof loadRecentOutreach  === 'function') await loadRecentOutreach();
    await loadMailboxBars();
    await loadTopRecipients();
    if (includeHistory && typeof loadHistoryFiltered === 'function') {
      if (typeof _populateHistFilters === 'function') _populateHistFilters();
      loadHistoryFiltered();
    }
    _analyticsLoaded = true;
  } catch (e) { console.error('analytics load failed', e); }
}

async function loadMailboxBars() {
  const el = document.getElementById('mailboxBars');
  if (!el) return;
  try {
    const r = await fetch('/api/campaigns/by-mailbox?days=30');
    const data = await r.json();
    const rows = data.by_mailbox || [];
    if (!rows.length) {
      el.innerHTML = '<div style="padding:14px;text-align:center;color:#6b7280;font-size:12px;">No sends in last 30 days.</div>';
      return;
    }
    const max = Math.max(...rows.map(r => r.sent || 0)) || 1;
    el.innerHTML = rows.map(r => {
      const pct = Math.round(((r.sent || 0) / max) * 100);
      const mb  = (r.mailbox || '—').split('@')[0];
      return '<div class="bar-row">' +
               '<div title="' + (r.mailbox || '') + '">' + mb + '</div>' +
               '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%"></div></div>' +
               '<div class="bar-value">' + (r.sent || 0) + '</div>' +
             '</div>';
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="padding:14px;text-align:center;color:#dc2626;font-size:12px;">Failed to load.</div>';
  }
}

async function loadTopRecipients() {
  const el = document.getElementById('topRecipients');
  if (!el) return;
  try {
    const r = await fetch('/api/campaigns/top-recipients?days=90&limit=15');
    const data = await r.json();
    const rows = data.recipients || [];
    if (!rows.length) {
      el.innerHTML = '<div style="padding:14px;text-align:center;color:#6b7280;font-size:12px;">No sends in last 90 days.</div>';
      return;
    }
    el.innerHTML = rows.map(r => {
      const name = r.contact_name || r.email;
      const sub  = [r.contact_title, r.sponsor_name].filter(Boolean).join(' · ') || r.email;
      const safe = (r.email || '').replace(/'/g, "&#39;");
      return '<div class="recip-row" onclick="openContactPanel(\'' + safe + '\')">' +
               '<div>' +
                 '<div class="recip-name clickable-name">' + name + '</div>' +
                 '<div class="recip-sub">' + sub + '</div>' +
               '</div>' +
               '<div class="recip-count">' + r.sent + '</div>' +
             '</div>';
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="padding:14px;text-align:center;color:#dc2626;font-size:12px;">Failed to load.</div>';
  }
}

document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
});
