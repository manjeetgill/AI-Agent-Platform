// Fixed product routes. No caller-supplied selectors or executable page instructions.
export const retailerOrigins={croma:'https://www.croma.com/*',jiomart:'https://www.jiomart.com/*',zepto:'https://www.zepto.com/*',instamart:'https://instamart.in/*'};
export function validRetailerCommand(c,now=Date.now()) {
  const paths={croma:/^\/[^/]+\/p\/\d+\/?$/,jiomart:/^\/product\/[a-z0-9-]+-\d+\/?$/,zepto:/^\/pn\/[a-z0-9-]+\/pvid\/[0-9a-f-]{36}\/?$/,instamart:/^\/p\/[a-zA-Z0-9-]+\/?$/};
  try {const u=new URL(c.url);return c.operation==='retailer.observe'&&/^[0-9a-f-]{36}$/.test(c.id)&&
    Date.parse(c.expires_at)>now&&Date.parse(c.expires_at)<=now+310000&&
    retailerOrigins[c.retailer]===u.origin+'/*'&&!u.username&&!u.password&&!u.search&&!u.hash&&paths[c.retailer].test(u.pathname)&&
    (!c.expected_pincode||/^[1-9]\d{5}$/.test(c.expected_pincode))&&(!c.expected_variant||(typeof c.expected_variant==='string'&&c.expected_variant.length<=160));
  } catch{return false;}
}
export function validPairing(p){return p&&p.api_base==='http://127.0.0.1:4060'&&/^rbr_[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}$/.test(p.pairing_code)&&Array.isArray(p.retailers)&&p.retailers.length>0&&p.retailers.length<=4&&p.retailers.every(x=>Object.hasOwn(retailerOrigins,x));}

// Self-contained for isolated-world injection; reads only visible product DOM.
// Does not access storage, scripts, cookies, forms, network or account details.
export function captureRetailer(command) {
  const url=location.origin+location.pathname;
  const base={outcome:'PAGE_CHANGED',url,observed_at:new Date().toISOString(),parser_version:'retailer-dom-1',issues:[]};
  if(globalThis.__workspaceStopped)return {...base,outcome:'INTERRUPTED'};
  if(Date.parse(command.expires_at)<=Date.now()||url.replace(/\/$/,'')!==command.url.replace(/\/$/,''))return base;
  const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
  const text=e=>visible(e)?(e.innerText||'').trim():'';
  const all=(s,root=document)=>[...root.querySelectorAll(s)].filter(visible);
  const one=s=>{const a=all(s);return a.length===1?a[0]:null;};
  const uniquePrice=values=>{const p=values.map(s=>/^\s*(?:₹\s*)?([\d,]+(?:\.\d{1,2})?)\s*$/.exec(s)).filter(Boolean).map(m=>Number(m[1].replaceAll(',','')));return p.length===1&&p[0]>0&&p[0]<=10000000?p[0]:null;};
  const pin=s=>{const p=[...s.matchAll(/\b[1-9]\d{5}\b/g)].map(m=>m[0]);return new Set(p).size===1?p[0]:null;};
  const norm=s=>(s||'').toLowerCase().replace(/\s+/g,'').replaceAll('inches','').replaceAll('inch','');
  const issues=new Set();let title='',variant=null,price=null,confirmed_pincode=null;
  // Error-page headings only; do not interpret unrelated page prose as instructions.
  const headings=all('h1').map(text).join(' ');
  if(/access denied|verify you are human|security check|just a moment/i.test(headings))return {...base,outcome:'BLOCKED'};
  if(command.retailer==='croma'){
    title=text(one('h1.pd-title'));price=uniquePrice(all('#pdp-product-price').map(text));
    const selected=[...document.querySelectorAll('input[type="radio"]:checked')].filter(e=>visible(e.parentElement)&&e.name!=='buywithExchange').map(e=>({name:e.name,value:text(e.parentElement)}));
    variant=selected.map(e=>e.name+': '+e.value).join('; ').slice(0,160)||null;
    for(const choice of selected){
      const specs=all('h4').filter(e=>text(e)===choice.name);
      const value=specs.length===1?text(specs[0].parentElement.nextElementSibling):'';
      if(!value)issues.add('variant_unconfirmed');else if(norm(value)!==norm(choice.value))issues.add('variant_conflict');
    }
    confirmed_pincode=pin(all('.pincode.custom-s-pincode').map(text).join(' '));
    const section=one('h1.pd-title')?.closest('ul');
    if(/not available for your pincode|out of stock/i.test(text(section)))issues.add('out_of_stock');
  }else if(command.retailer==='jiomart'){
    title=text(one('.product-description__productTitle'));
    price=uniquePrice(all('.PriceContainer__currentPrice').map(text));
    variant=title.match(/\b\d+(?:\.\d+)?\s*(?:kg|g|ml|l)\s*$/i)?.[0]||null;
    const rows=all('table tr').filter(e=>/^Net Quantity\s/i.test(text(e)));
    const stated=rows.length===1?text(rows[0]).replace(/^Net Quantity\s*/i,''):'';
    if(!variant||!stated)issues.add('variant_unconfirmed');else if(norm(variant)!==norm(stated))issues.add('variant_conflict');
    confirmed_pincode=pin(all('.DeliverySection__addressText').map(text).join(' '));
  }else if(command.retailer==='zepto'){
    const h=one('h1');title=text(h);const root=h?.parentElement?.parentElement;
    const summary=text(root);variant=summary.match(/Net Qty:\s*([^\n]+)/i)?.[1]?.trim()||null;
    const beforeMrp=summary.split(/\bMRP\b/)[0];
    price=uniquePrice([...beforeMrp.matchAll(/₹\s*[\d,]+(?:\.\d{1,2})?/g)].map(m=>m[0]));
    if(!variant)issues.add('variant_unconfirmed');
    // No verified postcode selector yet: fail closed instead of using seller address.
  }else if(command.retailer==='instamart'){
    title=text(one('h1'));const details=one('[data-testid="item-collection-pdp-price-details"]');
    price=uniquePrice(all('[data-testid="item-offer-price"]',details||document).map(text));
    variant=text(details).split('\n').map(s=>s.trim()).find(s=>/\b(?:g|kg|ml|l|pack|pc)\b/i.test(s))||null;
    const descriptions=all('.UxDGz').map(text);
    const packs=[...descriptions.join(' ').matchAll(/pack of (\d+)/gi)].map(m=>Number(m[1]));
    const selectedPack=variant?.match(/[x×]\s*(\d+)/i)?.[1];
    if(!selectedPack||!packs.length)issues.add('variant_unconfirmed');
    else if(packs.some(n=>n!==Number(selectedPack)))issues.add('variant_conflict');
  }
  if(!title||title.length>500||price===null)return {...base,issues:price===null?['ambiguous_price']:[]};
  if(!variant)issues.add('variant_unconfirmed');
  if(command.expected_variant&&norm(command.expected_variant)!==norm(variant))issues.add('variant_conflict');
  const locationRequired=command.retailer!=='croma';
  const locationConfirmed=!!command.expected_pincode&&confirmed_pincode===command.expected_pincode;
  if(locationRequired&&!locationConfirmed)issues.add('location_unconfirmed');
  return {...base,outcome:locationRequired&&!locationConfirmed?'LOCATION_REQUIRED':'OBSERVED',title,price,variant,confirmed_pincode,issues:[...issues]};
}
