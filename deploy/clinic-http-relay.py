"""Pilot-only private relay: public Caddy -> Docker gateway -> SSH loopback tunnel."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  if self.path not in ('/integrations/agent-platform/whatsapp','/integrations/agent-platform/calling'): self.send_error(404); return
  try: n=int(self.headers.get('Content-Length','0'))
  except ValueError: self.send_error(400); return
  if not 0<n<=65536 or self.headers.get('Transfer-Encoding'): self.send_error(413); return
  if not self.headers.get('X-Agent-Signature') or not self.headers.get('X-Agent-Timestamp'): self.send_error(403); return
  raw=self.rfile.read(n)
  conn=http.client.HTTPConnection('127.0.0.1',44000,timeout=45)
  try:
   conn.request('POST',self.path,body=raw,headers={k:self.headers[k] for k in ('Content-Type','X-Agent-Signature','X-Agent-Timestamp') if self.headers.get(k)})
   r=conn.getresponse(); data=r.read(65537)
   if len(data)>65536: raise ValueError('response too large')
   self.send_response(r.status); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(data)
  except Exception: self.send_error(503,'Clinic test connection unavailable')
  finally: conn.close()
ThreadingHTTPServer(('172.24.0.1',44001),Handler).serve_forever()
