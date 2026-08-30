# SPDX-License-Identifier: MIT
"""Minimal S-expression parser for KiCAD netlist files.

Produces a tree of nested lists/strings from KiCAD S-expression syntax::

    (export (version D)
      (nets
        (net (code 1) (name GND) ...)))

Each atom (symbol or quoted string) is a ``str``.
Each list node is a Python ``list`` whose first element is the tag string and
remaining elements are child nodes (str or list).

Example::

    parse('(net (code 1) (name GND))')
    → ['net', ['code', '1'], ['name', 'GND']]
"""

from __future__ import annotations

import re

# A node is either a string atom or a list whose first element is the tag.
SNode = str | list["SNode"]


def parse(text: str) -> list[SNode]:
    """Parse *text* as a top-level S-expression and return its children.

    The outermost ``(export ...)`` wrapper is stripped so callers receive the
    root node's child list directly — i.e. the return value of
    ``parse(text)`` is equivalent to ``tokenize_and_build(text)[1:]``.
    """
    tokens = _tokenize(text)
    pos, node = _build(tokens, 0)
    if not isinstance(node, list):
        raise ValueError("Top-level S-expression must be a list")
    return node  # includes tag as first element


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"'  # quoted string
    r"|[()]"  # parens
    r"|[^\s()\"]+",  # unquoted atom
    re.DOTALL,
)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _build(tokens: list[str], pos: int) -> tuple[int, SNode]:
    """Recursively build a node starting at *pos*.  Returns (new_pos, node)."""
    tok = tokens[pos]
    if tok == "(":
        pos += 1
        children: list[SNode] = []
        while tokens[pos] != ")":
            pos, child = _build(tokens, pos)
            children.append(child)
        pos += 1  # consume ')'
        return pos, children
    elif tok == ")":
        raise ValueError(f"Unexpected ')' at token index {pos}")
    else:
        # Atom — strip surrounding quotes if present.
        if tok.startswith('"') and tok.endswith('"'):
            atom = tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            atom = tok
        return pos + 1, atom


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def child(node: list[SNode], tag: str) -> list[SNode] | None:
    """Return the first child list whose first element equals *tag*, or None."""
    for item in node[1:]:
        if isinstance(item, list) and item and item[0] == tag:
            return item
    return None


def children(node: list[SNode], tag: str) -> list[list[SNode]]:
    """Return all child lists whose first element equals *tag*."""
    return [
        item for item in node[1:] if isinstance(item, list) and item and item[0] == tag
    ]


def attr(node: list[SNode], tag: str) -> str | None:
    """Return the string value of the first child tagged *tag*, or None.

    For nodes like ``(name GND)`` returns ``"GND"``.
    """
    c = child(node, tag)
    if c is None:
        return None
    # Value is either a direct string child or the second element.
    for item in c[1:]:
        if isinstance(item, str):
            return item
    return None
