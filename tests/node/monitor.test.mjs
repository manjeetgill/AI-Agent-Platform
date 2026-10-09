import test from 'node:test';
import assert from 'node:assert/strict';
import {controls,elapsed,reason,safeLink,stepUsage,waitHint} from '../../src/agent_platform/console/monitor-model.mjs';
test('uncertain outcomes allow reconciliation, never resume or retry',()=>{
 assert.deepEqual(controls('OUTCOME_UNKNOWN'),['reconcile','cancel']);
 assert.deepEqual(controls('PAUSED'),['resume','cancel']);
 assert.deepEqual(controls('FAILED'),[]);assert.deepEqual(controls('DONE'),[]);
 assert.ok(reason({status:'OUTCOME_UNKNOWN'}).includes('checked'));
});
test('elapsed time stops at terminal update and never fabricates missing usage',()=>{
 const task={created_at:'2026-10-09T00:00:00Z',updated_at:'2026-10-09T00:01:05Z',status:'DONE'};
 assert.equal(elapsed(task,Date.parse('2026-10-09T01:00:00Z')),'1m 5s');
 assert.equal(elapsed({...task,status:'RUNNING'},Date.parse('2026-10-09T01:00:00Z')),'1h 0m');
 assert.deepEqual(stepUsage([{position:0,result:{price:99}}]),[]);
 assert.equal(stepUsage([{position:1,result:{provider:'fixture'}}])[0].tokens,null);
});
test('evidence links cannot execute code or contain credentials',()=>{
 for(const url of ['javascript:alert(1)','data:text/html,bad','http://site.example','https://user:secret@site.example'])assert.equal(safeLink(url),null);
 assert.equal(safeLink('https://retailer.example/product'),'https://retailer.example/product');
});

test('stalled tasks describe recovery without offering blind execution',()=>{
 const now=Date.parse('2026-10-09T01:00:00Z');
 assert.match(waitHint({status:'RUNNING',lease_until:'2026-10-09T00:59:00Z'},now),/lease expired/);
 assert.match(waitHint({status:'READY',next_attempt_at:'2026-10-09T01:10:00Z'},now),/Next worker check/);
 assert.match(waitHint({status:'READY',updated_at:'2026-10-09T00:50:00Z'},now),/Still queued/);
 assert.equal(waitHint({status:'DONE',updated_at:'2026-10-09T00:50:00Z'},now),'');
});
