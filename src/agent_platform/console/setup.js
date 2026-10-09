'use strict';
const el = id => document.getElementById(id);
let revision = 0;
function node(tag, text) { const n = document.createElement(tag); n.textContent = text; return n; }
async function refresh() {
  const current = ++revision, app = el('application').value;
  el('error').hidden = true; el('refresh').disabled = true;
  el('status').textContent = 'Checking this application…'; el('checklist').replaceChildren();
  try {
    const path = app ? '/monitor/api/' + encodeURIComponent(app) + '/integrations/capability-readiness' : '/console/api/integrations/capability-readiness';
    const response = await fetch(path, {cache:'no-store', signal:AbortSignal.timeout(15000),
      headers:{'X-Console-Intent':'user-action','X-Monitor-Intent':'user-action'}});
    const report = await response.json();
    if (!response.ok) throw Error(typeof report.detail === 'string' ? report.detail : 'Setup check unavailable.');
    if (current !== revision) return;
    for (const item of report.capabilities) {
      const card = node('section', ''); card.className = 'capability-card';
      card.append(node('h2', item.title), node('strong', item.status.replaceAll('_', ' ')), node('p', item.summary));
      const list = node('ol', ''); for (const step of item.next_steps) list.append(node('li', step)); card.append(list);
      const details = node('details', ''); details.append(node('summary','Saved evidence'), node('pre',JSON.stringify(item.evidence,null,2))); card.append(details);
      const link = node('a','Open local capability screen →');
      if (['/workspace','/whatsapp','/calling','/voice','/telephony','/monitor/email','/memory','/connectors'].includes(item.workspace_url)) link.href = item.workspace_url;
      card.append(link); el('checklist').append(card);
    }
    el('status').textContent = 'Checked ' + new Date(report.checked_at).toLocaleString() + '. ' + report.note;
  } catch (error) {
    if (current !== revision) return;
    el('status').textContent = 'Check incomplete.'; el('error').textContent = error.message; el('error').hidden = false;
  } finally { if (current === revision) el('refresh').disabled = false; }
}
el('refresh').onclick = refresh; el('application').onchange = refresh;
async function profiles() {
  try {
    const response = await fetch('/monitor/connections',{cache:'no-store',signal:AbortSignal.timeout(8000),headers:{'X-Monitor-Intent':'user-action'}});
    if (!response.ok) return;
    for (const c of (await response.json()).connections) el('application').add(new Option(c.name,c.id));
  } catch { /* Local workspace remains usable when the main API is offline. */ }
}
profiles(); refresh();
