export function validCommand(c, now=Date.now()) {
  return c && c.operation==='wikipedia.prepare' && typeof c.query==='string' && c.query.length>0 && c.query.length<=150 &&
    !/[\x00-\x1f]/.test(c.query) && /^[a-f0-9-]{36}$/.test(c.id) && Date.parse(c.expires_at)>now;
}
export function fillWikipedia(query, expiry) {
  // Executes in Chrome's isolated world. It reads no page text, account data or secret values.
  if(globalThis.__workspaceStopped !== false || Date.now()>=expiry || location.origin!=='https://www.wikipedia.org' || location.pathname!=='/') return false;
  if(document.querySelector('input[type=password],input[autocomplete=one-time-code],input[autocomplete^=cc-]')) return false;
  const field=document.querySelector('#searchInput');
  if(!field || field.tagName!=='INPUT' || !['search','text'].includes(field.type) || field.disabled || field.readOnly) return false;
  if(typeof query!=='string' || query.length>150) return false;
  // No click, submit, keyboard Enter, screenshot, or reading of existing field values.
  field.value=query;
  field.dispatchEvent(new Event('input',{bubbles:true}));
  return field.value===query;
}
