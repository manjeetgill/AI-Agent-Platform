/** Backend only: node --experimental-strip-types capabilities.ts /private/client.json */
import {readFile} from 'node:fs/promises';
const config=JSON.parse(await readFile(process.argv[2],'utf8'));
const response=await fetch(new URL('/v1/integrations/capability-readiness',config.url),{
  redirect:'error',signal:AbortSignal.timeout(15000),
  // In production obtain this ID from the app's authenticated user session.
  headers:{Authorization:`Bearer ${config.credential}`,'X-External-User-ID':config.external_user}
});
if(!response.ok)throw Error(`Capability check unavailable (HTTP ${response.status}); check the backend connection.`);
const report=await response.json();
const states=new Map(report.capabilities.map((item:any)=>[item.id,item.status]));
console.log(report.integration_contracts.map((item:any)=>({
  capability:item.id,api_root:item.api_root,missing_scopes:item.missing_scopes,
  status:!item.permissions_granted?'ACCESS_REQUIRED':states.get(item.readiness_id)??'CONFIGURATION_CHECK_REQUIRED',
  next_step:item.setup_requirement
})));
