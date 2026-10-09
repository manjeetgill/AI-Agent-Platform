import {validPairing,retailerOrigins} from './retailer-policy.js';
const $=id=>document.getElementById(id);
$('connect').onclick=async()=>{const pair=$('pair').value.trim();if(!/^[a-f0-9-]{36}\.[A-Za-z0-9_-]{43}$/.test(pair)){$('status').textContent='Paste the complete pairing code.';return;}await chrome.storage.local.set({pair,enabled:true,status:'Connected. Waiting for approved work.'});$('pair').value='';await chrome.runtime.sendMessage({action:'tick'});};
$('stop').onclick=()=>chrome.runtime.sendMessage({action:'stop'});
async function refresh(){const value=await chrome.storage.local.get('status');$('status').textContent=value.status||'Not paired';chrome.runtime.sendMessage({action:'tick'}).catch(()=>{});}
refresh();setInterval(refresh,1000);

let pendingPair=null;
$('retailer-file').onchange=async()=>{
  pendingPair=null;$('retailer-connect').disabled=true;
  try{const file=$('retailer-file').files[0];if(!file||file.size>4000)throw Error();
    const p=JSON.parse(await file.text());if(!validPairing(p))throw Error();
    pendingPair=p;$('retailer-sites').textContent='Read product pages on: '+p.retailers.join(', ')+'. Connect to the local API on port 4060.';
    $('retailer-connect').disabled=false;
  }catch{$('retailer-sites').textContent='Choose a valid local retailer pairing file. Do not import an app API key.';}
};
$('retailer-connect').onclick=async()=>{
  if(!pendingPair)return;
  // Request from this explicit user gesture, never from a background task.
  const granted=await chrome.permissions.request({origins:['http://127.0.0.1:4060/*',...pendingPair.retailers.map(n=>retailerOrigins[n])]});
  if(!granted){$('retailer-status').textContent='Access not granted. Nothing started.';return;}
  await chrome.storage.local.set({retailerPair:pendingPair,retailerEnabled:true,retailerStatus:'Paired. Waiting for product checks.'});
  await chrome.storage.local.remove('retailerCurrent');pendingPair=null;$('retailer-file').value='';$('retailer-connect').disabled=true;
  await chrome.runtime.sendMessage({action:'retailer-connect'});
};
$('retailer-stop').onclick=()=>chrome.runtime.sendMessage({action:'retailer-stop'});
async function retailerRefresh(){const s=await chrome.storage.local.get('retailerStatus');$('retailer-status').textContent=s.retailerStatus||'Not paired';chrome.runtime.sendMessage({action:'retailer-tick'}).catch(()=>{});}
retailerRefresh();setInterval(retailerRefresh,2000);
