/** Only fixed text is displayed; provider error strings are never rendered. */
export function voiceFailureMessage(details) {
  const messages = {
    SPENDING_LIMIT: 'Call stopped at the application spending limit. Review usage and existing reservations before changing the limit.',
    RATE_CONFIGURATION_REQUIRED: 'Paid voice is paused because rates or spending limits need configuration. Ask the platform operator to review them.',
    PROVIDER_QUOTA: 'The speech provider rate or quota limit was reached. Check provider usage before trying again.',
    PROVIDER_BACKPRESSURE: 'The model provider is busy or its quota was reached. Check provider usage before trying again.',
    PROVIDER_AUTH: 'The provider rejected its credentials. Ask the platform operator to check the private connection settings.',
    TIMEOUT: 'The voice step timed out. Check any pending business action before starting another call.',
    MODEL_TIMEOUT: 'The AI answer timed out. Check any pending business action before starting another call.',
    MODEL_NOT_CONFIGURED: 'The reasoning model is not configured. Ask the platform operator to connect it.',
    CONTEXT_BUDGET_EXCEEDED: 'The call context exceeded its configured size limit. Ask the operator to review the agent configuration.',
    INVALID_MODEL_OUTPUT: 'The model returned an answer that could not be validated. No unverified answer was accepted.',
    MODEL_UNAVAILABLE: 'The reasoning provider is unavailable. Check its connection before trying again.',
    PROVIDER_ERROR: 'The speech provider failed. Check its service status before trying again.',
    FAILED: 'A voice step failed. Check call history and pending business actions before trying again.'
  };
  return details ? (messages[details.outcome] || messages.FAILED) : '';
}
