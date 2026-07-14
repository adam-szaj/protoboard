# SPDX-License-Identifier: MIT
"""What text a component prints on its body.

One policy point: resistors/inductors/capacitors print their coded marking
(``22k`` → ``223``); every other class prints its raw ``value`` (the part
number for a concrete part like ``BC547``).  An unset value (``""`` / ``"?"``)
prints nothing.  Pure — no Qt — so the policy is testable headless.
"""

from __future__ import annotations

from protoboard.gui.renderers.value_codes import parse_value, smd_code


def body_print_text(component_class: str, value: str) -> str | None:
    """The string to print on the body, or ``None`` to draw nothing."""
    text = value.strip()
    if not text or text == "?":
        return None
    decoded = parse_value(component_class, text)
    if decoded is not None:
        code = smd_code(decoded)
        if code is not None:
            return code
        # A coded class whose value did not fit a code still prints the raw
        # string rather than nothing (better a legible value than a blank body).
    return text
