'use strict';
const $=id=>document.getElementById(id);
const root=location.pathname;
let active=false,finished=false,codeSent=false,assetSent=false,polling=false,sdkReady=false,current;
async function call(action,body,headers={}){const r=await fetch(root+'/'+action,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',...headers},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(25000)});const value=await r.json();if(!r.ok)throw Error(typeof value.detail==='string'?value.detail:'Connection unavailable. Return to your app and try again.');return value;}
function error(message){$('error').hidden=false;$('error').textContent=message;}
function render(value){current=value;$('status').textContent=value.message;$('retry').hidden=!value.can_retry;$('done').hidden=value.state!=='CONNECTED';$('cancel').hidden=!['CONNECTING','EXCHANGING','WAITING_ASSETS'].includes(value.state);$('connect').disabled=!sdkReady||value.state!=='CONNECTING'||active;if(value.state==='CONNECTED'){finished=true;active=false;$('connect').hidden=true;}}
async function poll(){if(polling||finished)return;polling=true;try{render(await call('status'));}catch(e){error(e.message);}finally{polling=false;}if(!finished)setTimeout(poll,2500);}
window.addEventListener('message',async event=>{
  if(!active||!['https://www.facebook.com','https://web.facebook.com','https://business.facebook.com'].includes(event.origin)||typeof event.data!=='string'||event.data.length>16000)return;
  let data;try{data=JSON.parse(event.data);}catch{return;}
  if(data.type!=='WA_EMBEDDED_SIGNUP')return;
  if(data.event==='FINISH'&&!assetSent){
    const d=data.data||{};
    if(!/^\d{5,30}$/.test(d.waba_id||'')||!/^\d{5,30}$/.test(d.phone_number_id||'')){error('Meta did not return a verified number. Complete number setup and reconnect.');return;}
    assetSent=true;
    try{await call('assets',{waba_id:d.waba_id,phone_id:d.phone_number_id,...(d.business_id?{business_id:d.business_id}:{}),finish_type:'FINISH'});}catch(e){assetSent=false;error(e.message);}
  }else if(data.event==='CANCEL'||data.event==='ERROR'){
    // Final-screen closure may still yield a successful code: do not erase an exchange already received.
    if(!codeSent){active=false;await call('cancel',{}).catch(()=>{});error('Meta authorization was not completed. Return to your app to reconnect.');}
  }else if(String(data.event).startsWith('FINISH')&&data.event!=='FINISH'){
    error('This flow did not connect a standard Cloud API number. Complete number setup in Meta and reconnect.');
  }
});
$('connect').onclick=()=>{
  if(!sdkReady||active)return;active=true;$('connect').disabled=true;$('error').hidden=true;
  FB.login(response=>{
    const code=response?.authResponse?.code;
    if(typeof code==='string'&&code.length){codeSent=true;
      // Exchange immediately: Meta codes expire in 30 seconds. Asset events can arrive later.
      call('code',{code}).catch(e=>error(e.message));
    }else if(!assetSent){active=false;call('cancel',{}).catch(()=>{});error('The Meta window closed without authorization. Return to your app to reconnect.');}
  },{config_id:current.sdk.configuration_id,response_type:'code',override_default_response_type:true,extras:{setup:{}}});
};
$('retry').onclick=async()=>{try{await call('retry',{});await poll();}catch(e){error(e.message);}};
$('cancel').onclick=async()=>{try{await call('cancel',{});active=false;render(await call('status'));}catch(e){error(e.message);}};
async function init(){
  const ticket=new URLSearchParams(location.hash.slice(1)).get('ticket');
  history.replaceState(null,'',location.pathname);
  if(ticket)await call('bootstrap',{}, {'X-Signup-Ticket':ticket});
  render(await call('status'));
  window.fbAsyncInit=()=>{FB.init({appId:current.sdk.app_id,version:current.sdk.graph_version,autoLogAppEvents:false,xfbml:false});sdkReady=true;render(current);};
  const script=document.createElement('script');script.src='https://connect.facebook.net/en_US/sdk.js';script.async=true;script.defer=true;script.crossOrigin='anonymous';script.onerror=()=>error('Meta could not load. Check your connection and reload this page.');document.head.append(script);
  poll();
}
init().catch(e=>error(e.message));
