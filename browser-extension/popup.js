const $=id=>document.getElementById(id);
$('connect').onclick=async()=>{const pair=$('pair').value.trim();if(!/^[a-f0-9-]{36}\.[A-Za-z0-9_-]{43}$/.test(pair)){$('status').textContent='Paste the complete pairing code.';return;}await chrome.storage.local.set({pair,enabled:true,status:'Connected. Waiting for approved work.'});$('pair').value='';await chrome.runtime.sendMessage({action:'tick'});};
$('stop').onclick=()=>chrome.runtime.sendMessage({action:'stop'});
async function refresh(){const value=await chrome.storage.local.get('status');$('status').textContent=value.status||'Not paired';chrome.runtime.sendMessage({action:'tick'}).catch(()=>{});}
refresh();setInterval(refresh,1000);
