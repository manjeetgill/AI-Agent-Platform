import {retailerOrigins,validRetailerCommand,captureRetailer} from './retailer-policy.js';
let busy=false;
async function api(path,body){
  const {retailerPair:p}=await chrome.storage.local.get('retailerPair');
  if(!p)throw Error('Pair the retailer runner first.');
  const r=await fetch(p.api_base+'/v1/retailer-browser/'+path,{method:'POST',redirect:'error',signal:AbortSignal.timeout(10000),headers:{'Content-Type':'application/json',Authorization:'Bearer '+p.pairing_code},body:body?JSON.stringify(body):undefined});
  if(!r.ok)throw Error(r.status===401?'Pairing expired or revoked. Import a fresh file.':'Job paused, expired, or platform unavailable.');
  return r.json();
}
async function status(retailerStatus){await chrome.storage.local.set({retailerStatus});}
export async function retailerStop(){
  await chrome.storage.local.set({retailerEnabled:false,retailerStatus:'Stopped. Pair again to continue.'});
  const {retailerCurrent:c}=await chrome.storage.local.get('retailerCurrent');
  if(c?.tabId)try{await chrome.scripting.executeScript({target:{tabId:c.tabId},func:()=>{globalThis.__workspaceStopped=true;}});}catch{}
  try{await api('stop');}catch{}
  await chrome.storage.local.remove(['retailerCurrent','retailerPair']);
}
export async function retailerTick(){
  if(busy)return;busy=true;
  try{
    const state=await chrome.storage.local.get(['retailerEnabled','retailerPair','retailerCurrent']);
    if(!state.retailerEnabled||!state.retailerPair)return;
    const allowed=[];
    for(const name of state.retailerPair.retailers)if(await chrome.permissions.contains({origins:[retailerOrigins[name]]}))allowed.push(name);
    let current=state.retailerCurrent;
    if(!current){
      const command=(await api('poll',{retailers:allowed})).command;
      if(!command)return;
      if(!validRetailerCommand(command)||!allowed.includes(command.retailer))throw Error('Unsupported or expired product command.');
      // Persist before creating a tab. A crash here must not cause a second tab.
      current={command,phase:'opening'};
      await chrome.storage.local.set({retailerCurrent:current});
      await api(command.id+'/authorize');
      if(!(await chrome.storage.local.get('retailerEnabled')).retailerEnabled)return;
      const tab=await chrome.tabs.create({url:command.url,active:true});
      current={...current,phase:'reading',tabId:tab.id};
      await chrome.storage.local.set({retailerCurrent:current});
    }
    const command=current.command;
    if(!validRetailerCommand(command)||!allowed.includes(command.retailer))throw Error('Expired job or retailer permission removed.');
    if(!current.tabId)throw Error('Opening was interrupted. Inspect Chrome before starting a new check.');
    await status('Reading '+command.retailer+' product. Touch the page or press Stop to take over.');
    // Each tick is short; alarm/popup continues the same saved job, never reopens it.
    if((await chrome.tabs.get(current.tabId)).status!=='complete')return;
    await api(command.id+'/authorize');
    if(!(await chrome.storage.local.get('retailerEnabled')).retailerEnabled)return;
    const results=await chrome.scripting.executeScript({target:{tabId:current.tabId},func:captureRetailer,args:[command]});
    let capture=results.length===1?results[0].result:null;
    if(!capture)throw Error('Product page could not be read.');
    // A bounded hydration wait. No reloads, clicks, login or submission.
    if(capture.outcome==='PAGE_CHANGED'&&Date.now()-Date.parse(command.expires_at)+300000<20000)return;
    await api(command.id+'/result',capture);
    await chrome.storage.local.remove('retailerCurrent');
    await status(capture.outcome==='OBSERVED'?'Observation saved. Check the task for evidence quality.':capture.outcome==='LOCATION_REQUIRED'?'Delivery postcode could not be verified on this page. Review the task; do not use this as a location-specific price.':'Stopped: '+capture.outcome+'. Inspect the page.');
  }catch{
    // Do not print remote errors, URLs, page text or tokens to the popup/logs.
    await status('Check interrupted, permission missing, or platform unavailable. Inspect the task and page before pairing again.');
    await chrome.storage.local.set({retailerEnabled:false});
    // Retain current job for audit. Server expires it and blocks this source.
  }finally{busy=false;}
}
export async function registerRetailerGuard(){
  const {retailerPair:p}=await chrome.storage.local.get('retailerPair');
  const origins=[];
  for(const name of p?.retailers||[])if(await chrome.permissions.contains({origins:[retailerOrigins[name]]}))origins.push(retailerOrigins[name]);
  try{await chrome.scripting.unregisterContentScripts({ids:['retailer-takeover']});}catch{}
  if(origins.length)await chrome.scripting.registerContentScripts([{id:'retailer-takeover',matches:origins,js:['guard.js'],runAt:'document_start',persistAcrossSessions:true}]);
}
