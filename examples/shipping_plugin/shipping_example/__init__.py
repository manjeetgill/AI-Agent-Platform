"""Example business-owned package: estimate supplied rates, never book or purchase shipping."""
from pydantic import Field

from agent_platform.tasks import Action, Strict, TaskError


class EstimateInput(Strict):
    units: int = Field(ge=1, le=10000)
    rate_minor_units: int = Field(ge=0, le=10000000)
    currency: str = Field(pattern=r'^[A-Z]{3}$')


class EstimateOutput(Strict):
    total_minor_units: int
    currency: str
    basis: str


class ShippingEstimate:
    version = '1'
    risk = 'READ'
    timeout = 5
    description = 'Calculate a shipping estimate from a user-supplied unit count, unit rate in minor currency units and currency. No booking or purchase.'
    input_model = EstimateInput
    output_model = EstimateOutput

    def prepare(self, arguments):
        value = EstimateInput.model_validate(arguments, strict=True)
        return Action('shipping.estimate', self.version, value.model_dump(),
                      f'Estimate shipping for {value.units} units using the supplied rate. No purchase.', self.risk)

    async def execute(self, action, **context):
        if not await context['authorized']():
            raise TaskError('Execution authority revoked')
        value = EstimateInput.model_validate(action.arguments)
        return {'total_minor_units': value.units * value.rate_minor_units, 'currency': value.currency,
                'basis': 'Supplied rate only; no carrier quote, taxes, availability or booking verified.'}

    async def reconcile(self, action, **context):
        return None


def tools():
    return {'shipping.estimate': ShippingEstimate()}
