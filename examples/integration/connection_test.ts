/** Backend only. Node with TypeScript support: node --experimental-strip-types connection_test.ts /private/client.json [test-uuid] */
import {readFile} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
const config=JSON.parse(await readFile(process.argv[2],'utf8'));
const testId=process.argv[3]??randomUUID();
// Derive external_user from the app's authenticated session in a real integration.
async function api(path:string,body?:unknown){
 const response=await fetch(new URL(path,config.url),{method:body?'POST':'GET',redirect:'error',
  headers:{Authorization:`Bearer ${config.credential}`,'X-External-User-ID':config.external_user,'Content-Type':'application/json'},
  signal:AbortSignal.timeout(15000),...(body?{body:JSON.stringify(body)}:{})});
 if(!response.ok)throw Error(`Platform request failed (${response.status}); inspect readiness. Credentials are not logged.`);
 return await response.json();
}
console.log('Test ID (reuse after a timeout):',testId);
const readiness=await api('/v1/integrations/task-readiness?planning=false&required_tool=platform.connection_test');
if(!readiness.ready){console.log(readiness.checks);process.exitCode=2;}
else{
 const run=await api('/v1/integrations/connection-test',{test_id:testId});
 if(!/^[a-f0-9-]{36}$/.test(run.task_id))throw Error('Invalid task ID');
 console.log('Task ID:',run.task_id);
 const deadline=Date.now()+30000;let finished=false;
 while(Date.now()<deadline){
  const task=await api('/v1/tasks/'+run.task_id);
  if(['DONE','FAILED','CANCELLED','OUTCOME_UNKNOWN','WAITING_USER','WAITING_APPROVAL','PAUSED'].includes(task.status)){
   console.log({status:task.status,steps:task.steps});process.exitCode=task.status==='DONE'?0:2;finished=true;break;
  }
  await new Promise(resolve=>setTimeout(resolve,1000));
 }
 if(!finished){console.log('Still pending. Inspect the saved task; no duplicate submitted.');process.exitCode=2;}
}
