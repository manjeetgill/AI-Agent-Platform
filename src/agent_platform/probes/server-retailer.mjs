// Public, isolated rendering. No personal profile, arbitrary code, login or clicks.
import {lookup} from 'node:dns/promises';
import {pathToFileURL} from 'node:url';
import {isAbsolute} from 'node:path';
import {validRetailerCommand,captureRetailer} from './retailer-dom.mjs';

export function publicIPv4(ip){
  const parts=ip.split('.');if(parts.length!==4||parts.some(x=>!/^\d{1,3}$/.test(x)||+x>255))return false;
  const [a,b,c]=parts.map(Number);
  return !(a===0||a===10||a===127||a>=224||(a===169&&b===254)||(a===172&&b>=16&&b<=31)||
    (a===192&&(b===168||b===0||b===2))||(a===100&&b>=64&&b<=127)||(a===198&&(b===18||b===19||(b===51&&c===100)))||(a===203&&b===0&&c===113));
}
export function allowedRequest(url,method,hosts,type='document'){
  try{const u=new URL(url);return u.protocol==='https:'&&!u.username&&!u.password&&!u.port&&hosts.has(u.hostname)&&
    ['GET','HEAD'].includes(method)&&['document','script','stylesheet','xhr','fetch'].includes(type);}catch{return false;}
}
export async function renderPublic(chromium,command,extraHosts=[],resolve=lookup){
  if(!validRetailerCommand(command)||!Array.isArray(extraHosts)||extraHosts.length>16)throw Error('Invalid configuration');
  const hosts=new Set([new URL(command.url).hostname,...extraHosts]);
  const maps=[];
  for(const host of hosts){
    if(typeof host!=='string'||!/^([a-z0-9-]+\.)+[a-z]{2,}$/.test(host))throw Error('Invalid host');
    const ips=await resolve(host,{family:4,all:true});
    if(!ips.length||ips.some(v=>!publicIPv4(v.address)))throw Error('Nonpublic DNS');
    maps.push('MAP '+host+' '+ips[0].address);
  }
  // Pin allowed host resolutions for this fresh process. Unknown hosts fail DNS
  // as well as request interception; redirects are rechecked by the route.
  const browser=await chromium.launch({headless:true,chromiumSandbox:true,args:[
    '--host-resolver-rules='+[...maps,'MAP * ~NOTFOUND'].join(', '),'--disable-quic',
    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp','--disable-background-networking']});
  const diagnostics={transport:'server_browser',http_status:null,requests:0,blocked_resources:0,failed_requests:0,request_limit_reached:false};
  const boundedIncrement=key=>{diagnostics[key]=Math.min(10000,diagnostics[key]+1);};
  try{
    const context=await browser.newContext({acceptDownloads:false,serviceWorkers:'block',permissions:[],ignoreHTTPSErrors:false});
    await context.routeWebSocket('**/*',ws=>ws.close());
    let main;
    context.on('requestfailed',()=>boundedIncrement('failed_requests'));
    await context.route('**/*',async route=>{
      const r=route.request();
      boundedIncrement('requests');
      if(diagnostics.requests>160)diagnostics.request_limit_reached=true;
      if(diagnostics.request_limit_reached||!allowedRequest(r.url(),r.method(),hosts,r.resourceType())||
          (r.isNavigationRequest()&&main&&r.frame()!==main.mainFrame())){
        if(['script','stylesheet','xhr','fetch'].includes(r.resourceType()))boundedIncrement('blocked_resources');
        return route.abort();
      }
      return route.continue();
    });
    main=await context.newPage();context.on('page',p=>{if(p!==main)p.close().catch(()=>{});});
    main.on('dialog',d=>d.dismiss().catch(()=>{}));
    const response=await main.goto(command.url,{waitUntil:'domcontentloaded',timeout:20000});
    diagnostics.http_status=response?.status()??null;
    const base={url:command.url,observed_at:new Date().toISOString(),issues:[],parser_version:'retailer-dom-1',diagnostics};
    if([401,403,429].includes(response?.status()))return {...base,outcome:response.status()===401?'LOGIN_REQUIRED':response.status()===429?'RATE_LIMITED':'BLOCKED'};
    // Bounded rendering wait. Capture does not click or execute page instructions.
    let capture;
    for(let i=0;i<6;i++){
      capture=await main.evaluate(captureRetailer,command);
      if(capture.outcome!=='PAGE_CHANGED')break;
      await new Promise(r=>setTimeout(r,1500));
    }
    return {...capture,diagnostics};
  }catch{
    return {outcome:'UNAVAILABLE',url:command.url,observed_at:new Date().toISOString(),issues:[],parser_version:'retailer-dom-1',diagnostics};
  }finally{await browser.close();}
}
async function main(){
  const [modulePath,json,hosts]=process.argv.slice(2);
  try{
    if(!isAbsolute(modulePath))throw Error();
    const {chromium}=await import(pathToFileURL(modulePath).href);
    const value=await renderPublic(chromium,JSON.parse(json),JSON.parse(hosts));
    process.stdout.write(JSON.stringify(value));
  }catch{process.stdout.write(JSON.stringify({configuration_required:true}));}
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)await main();
