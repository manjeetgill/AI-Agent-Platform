import {voiceFailureMessage} from './voice-errors.js';
/** Embed in a first-party app page. api() must use that app's authenticated backend proxy. */
export function mountVoiceWidget(root, {api, agentId, callerPhone, iceServers = [], beforeStart, consentText = 'I agree to microphone audio being processed by the AI speech service for this call.'}) {
  root.replaceChildren();
  const notice=document.createElement('p'); notice.textContent='Speak with an AI assistant. Calls last up to two minutes. This platform does not record audio; audio is processed by the configured speech service.';
  if(callerPhone)notice.textContent+=' The business also receives the contact number you supplied for this call; it is not identity verification.';
  const label=document.createElement('label'), consent=document.createElement('input'); consent.type='checkbox'; label.append(consent, document.createTextNode(consentText));
  const start=document.createElement('button'), stop=document.createElement('button'), status=document.createElement('p'), audio=document.createElement('audio');
  start.textContent='Talk to AI'; stop.textContent='End call'; stop.disabled=true; status.setAttribute('role','status'); audio.autoplay=true; audio.controls=true;
  const transcriptLabel=document.createElement('label'), transcriptConsent=document.createElement('input'); transcriptConsent.type='checkbox'; transcriptLabel.append(transcriptConsent,document.createTextNode('Save my call transcript for up to 24 hours (optional).'));
  const mute=document.createElement('button');mute.textContent='Mute microphone';mute.disabled=true;
  root.append(notice,label,transcriptLabel,start,stop,mute,status,audio);
  mute.onclick=()=>{if(!stream)return;const enabled=!stream.getAudioTracks()[0]?.enabled;for(const t of stream.getAudioTracks())t.enabled=enabled;mute.textContent=enabled?'Mute microphone':'Unmute microphone';};
  let pc=null,stream=null,session=null,timer=null,poll=null,starting=false,epoch=0,disposed=false,after=0,polling=false,lastFailure='';
  const captions=document.createElement('p');captions.setAttribute('aria-live','polite');root.append(captions);
  function closeLocal() { clearTimeout(timer);clearInterval(poll);timer=poll=null; if(stream)stream.getTracks().forEach(t=>t.stop());stream=null;if(pc)pc.close();pc=null; audio.srcObject=null;stop.disabled=true;mute.disabled=true;mute.textContent='Mute microphone';start.disabled=disposed;starting=false; }
  async function end(message='Call ended') {const endedEpoch=++epoch;const id=session;session=null;closeLocal();status.textContent=lastFailure||message;if(id){try{await api(`voice/sessions/${id}/end`,'POST',{});}catch{if(epoch===endedEpoch)status.textContent=(lastFailure||message)+'. Audio is closed locally; the server session expires automatically.';}}}
  stop.onclick=()=>end();
  start.onclick=async()=>{
    if(starting||pc)return;
    if(!consent.checked){status.textContent='Please review and accept audio processing before starting.';return;}
    const id=typeof agentId==='function'?agentId():agentId;
    if(!id){status.textContent='Choose an agent first.';return;}
    if(!navigator.mediaDevices?.getUserMedia){status.textContent='Open this page using HTTPS or localhost in a browser with microphone support.';return;}
    lastFailure='';starting=true;start.disabled=true;stop.disabled=false;const current=++epoch;status.textContent='Allow microphone access to start.';
    try {
      if(beforeStart){status.textContent='Checking agent readiness…';await beforeStart(id);if(current!==epoch)return;}
      status.textContent='Allow microphone access to start.';
      const captured=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
      if(current!==epoch){captured.getTracks().forEach(t=>t.stop());return;}
      stream=captured;mute.disabled=false;pc=new RTCPeerConnection({iceServers});const connection=pc;
      for(const track of stream.getTracks())pc.addTrack(track,stream);
      pc.ontrack=e=>{audio.srcObject=e.streams[0]||new MediaStream([e.track]);audio.play().catch(()=>{status.textContent='Press Play on the audio control to hear the agent.';});};
      pc.onconnectionstatechange=()=>{if(current!==epoch)return;if(connection.connectionState==='connected')status.textContent='Connected. You may interrupt the assistant while it speaks.';if(['failed','closed'].includes(connection.connectionState)){const active=session;(async()=>{try{if(active){const r=await api(`voice/sessions/${active}`);if(current!==epoch)return;lastFailure=voiceFailureMessage(r.last_failure);}}catch{}if(current===epoch)end('Audio connection ended');})();}};
      await pc.setLocalDescription(await pc.createOffer());
      await new Promise((resolve,reject)=>{if(connection.iceGatheringState==='complete'){resolve();return;}const timeout=setTimeout(()=>reject(new Error('Network setup timed out')),12000);connection.addEventListener('icegatheringstatechange',()=>{if(connection.iceGatheringState==='complete'){clearTimeout(timeout);resolve();}});});
      if(current!==epoch)return;
      status.textContent='Connecting to your AI assistant…';
      const result=await api(`voice/agents/${id}/sessions`,'POST',{sdp:connection.localDescription.sdp,request_key:crypto.randomUUID(),audio_consent:true,...(callerPhone?{caller_phone:typeof callerPhone==='function'?callerPhone():callerPhone}:{}),transcript_consent:transcriptConsent.checked});
      if(current!==epoch){await api(`voice/sessions/${result.id}/end`,'POST',{});return;}
      session=result.id;after=0;captions.textContent='';await connection.setRemoteDescription({type:'answer',sdp:result.sdp});
      timer=setTimeout(()=>end('Two-minute session limit reached'),120000);
      poll=setInterval(async()=>{if(!session||polling)return;polling=true;const active=session;try{const r=await api(`voice/sessions/${active}`);if(active!==session)return;lastFailure=voiceFailureMessage(r.last_failure);if(lastFailure)status.textContent=lastFailure;if(['ENDED','FAILED','UNKNOWN'].includes(r.state)){end(r.state==='FAILED'?'Call failed. Check the call history.':r.state==='UNKNOWN'?'The connection was lost. Check any pending action before starting again.':'Call ended');return;}const timeline=await api(`voice/sessions/${active}/events?after=${after}`);if(active!==session)return;after=timeline.next_after;for(const e of timeline.events){if(e.content)captions.textContent=(e.kind==='asr_partial'?'Provisional: ':e.role+': ')+e.content;}}catch{/* Bounded local deadline still ends the call. */}finally{polling=false;}},2000);
    }catch(error){if(current===epoch)await end(error.name==='NotAllowedError'?'Microphone access was not allowed.':(error.message||'Unable to connect audio'));}
    finally{if(current===epoch)starting=false;}
  };
  const unload=()=>{epoch++;closeLocal();};window.addEventListener('pagehide',unload);
  return {end,destroy(){disposed=true;window.removeEventListener('pagehide',unload);return end();}};
}
