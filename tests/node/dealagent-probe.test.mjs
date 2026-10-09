import {test} from 'node:test';
import assert from 'node:assert/strict';
import {probe,ownsHost} from '../../src/agent_platform/probes/dealagent.mjs';
const cfg={healthCheckUrl:'https://retailer.example/p/1', parserVersion:'1', domains:['retailer.example']};
const page={ok:true, body:'fixture only', fetchedAt:Date.now(), status:200};
function context(override={}) {return {cfg,http:{fetch:async()=>page}, fetchCount:()=>({http_requests:2,product_requests:1}),
  collector:{extract:()=>({}),normalize:()=>({}),validate:()=>({ok:true,product:{url:cfg.healthCheckUrl,price:499,currency:'INR'}})},...override};}
test('fresh validated price keeps source time and unknown fees',async()=>{
 const result=await probe(context());
 assert.equal(result.status,'WORKING'); assert.equal(result.verification_mode,'live_source');
 assert.equal(result.observations[0].price,499);assert.equal(result.observations[0].fees_known,false);
 assert.equal(result.observations[0].observed_at,new Date(page.fetchedAt).toISOString());
 assert.equal(result.probe.product_requests,1);
});
for(const reason of ['http_403','robots_restricted','captcha','login_required','http_429','javascript_shell']){
 test(reason+' never produces price evidence or invokes parser',async()=>{
  const result=await probe(context({http:{fetch:async()=>({...page,ok:false,failure:{class:reason}})},collector:{extract:()=>assert.fail()}}));
  assert.notEqual(result.status,'WORKING');assert.deepEqual(result.observations,[]);assert.equal(result.reason_code,reason);
 });
}
test('location and rendering requirements never imply verified prices',async()=>{
 for(const extra of [{location:{required:true}},{browserRequired:true}]){
  const result=await probe(context({cfg:{...cfg,...extra},pincode:'560001'}));
  assert.equal(result.status,'CONFIGURATION_REQUIRED');assert.deepEqual(result.observations,[]);
 }
});
test('parser validation failures never become zero-priced observations',async()=>{
 const result=await probe(context({collector:{extract:()=>({}),normalize:()=>({}),validate:()=>({ok:false,product:null})}}));
 assert.equal(result.status,'BROKEN');assert.deepEqual(result.observations,[]);
});

test('configured domain permits its www form, never unrelated hosts',()=>{
 assert.ok(ownsHost({domains:['croma.com']},'www.croma.com'));
 assert.ok(ownsHost({domains:['croma.com']},'croma.com'));
 assert.equal(ownsHost({domains:['croma.com']},'evilcroma.com'),false);
 assert.equal(ownsHost({domains:['croma.com']},'croma.com.evil.example'),false);
});
test('failed robots fetch is not misreported as an explicit robots disallow',async()=>{
 const result=await probe(context({http:{fetch:async()=>({...page,ok:false,failure:{class:'robots_restricted'}})},accessFailure:()=> 'robots_unavailable'}));
 assert.equal(result.status,'UNAVAILABLE');assert.equal(result.reason_code,'robots_unavailable');
});
