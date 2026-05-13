"""
title: Unit Converter
description: >
    Convert between units of length, weight, temperature, and currency.
    Examples: "convert 5 miles to km", "100 USD to EUR",
    "350 F to C", "2.5 kg to lbs".
author: mdelponte
version: 1.0.0
license: MIT
requirements: httpx
"""

from typing import Optional, Awaitable, Callable
import httpx
from pydantic import BaseModel, Field


# ---------- conversion tables ----------
# All length units expressed as meters; weight as grams.
# Keys are normalized (lowercase, no trailing 's'); aliases below map onto them.

_LENGTH_TO_METERS = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "km": 1000.0,
    "in": 0.0254,
    "ft": 0.3048,
    "yd": 0.9144,
    "mi": 1609.344,
    "nmi": 1852.0,
}

_LENGTH_ALIASES = {
    "millimeter": "mm", "millimetre": "mm",
    "centimeter": "cm", "centimetre": "cm",
    "meter": "m", "metre": "m",
    "kilometer": "km", "kilometre": "km", "klick": "km",
    "inch": "in", "inche": "in", '"': "in",
    "foot": "ft", "feet": "ft", "'": "ft",
    "yard": "yd",
    "mile": "mi",
    "nauticalmile": "nmi", "nm": "nmi",
}

_WEIGHT_TO_GRAMS = {
    "mg": 0.001,
    "g": 1.0,
    "kg": 1000.0,
    "t": 1_000_000.0,             # metric tonne
    "oz": 28.349523125,
    "lb": 453.59237,
    "st": 6350.29318,             # stone
    "ton_us": 907184.74,          # short ton
    "ton_uk": 1016046.9088,       # long ton
}

_WEIGHT_ALIASES = {
    "milligram": "mg",
    "gram": "g", "gramme": "g",
    "kilogram": "kg", "kilo": "kg", "kilogramme": "kg",
    "tonne": "t", "metricton": "t",
    "ounce": "oz",
    "pound": "lb", "lbs": "lb", "#": "lb",
    "stone": "st",
    "shortton": "ton_us", "uston": "ton_us",
    "longton": "ton_uk", "ukton": "ton_uk", "imperialton": "ton_uk",
    "ton": "t",  # default plain "ton" to metric tonne; model can disambiguate
}

_TEMPERATURE_ALIASES = {
    "c": "c", "celsius": "c", "centigrade": "c", "°c": "c",
    "f": "f", "fahrenheit": "f", "°f": "f",
    "k": "k", "kelvin": "k",
    "r": "r", "rankine": "r", "°r": "r",
}


def _normalize(unit: str) -> str:
    """Lowercase, strip whitespace, strip trailing 's', strip degree marks."""
    u = unit.strip().lower().replace(" ", "")
    # Drop a trailing 's' (plurals) but preserve symbol units like "lbs" via alias map first.
    if u in _LENGTH_ALIASES or u in _WEIGHT_ALIASES or u in _TEMPERATURE_ALIASES:
        return u
    if u in _LENGTH_TO_METERS or u in _WEIGHT_TO_GRAMS:
        return u
    if u.endswith("s") and len(u) > 1:
        u = u[:-1]
    return u


def _resolve(unit: str):
    """Return (category, canonical_key) or (None, None) if unknown."""
    u = _normalize(unit)
    if u in _LENGTH_TO_METERS:
        return "length", u
    if u in _LENGTH_ALIASES:
        return "length", _LENGTH_ALIASES[u]
    if u in _WEIGHT_TO_GRAMS:
        return "weight", u
    if u in _WEIGHT_ALIASES:
        return "weight", _WEIGHT_ALIASES[u]
    if u in _TEMPERATURE_ALIASES:
        return "temperature", _TEMPERATURE_ALIASES[u]
    # Currency: 3-letter ISO code
    raw = unit.strip().upper()
    if len(raw) == 3 and raw.isalpha():
        return "currency", raw
    return None, None


def _temp_to_celsius(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return (value - 32.0) * 5.0 / 9.0
    if unit == "k":
        return value - 273.15
    if unit == "r":
        return (value - 491.67) * 5.0 / 9.0
    raise ValueError(f"Unknown temperature unit: {unit}")


def _celsius_to_temp(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return value * 9.0 / 5.0 + 32.0
    if unit == "k":
        return value + 273.15
    if unit == "r":
        return (value + 273.15) * 9.0 / 5.0
    raise ValueError(f"Unknown temperature unit: {unit}")


def _fmt(n: float) -> str:
    """Compact, readable number formatting."""
    if n == 0:
        return "0"
    a = abs(n)
    if a >= 1e12 or a < 1e-4:
        return f"{n:.6g}"
    if a >= 100:
        return f"{n:,.2f}".rstrip("0").rstrip(".")
    if a >= 1:
        return f"{n:,.4f}".rstrip("0").rstrip(".")
    return f"{n:.6g}"


class Tools:
    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        exchangerate_access_key: str = Field(
            "",
            description=(
                "Free access key from https://exchangerate.host (required for "
                "currency conversions). Length/weight/temperature work without it."
            ),
        )

    async def convert(
        self,
        value: float,
        from_unit: str,
        to_unit: str,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Convert a numeric value from one unit to another. Handles length, weight,
        temperature, and currency. Both units must belong to the same category.

        USE THIS when the user asks to convert between units, e.g.
        "5 miles to km", "100 USD in EUR", "350F to C", "2.5 kg in pounds".

        Supported units:
          - Length: mm, cm, m, km, in, ft, yd, mi, nmi (plus full names/plurals).
          - Weight: mg, g, kg, t (metric tonne), oz, lb, st (stone),
            ton_us (short ton), ton_uk (long ton).
          - Temperature: C, F, K, R (Celsius, Fahrenheit, Kelvin, Rankine).
          - Currency: any ISO 4217 3-letter code (USD, EUR, GBP, JPY, …).
            Requires the exchangerate_access_key valve to be set.

        :param value: The numeric quantity to convert.
        :param from_unit: Source unit (e.g. "miles", "kg", "F", "USD").
        :param to_unit: Target unit (e.g. "km", "lb", "C", "EUR").
        :return: A short plain-text string with the converted value.
        """
        from_cat, from_key = _resolve(from_unit)
        to_cat, to_key = _resolve(to_unit)

        if from_cat is None:
            return f"❌ Unknown unit: '{from_unit}'."
        if to_cat is None:
            return f"❌ Unknown unit: '{to_unit}'."
        if from_cat != to_cat:
            return (
                f"❌ Cannot convert {from_cat} to {to_cat} "
                f"('{from_unit}' → '{to_unit}')."
            )

        try:
            if from_cat == "length":
                result = value * _LENGTH_TO_METERS[from_key] / _LENGTH_TO_METERS[to_key]
            elif from_cat == "weight":
                result = value * _WEIGHT_TO_GRAMS[from_key] / _WEIGHT_TO_GRAMS[to_key]
            elif from_cat == "temperature":
                celsius = _temp_to_celsius(value, from_key)
                result = _celsius_to_temp(celsius, to_key)
            elif from_cat == "currency":
                if not self.valves.exchangerate_access_key:
                    return (
                        "❌ Currency conversion requires an exchangerate.host "
                        "access key. Set it in the tool's valves "
                        "(exchangerate_access_key)."
                    )
                result = await self._convert_currency(
                    value, from_key, to_key, __event_emitter__
                )
                if result is None:
                    return "❌ Currency conversion failed (see status)."
            else:
                return f"❌ Unsupported category: {from_cat}"
        except Exception as exc:
            return f"❌ Conversion error: {exc}"

        return f"{_fmt(value)} {from_unit} = {_fmt(result)} {to_unit}"

    async def _convert_currency(
        self,
        amount: float,
        from_code: str,
        to_code: str,
        emitter: Optional[Callable[[dict], Awaitable[None]]],
    ) -> Optional[float]:
        if emitter:
            await emitter({
                "type": "status",
                "data": {
                    "description": f"Fetching {from_code}→{to_code} rate…",
                    "done": False,
                },
            })

        url = "https://api.exchangerate.host/convert"
        params = {
            "access_key": self.valves.exchangerate_access_key,
            "from": from_code,
            "to": to_code,
            "amount": amount,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    url,
                    params=params,
                    headers={"User-Agent": "OpenWebUI-UnitConverter/1.0"},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            if emitter:
                await emitter({
                    "type": "status",
                    "data": {"description": f"API error: {exc}", "done": True},
                })
            return None

        if not data.get("success", False) or "result" not in data:
            err_info = data.get("error", {}).get("info") or "unknown error"
            if emitter:
                await emitter({
                    "type": "status",
                    "data": {"description": f"API error: {err_info}", "done": True},
                })
            return None

        if emitter:
            await emitter({
                "type": "status",
                "data": {"description": "Rate fetched ✓", "done": True, "hidden": True},
            })
        return float(data["result"])