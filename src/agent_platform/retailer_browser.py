"""Durable read-only retailer jobs for a scoped, explicitly paired browser."""
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from .app_catalogue import policy
from .automation import emit, scope
from .connector_health import BrowserProbeDetails, CheckInput, CheckResult, Observation, ProbeResult, evaluate
from .connector_recovery import recovery_options
from .external_jobs import DeferredJob
from .task_store import TaskStore
from .tasks import Action, Reconciliation, Strict, TaskError

TOOL='browser.retailer_observe'
Retailer=Literal['croma','jiomart','zepto','instamart']
SITES={
    'croma':('www.croma.com',r'^/[^/]+/p/[0-9]+/?$'),
    'jiomart':('www.jiomart.com',r'^/product/[a-z0-9-]+-[0-9]+/?$'),
    'zepto':('www.zepto.com',r'^/pn/[a-z0-9-]+/pvid/[0-9a-f-]{36}/?$'),
    'instamart':('instamart.in',r'^/p/[a-zA-Z0-9-]+/?$'),
}


class BrowserInput(Strict):
    retailer: Retailer
    url: str = Field(max_length=1000)
    expected_pincode: str | None = Field(default=None,pattern=r'^[1-9][0-9]{5}$')
    expected_variant: str | None = Field(default=None,min_length=1,max_length=160)

    @model_validator(mode='after')
    def fixed_site(self):
        u=urlsplit(self.url);host,pattern=SITES[self.retailer]
        if (u.scheme!='https' or u.netloc!=host or u.query or u.fragment
                or not re.fullmatch(pattern,u.path)):
            raise ValueError('Use a canonical product URL on the supported retailer domain')
        return self


class Capture(Strict):
    outcome: Literal['OBSERVED','BLOCKED','LOGIN_REQUIRED','LOCATION_REQUIRED','PAGE_CHANGED','INTERRUPTED','UNAVAILABLE','RATE_LIMITED']
    url: str = Field(max_length=1000)
    observed_at: str = Field(max_length=40)
    title: str | None = Field(default=None,max_length=500)
    variant: str | None = Field(default=None,max_length=160)
    price: float | None = Field(default=None,gt=0,le=10000000,allow_inf_nan=False)
    confirmed_pincode: str | None = Field(default=None,pattern=r'^[1-9][0-9]{5}$')
    issues: list[Literal['variant_conflict','variant_unconfirmed','ambiguous_price','location_unconfirmed','out_of_stock']] = Field(default_factory=list,max_length=5)
    parser_version: Literal['retailer-dom-1'] = 'retailer-dom-1'
    diagnostics: BrowserProbeDetails | None = None


async def job_allowed(conn, job):
    # A deferred step need not hold a worker lease while Chrome reads. Its saved
    # action, current step, task state, grant and automation still must be active.
    if not await TaskStore(None).automation_allowed(conn,job['task_id']):
        return False
    return bool(await (await conn.execute('''SELECT 1 FROM chore_tasks t
        JOIN chore_steps s ON s.task_id=t.id AND s.position=t.current_step
        WHERE t.id=%s AND t.status IN ('READY','RUNNING') AND t.current_step=%s
        AND s.external_job_state='RUNNING' AND s.external_job_deadline>now()
        AND s.external_job_reference->>'job_id'=%s
        AND s.tool=%s AND s.action_hash=%s
        AND (t.source_agent_id IS NULL OR EXISTS(SELECT 1 FROM task_agents a WHERE a.id=t.source_agent_id AND a.status='ACTIVE'))
        AND EXISTS(SELECT 1 FROM platform_tool_grants g WHERE g.app_id=t.source_app_id AND g.tool=%s)''',
        (job['task_id'],job['step_position'],str(job['id']),job['tool'],job['action_hash'],job['tool']))).fetchone())


def as_probe(job, capture):
    arguments=BrowserInput.model_validate(job['arguments'])
    stamp=datetime.fromisoformat(capture.observed_at)
    now=datetime.now(UTC)
    if (stamp.tzinfo is None or stamp>now+timedelta(seconds=2)
            or stamp<job['created_at']-timedelta(seconds=2) or stamp>job['expires_at']):
        raise TaskError('Capture timestamp is outside this job')
    if capture.url.rstrip('/')!=arguments.url.rstrip('/'):
        return ProbeResult(status='BROKEN',reason_code='browser_product_changed',verification_mode='live_source')
    if capture.outcome not in {'OBSERVED','LOCATION_REQUIRED'}:
        statuses={'BLOCKED':'BLOCKED','LOGIN_REQUIRED':'LOGIN_REQUIRED','PAGE_CHANGED':'BROKEN',
                  'INTERRUPTED':'CONFIGURATION_REQUIRED','UNAVAILABLE':'UNAVAILABLE','RATE_LIMITED':'RATE_LIMITED'}
        # Denied resources alongside missing product markup need operator
        # review; counts alone cannot establish whether hydration was blocked.
        restricted = (capture.outcome == 'PAGE_CHANGED' and capture.diagnostics
                      and (capture.diagnostics.blocked_resources or capture.diagnostics.request_limit_reached))
        return ProbeResult(status='CONFIGURATION_REQUIRED' if restricted else statuses[capture.outcome],
            reason_code='browser_resource_policy' if restricted else 'browser_'+capture.outcome.lower(),
            verification_mode='live_source', probe=capture.diagnostics)
    if not capture.title or capture.price is None:
        return ProbeResult(status='BROKEN',reason_code='browser_missing_product_price',verification_mode='live_source')
    issues=set(capture.issues)
    if not capture.variant:issues.add('variant_unconfirmed')
    if arguments.expected_variant and (capture.variant or '').casefold().strip()!=arguments.expected_variant.casefold().strip():
        issues.add('variant_conflict')
    localized=arguments.retailer!='croma'
    confirmed=bool(arguments.expected_pincode and capture.confirmed_pincode==arguments.expected_pincode)
    digest=hashlib.sha256(json.dumps(capture.model_dump(),sort_keys=True).encode()).hexdigest()
    obs=Observation(source_url=arguments.url,observation_id=digest,observed_at=capture.observed_at,
        price=capture.price,currency='INR',fees_known=False,title=capture.title,variant=capture.variant,
        location_required=localized,location_confirmed=confirmed,
        location_label=capture.confirmed_pincode if confirmed else None,quality_issues=sorted(issues))
    return ProbeResult(status='CONFIGURATION_REQUIRED' if localized and not confirmed else 'WORKING',
        reason_code='browser_location_required' if localized and not confirmed else 'browser_product_observed',
        observations=[obs],verification_mode='live_source',probe=capture.diagnostics)


class RetailerBrowser:
    tool,executor=TOOL,'paired_browser'
    version,risk,timeout='1','READ',10
    input_model,output_model=BrowserInput,CheckResult
    description=('Optional: use only when the user explicitly chooses their own paired Chrome. Observe a product in the app/user paired Chrome on Croma, JioMart, Zepto or Instamart. '
        'Read product price and selected variant; never purchase, log in, enter secrets or submit forms. '
        'Use only a real product URL. Never invent a delivery pincode. Chrome must be connected; '
        'missing location or conflicting variants are unusable evidence. Browser work waits durably.')

    def __init__(self,pool):self.pool=pool

    def prepare(self,arguments):
        value=BrowserInput.model_validate(arguments,strict=True)
        return Action(self.tool,'1',value.model_dump(),f'Read the {value.retailer} product in paired Chrome; no purchase.', 'READ')

    async def execute(self,action,*,task_id,authorized,operation_id,**context):
        if not await authorized():raise TaskError('Execution revoked')
        async with self.pool.connection() as conn,conn.transaction():
            await scope(conn,worker=True)
            previous=await (await conn.execute('SELECT * FROM retailer_browser_jobs WHERE operation_id=%s',(operation_id,))).fetchone()
            if previous:return DeferredJob({'job_id':str(previous['id'])},poll_after_seconds=2,timeout_seconds=360)
            t=await (await conn.execute('SELECT *,now() AS clock FROM chore_tasks WHERE id=%s',(task_id,))).fetchone()
            if not t or not t['source_app_id']:raise TaskError('Application identity required')
            app,owner=t['source_app_id'],t['owner_id'];source=action.arguments['retailer'];now=t['clock']
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE',(app,))
            await conn.execute('''INSERT INTO connector_check_state(tenant_id,app_id,owner_id,tool,source)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(t['tenant_id'],app,owner,self.tool,source))
            state=await (await conn.execute('SELECT * FROM connector_check_state WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s FOR UPDATE',(app,owner,self.tool,source))).fetchone()
            if state['needs_attention'] or state['next_check_at']>now or (state['lease_until'] and state['lease_until']>now):
                return CheckResult(connector=self.tool,source=source,execution='DEFERRED',status='NOT_CHECKED',
                    reason_code='owner_action_required' if state['needs_attention'] else 'connector_cooldown',
                    needs_attention=state['needs_attention'],next_check_at=state['next_check_at'].isoformat()).model_dump()
            limits=await policy(conn,app)
            count=await (await conn.execute("SELECT count(*) AS n FROM connector_check_state WHERE app_id=%s AND lease_until>now()",(app,))).fetchone()
            if count['n']>=3:
                return CheckResult(connector=self.tool,source=source,execution='DEFERRED',status='NOT_CHECKED',reason_code='app_concurrency_limit').model_dump()
            used=await (await conn.execute('''INSERT INTO connector_check_usage(app_id,hour,checks)
                SELECT %s,date_trunc('hour',now()),1 WHERE %s>0
                ON CONFLICT(app_id,hour) DO UPDATE SET checks=connector_check_usage.checks+1
                WHERE connector_check_usage.checks<%s RETURNING checks''',(app,limits.connector_checks_per_hour,limits.connector_checks_per_hour))).fetchone()
            if not used:return CheckResult(connector=self.tool,source=source,execution='DEFERRED',status='NOT_CHECKED',reason_code='app_hourly_limit').model_dump()
            identifier=uuid4()
            await conn.execute('''INSERT INTO retailer_browser_jobs(id,operation_id,tenant_id,app_id,owner_id,task_id,step_position,source,action_hash,arguments,tool,executor)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(identifier,operation_id,t['tenant_id'],app,owner,task_id,t['current_step'],source,action.digest,Jsonb(action.arguments),self.tool,self.executor))
            await conn.execute("UPDATE connector_check_state SET lease_token=%s,lease_until=now()+interval '6 minutes' WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s",(identifier,app,owner,self.tool,source))
            await emit(conn,{'tenant':t['tenant_id'],'app_id':app,'user':owner},'BrowserObservationQueued',{'job_id':str(identifier),'source':source},key='browser-queued:'+str(identifier),correlation=task_id)
        return DeferredJob({'job_id':str(identifier)},poll_after_seconds=2,timeout_seconds=360)

    async def poll(self,action,*,reference,task_id,authorized):
        if not await authorized():raise TaskError('Execution revoked')
        async with self.pool.connection() as conn,conn.transaction():
            await scope(conn,worker=True)
            job=await (await conn.execute('SELECT *,now() AS clock FROM retailer_browser_jobs WHERE id=%s AND task_id=%s AND action_hash=%s FOR UPDATE',(reference['job_id'],task_id,action.digest))).fetchone()
            if not job:raise TaskError('Browser job not found')
            old=await (await conn.execute('SELECT result FROM connector_check_reports WHERE id=%s',(job['id'],))).fetchone()
            if old:return old['result']
            if job['result'] is None and job['clock']<job['expires_at']:
                return DeferredJob(reference,poll_after_seconds=3,timeout_seconds=360)
            probe=ProbeResult.model_validate(job['result']) if job['result'] else ProbeResult(status='CONFIGURATION_REQUIRED',reason_code='server_runner_offline_or_interrupted' if self.executor=='server' else 'browser_offline_or_interrupted',verification_mode='unspecified')
            evidence,usable=evaluate(probe,CheckInput(tool=self.tool,version='1',require_price=True),now=job['clock'])
            state=await (await conn.execute('SELECT failures FROM connector_check_state WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s FOR UPDATE',(job['app_id'],job['owner_id'],self.tool,job['source']))).fetchone()
            failures=0 if usable else min((state['failures'] if state else 0)+1,20)
            delay=min(3600,60*2**min(failures,6))
            needs=probe.status in {'BLOCKED','LOGIN_REQUIRED','CONFIGURATION_REQUIRED'}
            report=CheckResult(connector=self.tool,source=job['source'],execution='EXECUTED',status=probe.status,
                reason_code=probe.reason_code,report_id=str(job['id']),checked_at=job['clock'].isoformat(),
                next_check_at=(job['clock']+timedelta(seconds=delay)).isoformat(),needs_attention=needs,
                usable_evidence=usable,evidence=evidence,evidence_issues=sorted({i for e in evidence for i in e['issues']}),
                verification_mode=probe.verification_mode,
                probe=probe.probe,
                recovery_options=recovery_options(job['app_id'],self.tool,job['source'],probe.status,usable=usable)).model_dump()
            await conn.execute('''INSERT INTO connector_check_reports(id,tenant_id,app_id,owner_id,task_id,step_position,tool,source,tool_version,action_hash,result)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'1',%s,%s)''',(job['id'],job['tenant_id'],job['app_id'],job['owner_id'],task_id,job['step_position'],self.tool,job['source'],action.digest,Jsonb(report)))
            await conn.execute("UPDATE retailer_browser_jobs SET state='DONE' WHERE id=%s",(job['id'],))
            await conn.execute('''UPDATE connector_check_state SET lease_until=NULL,lease_token=NULL,needs_attention=%s,
                next_check_at=%s,failures=%s
                WHERE app_id=%s AND owner_id=%s AND tool=%s AND source=%s AND lease_token=%s''',(needs,job['clock']+timedelta(seconds=delay),failures,job['app_id'],job['owner_id'],self.tool,job['source'],job['id']))
            await emit(conn,{'tenant':job['tenant_id'],'app_id':job['app_id'],'user':job['owner_id']},'ConnectorCheckCompleted',
                {'report_id':str(job['id']),'tool':self.tool,'source':job['source'],'status':probe.status,'usable_evidence':usable},key='connector-check:'+str(job['id']),correlation=task_id)
            return report

    async def reconcile(self,action,*,task_id,operation_id):
        async with self.pool.connection() as conn,conn.transaction():
            await scope(conn,worker=True)
            job=await (await conn.execute('SELECT id FROM retailer_browser_jobs WHERE operation_id=%s AND task_id=%s',(operation_id,task_id))).fetchone()
        return DeferredJob({'job_id':str(job['id'])},poll_after_seconds=2,timeout_seconds=360) if job else Reconciliation(False,{'browser_dispatch':'never_started'})


class ServerRetailerBrowser(RetailerBrowser):
    tool,executor='retailer.observe','server'
    description=('Default public retailer verification. Read public retailer product evidence on the platform server. Works from mobile apps; '
        'no customer browser or extension is required. Supports Croma, JioMart, Zepto and Instamart product URLs. '
        'No login, payment, form submission or access-control bypass. Returns unavailable or configuration '
        'required when rendering or delivery location cannot be verified. Never invent prices or pincodes.')

    def prepare(self,arguments):
        value=BrowserInput.model_validate(arguments,strict=True)
        return Action(self.tool,'1',value.model_dump(),f'Read the public {value.retailer} product on the platform server; no purchase.', 'READ')
