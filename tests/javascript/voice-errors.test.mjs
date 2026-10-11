import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../../src/agent_platform/console/voice-errors.js',import.meta.url),'utf8');
const {voiceFailureMessage}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('budget, quota and timeout have distinct safe instructions',()=>{
  assert.match(voiceFailureMessage({outcome:'SPENDING_LIMIT'}),/spending limit/);
  assert.match(voiceFailureMessage({outcome:'PROVIDER_QUOTA'}),/quota/);
  assert.match(voiceFailureMessage({outcome:'TIMEOUT'}),/timed out/);
  assert.doesNotMatch(voiceFailureMessage({outcome:'private-secret'}),/private-secret/);
  assert.equal(voiceFailureMessage(null),'');
});
test('widget preserves the reason when status is already terminal',async()=>{
  const helper='data:text/javascript;base64,'+Buffer.from(source).toString('base64');
  const widgetSource=(await readFile(new URL('../../src/agent_platform/console/voice-widget.js',import.meta.url),'utf8')).replace('./voice-errors.js',helper);
  const {mountVoiceWidget}=await import('data:text/javascript;base64,'+Buffer.from(widgetSource).toString('base64'));
  function element(){return{children:[],append(...nodes){this.children.push(...nodes);},replaceChildren(){this.children=[];},setAttribute(){}};}
  const saved={document:globalThis.document,window:globalThis.window,navigator:Object.getOwnPropertyDescriptor(globalThis,'navigator'),RTCPeerConnection:globalThis.RTCPeerConnection,setInterval,clearInterval,setTimeout,clearTimeout};
  let polling;
  globalThis.document={createElement:element,createTextNode:text=>({textContent:text})};
  globalThis.window={addEventListener(){},removeEventListener(){}};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[],getAudioTracks:()=>[]})}}});
  globalThis.setInterval=fn=>{polling=fn;return 1;};globalThis.clearInterval=()=>{};
  globalThis.setTimeout=()=>1;globalThis.clearTimeout=()=>{};
  globalThis.RTCPeerConnection=class{iceGatheringState='complete';addTrack(){}async createOffer(){return{};}async setLocalDescription(){this.localDescription={sdp:'fixture'};}async setRemoteDescription(){}close(){}};
  try{
    const root=element();const calls=[];
    const widget=mountVoiceWidget(root,{agentId:'agent',api:async path=>{
      calls.push(path);
      if(path.includes('/agents/'))return{id:'session',sdp:'fixture'};
      if(path.endsWith('/end'))throw new Error('fixture disconnect');
      return{state:'ENDED',last_failure:{outcome:'SPENDING_LIMIT',stage:'reasoning'}};
    }});
    root.children[1].children[0].checked=true;
    await root.children[3].onclick();await polling();
    assert.match(root.children[6].textContent,/spending limit/);
    assert.ok(calls.includes('voice/sessions/session/end'));
    assert.equal(calls.some(x=>x.includes('/events')),false);
    await widget.destroy();
  }finally{
    for(const key of ['document','window','RTCPeerConnection','setInterval','clearInterval','setTimeout','clearTimeout'])globalThis[key]=saved[key];
    if(saved.navigator)Object.defineProperty(globalThis,'navigator',saved.navigator);else delete globalThis.navigator;
  }
});
