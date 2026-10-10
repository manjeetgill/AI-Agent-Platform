const el = id => document.getElementById(id);
let generation = 0;
async function api(path) {
  const response = await fetch(path, {headers: {'X-Monitor-Intent':'user-action'}, cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Unable to read SMS activity.');
  return data;
}
function line(parent, text) {
  const p = document.createElement('p'); p.textContent = text; parent.append(p);
}
async function refresh() {
  const current = ++generation;
  el('error').hidden = true;
  for (const id of ['configuration','usage','messages']) el(id).replaceChildren();
  const selected = el('connection').value;
  if (!selected) return;
  try {
    const base = `/monitor/api/${encodeURIComponent(selected)}/notifications/sms`;
    const [config, usage, listing] = await Promise.all([api(base+'/configuration'),api(base+'/usage'),api(base)]);
    if (current !== generation) return;
    line(el('configuration'), config.configured ? `Provider: ${config.provider}${config.mock ? ' — simulated, no external delivery' : ''}. Automatic fallback: disabled.` : config.next_action);
    line(el('configuration'), config.delivery_meaning || config.next_action);
    if (config.max_segments) line(el('configuration'), `Maximum ${config.max_segments} segment(s) per message.`);
    if (config.testing_no_volume_caps) line(el('configuration'), 'Testing: daily and monthly caps disabled for this application. Per-minute pacing still applies.');
    for (const kind of ['limits','global_limits']) if (config[kind]) {
      const q = config[kind]; line(el('configuration'), `${kind === 'limits' ? 'Application' : 'Platform'}: ${q.daily ?? 'Unlimited'} segments/day, ${q.monthly ?? 'Unlimited'} segments/month, ${q.per_minute} segments/minute.`);
    }
    for (const [label, values] of [['Requests',usage.app_requests],['Attempts',usage.app_attempts]]) {
      line(el('usage'), `${label}: ${values.daily} today · ${values.monthly} this month · ${values.per_minute} in the last minute.`);
    }
    line(el('usage'), 'Counts are SMS segments. Retries reserve additional segments; actual charges depend on the provider.');
    if (!listing.messages.length) line(el('messages'), 'No SMS requests yet.');
    for (const sms of listing.messages) {
      line(el('messages'), `${sms.template} · ${sms.status} · delivery: UNCONFIRMED · ${sms.segments} segment(s) · ${sms.provider} · ${sms.attempts} attempt(s) · ${sms.created_at}`);
      if (sms.diagnostic) line(el('messages'), sms.diagnostic);
      line(el('messages'), `ID: ${sms.id}${sms.failure_code ? ' · '+sms.failure_code : ''}${sms.provider_id ? ' · Provider ID: '+sms.provider_id : ''}`);
    }
    if (listing.next_cursor) line(el('messages'), 'Showing the latest 25. Older records are available through the paginated API.');
  } catch (error) {
    if (current !== generation) return;
    el('error').textContent = error.message; el('error').hidden = false;
  }
}
el('connection').addEventListener('change', refresh);
el('refresh').addEventListener('click', refresh);
try {
  const data = await api('/monitor/connections');
  for (const connection of data.connections) {
    const option = document.createElement('option'); option.value = connection.id; option.textContent = connection.name; el('connection').append(option);
  }
  el('connection').disabled = !data.connections.length;
  await refresh();
} catch (error) { el('error').textContent = error.message; el('error').hidden = false; }
