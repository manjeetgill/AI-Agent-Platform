import {voiceFailureMessage} from './voice-errors.js';
import {mountVoiceWidget} from './voice-widget.js';
const $ = id => document.getElementById(id);
async function api(path, method = 'GET', body) {
  const response = await fetch('/console/api/' + path, {
    method, signal: AbortSignal.timeout(25000),
    headers: {'X-Console-Intent': 'user-action', 'Content-Type': 'application/json'},
    ...(body ? {body: JSON.stringify(body)} : {})
  });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'Request failed');
  return value;
}
let agents = [], history = [], revision = 0;
const widget = mountVoiceWidget($('voice-widget'), {
  api, agentId: () => $('agent').value,
  beforeStart: async id => {
    const readiness = await api('voice/agents/' + id + '/readiness');
    if (!readiness.ready_to_attempt) throw new Error(readiness.message);
  }
});
function reset() {
  history = []; revision++;
  $('rehearsal-log').replaceChildren(); $('rehearsal-status').textContent = '';
}
async function selection() {
  const agent = agents.find(a => a.id === $('agent').value);
  $('toggle-agent').disabled = !agent;
  $('toggle-agent').textContent = agent?.enabled ? 'Pause agent' : 'Enable agent';
  $('agent-versions').replaceChildren(new Option('Choose a version', ''));
  if (!agent) return;
  const config = agent.config || {};
  for (const field of ['asr', 'tts', 'reasoning', 'model']) if (config[field]) $(field).value = config[field];
  $('barge-in').checked = config.barge_in !== false;
  $('partial-transcripts').checked = config.partial_transcripts === true;
  $('stream-quotes').checked = config.stream_grounded_quotes === true;
  $('voice-config-status').textContent = 'Version ' + (agent.version || 1);
  const id = agent.id;
  try {
    const versions = await api('voice/agents/' + id + '/versions');
    if ($('agent').value !== id) return;
    for (const version of versions.versions) $('agent-versions').add(new Option('Version ' + version.version + ' — ' + new Date(version.created_at).toLocaleString(), version.version));
    const r = await api('voice/agents/' + id + '/readiness');
    if ($('agent').value !== id) return;
    $('readiness').textContent = r.message + ' ' + (r.business_actions_configured ?
      'Business actions are connected; a live action test is still required. ' :
      'This agent answers FAQs; it has no connected booking or other action tools. ') +
      'Calls happen in your browser. Mobile-number calling is not connected.';
  } catch (e) { if ($('agent').value === id) $('readiness').textContent = e.message; }
}
$('agent').onchange = () => {
  reset(); selection();
  widget.end('Agent selected. Press Talk to AI when ready.');
};
async function load(selected) {
  agents = (await api('voice/agents')).agents;
  $('agent').replaceChildren(new Option('Select an agent', ''));
  for (const a of agents) $('agent').add(new Option(a.name + (a.enabled ? '' : ' (paused)'), a.id));
  if (selected) $('agent').value = selected;
  selection();
}
$('toggle-agent').onclick = async () => {
  const agent = agents.find(a => a.id === $('agent').value);
  if (!agent) return;
  $('toggle-agent').disabled = true;
  try {
    await api('voice/agents/' + agent.id + '/settings', 'POST', {
      enabled: !agent.enabled, language: agent.language || 'auto'
    });
    await widget.end('Agent settings changed.');
    reset(); await load(agent.id);
    $('setup-status').textContent = agent.enabled ? 'Agent paused.' : 'Agent enabled.';
  } catch (e) { $('setup-status').textContent = e.message; selection(); }
};
$('create').onclick = async () => {
  const name = $('name').value.trim(), text = $('knowledge').value.trim();
  if (!name || !text || !$('greeting').value.trim()) {
    $('setup-status').textContent = 'Add a name, welcome message and business information first.'; return;
  }
  $('create').disabled = true;
  try {
    const external = crypto.randomUUID();
    const doc = await api('knowledge/documents', 'POST', {
      external_id: 'voice-' + external, title: name + ' FAQs', text,
      source_label: 'Business-provided information', visibility: 'app'
    });
    const a = await api('voice/agents', 'POST', {
      external_id: external, name, greeting: $('greeting').value.trim(),
      language: $('language').value, documents: [doc.id], visibility: 'app', config: selectedConfig()
    });
    await widget.end('New agent selected.'); reset(); await load(a.id);
    $('setup-status').textContent = 'Agent saved. Rehearse below or start an internet call.';
  } catch (e) { $('setup-status').textContent = e.message; }
  finally { $('create').disabled = false; }
};
function log(label, value) {
  const p = document.createElement('p'); p.textContent = label + ': ' + value;
  $('rehearsal-log').append(p);
}
$('reset-rehearsal').onclick = reset;
$('rehearse').onclick = async () => {
  const id = $('agent').value, text = $('rehearsal-text').value.trim();
  if (!id || !text) { $('rehearsal-status').textContent = 'Select an agent and enter a question.'; return; }
  const current = revision;
  $('rehearse').disabled = true; $('rehearsal-status').textContent = 'Preparing an answer…';
  log('You', text); $('rehearsal-text').value = '';
  try {
    const reply = await api('voice/agents/' + id + '/preview', 'POST', {
      text, history, request_key: crypto.randomUUID()
    });
    if (current !== revision) return;
    log('AI', reply.text);
    history.push({role: 'user', text}, {role: 'assistant', text: reply.text});
    history = history.slice(-8);
    $('rehearsal-status').textContent = reply.action === 'continue' ? 'Ready for a follow-up.' :
      'Conversation ended or needs reception. Start a new conversation to continue testing.';
  } catch (e) { if (current === revision) $('rehearsal-status').textContent = e.message; }
  finally { $('rehearse').disabled = false; }
};
try {
  await load(); const c = await api('voice/capabilities');
  if (!$('agent').value && c.default_config) { for (const field of ['asr','tts','reasoning','model']) $(field).value=c.default_config[field]; }
  $('readiness').textContent = (c.configured ? 'Internet voice configured. Microphone and language quality still need a real-user test. ' :
    'Internet audio is not configured. Text rehearsal requires model access. ') +
    'Telephone-number calling is not connected.';
} catch (e) { $('readiness').textContent = e.message; }

function selectedConfig() {
  const existing = agents.find(a => a.id === $('agent').value)?.config || {};
  return {...existing, asr: $('asr').value, tts: $('tts').value, reasoning: $('reasoning').value,
    model: $('model').value.trim(), barge_in: $('barge-in').checked, partial_transcripts: $('partial-transcripts').checked, stream_grounded_quotes: $('stream-quotes').checked};
}
$('reasoning').onchange = () => {
  $('model').value = {local: 'qwen2.5:3b', gemini: 'gemini-3.1-flash-lite', nvidia: 'nvidia/nemotron-3-nano-30b-a3b', openrouter: '', together: ''}[$('reasoning').value];
};
$('save-voice-config').onclick = async () => {
  const agent = agents.find(a => a.id === $('agent').value);
  if (!agent) { $('voice-config-status').textContent = 'Choose an agent first.'; return; }
  $('save-voice-config').disabled = true;
  try {
    await api('voice/agents/' + agent.id + '/configuration', 'POST', {expected_version: agent.version, config: selectedConfig()});
    await load(agent.id);
  } catch(e) { $('voice-config-status').textContent = e.message; }
  finally { $('save-voice-config').disabled = false; }
};
$('restore-version').onclick = async () => {
  const agent = agents.find(a => a.id === $('agent').value);
  const version = Number($('agent-versions').value);
  if (!agent || !version) { $('voice-config-status').textContent = 'Choose an agent and a saved version first.'; return; }
  $('restore-version').disabled = true;
  try {
    await api('voice/agents/' + agent.id + '/restore', 'POST', {expected_version: agent.version, source_version: version});
    await load(agent.id);
  } catch(e) { $('voice-config-status').textContent = e.message; }
  finally { $('restore-version').disabled = false; }
};
async function refreshCalls() {
  const usage = await api('voice/usage');
  $('voice-usage').textContent = `App allowance: ${usage.daily_sessions}/${usage.daily_session_limit} sessions in 24 hours; ${usage.monthly_sessions}/${usage.monthly_session_limit} this month. ${usage.active_sessions} active. Costs below are API estimates, not invoices.`;
  const accounting = usage.accounting;
  $('voice-accounting').textContent = accounting?.stages?.length ? accounting.stages.map(s => `${s.stage}: ${s.measured_units} ${s.unit}; ${s.currency} ${s.estimated_amount} estimated, ${s.held_amount} reserved for ${s.uncertain_attempts} uncertain attempts`).join('\n') + '\n' + accounting.scope : 'No metered voice activity yet. Earlier calls have unknown cost.';
  const {sessions} = await api('voice/sessions');
  $('call-history').replaceChildren(new Option('Choose a call', ''));
  for (const call of sessions) $('call-history').add(new Option(new Date(call.created_at).toLocaleString() + ' — ' + call.state + ' — ' + call.turns + ' turns', call.id));
}
async function showEvents() {
  const id = $('call-history').value;
  if (!id) return;
  const result = await api('voice/sessions/' + id + '/events');
  $('call-events').textContent = result.events.map(e => `${e.sequence}. ${e.kind} ${e.kind === 'turn_failed' ? voiceFailureMessage(e.details) : (e.content || JSON.stringify(e.details))}`).join('\n') + '\nEstimated cost: unknown';
}
$('refresh-calls').onclick = () => refreshCalls().catch(e => {$('call-events').textContent = e.message;});
$('call-history').onchange = () => showEvents().catch(e => {$('call-events').textContent = e.message;});
$('delete-transcript').onclick = async () => {
  const id = $('call-history').value;
  if (!id) return;
  try { await api('voice/sessions/' + id + '/transcript', 'DELETE'); await showEvents(); }
  catch(e) { $('call-events').textContent = e.message; }
};
refreshCalls().catch(e => {$('call-events').textContent = e.message;});
