'use strict';
const $ = id => document.getElementById(id);
let settings = {}, channels = [], documents = [], documentCursor = null, channelCursor = null;
let conversations = [], conversationCursor = null, selected = null, messageCursor = null;
let inboxEpoch = 0, detailEpoch = 0;
const showError = e => { $('error').textContent = e.message || 'Request unavailable. Refresh before retrying.'; $('error').hidden = false; };
const notice = value => { $('notice').textContent = value; $('notice').hidden = false; };
function el(tag, value, parent, className) { const node = document.createElement(tag); node.textContent = value; if (className) node.className = className; if (parent) parent.append(node); return node; }
async function api(path, method = 'GET', body) {
  const r = await fetch('/console/api/' + path, {method, headers: {'Content-Type':'application/json','X-Console-Intent':'user-action'}, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(60000)});
  const data = await r.json(); if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'Check the supplied information.'); return data;
}
function action(parent, label, fn) { const b = el('button', label, parent); b.type = 'button'; b.onclick = () => busy(b, fn); return b; }
async function busy(button, fn) { button.disabled = true; $('error').hidden = true; try { await fn(); } catch (e) { showError(e); } finally { if (button.isConnected) button.disabled = false; } }
function tab(inbox) { $('setup-view').hidden = inbox; $('inbox-view').hidden = !inbox; $('setup-tab').setAttribute('aria-current', inbox ? 'false':'page'); $('inbox-tab').setAttribute('aria-current', inbox ? 'page':'false'); }
$('setup-tab').onclick = () => tab(false);
$('inbox-tab').onclick = () => { tab(true); loadInbox().catch(showError); };
function renderDocuments() {
  const chosen = new Set(Array.from($('documents').querySelectorAll('input:checked'), x => x.value));
  $('documents').replaceChildren();
  for (const doc of documents.filter(d => d.visibility === 'app')) { const label = el('label', '', $('documents'), 'check'); const input = document.createElement('input'); input.type = 'checkbox'; input.value = doc.id; input.checked = chosen.has(doc.id); label.append(input); el('span', doc.title + ' · version ' + doc.version, label); }
  if (!$('documents').children.length) el('p', 'Publish customer-facing knowledge above to get started.', $('documents'));
  $('more-documents').hidden = !documentCursor;
}
async function loadDocuments(more = false) { const page = await api('knowledge/documents?limit=50' + (more && documentCursor ? '&after=' + documentCursor : '')); documents = more ? [...documents, ...page.documents] : page.documents; documentCursor = page.next_after; renderDocuments(); }
function renderChannels() {
  $('channels').replaceChildren(); const previous = $('inbox-channel').value; $('inbox-channel').replaceChildren(new Option('Choose an agent',''));
  for (const c of channels) {
    $('inbox-channel').append(new Option(c.name, c.id));
    const box = el('article','',$('channels'),'channel-card'); el('h3',c.name,box); el('span',c.enabled?'Automatic replies enabled':'Inactive',box,'badge');
    el('p','Number ID: ' + c.phone_id,box,'muted'); const list = el('ul','',box,'checklist');
    const checks = [[settings.phone_ids?.includes(c.phone_id),'Business credentials saved'], [Boolean(c.webhook_verified_at),'Meta webhook verified'], [settings.worker_online,'Message worker online'],[settings.model_configured,'AI model configured']];
    for (const [ok,label] of checks) el('li',(ok?'✓ ':'○ ')+label,list);
    el('p','Webhook callback to enter in Meta:',box); el('code',(settings.public_url || 'https://YOUR-PUBLIC-PLATFORM-HOST')+'/webhooks/whatsapp/'+c.id,box);
    if (!settings.public_url) el('p','A public HTTPS deployment is still needed. Localhost cannot receive Meta webhooks. Set AI_PLATFORM_PUBLIC_URL on the server.',box,'muted');
    const enable = action(box,c.enabled?'Pause automatic replies':'Activate automatic replies',async()=>{ await api('whatsapp/channels/'+c.id+'/activation','POST',{enabled:!c.enabled}); notice(c.enabled?'Automatic replies paused.':'Automatic replies activated for incoming customer messages.'); await loadSetup(); });
    if (!c.enabled) enable.disabled = !checks.every(([ok])=>ok);
    action(box,'Open inbox',async()=>{ $('inbox-channel').value=c.id; tab(true); await loadInbox(); });
  }
  if (!channels.length) el('p','No agents yet. Create your first support agent above.', $('channels'));
  if (channels.some(c=>c.id===previous)) $('inbox-channel').value=previous; else if(channels.length===1) $('inbox-channel').value=channels[0].id;
  $('more-channels').hidden = !channelCursor;
}
async function loadChannels(more=false) { const rows = await api('whatsapp/channels?limit=25'+(more&&channelCursor?'&before='+channelCursor:'')); channels=more?[...channels,...rows]:rows; channelCursor=rows.length===25?rows.at(-1).id:null; renderChannels(); }
async function loadSetup() {
  settings = await api('whatsapp/setup'); $('health').textContent = settings.worker_online?'Message worker online':'Message worker offline';
  $('numbers-status').textContent = settings.phone_ids.length ? settings.phone_ids.length+' business connection(s) saved.' : 'No business number connected yet. You can add knowledge while you prepare your Meta account.';
  const selectedPhone=$('phone-select').value; $('phone-select').replaceChildren(new Option('Choose a connected number','')); for(const phone of settings.phone_ids)$('phone-select').append(new Option(phone,phone)); $('phone-select').value=settings.phone_ids.includes(selectedPhone)?selectedPhone:(settings.phone_ids[0]||'');
  $('create-agent').disabled=!settings.phone_ids.length;
  await Promise.all([loadChannels(),loadDocuments()]);
}
$('refresh-setup').onclick=()=>busy($('refresh-setup'),loadSetup);
$('more-documents').onclick=()=>busy($('more-documents'),()=>loadDocuments(true));
$('more-channels').onclick=()=>busy($('more-channels'),()=>loadChannels(true));
$('connection-form').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy(form.querySelector('button'),async()=>{
  const payload=Object.fromEntries(new FormData(form));
  try { const r=await fetch('/console/whatsapp/connection',{method:'POST',headers:{'Content-Type':'application/json','X-Console-Intent':'user-action'},body:JSON.stringify(payload),signal:AbortSignal.timeout(15000)});const data=await r.json();if(!r.ok)throw Error(data.detail||'Connection unavailable');notice('Connection saved. Create an agent next, then verify its webhook in Meta.'); }
  finally { for(const name of ['access_token','app_secret','verify_token']){form.elements.namedItem(name).value='';delete payload[name];} }
  await loadSetup();
});};
$('document-form').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy(form.querySelector('button'),async()=>{ const values=new FormData(form);let id=form.dataset.requestId;if(!id){id=crypto.randomUUID();form.dataset.requestId=id;} await api('knowledge/documents','POST',{external_id:'whatsapp-'+id,title:values.get('title'),text:values.get('text'),visibility:'app'});form.reset();delete form.dataset.requestId;await loadDocuments();notice('Knowledge published. Select it when creating your agent.'); });};
$('agent-form').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;busy($('create-agent'),async()=>{const ids=Array.from($('documents').querySelectorAll('input:checked'),x=>x.value);if(!ids.length)throw Error('Select at least one customer-facing document.');const values=new FormData(form);await api('whatsapp/channels','POST',{name:values.get('name'),phone_id:values.get('phone_id'),documents:ids});notice('Agent created inactive. Complete its activation checklist below.');await loadChannels();});};
async function loadInbox(more=false) {
  const channel=$('inbox-channel').value; const epoch=++inboxEpoch; if(!more){selected=null;detailEpoch++;$('conversation-detail').replaceChildren();el('p','Select a conversation to see messages.',$('conversation-detail'));}
  if(!channel){conversations=[];$('conversations').replaceChildren();$('more-conversations').hidden=true;return;}
  const rows=await api('whatsapp/channels/'+channel+'/conversations?limit=25'+(more&&conversationCursor?'&before='+conversationCursor:''));if(epoch!==inboxEpoch)return;
  conversations=more?[...conversations,...rows]:rows;conversationCursor=rows.length===25?rows.at(-1).id:null;$('conversations').replaceChildren();
  for(const c of [...conversations].sort((a,b)=>b.updated_at.localeCompare(a.updated_at))){const b=action($('conversations'),c.customer+' · '+(c.mode==='HUMAN'?'Needs a person':'Automatic'),()=>openConversation(c));b.className='conversation';}
  if(!conversations.length)el('p','No incoming messages yet. After activation, send a question from a customer WhatsApp account to your business number.',$('conversations'));
  $('more-conversations').hidden=!conversationCursor;
}
async function openConversation(c,more=false) {
  const channel=$('inbox-channel').value,epoch=++detailEpoch;selected=c;
  const rows=await api('whatsapp/channels/'+channel+'/conversations/'+c.id+'?limit=50'+(more&&messageCursor?'&before='+messageCursor:''));if(epoch!==detailEpoch||channel!==$('inbox-channel').value)return;
  messageCursor=rows.length===50?rows.at(-1).id:null;const panel=$('conversation-detail');if(!more){panel.replaceChildren();el('h3',c.customer,panel);el('p',c.mode==='HUMAN'?'Human takeover active':'Automatic replies active',panel);
    action(panel,c.mode==='HUMAN'?'Resume for new messages':'Take over conversation',async()=>{await api('whatsapp/channels/'+channel+'/conversations/'+c.id+'/mode','POST',{mode:c.mode==='HUMAN'?'AUTO':'HUMAN'});c.mode=c.mode==='HUMAN'?'AUTO':'HUMAN';await openConversation(c);});}
  panel.querySelector('[data-more]')?.remove();
  for(const m of [...rows].sort((a,b)=>a.received_at.localeCompare(b.received_at))){const box=el('article','',panel,'message');el('small',new Date(m.received_at).toLocaleString()+' · '+m.status+(m.delivery?' · '+m.delivery:''),box);el('p',m.body||'Unsupported attachment — human review needed',box);
    if(m.reply)el('p',(m.human_reply?'Human reply: ':'Agent reply: ')+m.reply,box,'reply');
    if(m.status==='UNKNOWN')el('p','Delivery or generation is uncertain. Check provider records before sending anything else. Automatic retries are blocked.',box);
    if(m.evidence?.citations?.length){const d=el('details','',box);el('summary','Sources used',d);for(const source of m.evidence.citations)el('p',source.title+' · '+source.text,d);}
    if(c.mode==='HUMAN'&&['HANDOFF','CANCELLED'].includes(m.status)){const form=document.createElement('form');box.append(form);const label=el('label','Your reply',form);const input=document.createElement('textarea');input.required=true;input.maxLength=4000;input.rows=3;label.append(input);const send=el('button','Send WhatsApp reply',form);send.type='submit';send.className='primary';
      const expired=Date.now()-new Date(m.received_at).getTime()>((23*60+55)*60000);send.disabled=expired;if(expired)el('p','The reply window has expired. Wait for a new customer message.',form);
      form.onsubmit=event=>{event.preventDefault();busy(send,async()=>{await api('whatsapp/channels/'+channel+'/messages/'+m.id+'/reply','POST',{text:input.value});notice('Reply queued. Refresh to check provider acceptance and delivery.');await openConversation(c);});};}
  }
  if(messageCursor){const b=action(panel,'More messages',()=>openConversation(c,true));b.dataset.more='true';}
}
$('inbox-channel').onchange=()=>loadInbox().catch(showError);
$('refresh-inbox').onclick=()=>busy($('refresh-inbox'),async()=>{const current=selected;await loadInbox();if(current){const refreshed=conversations.find(c=>c.id===current.id);if(refreshed)await openConversation(refreshed);}});
$('more-conversations').onclick=()=>busy($('more-conversations'),()=>loadInbox(true));
loadSavedConnections().catch(showError);
loadSetup().then(()=>{if(location.hash==='#inbox'){tab(true);return loadInbox();}}).catch(showError);

async function loadSavedConnections(){
 const r=await fetch('/console/whatsapp/saved-connections',{headers:{'X-Console-Intent':'user-action'},signal:AbortSignal.timeout(10000)});
 if(!r.ok)throw Error('Could not load saved Meta connections');
 const data=await r.json(); const select=$('refresh-phone'),previous=select.value;
 select.replaceChildren(...data.connections.map(c=>new Option(c.phone_id+' · '+c.graph_version,c.phone_id)));
 if(data.connections.some(c=>c.phone_id===previous))select.value=previous;
 $('token-refresh').hidden=!data.connections.length;
}
$('token-refresh-form').onsubmit=event=>{event.preventDefault();const form=event.currentTarget;
 busy(event.submitter,async()=>{
  const payload=Object.fromEntries(new FormData(form));
  try{
   const r=await fetch('/console/whatsapp/refresh-token',{method:'POST',headers:{'Content-Type':'application/json','X-Console-Intent':'user-action'},body:JSON.stringify(payload),signal:AbortSignal.timeout(15000)});
   const data=await r.json();if(!r.ok)throw Error(data.detail||'Token refresh failed');
   notice('New token saved privately. Other connection details are unchanged. Verify the connection before activating automatic replies.');
  }finally{form.elements.access_token.value='';delete payload.access_token;}
  await loadSetup();
 });
};
