'use strict';
const el = id => document.getElementById(id);
let profiles = [], selected = null, timer = null;
const api = (path, method, value) => platform('/connectors/feeds' + path, method, value);
async function platform(path, method = 'GET', value) {
  const response = await fetch('/console/api' + path, {
    method, headers: {'Content-Type': 'application/json', 'X-Console-Intent': 'user-action'},
    body: value === undefined ? undefined : JSON.stringify(value), cache: 'no-store'
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed; review the connection form.');
  return data;
}
async function action(fn) {
  try { await fn(); } catch (error) { el('notice').textContent = error.message; }
}
function profile() { return profiles.find(p => p.id === el('profile').value); }
function describe() {
  const p = profile();
  el('endpoint').textContent = p ? 'Approved endpoint: ' + p.endpoint : '';
  el('help').textContent = p ? p.setup_help : 'Your operator needs to configure an approved API/feed profile for this app.';
  el('alternatives').textContent = p && p.alternatives.length ? 'Authorized alternatives: ' + p.alternatives.join('; ') : '';
  el('connect').disabled = !p;
  clearTimeout(timer); selected = null; el('setup').hidden = true; el('secret').value = '';
}
async function refresh() {
  if (!selected) return;
  const id = selected.id;
  const data = await api('');
  if (!selected || selected.id !== id) return;
  selected = data.connections.find(c => c.id === id);
  if (!selected) throw new Error('Connection no longer available.');
  el('connection-status').textContent = selected.state.replaceAll('_', ' ') + ' — ' + selected.reason_code.replaceAll('_', ' ');
  renderSchedules();
  el('recovery').replaceChildren(...(selected.recovery_options || []).map(option => {const li = document.createElement('li'); li.textContent = option.summary; return li;}));
  const needsKey = profile().authorization_required;
  el('credentials').hidden = !needsKey && !selected.disabled && selected.profile_hash === profile().profile_hash;
  el('secret').hidden = !needsKey;
  document.querySelector('label[for=secret]').hidden = !needsKey;
  el('secret-help').textContent = needsKey ? 'Enter the provider key privately. Saved keys are never displayed back.' : 'Confirm the approved endpoint above to reconnect and test this public feed.';
  el('save').textContent = needsKey ? 'Save privately and test' : 'Reconnect and test';
  el('test').disabled = selected.disabled || selected.state === 'TESTING';
  el('result').textContent = JSON.stringify({state: selected.state, reason: selected.reason_code,
    task_id: selected.test_task_id, task_status: selected.task_status, credential_saved: selected.credential_saved}, null, 2);
  if (selected.test_task_id && selected.task_status === 'DONE') {
    const response = await fetch('/console/api/tasks/' + selected.test_task_id, {headers: {'X-Console-Intent':'user-action'}, cache:'no-store'});
    if (response.ok) { const task = await response.json(); if (selected && selected.id === id) el('result').textContent = JSON.stringify(task.steps[0].result, null, 2); }
  }
  if (!selected || selected.id !== id) return;
  clearTimeout(timer);
  if (selected.state === 'TESTING') timer = setTimeout(() => action(refresh), 3000);
  el('notice').textContent = selected.state === 'READY' ? 'Connection tested successfully. Review evidence before relying on prices.' : 'Status updated. Follow the setup instructions for any required action.';
}
el('profile').addEventListener('change', describe);
el('connect').addEventListener('click', () => action(async () => {
  const p = profile();
  selected = await api('', 'POST', {profile: p.id, profile_hash: p.profile_hash});
  el('setup').hidden = false; await refresh();
}));
el('credentials').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    const value = {expected_revision: selected.revision, profile_hash: profile().profile_hash, secret: el('secret').value || null};
    el('secret').value = ''; el('save').disabled = true;
    try { await api('/' + selected.id + '/credentials', 'PUT', value); await refresh(); }
    finally { value.secret = null; el('save').disabled = false; }
  });
});
el('test').addEventListener('click', () => action(async () => {
  await api('/' + selected.id + '/test', 'POST', {expected_revision: selected.revision, idempotency_key: crypto.randomUUID()}); await refresh();
}));
el('refresh').addEventListener('click', () => action(refresh));
el('disconnect').addEventListener('click', () => action(async () => {await api('/' + selected.id, 'DELETE'); await refresh();}));
action(async () => {
  const data = await api('/profiles'); profiles = data.profiles;
  for (const p of profiles) { const option = document.createElement('option'); option.value = p.id; option.textContent = p.name; el('profile').append(option); }
  describe(); el('notice').textContent = !profiles.length ? 'No approved sources configured for this app yet.' : data.credential_storage_ready ? 'Choose an approved source to begin.' : 'The operator must configure credential encryption before saving API keys.';
});

function renderSchedules() {
  el('prepare-schedule').disabled = selected.state !== 'READY' || selected.disabled;
  const rows = (selected.schedules || []).map(schedule => {
    const box = document.createElement('section');
    const text = document.createElement('p');
    const def = schedule.definition;
    text.textContent = `${schedule.status}: check ${profile().name} every ${def.trigger.interval_seconds / 60} minutes, up to ${def.max_runs} checks (${schedule.run_count} queued). Connection revision ${schedule.connection_arguments.revision}. Provider charges may apply; request quotas remain enforced.`;
    box.append(text);
    const current = schedule.connection_arguments.revision === selected.revision && schedule.connection_arguments.profile_hash === selected.profile_hash;
    function button(label, fn) {
      const node = document.createElement('button'); node.type = 'button'; node.textContent = label;
      node.addEventListener('click', () => action(async () => {node.disabled = true; try {await fn(); await refresh();} finally {node.disabled = false;}})); box.append(node);
    }
    if (['DRAFT','PAUSED'].includes(schedule.status) && current && selected.state === 'READY') {
      button('Yes, activate this schedule', () => platform('/orchestration/automations/' + schedule.id + '/approve', 'POST', {decision:'yes', action_hash:schedule.definition_hash}));
    }
    if (!current) {const note = document.createElement('p'); note.textContent = 'Connection changed. Archive this schedule and prepare a new one after testing.'; box.append(note);}
    if (schedule.status === 'ACTIVE') button('Pause checks', () => platform('/orchestration/automations/' + schedule.id, 'PATCH', {status:'PAUSED'}));
    button('Archive schedule', () => platform('/orchestration/automations/' + schedule.id, 'PATCH', {status:'ARCHIVED'}));
    button('Show recent runs', async () => {
      const data = await platform('/orchestration/automations/' + schedule.id + '/runs');
      el('run-results').textContent = JSON.stringify(data.runs, null, 2);
    });
    return box;
  });
  el('schedules').replaceChildren(...rows);
}
el('schedule-form').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    const connection = selected, p = profile();
    const interval = Number(el('interval').value) * 60, maxRuns = Number(el('max-runs').value);
    if (!connection || connection.state !== 'READY') throw new Error('Test this connection successfully first.');
    el('prepare-schedule').disabled = true;
    try {
      const agent = await platform('/task-agents', 'POST', {
        idempotency_key:`feed-${connection.id}-${connection.revision}-${connection.profile_hash.slice(0,16)}`,
        blueprint:{name:'Verify API/feed evidence', description:'Bounded recurring verification of the reviewed connection.',
          steps:[{tool:'feed.read', version:'1', arguments:{connection_id:connection.id, revision:connection.revision, profile_hash:connection.profile_hash}}]}
      });
      await platform('/orchestration/automations', 'POST', {
        idempotency_key:`feed-${connection.id}-${connection.revision}-${interval}-${maxRuns}-${connection.profile_hash.slice(0,8)}`,
        definition:{agent_id:agent.id, name:('Verify ' + p.name).slice(0,120),
          trigger:{type:'interval', interval_seconds:interval}, max_runs:maxRuns,
          condition:{path:'result.usable_evidence', operator:'eq', value:false},
          notification:{title:'Data source needs attention', on:'crossing'}}
      });
      await refresh(); el('notice').textContent = 'Schedule prepared. Review its frequency and limit, then activate it below.';
    } finally {el('prepare-schedule').disabled = !selected || selected.state !== 'READY';}
  });
});
