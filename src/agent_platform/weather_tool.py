"""Read public weather from one fixed HTTPS endpoint; no arbitrary URLs or credentials."""
from datetime import UTC, datetime

import httpx
from pydantic import Field

from .tasks import Action, Strict, TaskError


class WeatherInput(Strict):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    days: int = Field(default=3, ge=1, le=7)


class ForecastDay(Strict):
    date: str
    minimum_c: float
    maximum_c: float
    precipitation_probability_percent: float


class WeatherResult(Strict):
    source: str
    fetched_at: str
    timezone: str
    days: list[ForecastDay]


class WeatherForecast:
    version, risk, timeout = '1', 'READ', 20
    description = ('Read an Open-Meteo weather forecast for user-supplied latitude and longitude. '
                   'Ask for coordinates if missing; do not infer the user location. No account or payment.')
    input_model, output_model = WeatherInput, WeatherResult

    def __init__(self, transport=None):
        self.transport = transport

    def prepare(self, arguments):
        value = WeatherInput.model_validate(arguments, strict=True)
        return Action('weather.forecast', '1', value.model_dump(),
            f'Get {value.days}-day public forecast for {value.latitude}, {value.longitude} from Open-Meteo.', 'READ')

    async def execute(self, action, **context):
        if not await context['authorized']():
            raise TaskError('Execution authority revoked')
        value = WeatherInput.model_validate(action.arguments)
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, transport=self.transport) as client:  # noqa: SIM117
            async with client.stream('GET', 'https://api.open-meteo.com/v1/forecast', params={
                'latitude': value.latitude, 'longitude': value.longitude, 'forecast_days': value.days,
                'daily': 'temperature_2m_max,temperature_2m_min,precipitation_probability_max', 'timezone': 'auto'
            }) as response:
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks)>100_000:
                        raise TaskError('Forecast response is too large')
        import json
        payload = json.loads(chunks)
        daily = payload['daily']
        days = [ForecastDay(date=day, minimum_c=daily['temperature_2m_min'][i],
            maximum_c=daily['temperature_2m_max'][i],
            precipitation_probability_percent=daily['precipitation_probability_max'][i])
            for i, day in enumerate(daily['time'])]
        if len(days) != value.days:
            raise TaskError('Forecast is incomplete')
        return WeatherResult(source='https://open-meteo.com/', fetched_at=datetime.now(UTC).isoformat(),
                             timezone=payload['timezone'], days=days).model_dump()

    async def reconcile(self, action, **context):
        return None
