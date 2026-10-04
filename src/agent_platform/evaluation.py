"""Deterministic evidence and structured-output evaluation for provider changes."""
from pydantic import ValidationError

from .models import Report
from .runtime import RunFailure, compact_evidence, validate_report


def evaluate(evidence, output):
    try:
        bounded = compact_evidence(evidence)
        report = validate_report(Report.model_validate(output), bounded)
        facts = [item for item in report.supporting + report.conflicting
                 if item.category in {'FACT', 'CALCULATED FACT'}]
        cited = sum(bool(item.source_ids) for item in facts)
        return {'valid': True, 'facts': len(facts), 'cited_facts': cited, 'error': None}
    except (RunFailure, ValidationError) as exc:
        return {'valid': False, 'facts': 0, 'cited_facts': 0, 'error': type(exc).__name__}
