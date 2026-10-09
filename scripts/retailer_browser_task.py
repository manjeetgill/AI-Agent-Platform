"""Queue one read-only retailer observation, or inspect its saved worker result."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
from verify_dealagent_live import save


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--client-file',type=Path,required=True)
    p.add_argument('--state-file',type=Path,required=True)
    p.add_argument('--retailer',choices=['croma','jiomart','zepto','instamart'])
    p.add_argument('--url')
    p.add_argument('--paired-browser',action='store_true',help='Optional signed-in Chrome path; default runs on the platform server')
    p.add_argument('--expected-pincode')
    p.add_argument('--expected-variant')
    a=p.parse_args();c=json.loads(a.client_file.read_text());base=c['url'].rstrip('/')
    if base!='http://127.0.0.1:4060':raise SystemExit('This browser pilot uses the local API on port 4060')
    state=json.loads(a.state_file.read_text()) if a.state_file.exists() else None
    with httpx.Client(base_url=base,headers={'Authorization':'Bearer '+c['credential'],
            'X-External-User-ID':c['external_user']},timeout=10,trust_env=False,follow_redirects=False) as client:
        def request(method,path,body=None):
            r=client.request(method,path,json=body)
            if r.status_code>=400:raise SystemExit(f'Platform request failed ({r.status_code}); saved state retained')
            return r.json()
        if state and (state['app_id']!=c['app_id'] or state['external_user']!=c['external_user']):
            raise SystemExit('This task belongs to a different app or external user')
        if state is None:
            if not a.retailer or not a.url:p.error('--retailer and --url are required for a new check')
            arguments={'retailer':a.retailer,'url':a.url,'expected_pincode':a.expected_pincode,'expected_variant':a.expected_variant}
            state={'app_id':c['app_id'],'external_user':c['external_user'],'request_key':uuid4().hex,'arguments':arguments,'tool':'browser.retailer_observe' if a.paired_browser else 'retailer.observe'}
            save(a.state_file,state)
        if not state.get('agent_id'):
            arguments=state['arguments'];digest=hashlib.sha256(json.dumps({'tool':state.get('tool','browser.retailer_observe'),'arguments':arguments},sort_keys=True).encode()).hexdigest()
            agent=request('POST','/v1/task-agents',{'idempotency_key':digest,'blueprint':{
                'name':'Browser '+arguments['retailer']+' observation','description':'Read only product evidence with delivery and variant checks.',
                'steps':[{'tool':state.get('tool','browser.retailer_observe'),'version':'1','arguments':arguments}]}})
            state['agent_id']=agent['id'];save(a.state_file,state)
        if not state.get('task_id'):
            task=request('POST','/v1/task-agents/'+state['agent_id']+'/runs',{'idempotency_key':state['request_key']})
            state['task_id']=task['id'];save(a.state_file,state)
        task=request('GET','/v1/tasks/'+state['task_id'])
        state.update(status=task['status'],result=task['steps'][0].get('result'));save(a.state_file,state)
        print(json.dumps({'task_id':state['task_id'],'status':state['status'],'result':state['result']},indent=2))
        if state['status'] in {'READY','RUNNING'}:print('Waiting for the saved worker job. Run the same command/state file to inspect; no new task will be created.')


if __name__=='__main__':main()
