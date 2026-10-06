/* This local preview uses a server-side proxy; no provider or service key is in the browser. */
const $ = id => document.getElementById(id);
let agent = null, pending = null, ready = false;
async function api(path, method='GET', body) {
  const response = await fetch('/console/api/' + path, {method, headers:{'Content-Type':'application/json','X-Console-Intent':'user-action'}, ...(body ? {body:JSON.stringify(body)} : {})});
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'Unable to complete this step.');
  return value;
}
function action(id, work) {
  $(id).addEventListener('click', async () => {
    $('error').hidden = true; $(id).disabled = true;
    try { await work(); } catch(e) { $('error').textContent=e.message; $('error').hidden=false; }
    finally { $(id).disabled=false; $('activate').disabled=!ready || !$('costs').checked; }
  });
}
function route(suffix) { if (!agent) throw new Error('Choose an agent first.'); return 'calling/agents/'+agent.id+suffix; }
async function choose(row) {
  agent=row; pending=null; ready=false; $('selected').hidden=false; $('selected-name').textContent=row.name;
  $('approval').hidden=true; $('refresh-test').hidden=true; $('activate').disabled=true;
  $('test-result').textContent=''; $('permission-result').textContent=''; $('readiness').textContent='Check readiness to see the next step.';
  const setup=await api(route('/setup'));
  $('connection').textContent=setup.next_step; $('status').textContent=setup.enabled?'Active':'Not active';
  if (setup.channel_id) $('channel').value=setup.channel_id;
}
async function load() {
  const [agents, channels] = await Promise.all([api('calling/agents'),api('whatsapp/channels?limit=100')]);
  $('agents').replaceChildren();
  for (const row of agents.agents) { const b=document.createElement('button'); b.textContent=row.name; b.onclick=()=>choose(row).catch(showError); $('agents').append(b); }
  $('channel').replaceChildren();
  for (const row of channels) { const o=document.createElement('option'); o.value=row.id; o.textContent=row.name; $('channel').append(o); }
  $('number-help').textContent=channels.length?'Choose a business connection owned by this workspace.':'Connect a WhatsApp business number first using the link on the left.';
  $('status').textContent='Choose or create an agent';
}
function showError(e) { $('error').textContent=e.message; $('error').hidden=false; }
$('create').addEventListener('submit',async e=>{
  e.preventDefault(); const button=e.target.querySelector('button'); button.disabled=true;
  try { const body=Object.fromEntries(new FormData(e.target)); body.external_id='voice-'+crypto.randomUUID(); const row=await api('calling/agents','POST',body); await load(); await choose(row); }
  catch(error) { showError(error); } finally {button.disabled=false;}
});
action('connect',async()=>{ if (!$('authorized').checked) throw new Error('Confirm authorization for this business number.'); const r=await api(route('/connection'),'POST',{channel_id:$('channel').value,authorized_to_connect:true}); $('connection').textContent=r.next_step; ready=false; });
action('check',async()=>{const r=await api(route('/readiness')); ready=r.ready; $('readiness').textContent=r.message; $('status').textContent=r.enabled?'Active':'Not active';});
action('enable-provider',async()=>{if (!$('provider-consent').checked) throw new Error('Review and authorize enabling WhatsApp calling for this number.'); const r=await api(route('/provider-settings'),'POST',{approved:true}); $('readiness').textContent=r.message; ready=false;});
$('costs').onchange=()=>{$('activate').disabled=!ready || !$('costs').checked;};
action('activate',async()=>{if (!$('costs').checked) throw new Error('Review activation and usage charges first.'); await api(route('/activation'),'POST',{enabled:true}); $('status').textContent='Active';});
action('pause',async()=>{await api(route('/activation'),'POST',{enabled:false}); $('status').textContent='Paused';});
action('permission',async()=>{const r=await api(route('/permission')+'?recipient='+encodeURIComponent($('recipient').value.trim())); $('permission-result').textContent=r.can_call?'Recipient permits a WhatsApp call.':'Recipient has not granted current call permission, or the provider call limit has been reached. Request permission through your WhatsApp inbox.';});
action('prepare',async()=>{pending=await api(route('/test-calls'),'POST',{recipient:$('recipient').value.trim(),idempotency_key:crypto.randomUUID()}); $('summary').textContent=pending.summary; $('approval').hidden=false;});
action('approve',async()=>{if (!pending) return; const r=await api(route('/test-calls/'+pending.id+'/approve'),'POST',{approved:true}); $('test-result').textContent=r.state; $('approval').hidden=true; $('refresh-test').hidden=false;});
action('refresh-test',async()=>{if (!pending) return; const r=await api(route('/test-calls/'+pending.id)); $('test-result').textContent=r.state+(r.state==='UNKNOWN'?' — Outcome uncertain. Check provider call history; do not redial.':'');});
action('history',async()=>{const r=await api(route('/calls')); $('calls').textContent=r.calls.length?JSON.stringify(r.calls,null,2):'No calls received yet.';});
load().catch(showError);
