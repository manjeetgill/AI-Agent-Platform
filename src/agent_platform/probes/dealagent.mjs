// Read-only bridge: reuse DealAgent HTTP politeness and extraction, never its
// browser runner or queue. No retailer HTML/credentials leave this process.
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import {createHash} from 'node:crypto';

export const ownsHost = (cfg, host) => cfg.domains.some(domain => host === domain || host === 'www.' + domain);

export async function probe({cfg, http, collector, fetchCount, pincode, accessFailure=()=>null}) {
  const canonical = new URL(cfg.healthCheckUrl);
  canonical.search = ''; canonical.hash = '';
  const details = {source_url:canonical.href, checked_at:new Date().toISOString(), parser_version:cfg.parserVersion,
    http_status:null, http_requests:0, product_requests:0, location_confirmed:false, transport:'http'};
  function result(status, reason_code, observations=[]) {
    Object.assign(details, fetchCount());
    return {status, reason_code, observations, verification_mode:'live_source', probe:details};
  }
  // Even a browser/location-dependent source gets an honest public access
  // check first. We do not attempt its UI flow or accept an unconfirmed price.
  const page = await http.fetch({url:cfg.healthCheckUrl, accept:'html', useCache:false});
  details.checked_at = new Date(page.fetchedAt).toISOString();
  details.http_status = page.status ?? null;
  if (!page.ok) {
    const reason = (page.failure?.class === 'robots_restricted' && accessFailure()) || page.failure?.class || 'invalid_response';
    const status = ['http_403','captcha','access_control','robots_restricted','robots_http_403'].includes(reason) ? 'BLOCKED'
      : ['http_429','cooldown','robots_http_429'].includes(reason) ? 'RATE_LIMITED'
      : ['login_required','robots_http_401'].includes(reason) ? 'LOGIN_REQUIRED'
      : ['javascript_shell','browser_unavailable','location_required'].includes(reason) ? 'CONFIGURATION_REQUIRED'
      : reason === 'unsupported_url' ? 'BLOCKED' : 'UNAVAILABLE';
    return result(status, reason);
  }
  if (cfg.location?.required) {
    // Cookie/UI flows need a separate authorized, confirmed location adapter.
    // Providing a pincode alone is never evidence of the retailer's location.
    return result('CONFIGURATION_REQUIRED', pincode ? 'location_flow_required' : 'location_required');
  }
  if (cfg.browserRequired) return result('CONFIGURATION_REQUIRED', 'rendering_required');
  try {
    const extracted = collector.extract(page);
    const verdict = collector.validate(collector.normalize(extracted, page), extracted);
    const product = verdict.product;
    if (!verdict.ok || !product || !Number.isFinite(product.price) || product.price <= 0)
      return result('BROKEN', 'parser_or_price_validation_failed');
    const url = new URL(product.url);
    if (url.protocol !== 'https:' || url.username || url.password || url.port ||
        !ownsHost(cfg,url.hostname)) return result('BROKEN', 'invalid_evidence_url');
    url.search=''; url.hash='';
    const stamp = new Date(page.fetchedAt).toISOString();
    const id = createHash('sha256').update(url.href+'|'+stamp+'|'+product.price).digest('hex');
    return result('WORKING', 'live_product_observed', [{source_url:url.href, observation_id:id,
      observed_at:stamp, price:product.price, currency:product.currency, fees_known:false}]);
  } catch {return result('BROKEN', 'parser_error');}
}

async function main() {
  const [root, retailer, pincode] = process.argv.slice(2);
  const allowed = ['croma','reliancedigital','vijaysales','jiomart','blinkit','zepto','instamart','bigbasket'];
  if (!path.isAbsolute(root ?? '') || !allowed.includes(retailer) || (pincode && !/^[1-9][0-9]{5}$/.test(pincode))) throw Error('Invalid probe configuration');
  const base = path.join(root,'engine/src/crawler');
  const [{loadConfigs},{HttpFetcher},{ConfigDrivenCollector}] = await Promise.all(
    ['config','fetcher','collector'].map(name => import(pathToFileURL(path.join(base,name+'.ts')).href)));
  const loaded = loadConfigs(path.join(root,'engine/retailers'));
  const cfg = loaded.configs.find(item => item.id === retailer);
  if (!cfg?.healthCheckUrl) throw Error('Configured product URL required');
  const url = new URL(cfg.healthCheckUrl);
  if (url.protocol !== 'https:' || url.username || url.password || url.port || !ownsHost(cfg,url.hostname)) throw Error('Invalid source URL');
  let http_requests=0, product_requests=0, robots_http_status=null, robotsFailure=null;
  const fetchFn = async (input, init) => {
    const target = new URL(String(input));
    if (target.protocol !== 'https:' || !ownsHost(cfg,target.hostname) || target.port || target.username || target.password)
      throw Error('Destination outside configured retailer');
    if (http_requests>=8) throw Error('Request limit');
    http_requests++;
    if (target.pathname !== '/robots.txt') product_requests++;
    try {
      const response = await fetch(input, {...init, signal:AbortSignal.any([init.signal, AbortSignal.timeout(5000)])});
      if (target.pathname === '/robots.txt') {
        robots_http_status=response.status;
        if ([401,403,429].includes(response.status)) robotsFailure='robots_http_'+response.status;
        else if (response.status>=500) robotsFailure='robots_unavailable';
      }
      return response;
    } catch(error) {
      if (target.pathname === '/robots.txt') robotsFailure='robots_unavailable';
      throw error;
    }
  };
  const http = new HttpFetcher({fetchFn, retries:0, timeoutMs:5000, maxBodyBytes:2500000,
    minIntervalMs:Math.max(cfg.crawl.minIntervalMs,1000), respectRobots:true, publicNetworkOnly:true,
    userAgent:'DealAgentPlatform/1.0 (+https://myalgodesk.in)'});
  const collector = new ConfigDrivenCollector(cfg,http);
  if (collector.identify(cfg.healthCheckUrl).kind !== 'product') throw Error('Configured URL must be a product');
  const output = await probe({cfg,http,collector,pincode,accessFailure:()=>robotsFailure,
    fetchCount:()=>({http_requests,product_requests,robots_http_status})});
  process.stdout.write(JSON.stringify(output));
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => {process.stdout.write(JSON.stringify({status:'BROKEN',reason_code:'probe_configuration_or_runtime_error',observations:[]}));process.exitCode=1;});
}
