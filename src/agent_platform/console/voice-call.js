import {mountVoiceWidget} from './voice-widget.js';
const token=location.hash.slice(1);
history.replaceState(null,'',location.pathname);
const status=document.getElementById('invitation-status');
async function request(path,method='GET',body){
  const r=await fetch('/public/voice/'+path,{method,signal:AbortSignal.timeout(65000),headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
  const value=await r.json();
  if(!r.ok)throw new Error(typeof value.detail==='string'?value.detail:'Call unavailable. Return to WhatsApp.');
  return value;
}
try{
  if(!/^[A-Za-z0-9_-]{43}$/.test(token))throw new Error('Open the private call link from WhatsApp.');
  const invitation=await request('invitation');
  if(invitation.used)throw new Error('This link has already been used. Request another call in WhatsApp.');
  status.textContent='Your booking conversation is ready. Review audio consent and start your call.';
  mountVoiceWidget(document.getElementById('voice-widget'),{
    agentId:invitation.agent_id,iceServers:invitation.ice_servers,
    consentText:'I agree to my audio being processed by the AI speech service to continue my appointment conversation.',
    api:(path,method,body)=>{
      if(path===`voice/agents/${invitation.agent_id}/sessions`)return request('session',method,body);
      const match=path.match(/^voice\/sessions\/([a-f0-9-]+)(\/end)?$/);
      if(!match)throw new Error('Unsupported call operation');
      return request('session/'+match[1]+(match[2]||''),method,body);
    }
  });
}catch(e){status.textContent=e.message;}
