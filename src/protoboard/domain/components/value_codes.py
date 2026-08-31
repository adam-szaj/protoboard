# SPDX-License-Identifier: MIT
"""Component ``value`` strings → the codes printed/painted on real parts.

Pure helpers shared by the value-decoding renderers: the DIN colour-band
plugin (``library/renderers/resistor_bands.py``) and the SMD chip's digit
code (``SmdChipRenderer``).  Parsing is deliberately forgiving — anything
that does not parse returns ``None`` and the caller draws the plain body —
and pure Python (no Qt), so the decoding is testable headless.

Two decodings from one core:

* :func:`code_digits` — significant digits + multiplier exponent, the shared
  root of both markings (``"22k"`` → ``([2, 2], 3)``);
* :func:`smd_code` — the printed chip marking (``"22k"`` → ``"223"``,
  ``"4.7"`` → ``"4R7"``).
"""

from __future__ import annotations

import re

from protoboard.domain.components.definition import ComponentClass

__all__ = [
    "code_digits",
    "parse_capacitance",
    "parse_inductance",
    "parse_resistance",
    "parse_tolerance",
    "parse_value",
    "smd_code",
]


def _scaled_number(text: str, scales: dict[str, float]) -> float | None:
    """``"4.7k"`` / ``"4k7"`` / ``"470"`` → the number × its prefix scale.

    *scales* maps a (case-sensitive) prefix letter to its factor; ``""`` must
    be present for the bare-number case.  ``None`` for anything that does not
    parse.
    """
    s = text.strip()
    # Infix prefix as the decimal point: 4k7, 1R2, 4n7.
    m = re.fullmatch(r"(\d+)([A-Za-zµΩ])(\d*)", s)
    if m and m.group(2) in scales:
        frac = m.group(3) or "0"
        num = float(m.group(1)) + float(frac) / 10.0 ** len(frac)
        return num * scales[m.group(2)]
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([A-Za-zµΩ]?)", s)
    if not m:
        return None
    scale = scales.get(m.group(2))
    if scale is None:
        return None
    return float(m.group(1).replace(",", ".")) * scale


def parse_resistance(text: str) -> float | None:
    """A resistor ``value`` string → ohms (``None`` when it doesn't parse)."""
    s = text.strip()
    s = s.removesuffix("Ω").removesuffix("ohm").strip()
    return _scaled_number(
        s,
        {
            "m": 1e-3,
            "": 1.0,
            "R": 1.0,
            "r": 1.0,
            "k": 1e3,
            "M": 1e6,
            "G": 1e9,
        },
    )


def parse_inductance(text: str) -> float | None:
    """An inductor ``value`` string → µH (``None`` when it doesn't parse).

    A bare number means µH (the unit the inductance code is written in);
    ``H`` with no prefix is full henries.
    """
    s = text.strip()
    had_h = s.endswith(("H", "h"))
    if had_h:
        s = s[:-1].strip()
    scales = {"n": 1e-3, "u": 1.0, "m": 1e3, "": 1e6}
    scales[""] = 1e6 if had_h else 1.0
    return _scaled_number(s, scales)


def parse_capacitance(text: str) -> float | None:
    """A capacitor ``value`` string → picofarads (``None`` when unparseable).

    Capacitor markings are conventionally expressed in pF (the EIA 3-digit
    code), so pF is the unit that feeds ``code_digits`` / ``smd_code``.  A bare
    number means pF; ``p``/``n``/``u``/``µ``/``m`` scale up from there.
    """
    s = text.strip()
    s = s.removesuffix("F").removesuffix("f").strip()
    return _scaled_number(
        s,
        {
            "p": 1.0,
            "n": 1e3,
            "u": 1e6,
            "m": 1e9,
            "": 1e12,
        },
    )


def parse_tolerance(text: str) -> float | None:
    """A tolerance string → percent (``"5%"`` / ``"±5%"`` / ``"0.5"`` → 5.0 /
    5.0 / 0.5); ``None`` when it doesn't parse."""
    m = re.fullmatch(r"±?\s*(\d+(?:[.,]\d+)?)\s*%?", text.strip())
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def parse_value(component_class: str, text: str) -> float | None:
    """A part's ``value`` string → the number its marking encodes, or ``None``.

    The single policy for which component classes carry a decodable value
    code, and in which unit: a resistor decodes as ohms, an inductor as µH,
    a capacitor as pF; any other class (or a value that does not parse) has
    no code.
    """
    if component_class == ComponentClass.RESISTOR:
        return parse_resistance(text)
    if component_class == ComponentClass.INDUCTOR:
        return parse_inductance(text)
    if component_class in (
        ComponentClass.CAPACITOR_POLAR,
        ComponentClass.CAPACITOR_NONPOLAR,
    ):
        return parse_capacitance(text)
    return None


def code_digits(value: float) -> tuple[list[int], int] | None:
    """*value* → (significant digits, multiplier exponent), or ``None``.

    Two significant digits when the value expresses exactly as
    ``DD × 10^m`` (the E-series case), else three (``DDD × 10^m``); ``None``
    when neither fits — an odd value gets no code rather than a wrong one.
    The exponent spans −2 … 9 (the silver…white band range).
    """
    if value <= 0:
        return None
    for lo, hi in ((10, 99), (100, 999)):
        for mult in range(-2, 10):
            mant = value / 10.0**mult
            r = round(mant)
            if lo <= r <= hi and abs(mant - r) <= 1e-6 * max(1.0, mant):
                return [int(c) for c in str(r)], mult
    return None


def smd_code(value: float) -> str | None:
    """*value* → the digit code printed on an SMD chip, or ``None``.

    The standard marking: significant digits + the zero count (``22000`` →
    ``"223"``, ``4700`` → ``"472"``, ``12400`` → ``"1242"``); below ten the
    ``R``-as-decimal-point form (``4.7`` → ``"4R7"``, ``0.22`` → ``"R22"``).
    """
    code = code_digits(value)
    if code is None:
        return None
    digits, mult = code
    text = "".join(str(d) for d in digits)
    if mult >= 0:
        return f"{text}{mult}"
    # R sits at the decimal point: 4.7 → 4R7, 0.22 → R22, 4.75 → 4R75.
    point = len(digits) + mult
    return f"{text[:point]}R{text[point:]}"
