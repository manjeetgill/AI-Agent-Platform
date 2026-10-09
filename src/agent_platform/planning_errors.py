"""Actionable, allowlisted explanations; never expose the provider response."""
import httpx


def explanation(error):
    cause = error.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        status = cause.response.status_code
        if status in {401, 403}:
            return 'Model access was rejected. Check the private API key and permission to use the selected model.'
        if status == 404:
            return 'The configured model or endpoint was not found. Check the selected model ID.'
        if status == 429:
            return 'The model provider rejected the request because of quota or rate limits. Check the provider allowance before retrying.'
    code = getattr(error, 'code', '')
    if isinstance(error, TimeoutError) or code == 'MODEL_TIMEOUT':
        return 'The planning request timed out. No task was started; check provider availability before requesting another plan.'
    if code == 'MODEL_NOT_CONFIGURED':
        return 'The planning model is disabled or incomplete. Configure its private API key and model ID, then restart the service.'
    if code == 'INVALID_MODEL_OUTPUT':
        return 'The model returned an invalid plan. No tool was run. Try a smaller request with explicit inputs.'
    return 'Planner unavailable. Check the configured provider and service health.'
