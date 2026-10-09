import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {publicIPv4,allowedRequest,renderPublic} from '../../src/agent_platform/probes/server-retailer.mjs';
const c={id:'12345678-1234-1234-1234-123456789abc',operation:'retailer.observe',retailer:'croma',url:'https://www.croma.com/fixture/p/12345',expires_at:new Date(Date.now()+300000).toISOString()};
test('renderer only allows HTTPS public IPv4 destinations and read methods',()=>{
 for(const ip of ['127.0.0.1','10.0.1.1','169.254.169.254','172.16.0.1','192.168.1.1','100.64.1.1','224.1.1.1','::1','999.1.1.1','198.18.1.1'])assert.equal(publicIPv4(ip),false);
 assert.ok(publicIPv4('8.8.8.8'));
 const hosts=new Set(['www.croma.com']);assert.ok(allowedRequest(c.url,'GET',hosts));
 for(const [url,method,type] of [['http://www.croma.com/x','GET','document'],['https://127.0.0.1/x','GET','document'],[c.url,'POST','fetch'],['https://user:secret@www.croma.com/x','GET','document'],[c.url,'GET','websocket']])assert.equal(allowedRequest(url,method,hosts,type),false);
});
test('server and extension use the same versioned DOM extraction',async()=>{
 assert.equal(await readFile(new URL('../../browser-extension/retailer-policy.js',import.meta.url),'utf8'),await readFile(new URL('../../src/agent_platform/probes/retailer-dom.mjs',import.meta.url),'utf8'));
});
test('DNS resolving to private IP never launches a browser',async()=>{
 let called=false;await assert.rejects(renderPublic({launch:async()=>{called=true;}},c,[],async()=>[{address:'127.0.0.1'}]));assert.equal(called,false);
});
test('fresh context, sandbox, blocked websockets, access denials and cleanup',async()=>{
 let closed=false;let opts;let routeHandler;
 const page={on(){},goto:async()=>({status:()=>403}),mainFrame:()=>({})};
 const context={routeWebSocket:async(pattern,handler)=>{let shut=false;handler({close(){shut=true;}});assert.ok(shut);},route:async(pattern,handler)=>{routeHandler=handler;},newPage:async()=>page,on(){}};
 const chromium={launch:async options=>{assert.equal(options.chromiumSandbox,true);assert.ok(options.args.some(x=>x.includes('MAP www.croma.com 8.8.8.8')));return {newContext:async o=>{opts=o;return context;},close:async()=>{closed=true;}};}};
 const result=await renderPublic(chromium,c,[],async()=>[{address:'8.8.8.8'}]);
 assert.equal(result.outcome,'BLOCKED');assert.ok(closed);assert.equal(opts.serviceWorkers,'block');assert.equal(opts.ignoreHTTPSErrors,false);assert.deepEqual(opts.permissions,[]);assert.equal(opts.storageState,undefined);
 let aborted=false;await routeHandler({request:()=>({url:()=>c.url,method:()=> 'POST',resourceType:()=> 'fetch'}),abort:()=>{aborted=true;},continue:()=>assert.fail('POST permitted')});assert.ok(aborted);
});

test('navigation failure closes the browser and reports unavailability without raw errors',async()=>{
 let closed=false;
 const page={on(){},goto:async()=>{throw Error('sensitive URL data');}};
 const context={routeWebSocket:async()=>{},route:async()=>{},newPage:async()=>page,on(){}};
 const chromium={launch:async()=>({newContext:async()=>context,close:async()=>{closed=true;}})};
 const result=await renderPublic(chromium,c,[],async()=>[{address:'8.8.8.8'}]);
 assert.equal(result.outcome,'UNAVAILABLE');assert.ok(closed);assert.ok(!JSON.stringify(result).includes('sensitive'));
});
