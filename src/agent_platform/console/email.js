const el = id => document.getElementById(id);
let generation = 0;
async function api(path) {
  const response = await fetch(path, {headers: {'X-Monitor-Intent':'user-action'}, cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Unable to read email activity.');
  return data;
}
function line(parent, text) {
  const p = document.createElement('p'); p.textContent = text; parent.append(p);
}
async function refresh() {
  const current = ++generation;
  el('error').hidden = true;
  for (const id of ['configuration','usage','emails']) el(id).replaceChildren();
  const selected = el('connection').value;
  if (!selected) return;
  try {
    const base = `/monitor/api/${encodeURIComponent(selected)}/notifications/email`;
    const [config, usage, listing] = await Promise.all([api(base+'/configuration'),api(base+'/usage'),api(base)]);
    if (current !== generation) return;
    line(el('configuration'), config.configured ? `Provider: ${config.provider}${config.mock ? ' — simulated, no external delivery' : ''}. Automatic fallback: disabled.` : config.next_action);
    if (config.configured && !config.mock) line(el('configuration'), config.webhook_configured ? 'Webhook secret configured. Receipt of real provider events still requires the provider endpoint setup.' : 'Delivery tracking: operator must configure the provider webhook.');
    for (const kind of ['limits','global_limits']) if (config[kind]) {
      const q = config[kind]; line(el('configuration'), `${kind === 'limits' ? 'Application' : 'Platform'}: ${q.daily}/day, ${q.monthly}/month, ${q.per_minute} attempts/minute.`);
    }
    for (const [label, values] of [['Requests',usage.app_requests],['Attempts',usage.app_attempts]]) {
      line(el('usage'), `${label}: ${values.daily} today · ${values.monthly} this month · ${values.per_minute} in the last minute.`);
    }
    line(el('usage'), `Suppressed recipients in this application: ${usage.app_suppressed_recipients ?? 0}.`);
    for (const [status, count] of Object.entries(usage.user_delivery_statuses || {})) line(el('usage'), `${status}: ${count} emails for this user.`);
    if (!listing.emails.length) line(el('emails'), 'No email requests yet.');
    for (const email of listing.emails) {
      line(el('emails'), `${email.template} · ${email.status} · delivery: ${email.delivery_status || 'UNCONFIRMED'} · ${email.provider} · ${email.attempts} attempt(s) · ${email.created_at}`);
      line(el('emails'), `ID: ${email.id}${email.failure_code ? ' · '+email.failure_code : ''}${email.provider_id ? ' · Provider ID: '+email.provider_id : ''}`);
    }
    if (listing.next_cursor) line(el('emails'), 'Showing the latest 25. Older records are available through the paginated API.');
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
