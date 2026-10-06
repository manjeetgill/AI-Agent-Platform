"use strict";
const form=document.getElementById('refresh-form'), phone=document.getElementById('phone'), save=document.getElementById('save'), status=document.getElementById('status');
async function load(){
 try{
  const r=await fetch('/console/whatsapp/saved-connections',{cache:'no-store',signal:AbortSignal.timeout(15000)});
  const data=await r.json();if(!r.ok)throw Error(data.detail||'Cannot load saved connections');
  phone.replaceChildren(...data.connections.map(c=>new Option(c.phone_id+' · '+c.graph_version,c.phone_id)));
  phone.disabled=save.disabled=!data.connections.length;
  status.textContent=data.connections.length?'Saved connection found. Paste only your new access token below.':'No saved direct Meta connection was found. Return to WhatsApp setup.';
 }catch(e){status.textContent=e.message;}
}
form.addEventListener('submit',async event=>{
 event.preventDefault();save.disabled=true;
 const body={phone_id:phone.value,access_token:form.elements.access_token.value};
 try{
  const r=await fetch('/console/whatsapp/refresh-token',{method:'POST',headers:{'Content-Type':'application/json','X-Console-Intent':'user-action'},body:JSON.stringify(body),signal:AbortSignal.timeout(15000)});
  const data=await r.json();if(!r.ok)throw Error(data.detail||'Could not save token');
  status.textContent='New access token saved securely. Tell the assistant “saved” so it can check the connection.';
 }catch(e){status.textContent=e.message;}
 finally{form.elements.access_token.value='';delete body.access_token;save.disabled=false;}
});
load();
