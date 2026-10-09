"""Explicit local synthetic pilot. No messages, telephone calls or business writes.

--live-model uses the configured provider quota for one plan and one cited answer.
The comparison task is executed by the real worker. Provider errors are redacted.
"""
import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model',action='store_true')
    parser.add_argument('--client-file',type=Path,help='Private 0600 service client file for an isolated verification app')
    parser.add_argument('--output',type=Path,default=Path('docs/verification/capability-pilot.json'))
    args=parser.parse_args()
    report={'checked_at':datetime.now(UTC).isoformat(),'checks':{},'external_messages_sent':False,
            'telephone_calls_placed':False,'human_microphone_verified':False}
    document=None
    url='http://127.0.0.1:4061'; prefix='/console/api/'; headers={'X-Console-Intent':'user-action'}
    if args.client_file:
        path=args.client_file
        if path.is_symlink() or path.stat().st_uid!=os.getuid() or path.stat().st_mode & 0o077:
            raise SystemExit('Use a private owner-only 0600 client file.')
        cfg=json.loads(path.read_text())
        if cfg['url'].rstrip('/') not in {'http://127.0.0.1:4060','http://localhost:4060'}:
            raise SystemExit('Only the local platform API is supported.')
        url=cfg['url']; prefix='/v1/'
        headers={'Authorization':'Bearer '+cfg['credential'],'X-External-User-ID':cfg['external_user']}
        report['app_id']=cfg['app_id']
    with httpx.Client(base_url=url,timeout=55,trust_env=False,headers=headers) as client:
        def request(path,method='GET',body=None):
            r=client.request(method,prefix+path,json=body)
            if r.status_code>=400:
                # Never include arbitrary upstream response bodies in the evidence file.
                raise RuntimeError('HTTP '+str(r.status_code))
            return None if r.status_code==204 else r.json()
        try:
            setup=request('integrations/capability-readiness')
            report['checks']['setup']={x['id']:x['status'] for x in setup['capabilities']}
            marker='pilot-'+uuid4().hex
            document=request('knowledge/documents','POST',{'external_id':marker,'title':'Synthetic readiness pilot',
                'source_label':'Synthetic verification fixture','visibility':'private',
                'text':'The fictional Amberdesk support office closes at 18:00 every weekday. This is synthetic test information.'})
            passages=request('knowledge/search','POST',{'question':'When does the Amberdesk support office close?'})
            found=any(p['document_id']==document['id'] and '18:00' in p['text'] for p in passages['passages'])
            report['checks']['retrieval']={'passed':found,'document_id':document['id']}
            if args.live_model:
                for kind in ('planning','knowledge_answer'):
                    try:
                        if kind=='planning':
                            plan=request('task-agents/interpret','POST',{'request':
                                'Use only decision.compare. Compare two fictional options A and B using one criterion, cost, with weight 1. A has score 8 and B has score 6. Higher scores are better. These are synthetic test inputs.'})
                            if not plan['ready'] or not plan['blueprint']['steps'] or any(
                                    s['tool']!='decision.compare' for s in plan['blueprint']['steps']):
                                raise RuntimeError('No permitted comparison plan produced')
                            agent=request('task-agents','POST',{'blueprint':plan['blueprint'],'idempotency_key':marker})
                            run=request('task-agents/'+agent['id']+'/runs','POST',{'idempotency_key':marker})
                            task_id=run.get('task_id') or run.get('id')
                            if not task_id:
                                raise RuntimeError('Task identifier missing')
                            deadline=time.monotonic()+25
                            while True:
                                task=request('tasks/'+task_id)
                                if task['status'] in {'DONE','FAILED','CANCELLED','OUTCOME_UNKNOWN','WAITING_APPROVAL'} or time.monotonic()>deadline:
                                    break
                                time.sleep(1)
                            expected=[{'name':'A','score':8.0},{'name':'B','score':6.0}]
                            correct=bool(task['steps']) and all((s.get('result') or {}).get('ranking')==expected for s in task['steps'])
                            report['checks'][kind]={'passed':task['status']=='DONE' and correct,'task_id':task_id,
                                'status':task['status'],'usage':plan['usage'],
                                'results':[s['result'] for s in task['steps'] if s.get('result')]}
                        else:
                            answer=request('knowledge/ask','POST',{'question':'When does the fictional Amberdesk support office close?',
                                'idempotency_key':marker})
                            cited=any(c.get('document_id')==document['id'] for c in answer['citations'])
                            correct=any('18:00' in c['text'] or '6:00' in c['text'] or '6 PM' in c['text'] for c in answer['claims'])
                            report['checks'][kind]={'passed':cited and correct,'answer_id':answer.get('answer_id'),
                                'cited_fixture':cited,'expected_time_found':correct,'provider':answer['provider'],'model':answer['model']}
                    except (RuntimeError,httpx.HTTPError,KeyError,TypeError,ValueError) as exc:
                        report['checks'][kind]={'passed':False,'error':str(exc) if type(exc) is RuntimeError else type(exc).__name__}
        except (RuntimeError,httpx.HTTPError,KeyError,TypeError,ValueError) as exc:
            report['checks']['pilot']={'passed':False,'error':str(exc) if type(exc) is RuntimeError else type(exc).__name__}
        finally:
            if document:
                try:
                    request('knowledge/documents/'+document['id'],'DELETE')
                    report['synthetic_document_removed']=True
                except (RuntimeError,httpx.HTTPError):
                    report['synthetic_document_removed']=False
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    return 2 if any(isinstance(v,dict) and v.get('passed') is False for v in report['checks'].values()) else 0


if __name__=='__main__':
    raise SystemExit(main())
