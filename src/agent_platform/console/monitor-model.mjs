export const terminal = new Set(['DONE','FAILED','CANCELLED']);
export const labels = {DRAFT:'Draft',READY:'Queued',RUNNING:'Running',WAITING_APPROVAL:'Needs approval',WAITING_USER:'Needs you',PAUSED:'Paused',OUTCOME_UNKNOWN:'Outcome uncertain',DONE:'Completed',FAILED:'Failed',CANCELLED:'Cancelled'};
export function controls(status) {
  if(status==='PAUSED')return ['resume','cancel'];
  if(['OUTCOME_UNKNOWN','WAITING_USER'].includes(status))return ['reconcile','cancel'];
  if(['READY','RUNNING','WAITING_APPROVAL'].includes(status))return ['pause','cancel'];
  return [];
}
export function elapsed(task, now=Date.now()) {
  const end=terminal.has(task.status)?Date.parse(task.updated_at):now;
  const seconds=Math.max(0,Math.floor((end-Date.parse(task.created_at))/1000));
  if(!Number.isFinite(seconds))return 'Unknown';
  if(seconds<60)return seconds+'s';
  if(seconds<3600)return Math.floor(seconds/60)+'m '+seconds%60+'s';
  return Math.floor(seconds/3600)+'h '+Math.floor(seconds%3600/60)+'m';
}
export function reason(task) {
  const step=task.steps?.find(s=>s.position===task.current_step)??task.steps?.at(-1);
  if(step?.result?.reason_code)return step.result.reason_code;
  if(task.status==='OUTCOME_UNKNOWN')return 'Actual outcome must be checked before further execution.';
  if(task.status==='FAILED')return 'Execution failed. A more detailed reason may not have been recorded; inspect steps and activity.';
  if(task.status==='WAITING_APPROVAL')return 'Review and approve the exact action in the originating app.';
  if(task.status==='WAITING_USER')return 'Complete the user-only step, then check its actual outcome.';
  if(task.status==='PAUSED')return 'Task is paused; inspect activity and readiness before resuming.';
  if(task.status==='READY'&&step?.external_job_state==='RUNNING')return 'Waiting for an external job; the worker will check its saved reference.';
  return '';
}
export function safeLink(value) {
  try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password?u.href:null;}catch{return null;}
}
export function waitHint(task,now=Date.now()) {
  if(task.status==='RUNNING'&&task.lease_until&&Date.parse(task.lease_until)<now)return 'Worker lease expired; check readiness and allow recovery to verify the saved state.';
  if(task.status==='READY'&&task.next_attempt_at&&Date.parse(task.next_attempt_at)>now)return 'Next worker check: '+new Date(task.next_attempt_at).toLocaleString();
  if(task.status==='READY'&&now-Date.parse(task.updated_at)>60000)return 'Still queued; check worker readiness below.';
  return '';
}
export function stepUsage(steps=[]) {
  return steps.filter(s=>s.result&&(s.result.provider||s.result.model||s.result.tokens!=null)).map(s=>({
    step:s.position+1,provider:s.result.provider??'Not recorded',model:s.result.model??'Not recorded',tokens:s.result.tokens??null
  }));
}
