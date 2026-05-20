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
async function refreshOpportunityList() {
  const opps = await API.get('/api/opportunities/?limit=50');
  const sel = $('oppSelect');
  clear(sel);
  if (opps.length === 0) {
    sel.appendChild(new Option('— upload a CSV first —', ''));
    show('emptyState');
    hide('mainShell');
    return;
  }
  hide('emptyState');
  show('mainShell');
  opps.forEach(o => {
    const opt = new Option(`#${o.id} · ${o.trial_title.slice(0, 60)} (${o.status})`, o.id);
    sel.appendChild(opt);
  });
  // Pick the currently-selected one if still present, else first
  if (state.oppId && opps.some(o => o.id === state.oppId)) {
    sel.value = state.oppId;
  } else {
    sel.value = opps[0].id;
    state.oppId = opps[0].id;
  }
  await loadOpportunity(state.oppId);
}

async function loadOpportunity(oppId) {
  state.oppId = parseInt(oppId, 10);
  const opp = await API.get(`/api/opportunities/${oppId}`);
  state.opp = opp;
  state.contacts = opp.contacts || [];
  state.primaryContact = state.contacts.find(c => c.is_primary) || state.contacts[0] || null;

  renderOpportunity(opp);
  renderContact(state.primaryContact);
  renderOtherContacts(state.contacts.filter(c => c !== state.primaryContact));
  resetDraftView();

  // Both buttons visible whenever an opportunity is loaded — let user choose
  show('addContactBtn');
  show('seedBtn');

  if (state.contacts.length === 0) {
    $('contactBody').innerHTML = `<div style="padding:16px;color:var(--amber);font-size:13px;background:var(--amber-light);border:1px solid #fed7aa;border-radius:8px;margin:4px;">
      <strong>No contacts on this opportunity yet.</strong><br>
      <span style="font-size:12px;color:var(--ink-mid);display:block;margin-top:8px;">
        Two options at the top of the page:<br>
        • <strong>"+ Add real contact"</strong> — type in a real person (LinkedIn / Apollo lookup)<br>
        • <strong>"Seed demo contacts"</strong> — generates 3 placeholder contacts for testing the flow
      </span>
    </div>`;
    $('generateBtn').disabled = true;
    $('generateBtn').title = 'Add a contact first (real or demo)';
  } else {
    $('generateBtn').disabled = false;
    $('generateBtn').title = '';
  }
}

// ── Renderers ────────────────────────────────────
function renderOpportunity(o) {
  $('oppDate').textContent = fmtDate(o.created_at);
  $('oppBody').innerHTML = `
    <div class="source-tag">
      <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="8" r="6"/></svg>
      ${escapeHtml((o.source || 'clinwire').toUpperCase())} Feed
    </div>
    <div class="opp-name">${escapeHtml(o.trial_title)}</div>
    <div class="opp-sub">${escapeHtml(o.sponsor_name || '')}</div>
    <div class="opp-grid">
      ${field('Phase',          o.phase)}
      ${field('Indication',     o.indication)}
      ${field('Sites needed',   o.sites_needed != null ? o.sites_needed + '+' : '—')}
      ${field('Geography',      o.geography)}
      ${field('CRO',            o.cro_name, true)}
      ${field('Therapeutic Area', o.therapeutic_area, true)}
      ${field('Protocol Start', fmtDate(o.protocol_start), true)}
    </div>
    ${renderRawData(o.raw_data)}
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
  // Pre-fill the "To:" override field with the contact's stored email
  $('toEmailInput').value = c.email || '';
  const sr = c.score_reasoning || {};
  const initials = ((c.first_name || '?')[0] + (c.last_name || '?')[0]).toUpperCase();
  const score = c.contact_score ?? 0;

  $('contactBody').innerHTML = `
    <div class="contact-row">
      <div class="avatar">${escapeHtml(initials)}</div>
      <div>
        <div class="contact-name">${escapeHtml((c.first_name || '') + ' ' + (c.last_name || ''))}</div>
        <div class="contact-title">${escapeHtml(c.title || '')}</div>
        <div class="contact-co">${escapeHtml(state.opp?.cro_name || state.opp?.sponsor_name || '')}</div>
      </div>
    </div>
    <div class="score-row">
      <div>
        <div class="score-label">Relevance Score</div>
        <div style="font-size:12px;color:#15803d;margin-top:3px;">Best match for site selection outreach</div>
      </div>
      <div style="text-align:right;">
        <div class="score-val">${score}</div>
        <div class="score-sub">out of 100</div>
      </div>
    </div>
    ${scoreBar('Title relevance', sr.title_relevance, 35)}
    ${scoreBar('Seniority',       sr.seniority,       25)}
    ${scoreBar('Department match', sr.department,    20)}
    ${scoreBar('Geography fit',   sr.geography,      10)}
    ${scoreBar('Email verified',  sr.email_verified, 10)}
    ${sr.rationale ? `
      <div class="ai-reasoning">
        <div class="ai-reasoning-label">Scoring rationale</div>
        ${escapeHtml(sr.rationale)}
      </div>
    ` : ''}
    <div class="contact-meta">
      ${c.email ? `<span class="meta-chip">${escapeHtml(c.email)}</span>` : ''}
      ${c.geography ? `<span class="meta-chip">${escapeHtml(c.geography)}</span>` : ''}
      <span class="meta-chip">${c.email_verified ? '✓ verified' : '⚠ unverified'}</span>
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
      const cid = parseInt(el.dataset.cid, 10);
      const newPrimary = state.contacts.find(c => c.id === cid);
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
    await refreshOpportunityList();
  } catch (err) {
    logEntry(`Upload failed: ${err.message}`, 'err');
  }
}

async function onSyncSheets() {
  const btn = $('syncBtn');
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = '↻ Syncing...';
  try {
    const res = await API.post('/api/sync/sheets', {});
    const opp = res.opportunities || {};
    const c = res.contacts || {};
    logEntry(
      `Sync done in ${res.elapsed_seconds}s · opps: ${opp.inserted} new / ${opp.duplicates} dup · ` +
      `contacts: ${c.inserted} inserted / ${c.skipped_no_opp} skipped (no opp match)`,
      'ok'
    );
    await refreshOpportunityList();
  } catch (err) {
    logEntry(`Sheet sync failed: ${err.message}`, 'err');
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
  if (!state.oppId) {
    $('acError').textContent = 'Pick an opportunity first';
    return;
  }
  const first = $('acFirst').value.trim();
  const email = $('acEmail').value.trim();
  if (!first || !email) {
    $('acError').textContent = 'First name and email are required';
    return;
  }
  $('acError').textContent = '';
  $('acSubmit').disabled = true;
  try {
    await API.post('/api/contacts/', {
      opportunity_id: state.oppId,
      first_name: first,
      last_name:  $('acLast').value.trim() || null,
      email:      email,
      title:      $('acTitle').value.trim() || null,
    });
    logEntry(`Manually added contact: ${first} ${$('acLast').value.trim()} <${email}>`, 'ok');
    // Clear and hide
    ['acFirst','acLast','acEmail','acTitle'].forEach(id => $(id).value = '');
    $('addContactForm').classList.add('hidden');
    await loadOpportunity(state.oppId);
  } catch (err) {
    $('acError').textContent = err.message;
    logEntry(`Add contact failed: ${err.message}`, 'err');
  } finally {
    $('acSubmit').disabled = false;
  }
}

async function onGenerate() {
  if (!state.oppId || !state.primaryContact) {
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
      opportunity_id: state.oppId,
      contact_id: state.primaryContact.id,
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
            <span style="opacity:0.7;">${escapeHtml(s.opportunity_title || '')}  ·  ${escapeHtml(s.contact_name || '')}</span>
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
window.addEventListener('DOMContentLoaded', async () => {
  $('csvFile').addEventListener('change', onUpload);
  $('oppSelect').addEventListener('change', e => loadOpportunity(e.target.value));
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
  await refreshOpportunityList();
  await loadSendHistory();
  await pollLog();
  state.logPollInterval = setInterval(pollLog, 2000);
});
