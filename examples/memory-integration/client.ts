/** Backend only. Never ship a service credential to your app's browser bundle. */
export class AgentMemoryClient {
  constructor(private base: string, private credential: string, private externalUser: string) {
    const u = new URL(base);
    if (u.protocol !== 'https:' && !(u.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(u.hostname))) throw Error('HTTPS required');
  }
  async request(path: string, method = 'GET', body?: unknown) {
    const response = await fetch(`${this.base.replace(/\/$/, '')}/v1/memory/${path}`, {
      method, redirect: 'error', signal: AbortSignal.timeout(60000),
      headers: {'Authorization': `Bearer ${this.credential}`, 'X-External-User-ID': this.externalUser, 'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    if (!response.ok) throw Error(`Agent memory request failed (${response.status})`);
    return response.status === 204 ? undefined : response.json();
  }
  search(question: string) { return this.request(`search?q=${encodeURIComponent(question)}`); }
  metrics() { return this.request('metrics'); }
  checkpoint(taskId: string) { return this.request(`tasks/${encodeURIComponent(taskId)}/checkpoint`); }
}
