const $=id=>document.getElementById(id);
let requestId=crypto.randomUUID(),savedBody=null,testId=null,taskId=null,busy=false,setupComplete=false,generation=0;
const limits={planning_calls_per_day:['Planning calls per app / day',0,100000],user_planning_calls_per_day:['Planning calls per user / day',0,10000],max_plan_steps:['Maximum plan steps',1,30],automation_runs_per_day:['Scheduled runs per app / day',0,10000],connector_checks_per_hour:['Source checks per app / hour',0,10000],feed_requests_per_hour:['Feed requests per app / hour',0,10000]};
function el(tag,text,parent){const n=document.createElement(tag);if(text!=null)n.textContent=text;parent?.append(n);return n;}
function fail(error){$('error').textContent=error.message;$('error').hidden=false;}
async function api(path,body){const response=await fetch(path,{method:body?'POST':'GET',headers:{'X-Monitor-Intent':'user-action',...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(15000)});let data;try{data=await response.json();}catch{throw Error('Platform returned an unreadable response. Check its local services.');}if(!response.ok){const error=Error(typeof data.detail==='string'?data.detail:'Invalid setup settings; review the selected fields.');error.status=response.status;throw error;}return data;}
function json(parent,data){el('pre',JSON.stringify(data,null,2),parent);}
function choice(parent,value,text,checked){const label=el('label',null,parent);const input=el('input',null,label);input.type='checkbox';input.value=value;input.checked=checked;el('span',text,label);}
function chosen(id){return [...$(id).querySelectorAll('input:checked')].map(n=>n.value);}
async function inventory(select){const r=await api('/monitor/connections');$('connection').replaceChildren();for(const c of r.connections){const option=el('option',c.name,$('connection'));option.value=c.id;}if(select)$('connection').value=select;}
async function run(fn){if(busy)return;busy=true;$('error').hidden=true;for(const id of ['save','check','test','result','connection','load-services'])$(id).disabled=true;try{await fn();}catch(e){fail(e);}finally{busy=false;$('save').disabled=setupComplete;$('check').disabled=false;$('connection').disabled=false;$('load-services').disabled=false;$('result').disabled=!taskId;}}
async function check(){const connection=$('connection').value;if(!connection)throw Error('Create or select a connection first.');const epoch=generation;const r=await api('/monitor/setup/'+connection+'/check?planning='+$('planning').checked);if(epoch!==generation)return;const root=$('checks');root.replaceChildren();el('h3',r.status,root);for(const c of r.checks){const p=el('div',null,root);p.className='check';el('strong',c.status+' · '+c.summary,p);if(c.next_action)el('p',c.next_action,p);}el('h3','Callbacks: '+r.callbacks.status,root);el('p',r.callbacks.next_action,root);el('p',r.callbacks.deliveries.length+' recent callback delivery records; this check does not verify live receipt.',root);if(r.callbacks.deliveries.length)json(root,r.callbacks.deliveries);$('test').disabled=!r.worker_test_available;if(!r.worker_test_available)el('p','Worker test unavailable: the operator must grant platform.connection_test and tasks:read, tasks:write, agents:run. Existing app permissions are preserved.',root);}
$('setup-form').onsubmit=e=>{e.preventDefault();run(async()=>{const body=savedBody?JSON.parse(savedBody):{request_id:requestId,name:$('name').value.trim(),external_user:$('user').value.trim(),days:Number($('days').value),scopes:chosen('scopes'),tools:chosen('tools'),policy:Object.fromEntries(Object.keys(limits).map(k=>[k,Number($('limit-'+k).value)]))};const payload=JSON.stringify(body);if(savedBody&&savedBody!==payload)throw Error('Retry the original settings after an uncertain save; reload to begin a different application.');savedBody=payload;let r;try{r=await api('/monitor/setup/apps',body);}catch(error){$('save').textContent='Retry same connection setup';if(error.status===422)savedBody=null;throw error;}const root=$('created');root.replaceChildren();el('h3','Application connection saved',root);el('p','Private backend connection file:',root);el('code',r.credential_file,root);el('p','App ID: '+r.app_id,root);el('p','Permissions: '+r.scopes.join(', '),root);el('p','Tools: '+r.tools.join(', '),root);setupComplete=true;$('save').textContent='Connection created';await inventory(r.connection);generation++;await check();});};
$('check').onclick=()=>run(check);
$('connection').onchange=()=>{generation++;testId=null;taskId=null;$('test').disabled=true;$('result').disabled=true;$('checks').replaceChildren();$('test-result').replaceChildren();$('service-choices').replaceChildren();};
$('test').onclick=()=>run(async()=>{testId??=crypto.randomUUID();const r=await api('/monitor/setup/'+$('connection').value+'/test',{test_id:testId});taskId=r.task_id;$('test-result').replaceChildren();el('p','Worker test '+r.status+'. Task: '+taskId,$('test-result'));el('p','Click Refresh test result to check execution. Repeating this request returns the same test.',$('test-result'));});
$('result').onclick=()=>run(async()=>{const r=await api('/monitor/api/'+$('connection').value+'/tasks/'+taskId);const root=$('test-result');root.replaceChildren();el('h3','Worker test: '+r.status,root);json(root,{task_id:r.id,status:r.status,results:r.steps.map(s=>({status:s.status,result:s.result}))});if(r.status==='DONE')el('p','Worker round trip passed. Test your app’s real tools next.',root);else el('p','If this remains queued, check worker readiness. Failed or uncertain tasks are not retried automatically here.',root);});
try{const c=await api('/monitor/setup/catalog');for(const t of c.tools)choice($('tools'),t.name,t.name+' · '+t.risk+' · '+t.description,t.name==='platform.connection_test');for(const s of c.scopes)choice($('scopes'),s,s,c.default_scopes.includes(s));for(const [key,[label,min,max]]of Object.entries(limits)){const l=el('label',label,$('limits'));const input=el('input',null,l);input.id='limit-'+key;input.type='number';input.min=min;input.max=max;input.required=true;input.value=c.default_policy[key];}await inventory();}catch(e){fail(e);$('save').disabled=true;}

async function loadServiceChoices(){
  const connection=$('connection').value, epoch=generation;
  if(!connection)throw Error('Select an application connection first.');
  const result=await api('/monitor/setup/'+connection+'/services');
  if(epoch!==generation||connection!==$('connection').value)return;
  const root=$('service-choices');root.replaceChildren();
  el('p',result.note,root);
  for(const service of result.services){
    const card=el('fieldset',null,root);el('legend',service.title,card);
    el('p','Use our service — '+service.ours,card);
    el('p','Use a provider-backed service — '+service.extra,card);
    const label=el('label','Service option',card), select=el('select',null,label);
    select.add(new Option('Choose an option',''));
    for(const route of ['platform','provider'])for(const provider of service[route]){
      select.add(new Option(result.labels[route]+' — '+provider,route+':'+provider));
    }
    if(service.choice)select.value=service.choice.route+':'+service.choice.provider;
    el('p',service.setup_status+' · Price to be confirmed · This preference has not activated a route.',card);
    const save=el('button','Request this service option',card);save.type='button';
    save.onclick=()=>run(async()=>{
      if(connection!==$('connection').value)throw Error('Application changed; reload service choices.');
      if(!select.value)throw Error('Choose a service option.');
      const [route,provider]=select.value.split(':');
      await api('/monitor/setup/'+connection+'/services',{capability:service.capability,route,provider,expected_revision:service.choice?.revision||0});
      await loadServiceChoices();
    });
  }
}
$('load-services').onclick=()=>run(loadServiceChoices);
