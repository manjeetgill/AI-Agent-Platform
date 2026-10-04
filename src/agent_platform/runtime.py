"""Bounded read-only tool call followed by a validated model report."""
import json
import re

import httpx

from .gateway import Gateway, NVIDIAProvider
from .runtime_errors import RunFailure
from .tokens import issue


def compact_evidence(data):
    if not isinstance(data, dict):
        raise RunFailure('INVALID_EVIDENCE')
    entries = data.get('events') if 'events' in data else data.get('facts')
    if not isinstance(entries, list):
        raise RunFailure('INVALID_EVIDENCE')
    if data.get('evidenceVersion') == 'research-v1' and len(entries) > 100:
        raise RunFailure('EVIDENCE_TOO_LARGE')
    entries = entries[:100]
    if not entries or any(not isinstance(item, dict) for item in entries):
        raise RunFailure('MISSING_EVIDENCE')
    allowed = {'instrument', 'session', 'timeframe', 'source', 'asOf', 'events', 'facts', 'indicators',
               'conditions', 'freshness', 'historicalContext', 'invalidationConditions',
               'measurements', 'methodology', 'limitations', 'evidenceVersion', 'digest', 'stale'}
    evidence = {key: value for key, value in data.items() if key in allowed}
    key = 'events' if 'events' in data else 'facts'
    evidence[key] = [{**entry, 'source_id': str(index)} for index, entry in enumerate(entries)]
    encoded = json.dumps(evidence, default=str, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode()) > 32000:
        raise RunFailure('EVIDENCE_TOO_LARGE')
    return evidence


def validate_report(report, evidence):
    known = {str(index) for index in range(len(evidence.get('events', evidence.get('facts', []))))}
    if report.historical_context and not evidence.get('historicalContext'):
        raise RunFailure('UNSUPPORTED_CONTEXT')
    if report.invalidation_conditions and not evidence.get('invalidationConditions'):
        raise RunFailure('UNSUPPORTED_CONTEXT')
    for finding in report.supporting + report.conflicting:
        if finding.source_ids and not set(finding.source_ids) <= known:
            raise RunFailure('UNSUPPORTED_SOURCE')
        if finding.category in {'FACT', 'CALCULATED FACT'} and not finding.source_ids:
            raise RunFailure('UNSUPPORTED_FACT')
    if evidence.get('evidenceVersion') == 'research-v1':
        measurements = evidence.get('measurements', evidence.get('facts', []))
        verified = {f.get('label'): f for f in measurements}
        for claim in report.numeric_claims:
            original = verified.get(claim.label)
            if (not original or original.get('unit') != claim.unit
                    or not isinstance(original.get('value'), (int, float))
                    or isinstance(original.get('value'), bool)
                    or claim.value != original['value']):
                raise RunFailure('UNSUPPORTED_NUMBER')
        # Numerical prose has no label/unit binding, even when the number happens
        # to exist elsewhere. Omit whole statements, never rewrite their meaning.
        numeric = re.compile(r'\d|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|half|twice|double|triple)\b', re.IGNORECASE)
        omitted = False
        def qualitative(text):
            nonlocal omitted
            if numeric.search(text):
                omitted = True
                return False
            return True
        changes = {}
        for key in ('summary', 'why_triggered', 'data_freshness'):
            original = getattr(report, key)
            if original is None:
                continue
            sentences = re.split(r'(?<=[.!?])\s+', original)
            retained = ' '.join(s for s in sentences if qualitative(s))
            changes[key] = retained or ('Review the verified measurements and source coverage.' if key == 'summary'
                                        else 'See saved source timestamps.' if key == 'data_freshness' else None)
        for key in ('supporting', 'conflicting'):
            changes[key] = [f for f in getattr(report, key) if qualitative(f.text)]
        for key in ('missing', 'risks', 'historical_context', 'invalidation_conditions', 'questions'):
            changes[key] = [text for text in getattr(report, key) if qualitative(text)]
        if omitted:
            changes['missing'] = ['Some numerical model prose was omitted. Use the verified measurements for quantities.'] + changes['missing'][:11]
        report = report.model_copy(update=changes)
    return report


class Runtime:
    def __init__(self, connector_url, signing_key, provider_key, *, connector_client=None, model_client=None):
        self.connector_url = connector_url.rstrip('/')
        self.signing_key = signing_key
        self.provider_key = provider_key
        self.connector_client = connector_client
        self.gateway = Gateway(NVIDIAProvider(provider_key), client=model_client)

    async def retrieve(self, tenant, user, agent_id, run_id, query):
        endpoints = {'scanner_evidence': 'scanner-evidence', 'backtest_evidence': 'backtest-evidence',
                     'paper_trade_evidence': 'paper-trade-evidence', 'research_evidence': 'research-evidence'}
        endpoint = endpoints[query.tool]
        token = issue(self.signing_key, issuer='agent-platform', audience='nraialgo-connector',
                      tenant=tenant, user=user, agent=agent_id, run=run_id, scope=query.tool + ':read')
        client = self.connector_client or httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3))
        try:
            response = await client.post(self.connector_url + '/v1/agent-connector/' + endpoint,
                                         headers={'Authorization': 'Bearer ' + token},
                                         json=query.model_dump(exclude={'tool'}))
            if response.status_code == 404:
                raise RunFailure('EVIDENCE_NOT_FOUND')
            response.raise_for_status()
            return compact_evidence(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RunFailure('CONNECTOR_UNAVAILABLE') from exc
        finally:
            if self.connector_client is None:
                await client.aclose()

    async def generate(self, definition, evidence, preferences=None):
        task = 'challenge' if definition['name'] in {'Signal Challenger', 'Backtest Auditor'} else 'research'
        return await self.gateway.report(task, definition['instructions'], evidence, validate_report, preferences)
