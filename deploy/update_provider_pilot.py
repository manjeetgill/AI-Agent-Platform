"""Operator rollout: stage agent_platform/ and private provider.env beside this file.

Backs up the pilot database/config, builds without credentials, migrates, then
restarts only API and WhatsApp worker. Active speech selection is preserved.
"""
import fcntl,json,os,shutil,subprocess,time,urllib.request
from pathlib import Path
root=Path('/opt/agent-platform/pilot');stage=Path(__file__).resolve().parent
secrets=Path('/opt/agent-platform/secrets');env=root/'.env';overlay=root/'deploy/compose.internet-voice.json'
def run(*args):
 r=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 if r.returncode: raise RuntimeError('Deployment command failed: '+args[0]+' '+args[1]+' (output withheld to protect configuration)')
 if args[0]=='docker' and args[1]=='build':print('Image build passed',flush=True)
def ready():
 try:
  with urllib.request.urlopen('http://127.0.0.1:4060/readiness',timeout=4) as r:return r.status==200
 except Exception:return False
with Path('/opt/myalgodesk/release.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime());backup=secrets/('provider-rollout-'+stamp);backup.mkdir(mode=0o700)
 shutil.copy2(env,backup/'env');shutil.copy2(overlay,backup/'overlay.json')
 with (backup/'database.dump').open('wb') as f:
  os.chmod(f.name,0o600);subprocess.run(['docker','exec','agent-platform-db-1','pg_dump','-U','postgres','-d','agent_platform','-Fc'],stdout=f,check=True)
 old=subprocess.check_output(['docker','inspect','agent-platform-api-1','--format','{{.Config.Image}}'],text=True).strip()
 image='agent-platform:providers-'+stamp.lower()
 (stage/'Dockerfile').write_text(f'FROM {old}\nCOPY agent_platform/ /usr/local/lib/python3.14/site-packages/agent_platform/\n')
 # Keep credentials out of the Docker build context.
 build=stage/'build';build.mkdir(exist_ok=True)
 shutil.copy2(stage/'Dockerfile',build/'Dockerfile');shutil.copytree(stage/'agent_platform',build/'agent_platform',dirs_exist_ok=True)
 for path in (build/'agent_platform').rglob('*'):path.chmod(0o755 if path.is_dir() else 0o644)
 (build/'agent_platform').chmod(0o755)
 run('docker','build','--network=none','-t',image,str(build))
 run('docker','run','--rm','--network=none',image,'python','-c','from agent_platform import app, sarvam_speech, chakra_onboarding, channel_trials; print("Image imports passed")')
 incoming={}
 for line in (stage/'provider.env').read_text().splitlines():
  if '=' in line and not line.lstrip().startswith('#'):
   k,v=line.split('=',1)
   if k.strip() in ('AI_CHAKRA_PARTNER_TOKEN','AI_SARVAM_API_KEY'):incoming[k.strip()]=v.strip().strip('\"\'')
 if set(incoming)!={'AI_CHAKRA_PARTNER_TOKEN','AI_SARVAM_API_KEY'} or not all(incoming.values()):raise RuntimeError('Missing provider keys')
 lines=env.read_text().splitlines();current={}
 for line in lines:
  if '=' in line and not line.startswith('#'):
   k,v=line.split('=',1);current[k]=v.strip().strip('\"\'')
 chakra=[v for v in json.loads(current.get('AI_WHATSAPP_CONNECTIONS','{}')).values() if v.get('provider')=='chakra']
 if len(chakra)!=1 or len(chakra[0].get('webhook_secret',''))<32:raise RuntimeError('Existing webhook configuration needs operator review')
 incoming.update(AI_CHAKRA_WEBHOOK_SECRET=chakra[0]['webhook_secret'],AI_CHAKRA_GRAPH_VERSION=chakra[0]['graph_version'],AI_PLATFORM_PUBLIC_URL='https://agents.myalgodesk.in')
 # Preserve the active speech selection until bounded call admission is configured.
 newlines=[line for line in lines if line.split('=',1)[0] not in incoming]+[k+'='+v for k,v in incoming.items()]
 candidate=backup/'candidate.env';candidate.write_text('\n'.join(newlines)+'\n');candidate.chmod(0o600)
 compose=['docker','compose','--project-directory',str(root),'-p','agent-platform','-f',str(root/'compose.yaml'),'-f',str(root/'deploy/compose.small.yaml'),'-f',str(overlay)]
 services=['api','whatsapp-worker']
 try:
  docker_env=backup/'docker.env';docker_env.write_text('\n'.join(k+'='+v for k,v in {**current,**incoming}.items())+'\n');docker_env.chmod(0o600)
  run('docker','run','--rm','--network','agent-platform_default','--env-file',str(docker_env),image,'python','-m','agent_platform.migrate')
  data=json.loads(overlay.read_text())
  for name in services+['migrate']:data['services'].setdefault(name,{})['image']=image
  env.write_bytes(candidate.read_bytes());env.chmod(0o600);overlay.write_text(json.dumps(data,indent=2)+'\n')
  run(*compose,'up','-d','--no-deps','--no-build','--pull','never',*services)
  for _ in range(20):
   if ready():break
   time.sleep(2)
  else:raise RuntimeError('API readiness failed')
  run('docker','exec','agent-platform-api-1','python','-c','from agent_platform.chakra_onboarding import settings; from agent_platform.sarvam_speech import key; settings(); key(); print("Provider runtime configuration ready")')
  print(json.dumps({'deployed':True,'image':image,'backup':str(backup),'speech_provider_unchanged':True}))
 except Exception:
  env.write_bytes((backup/'env').read_bytes());overlay.write_bytes((backup/'overlay.json').read_bytes())
  run(*compose,'up','-d','--no-deps','--no-build','--pull','never',*services)
  raise
 finally:
  (stage/'provider.env').unlink(missing_ok=True)
