"""Example plugin. Copy to ~/.lotus/plugins/ and restart lotus.

Adds a "weather" tool pack (the model loads it when it needs it) and a /weather command."""
import json
import urllib.parse
import urllib.request

from lotus.tools import command, pack, tool

pack("weather", "current weather and forecasts")


@tool(pack="weather")
def weather(city: str, days: int = 1, _ctx=None):
    """Current weather and a short forecast for a city (no API key needed).
    city: city name, e.g. 'Boston'
    days: forecast days, 1-3"""
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "curl"}), timeout=15) as r:
        d = json.load(r)
    now = d["current_condition"][0]
    lines = [f"{city}: {now['weatherDesc'][0]['value']}, {now['temp_C']}°C (feels {now['FeelsLikeC']}°C), humidity {now['humidity']}%"]
    for day in d["weather"][:max(1, min(days, 3))]:
        lines.append(f"{day['date']}: {day['mintempC']}–{day['maxtempC']}°C")
    return "\n".join(lines)


@command("weather", "quick weather: /weather <city>")
def weather_cmd(agent, arg):
    return weather(arg or "Boston", _ctx=agent)
