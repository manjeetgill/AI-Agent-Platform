const $ = id => document.getElementById(id);
const labels = {
  carrier_authorization_required: 'Your app administrator must provide carrier authorization and routing evidence.',
  gateway_offline: 'Start the app’s private Asterisk bridge and check its gateway key and HTTPS connection.',
  agent_unavailable: 'Enable or replace the selected voice agent.',
  model_configuration_required: 'The platform operator must configure the voice model.',
  business_tools_unavailable: 'The operator must restore the app tool endpoint approval or credential encryption.'
};
async function api(path, method = 'GET', body) {
  const response = await fetch('/console/api/' + path, {
    method, signal: AbortSignal.timeout(25000),
    headers: {'Content-Type': 'application/json', 'X-Console-Intent': 'user-action'},
    ...(body ? {body: JSON.stringify(body)} : {})
  });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'Connection is not ready. Refresh its status.');
  return value;
}
function line(parent, text, tag = 'p') {
  const element = document.createElement(tag); element.textContent = text; parent.append(element); return element;
}
async function load() {
  const {connections} = await api('telephony/connections');
  $('connections').replaceChildren();
  $('tool-connection').replaceChildren(new Option('Select connection', ''));
  for (const r of connections) $('tool-connection').add(new Option(r.external_id + ' — ' + r.number, r.id));
  if (!connections.length) line($('connections'), 'No telephone connections yet.');
  for (const row of connections) {
    const card = document.createElement('section'); $('connections').append(card);
    line(card, `${row.external_id} — ${row.number}`, 'h3');
    line(card, `Connection ID: ${row.id}`);
    try {
      const r = await api(`telephony/connections/${row.id}/readiness`);
      line(card, r.accepting_calls ? 'Activated. Make a real inbound audio test before customer rollout.' : 'Not accepting calls yet.');
      for (const blocker of r.blockers) line(card, labels[blocker] || blocker);
      if (r.ready_to_activate && !r.enabled) line(card, 'Configuration ready. You can activate the connection for testing.');
      const action = line(card, r.enabled ? 'Pause connection' : 'Activate connection', 'button');
      action.disabled = !r.enabled && !r.ready_to_activate;
      action.onclick = async () => {
        action.disabled = true;
        try { await api(`telephony/connections/${row.id}`, 'PATCH', {enabled: !r.enabled}); await load(); }
        catch (e) { $('status').textContent = e.message; action.disabled = false; }
      };
      const {calls} = await api(`telephony/connections/${row.id}/calls?limit=5`);
      for (const call of calls) line(card, `${new Date(call.created_at).toLocaleString()} — ${call.state} — ${call.turns} turns`);
    } catch (e) { line(card, e.message); }
  }
}
$('create-form').onsubmit = async event => {
  event.preventDefault(); $('create').disabled = true; $('status').textContent = 'Preparing connection…';
  try {
    const result = await api('telephony/connections', 'POST', {
      external_id: $('external').value.trim(), agent_id: $('agent').value, number: $('number').value.trim()
    });
    $('key').value = result.gateway_key; $('credential').hidden = false;
    $('status').textContent = 'Connection prepared. Save its private gateway key, then complete the connection steps below.';
    await load();
  } catch (e) { $('status').textContent = e.message; }
  finally { $('create').disabled = false; }
};
$('hide-key').onclick = () => { $('key').value = ''; $('credential').hidden = true; };
$('refresh').onclick = () => load().catch(e => { $('status').textContent = e.message; });
async function init() {
  const {agents} = await api('voice/agents');
  $('agent').replaceChildren(new Option('Choose an existing voice agent', ''));
  for (const a of agents) $('agent').add(new Option(a.name, a.id));
  if (!agents.length) $('status').textContent = 'Create a voice agent and add business FAQs on the Voice agents page first.';
  await load();
}
init().catch(e => { $('status').textContent = e.message; });

$('tools-form').onsubmit = async event => {
  event.preventDefault(); $('save-tools').disabled = true;
  try {
    const tools = JSON.parse($('tool-definitions').value);
    await api(`telephony/connections/${$('tool-connection').value}/tools`, 'PUT', {
      endpoint: $('tool-endpoint').value.trim(), secret: $('tool-secret').value, tools
    });
    $('tool-secret').value = '';
    $('tools-status').textContent = 'Tools connected. Test availability, caller verification, confirmation and booking before activating customers.';
  } catch (e) { $('tools-status').textContent = e.message; }
  finally { $('save-tools').disabled = false; }
};
