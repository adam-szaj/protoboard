#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Render every Markdown doc that contains a ```mermaid``` block into a
self-viewable HTML page under ``docs/_html/``.

The pages are thin shells that link the committed shared base in
``docs/shared/`` — ``style.css`` (look), ``viewer.js`` (live render + theme +
pan) and ``mermaid.min.js`` (the library) — so the generated ``_html/`` stays
small and the styling is versioned. Prose around the diagrams is converted by a
tiny built-in Markdown subset — no pip dependencies, runs with a bare
Python 3.11+.

Usage:
    python docs/build_html.py            # scan docs/**/*.md, write docs/_html/
    python docs/build_html.py --serve    # build, then serve on localhost:8000

Point it at a vendored mermaid with --mermaid PATH, or let it try common spots
(node_modules, npm global). Re-run after editing any .md.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent
REPO = DOCS.parent
OUT = DOCS / "_html"
SHARED = DOCS / "shared"  # committed base: style.css, viewer.js, mermaid.min.js


# --------------------------------------------------------------------------- #
# Tiny Markdown → HTML (headings, para, lists, tables, blockquote, code,
# inline code/bold/links). Fenced ```mermaid``` and ```lang``` handled by the
# block splitter below, never by this inline pass.
# --------------------------------------------------------------------------- #
def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    head, body = cells[0], cells[2:]  # row 1 is the --- separator
    out = ["<table><thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def md_to_html(md: str) -> str:
    """Convert a Markdown doc (mermaid fences preserved) to an HTML fragment."""
    parts: list[str] = []
    lines = md.split("\n")
    i, n = 0, len(lines)
    para: list[str] = []
    listbuf: list[str] = []
    listkind: str | None = None

    def flush_para() -> None:
        if para:
            parts.append("<p>" + _inline(" ".join(para).strip()) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal listkind
        if listbuf:
            tag = "ol" if listkind == "ol" else "ul"
            parts.append(f"<{tag}>" + "".join(listbuf) + f"</{tag}>")
            listbuf.clear()
            listkind = None

    while i < n:
        line = lines[i]

        # fenced code block (mermaid or other)
        m = re.match(r"^```(\w*)\s*$", line)
        if m:
            flush_para()
            flush_list()
            lang = m.group(1)
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            body = "\n".join(buf)
            if lang == "mermaid":
                parts.append(
                    '<div class="stage"><pre class="mermaid">'
                    + html.escape(body)
                    + "</pre></div>"
                )
            else:
                parts.append(
                    f'<pre class="code"><code>{html.escape(body)}</code></pre>'
                )
            continue

        # table (a line with | and a following |---| separator)
        if (
            "|" in line
            and i + 1 < n
            and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1])
            and "-" in lines[i + 1]
        ):
            flush_para()
            flush_list()
            tb = [line]
            i += 1
            while i < n and "|" in lines[i]:
                tb.append(lines[i])
                i += 1
            parts.append(_table(tb))
            continue

        # heading
        hm = re.match(r"^(#{1,6})\s+(.*)$", line)
        if hm:
            flush_para()
            flush_list()
            lvl = len(hm.group(1))
            parts.append(f"<h{lvl}>{_inline(hm.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        # blockquote
        if line.startswith(">"):
            flush_para()
            flush_list()
            qb = []
            while i < n and lines[i].startswith(">"):
                qb.append(lines[i][1:].strip())
                i += 1
            parts.append("<blockquote>" + _inline(" ".join(qb)) + "</blockquote>")
            continue

        # list item
        lm = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
        if lm:
            flush_para()
            kind = "ol" if lm.group(2)[0].isdigit() else "ul"
            listkind = listkind or kind
            listbuf.append(f"<li>{_inline(lm.group(3))}</li>")
            i += 1
            continue

        # hr
        if re.match(r"^---+\s*$", line):
            flush_para()
            flush_list()
            parts.append("<hr>")
            i += 1
            continue

        # blank → paragraph break
        if not line.strip():
            flush_para()
            flush_list()
            i += 1
            continue

        # comment line <!-- ... -->
        if line.strip().startswith("<!--"):
            i += 1
            continue

        para.append(line.strip())
        i += 1

    flush_para()
    flush_list()
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Page shell (PCB palette, light/dark with live mermaid re-render).
# --------------------------------------------------------------------------- #
def page_html(
    title: str,
    body_html: str,
    nav_html: str,
    style_rel: str,
    mermaid_rel: str,
    viewer_rel: str,
) -> str:
    return (
        PAGE.replace("__TITLE__", html.escape(title))
        .replace("__BODY__", body_html)
        .replace("__NAV__", nav_html)
        .replace("__STYLE__", style_rel)
        .replace("__MERMAID__", mermaid_rel)
        .replace("__VIEWER__", viewer_rel)
    )


# The page is a thin shell: all styling lives in docs/shared/style.css and all
# behaviour in docs/shared/viewer.js, with mermaid.min.js vendored beside them.
# Only the per-page content (title, nav, body) is templated here.
PAGE = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    "<title>__TITLE__ · Protoboard Designer docs</title>\n"
    '<link rel="stylesheet" href="__STYLE__">\n'
    "</head>\n"
    "<body>\n"
    '<div class="wrap">\n'
    '  <aside class="rail">\n'
    '    <div class="brand"><div class="chip" aria-hidden="true"></div>\n'
    "      <div><strong>Protoboard&nbsp;Designer</strong><span>docs · live "
    "diagrams</span></div></div>\n"
    '    <nav class="nav">__NAV__</nav>\n'
    '    <div class="rail-foot">\n'
    '      <button id="themeBtn" type="button" aria-label="Toggle light / '
    'dark"><span class="dot"></span><span>Theme</span></button>\n'
    '      <p class="note">Diagrams drawn live by mermaid.js. Regenerate '
    "with <code>python docs/build_html.py</code>.</p>\n"
    "    </div>\n"
    "  </aside>\n"
    '  <main class="main">__BODY__</main>\n'
    "</div>\n"
    '<script src="__MERMAID__"></script>\n'
    '<script src="__VIEWER__"></script>\n'
    "</body>\n"
    "</html>\n"
)

INDEX_BODY = (
    '<div class="hero-eyebrow" '
    'style="font-size:11px;text-transform:uppercase;letter-spacing:.16em;'
    'color:var(--accent);margin-bottom:12px;">connectivity-aware · PySide6 · '
    "Qt-free domain</div>\n"
    "<h1>Documentation with live diagrams</h1>\n"
    "<p>Every Markdown doc under <code>docs/</code> that contains a "
    "Mermaid diagram,\n"
    "rendered live in the browser. Pick a page from the list. Regenerate "
    "after\n"
    "editing any <code>.md</code> with <code>python "
    "docs/build_html.py</code>.</p>\n"
    "__LIST__"
)


def find_mermaid_docs() -> list[Path]:
    docs = []
    for md in sorted(DOCS.rglob("*.md")):
        if OUT in md.parents:
            continue
        if "```mermaid" in md.read_text(encoding="utf-8"):
            docs.append(md)
    return docs


def locate_mermaid(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    candidates = [
        REPO / "node_modules/mermaid/dist/mermaid.min.js",
        Path.home()
        / ".claude/jobs/aa830040/tmp/node_modules/mermaid/dist/mermaid.min.js",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # npm global
    found = shutil.which("mmdc")
    if found:
        guess = Path(found).resolve().parent.parent / (
            "lib/node_modules/@mermaid-js/mermaid-cli/"
            "node_modules/mermaid/dist/mermaid.min.js"
        )
        if guess.is_file():
            return guess
    return None


def title_of(md: Path) -> str:
    for line in md.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#\s+(.*)$", line)
        if m:
            return m.group(1).strip()
    return md.stem


def rel_href(from_page: Path, to_page: Path) -> str:
    import os

    return os.path.relpath(to_page, from_page.parent).replace("\\", "/")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mermaid", help="path to mermaid.min.js to vendor")
    ap.add_argument(
        "--serve", action="store_true", help="serve _html on :8000 after build"
    )
    args = ap.parse_args()

    docs = find_mermaid_docs()
    if not docs:
        print("No .md files with ```mermaid``` under docs/. Nothing to build.")
        return 0

    # Ensure the committed shared base has mermaid.min.js. Vendor it there once
    # (from --mermaid or a discovered copy) if it's missing; otherwise reuse the
    # committed one — the pages link to docs/shared/, never a per-build copy.
    SHARED.mkdir(parents=True, exist_ok=True)
    shared_mm = SHARED / "mermaid.min.js"
    if not shared_mm.is_file():
        mm = locate_mermaid(args.mermaid)
        if mm is None:
            print(
                "ERROR: docs/shared/mermaid.min.js is missing and none could be\n"
                "found. Pass --mermaid PATH, or run `npm install mermaid` in the\n"
                "repo root, then re-run.",
                file=sys.stderr,
            )
            return 2
        shutil.copyfile(mm, shared_mm)
        print("  ·", "vendored", shared_mm.relative_to(REPO))
    for asset in ("style.css", "viewer.js"):
        if not (SHARED / asset).is_file():
            print(
                f"WARNING: docs/shared/{asset} is missing — pages will be unstyled.",
                file=sys.stderr,
            )

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # output path for each doc: mirror the docs/-relative path under _html/
    pages = []
    for md in docs:
        rel = md.relative_to(DOCS).with_suffix(".html")
        out_path = OUT / rel
        pages.append((md, out_path))

    # nav is the full list, links relative to each page
    def nav_for(current: Path) -> str:
        items = [f'<a href="{rel_href(current, OUT / "index.html")}">Index</a>']
        for md, out_path in pages:
            cls = ' class="here"' if out_path == current else ""
            items.append(
                f'<a{cls} href="{rel_href(current, out_path)}">'
                f"{html.escape(title_of(md))}</a>"
            )
        return "".join(items)

    def shared_links(page: Path) -> tuple[str, str, str]:
        return (
            rel_href(page, SHARED / "style.css"),
            rel_href(page, SHARED / "mermaid.min.js"),
            rel_href(page, SHARED / "viewer.js"),
        )

    for md, out_path in pages:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        body = md_to_html(md.read_text(encoding="utf-8"))
        css, mmjs, vjs = shared_links(out_path)
        out_path.write_text(
            page_html(title_of(md), body, nav_for(out_path), css, mmjs, vjs),
            encoding="utf-8",
        )
        print("  ✓", out_path.relative_to(REPO))

    # index
    lst = ["<ul>"]
    for md, out_path in pages:
        n = sum(1 for _ in re.finditer(r"```mermaid", md.read_text(encoding="utf-8")))
        lst.append(
            f'<li><a href="{rel_href(OUT/"index.html", out_path)}">'
            f"{html.escape(title_of(md))}</a> "
            f'<span style="color:var(--ink-soft)">— {n} diagram{"s" if n!=1 else ""}, '
            f"{html.escape(str(md.relative_to(REPO)))}</span></li>"
        )
    lst.append("</ul>")
    index_body = INDEX_BODY.replace("__LIST__", "".join(lst))
    idx = OUT / "index.html"
    css, mmjs, vjs = shared_links(idx)
    idx.write_text(
        page_html("Docs index", index_body, nav_for(idx), css, mmjs, vjs),
        encoding="utf-8",
    )
    print("  ✓", idx.relative_to(REPO))
    print(f"\nBuilt {len(pages)} page(s) + index into {OUT.relative_to(REPO)}/")
    print(f"Open: {OUT / 'index.html'}")

    if args.serve:
        import functools
        import http.server
        import os
        import socketserver

        os.chdir(OUT)
        h = functools.partial(http.server.SimpleHTTPRequestHandler)
        with socketserver.TCPServer(("127.0.0.1", 8000), h) as httpd:
            print("Serving on http://127.0.0.1:8000/  (Ctrl+C to stop)")
            httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
