"""Recovery guidance from platform policy and operator-approved profiles only."""
from .connector_health import RecoveryOption
from .feed_config import profiles


def recovery_options(app_id, tool, source, status, *, usable=False):
    guidance = {
        'BLOCKED': ('contact_provider', 'Ask the source owner for an authorized API or export feed. Automatic checks remain stopped until access is resolved.'),
        'LOGIN_REQUIRED': ('authorize', 'Renew the provider authorization or credential, then test the connection again.'),
        'CONFIGURATION_REQUIRED': ('configure', 'Complete the approved connector configuration before testing again.'),
        'RATE_LIMITED': ('wait', 'Wait until next_check_at; provider cooldown and application quotas still apply.'),
        'NOT_CHECKED': ('wait', 'Review the last report and next_check_at. A deferred check did not contact the source.'),
        'BROKEN': ('repair_adapter', 'Review the adapter and its response mapping before retrying. Do not treat this result as price evidence.'),
        'UNAVAILABLE': ('wait', 'The source could not be checked. Retry only after the recorded backoff.'),
    }
    options = []
    if status in guidance:
        action, summary = guidance[status]
        options.append(RecoveryOption(action=action, summary=summary))
    elif not usable:
        options.append(RecoveryOption(action='review_evidence', summary='Obtain a fresh authorized observation with the missing evidence fields. Unknown fees remain unknown.'))
    if status in {'BLOCKED', 'LOGIN_REQUIRED', 'CONFIGURATION_REQUIRED'}:
        try:
            approved = profiles(app_id)
        except ValueError:
            approved = {}
        for slug, profile in approved.items():
            if any(binding.tool == tool and binding.source == source for binding in profile.replaces):
                options.append(RecoveryOption(action='setup_feed', profile=slug,
                    setup_path='/connectors', summary=f'Set up {profile.name}. Provider account authorization may still be required.'))
                if len(options) == 6:
                    break
    return [option.model_dump(mode='json') for option in options]
