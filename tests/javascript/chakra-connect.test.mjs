import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../examples/whatsapp-integration/connect-chakra.js',import.meta.url),'utf8');
const {connectWhatsApp}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
function setup(responses,saved='expired') {
  const calls=[],storage=new Map(saved?[['chakra-signup:clinic',saved]]:[]);
  const root={children:[],append(x){this.children.push(x);},replaceChildren(){this.children=[];}};
  globalThis.document={createElement:()=>({})};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.fetch=async(url,init)=>{calls.push({url,init});return{ok:true,json:async()=>responses.shift()};};
  let opened;
  globalThis.window={ChakraWhatsappConnect:{init:opts=>{opened=opts;return{destroy(){}};}}};
  return {calls,storage,root,status:{},opened:()=>opened};
}
test('expired unreviewed signup only offers status refresh',async()=>{
  const f=setup([{next_action:'operator_review',message:'Operator must check the provider'}]);
  const dispose=await connectWhatsApp(f.root,f.status,'/api/whatsapp','clinic');
  assert.equal(f.calls.length,1);assert.equal(f.calls[0].init.method,'GET');
  assert.deepEqual(f.root.children.map(x=>x.textContent),['Check saved status']);
  assert.equal(f.opened(),undefined);dispose();
});
test('reviewed signup renewal is explicit and does not create another signup',async()=>{
  const f=setup([{next_action:'renew',message:'Ready to renew'}, {id:'expired',connect_token:'private-token'}]);
  const dispose=await connectWhatsApp(f.root,f.status,'/api/whatsapp','clinic');
  assert.equal(f.calls.length,1);
  await f.root.children[0].onclick();
  assert.equal(f.calls[1].url,'/api/whatsapp/onboarding/expired/renew');
  assert.equal(f.opened().connectToken,'private-token');
  assert.deepEqual([...f.storage.values()],['expired']);dispose();
});
test('new signup requires a click and persists only its session ID',async()=>{
  const f=setup([{id:'new',connect_token:'private-token'}],null);
  const dispose=await connectWhatsApp(f.root,f.status,'/api/whatsapp','clinic');
  assert.equal(f.calls.length,0);await f.root.children[0].onclick();
  assert.equal(f.calls[0].url,'/api/whatsapp/onboarding');
  assert.deepEqual([...f.storage.values()],['new']);dispose();
});
