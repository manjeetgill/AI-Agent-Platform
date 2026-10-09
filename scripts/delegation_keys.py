"""Generate an issuer's private key and a public platform keyring entry, never print secrets."""
import argparse
import base64
import json
import os
import time
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issuer',required=True)
    parser.add_argument('--tenant',required=True)
    parser.add_argument('--app-id',type=UUID,required=True)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--legacy-research-access',action='store_true',help='Only for the existing NRAIAlgo research application')
    parser.add_argument('--valid-days',type=int,default=90)
    args=parser.parse_args()
    if not 1<=args.valid_days<=365 or not args.issuer or not args.tenant:
        parser.error('Use a valid issuer, tenant, and 1–365 days')
    args.directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    key=Ed25519PrivateKey.generate()
    kid=uuid4().hex
    now=int(time.time())
    public={'issuer':args.issuer,'kid':kid,'tenant':args.tenant,'app_id':str(args.app_id),
            'public_key':base64.b64encode(key.public_key().public_bytes_raw()).decode(),
            'not_before':now-5,'not_after':now+args.valid_days*86400}
    if args.legacy_research_access:
        if args.issuer!='nraialgo' or args.tenant!='nraialgo':
            parser.error('Legacy research access is reserved for NRAIAlgo')
        public['legacy_research_access']=True
    private={**public,'private_key':base64.b64encode(key.private_bytes_raw()).decode()}
    for name,value in [('issuer-private.json',private),('platform-public.json',[public])]:
        fd=os.open(args.directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,indent=2)
    print('Created private issuer material and public keyring entry. Install only the public entry on the platform.')


if __name__=='__main__':
    main()
