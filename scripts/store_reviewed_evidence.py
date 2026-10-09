"""Operator-only: validate and pin a reviewed browser observation for an app/owner.

Does not acquire data, open a browser, reset source blocks, or accept credentials.
The input is an Observation JSON, transcribed from a browser check by an operator.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from agent_platform.connector_health import Observation
from agent_platform.reviewed_evidence import evidence_path


def store(root, app_id, owner_id, observation):
    value=Observation.model_validate(observation,strict=True)
    record={'schema_version':1,'acquisition':'operator_reviewed_browser',
            'app_id':str(UUID(str(app_id))),'owner_id':str(UUID(str(owner_id))),
            'observation':value.model_dump()}
    data=json.dumps(record,sort_keys=True,ensure_ascii=False).encode()
    if len(data)>12000:
        raise ValueError('Evidence is too large')
    evidence_id=hashlib.sha256(data).hexdigest()
    root=Path(root).resolve()
    path=evidence_path(root,app_id,owner_id,evidence_id)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.resolve()!=path.absolute():
        raise ValueError('Evidence paths must not contain symlinks')
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        if path.read_bytes()!=data:
            raise ValueError('Existing evidence content mismatch') from None
    else:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data)
    return evidence_id


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--app-id',required=True,type=UUID)
    parser.add_argument('--owner-id',required=True,type=UUID)
    parser.add_argument('--input',required=True,type=Path)
    args=parser.parse_args()
    print(store(args.root,args.app_id,args.owner_id,json.loads(args.input.read_text())))
