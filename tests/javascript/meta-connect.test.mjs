import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = await readFile(new URL('../../examples/whatsapp-integration/connect.js', import.meta.url), 'utf8');
const {connectWhatsApp} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
function element() { return {children: [], append(x) {this.children.push(x);}, replaceChildren(...x) {this.children=x;}}; }
function setup(responses) {
  const calls=[];
  globalThis.document={createElement: () => element()};
  const storage=new Map();
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.fetch=async (url, init)=>{calls.push({url,init});return {ok:true,json:async()=>responses.shift()};};
  return {calls,storage,container:element(),status:{}};
}
test('missing Meta configuration does not fall back to Chakra', async()=>{
  const f=setup([{available:false,message:'Finish Meta setup'}]);
  const cleanup=await connectWhatsApp(f.container,f.status);
  assert.equal(f.container.children.length,0);
  assert.equal(f.status.textContent,'Finish Meta setup');
  assert.equal(f.calls.length,1); cleanup();
});
test('direct signup retains only ID and checks authoritative completion', async()=>{
  const f=setup([{available:true,message:'Ready',public_url:'https://agents.example.com'},
    {id:'session',connect_url:'https://agents.example.com/connect/whatsapp/session#ticket=private'},
    {state:'CONNECTED',message:'Connected'}]);
  const cleanup=await connectWhatsApp(f.container,f.status,'/api/whatsapp','clinic-one');
  await f.container.children[0].onclick();
  assert.equal(f.calls[1].init.method,'POST');
  assert.equal(f.calls[2].init.method,'GET');
  assert.equal(f.status.textContent,'Connected');
  assert.equal(f.storage.size,0);
  assert.equal(f.container.children[0].rel,'noopener noreferrer'); cleanup();
});
test('reload reads saved session without starting another signup',async()=>{
  const f=setup([{state:'WEBHOOK_PENDING',message:'Check setup',can_retry:true}]);
  f.storage.set('meta-signup:clinic-one','saved');
  const cleanup=await connectWhatsApp(f.container,f.status,'/api/whatsapp','clinic-one');
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].url,'/api/whatsapp/onboarding/saved');
  assert.equal(f.calls[0].init.method,'GET'); cleanup();
});
