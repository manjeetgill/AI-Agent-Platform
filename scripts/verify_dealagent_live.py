"""Run one resumable worker verification per configured retailer; no LLM or purchases."""
import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

RETAILERS = ('croma','reliancedigital','vijaysales','jiomart','blinkit','zepto','instamart','bigbasket')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream,indent=2)
    tmp.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--retailer',choices=RETAILERS,action='append',
                        help='Check only these retailers; repeat to select more than one.')
    args=parser.parse_args()
    connection=json.loads(args.client_file.read_text())
    base=connection['url'].rstrip('/')
    url=urlsplit(base)
    if url.scheme!='https' and not (url.scheme=='http' and url.hostname in {'127.0.0.1','localhost'}):
        raise SystemExit('HTTPS or a loopback platform API is required')
    headers={'Authorization':'Bearer '+connection['credential'], 'X-External-User-ID':connection['external_user']}
    report=json.loads(args.output.read_text()) if args.output.exists() else {
        'run_id':uuid4().hex,'started_at':datetime.now(UTC).isoformat(),'app_id':connection['app_id'],'checks':[]}
    assert report['app_id']==connection['app_id']
    with httpx.Client(base_url=base,headers=headers,timeout=10,trust_env=False,follow_redirects=False) as client:
        def request(method,path,body=None):
            response=client.request(method,path,json=body)
            if response.status_code>=400:
                raise RuntimeError(f'Platform request failed ({response.status_code}) at {path}; saved state retained')
            return response.json()
        for retailer in dict.fromkeys(args.retailer or RETAILERS):
            check=next((r for r in report['checks'] if r['retailer']==retailer),None)
            if check is None:
                agent=request('POST','/v1/task-agents',{'idempotency_key':'live-check-v1-'+retailer,
                    'blueprint':{'name':'Live '+retailer+' verification','description':'One configured product URL with validated price evidence or exact access failure.',
                        'steps':[{'tool':'dealagent.live_probe','version':'1','arguments':{'retailer':retailer}}]}})
                check={'retailer':retailer,'agent_id':agent['id'],'request_key':report['run_id']+'-'+retailer}
                report['checks'].append(check);save(args.output,report)
            if not check.get('task_id'):
                states=request('GET','/v1/connectors')['states']
                state=next((s for s in states if s['tool']=='dealagent.live_probe' and s['source']==retailer),None)
                if state and not state['needs_attention']:
                    delay=(datetime.fromisoformat(state['next_check_at'])-datetime.now(UTC)).total_seconds()
                    if delay>300:raise RuntimeError('Long provider cooldown remains; retry later with the same output file')
                    if delay>0:time.sleep(delay+0.1)
                task=request('POST','/v1/task-agents/'+check['agent_id']+'/runs',{'idempotency_key':check['request_key']})
                check['task_id']=task['id'];save(args.output,report)
            deadline=time.monotonic()+60
            while True:
                task=request('GET','/v1/tasks/'+check['task_id'])
                if task['status'] in {'DONE','FAILED','CANCELLED','OUTCOME_UNKNOWN','WAITING_FOR_USER'}:break
                if time.monotonic()>deadline:raise RuntimeError('Worker still pending; rerun this command to resume the same task')
                time.sleep(0.5)
            check.update(task_status=task['status'],result=task['steps'][0].get('result'))
            save(args.output,report)
            result=check['result'] or {}
            print(retailer,check['task_id'],task['status'],result.get('status'),result.get('reason_code'),flush=True)
            if task['status']!='DONE':raise RuntimeError('Task did not complete; investigate before proceeding')
    report['completed_at']=datetime.now(UTC).isoformat();save(args.output,report)


if __name__=='__main__':main()
