"""Create a scoped 24-hour pairing file without putting app credentials in Chrome."""
import argparse
import json
from pathlib import Path

import httpx
from verify_dealagent_live import save


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-file',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--retailer',choices=['croma','jiomart','zepto','instamart'],action='append',required=True)
    args=parser.parse_args()
    connection=json.loads(args.client_file.read_text())
    base=connection['url'].rstrip('/')
    if base!='http://127.0.0.1:4060':raise SystemExit('This pilot pairs only with the local API on port 4060')
    if args.output.exists():raise SystemExit('Choose a new private output filename; existing pairing retained')
    with httpx.Client(base_url=base,timeout=10,trust_env=False,follow_redirects=False) as client:
        response=client.post('/v1/retailer-browser/devices',headers={
            'Authorization':'Bearer '+connection['credential'],'X-External-User-ID':connection['external_user']},
            json={'retailers':sorted(set(args.retailer))})
        if response.status_code!=201:raise SystemExit(f'Pairing failed ({response.status_code}); inspect app grants and feature flags')
        result=response.json()
    save(args.output,{'api_base':base,'pairing_code':result['pairing_code'],'retailers':result['retailers']})
    print('Private pairing file saved:',args.output)
    print('Expires in 24 hours. Import it in the Browser Runner extension; approve only the listed retailer sites.')


if __name__=='__main__':main()
