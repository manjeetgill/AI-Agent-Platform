import test from 'node:test';
import assert from 'node:assert/strict';
import {retailerTick,retailerStop} from './retailer-worker.js';
const cmd={id:'12345678-1234-1234-1234-123456789abc',operation:'retailer.observe',retailer:'croma',url:'https://www.croma.com/fixture/p/12345',expires_at:new Date(Date.now()+290000).toISOString()};
function mock(){
 const saved={retailerEnabled:true,retailerPair:{api_base:'http://127.0.0.1:4060',pairing_code:'private',retailers:['croma']}};
 const log=[];let polled=false,ready=false;
 globalThis.chrome={storage:{local:{get:async keys=>Object.fromEntries((Array.isArray(keys)?keys:[keys]).map(k=>[k,saved[k]])),set:async v=>Object.assign(saved,v),remove:async keys=>{for(const k of Array.isArray(keys)?keys:[keys])delete saved[k];}}},permissions:{contains:async()=>true},tabs:{create:async v=>{log.push('tab');return {id:5};},get:async()=>({status:ready?'complete':'loading'})},scripting:{executeScript:async()=>[{result:{outcome:'OBSERVED',url:cmd.url,observed_at:new Date().toISOString(),title:'Fixture',price:100,variant:'1pc',issues:[]}}]}};
 globalThis.fetch=async(url,options)=>{const path=url.split('/v1/retailer-browser/')[1];log.push(path);assert.equal(options.redirect,'error');return {ok:true,json:async()=>path==='poll'?{command:polled?null:(polled=true,cmd)}:{recorded:true}};};
 return {saved,log,ready:()=>ready=true};
}
test('hydration ticks resume one saved job without opening duplicate tabs',async()=>{
 const f=mock();await retailerTick();assert.equal(f.saved.retailerCurrent.tabId,5);f.ready();await retailerTick();await retailerTick();
 assert.equal(f.log.filter(x=>x==='tab').length,1);assert.equal(f.log.filter(x=>x.endsWith('/result')).length,1);assert.equal(f.saved.retailerCurrent,undefined);
});
test('ambiguous opening crash never opens another tab',async()=>{
 const f=mock();f.saved.retailerCurrent={command:cmd,phase:'opening'};await retailerTick();assert.equal(f.log.includes('tab'),false);assert.equal(f.saved.retailerEnabled,false);
});
test('stop removes local authority even when server is offline',async()=>{
 const f=mock();globalThis.fetch=async()=>{throw Error('offline');};await retailerStop();assert.equal(f.saved.retailerEnabled,false);assert.equal(f.saved.retailerPair,undefined);
});
