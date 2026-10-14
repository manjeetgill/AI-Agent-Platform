"""Provider-neutral integration contracts. Permission does not imply activation."""
from .business_workspaces import FEATURE_SCOPES

# API roots, not automatic actions: discovery must never dispatch a provider call.
DETAILS = {
    'whatsapp_support': ('WhatsApp support', '/v1/whatsapp/channels', 'whatsapp',
        'Authorize a business number, verify its webhook, publish knowledge and test a reply.'),
    'whatsapp_notifications': ('WhatsApp alerts', '/v1/whatsapp/alerts', 'whatsapp',
        'Configure an authorized sender route, recipient consent and a permitted message template.'),
    'whatsapp_calling': ('WhatsApp calling', '/v1/calling/agents', 'whatsapp_calling',
        'Verify number calling eligibility, audio runtime and recipient permission before a real test.'),
    'internet_voice': ('Internet voice', '/v1/voice/agents', 'internet_voice',
        'Configure speech/model services, knowledge or business tools, then verify microphone and audio.'),
    'telephone_voice': ('Telephone calling', '/v1/telephony/connections', 'telephone',
        'Supply an authorized carrier, configure the Asterisk gateway and verify an inbound call.'),
    'memory': ('Reviewed memory', '/v1/memory', None,
        'Configure retention and review facts before sharing them within this application.'),
    'agent_tasks': ('Agent tasks', '/v1/task-agents', 'planning',
        'Install and grant typed tools, configure a model and run a task through the worker.'),
    'knowledge': ('Knowledge and RAG', '/v1/knowledge', 'knowledge',
        'Upload app-owned documents and check retrieval and citations.'),
    'email': ('Email notifications', '/v1/notifications/email', 'email',
        'Configure a sender, private transport credentials and usage limits; verify receipt.'),
    'sms': ('SMS notifications', '/v1/notifications/sms', 'sms',
        'Configure an authorized SMS transport and segment limits; verify handset receipt.'),
    'api_feeds': ('API and feed connectors', '/v1/connectors/feeds', 'feeds',
        'Install and grant feed.read, configure an approved source and run its bounded connection test.'),
    'schedules': ('Scheduled tasks', '/v1/orchestration/automations', 'schedules',
        'Grant the task tools, configure a nonzero run allowance and approve a bounded schedule.'),
}


def catalogue(scopes):
    granted = set(scopes)
    return [{
        'id': feature, 'title': title, 'api_root': root,
        'required_scopes': sorted(FEATURE_SCOPES[feature]),
        'missing_scopes': sorted(FEATURE_SCOPES[feature] - granted),
        'permissions_granted': FEATURE_SCOPES[feature] <= granted,
        'readiness_id': readiness_id, 'setup_requirement': requirement,
        'required_tool_grants': ['feed.read'] if feature == 'api_feeds' else [],
        'tool_grants_note': 'Task-specific tools require separate operator grants; they are not inherited.',
        'permission_note': ('Email and SMS share notification scopes; per-app transport configuration and limits control availability.'
                            if feature in {'email', 'sms'} else None),
    } for feature, (title, root, readiness_id, requirement) in DETAILS.items()]
