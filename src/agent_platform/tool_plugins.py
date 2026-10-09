"""Explicit operator allowlist of installed Python adapter packages, shared by API and worker."""
import os
from importlib.metadata import entry_points

from .tasks import TaskError


def load_plugins(registry):
    selected = [name.strip() for name in os.environ.get('AI_PLATFORM_TOOL_PLUGINS', '').split(',') if name.strip()]
    if len(selected) != len(set(selected)):
        raise TaskError('Duplicate tool plugin selection')
    installed = list(entry_points(group='agent_platform.tools'))
    for name in selected:
        matches = [entry for entry in installed if entry.name == name]
        if len(matches) != 1:
            raise TaskError('Selected tool plugin is missing or ambiguous: ' + name)
        adapters = matches[0].load()()
        if not isinstance(adapters, dict) or not adapters:
            raise TaskError('Tool plugin must return named adapters')
        for tool, adapter in adapters.items():
            if not isinstance(tool, str) or not tool or len(tool) > 100 or tool in registry.adapters or tool in {'research.explain', 'browser.wikipedia_search', 'knowledge.answer', 'runtime.next', 'runtime.workflow', 'runtime.connector_check', 'feed.read'}:
                raise TaskError('Tool plugins cannot overwrite installed tools')
            if (not all(hasattr(adapter, field) for field in ('input_model', 'output_model', 'description', 'version', 'prepare', 'execute', 'reconcile'))
                    or adapter.risk not in {'READ', 'SEND', 'USER_ONLY'} or not 0 < adapter.timeout <= 30):
                raise TaskError('Invalid typed tool plugin contract')
            registry.adapters[tool] = adapter
    return registry
