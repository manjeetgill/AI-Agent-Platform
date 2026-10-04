"""A useful, side-effect-free first tool. Scores and weights must be supplied by the user."""
from pydantic import Field, model_validator

from .tasks import Action, Registry, Strict, TaskError


class Criterion(Strict):
    name: str = Field(min_length=1, max_length=80)
    weight: float = Field(gt=0, le=100, allow_inf_nan=False)


class Option(Strict):
    name: str = Field(min_length=1, max_length=80)
    scores: dict[str, float]


class ComparisonInput(Strict):
    criteria: list[Criterion] = Field(min_length=1, max_length=10)
    options: list[Option] = Field(min_length=2, max_length=10)

    @model_validator(mode='after')
    def consistent(self):
        names = {c.name for c in self.criteria}
        if len(names) != len(self.criteria) or len({o.name for o in self.options}) != len(self.options):
            raise ValueError('Names must be unique')
        for option in self.options:
            if set(option.scores) != names or any(not 0 <= score <= 10 for score in option.scores.values()):
                raise ValueError('Each option needs a score from 0 to 10 for every criterion; higher is better')
        return self


class RankedOption(Strict):
    name: str
    score: float


class ComparisonResult(Strict):
    ranking: list[RankedOption]
    explanation: str


class DecisionComparison:
    version = '1'
    risk = 'READ'
    timeout = 5
    description = ('Compare options using user-supplied criteria, positive weights and scores 0–10 '
                   '(higher is better). Never invent criteria, weights or scores; ask for missing values.')
    input_model = ComparisonInput
    output_model = ComparisonResult

    def prepare(self, arguments):
        value = self.input_model.model_validate(arguments, strict=True)
        return Action('decision.compare', self.version, value.model_dump(),
                      f'Compare {len(value.options)} options using {len(value.criteria)} weighted criteria.', self.risk)

    async def execute(self, action, **context):
        if not await context['authorized']():
            raise TaskError('Execution authority revoked')
        value = self.input_model.model_validate(action.arguments)
        weight = sum(c.weight for c in value.criteria)
        ranked = [RankedOption(name=o.name, score=round(sum(o.scores[c.name]*c.weight
                  for c in value.criteria)/weight, 6)) for o in value.options]
        ranked.sort(key=lambda option: (-option.score, option.name))
        return ComparisonResult(ranking=ranked, explanation=(
            'Weighted average of the supplied scores, on a 0–10 scale. Equal scores are ties; '
            'names only determine display order. Results depend on your inputs and are not independently verified.'
        )).model_dump()

    async def reconcile(self, action, **context):
        return None


def default_registry():
    from .tool_plugins import load_plugins
    from .weather_tool import WeatherForecast
    return load_plugins(Registry({'decision.compare': DecisionComparison(), 'weather.forecast': WeatherForecast()}))
