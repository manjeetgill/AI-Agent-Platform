import pytest

from agent_platform.models import Report, ResearchInput, RunRequest
from agent_platform.runtime import compact_evidence, validate_report
from agent_platform.runtime_errors import RunFailure


def evidence():
    return compact_evidence({'evidenceVersion':'research-v1','source':'fixture',
        'facts':[{'label':'prior20_mean_volume','value':80,'unit':'shares'}],
        'limitations':['Small sample'], 'methodology':{'version':'fixture-v1'}})


def test_typed_numeric_claims_cannot_change_units_statistic_or_value():
    data = evidence()
    report = Report(summary='Volume comparison is supplied.', data_freshness='Saved snapshot',
                    numeric_claims=[{'label':'prior20_mean_volume','value':80,'unit':'shares'}])
    assert validate_report(report,data) == report
    for change in ({'unit':'INR'}, {'label':'prior20_median_volume'}, {'value':81}):
        bad = report.model_copy(deep=True)
        bad.numeric_claims[0] = bad.numeric_claims[0].model_copy(update=change)
        with pytest.raises(RunFailure,match='UNSUPPORTED_NUMBER'):
            validate_report(bad,data)


@pytest.mark.parametrize('summary', ['The success rate is 99%.', 'Ninety nine percent succeed.', 'The volume doubled by two times.'])
def test_numeric_prose_cannot_bypass_typed_claim_channel(summary):
    result = validate_report(Report(summary=summary,data_freshness='Saved'),evidence())
    assert result.summary != summary
    assert 'omitted' in result.missing[0]
    assert evidence()['methodology']['version'] == 'fixture-v1'
    assert evidence()['limitations'] == ['Small sample']


def test_research_tool_is_reference_only_not_client_evidence():
    request = RunRequest(agent_id='11111111-1111-4111-8111-111111111111',
        evidence={'tool':'research_evidence','snapshot_id':'22222222-2222-4222-8222-222222222222'},idempotency_key='research-test')
    assert isinstance(request.evidence,ResearchInput)
    with pytest.raises(ValueError):
        ResearchInput(tool='research_evidence',snapshot_id='22222222-2222-4222-8222-222222222222',facts=[])


def test_omission_preserves_qualitative_statements_and_verified_quantities():
    report = Report(summary='The sample is limited. There are ninety matches.',data_freshness='Saved',
        numeric_claims=[{'label':'prior20_mean_volume','value':80,'unit':'shares'}],
        supporting=[{'category':'FACT','text':'Median volume is 80.','source_ids':['0']}])
    result = validate_report(report,evidence())
    assert result.summary == 'The sample is limited.'
    assert result.supporting == []
    assert result.numeric_claims[0].label == 'prior20_mean_volume'


def test_large_measurements_are_exact_not_relative_tolerance():
    data = evidence()
    data['facts'][0]['value'] = 1_000_000_000
    report = Report(summary='Saved measurement.',data_freshness='Saved',
        numeric_claims=[{'label':'prior20_mean_volume','value':1_000_000_001,'unit':'shares'}])
    with pytest.raises(RunFailure,match='UNSUPPORTED_NUMBER'):
        validate_report(report,data)
