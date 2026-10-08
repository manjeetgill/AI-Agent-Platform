"""Explicit service flags; unsupported execution features are always disabled."""
import os


def enabled(name, *, default=False):
    value = os.environ.get(name, os.environ.get(name + '_ENABLED'))
    return default if value is None else value.strip().lower() in {'1', 'true', 'yes'}


def capabilities():
    return {
        'AI_PLATFORM': enabled('AI_PLATFORM', default=True),
        'AI_NVIDIA': enabled('AI_NVIDIA', default=True),
        'AI_BUILTIN_AGENTS': enabled('AI_BUILTIN_AGENTS', default=True),
        'AI_CUSTOM_AGENTS': enabled('AI_CUSTOM_AGENTS', default=True),
        'AI_MEMORY': enabled('AI_MEMORY', default=True),
        'AI_RAG': enabled('AI_RAG', default=False),
        'AI_SCHEDULER': enabled('AI_AUTOMATIONS'),
        'AI_WORKFLOWS': enabled('AI_WORKFLOWS', default=False),
        'AI_EXECUTION_REQUESTS': False,
    }
