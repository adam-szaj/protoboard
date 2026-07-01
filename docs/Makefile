# Makefile — render the Markdown/text docs to standalone HTML with pandoc.
#
#   make          # build HTML into _html/ (default)
#   make open     # build, then open the index in a browser
#   make clean     # remove _html/
#
# Requires `pandoc` on PATH (https://pandoc.org/installing.html). Each source
# file becomes a self-contained .html mirroring the docs/ tree; intra-doc
# .md/.txt links are rewritten to .html (md2html-links.lua) so the output is
# browsable, and images are copied alongside. No project dependency is added —
# this uses the system pandoc.

PANDOC      ?= pandoc
OUT         ?= _html
LINK_FILTER := md2html-links.lua

# Source docs (this Makefile and the Lua filter live in docs/ but are not docs).
# The .txt state-machine spec is rendered too.
SOURCES := $(shell find . -type f \( -name '*.md' -o -name '*.txt' \) \
             -not -path './$(OUT)/*' | sed 's|^\./||')
# Images to copy verbatim into the output tree.
ASSETS  := $(shell find . -type f \( -iname '*.png' -o -iname '*.jpg' \
             -o -iname '*.jpeg' -o -iname '*.svg' -o -iname '*.gif' \) \
             -not -path './$(OUT)/*' | sed 's|^\./||')

HTML   := $(addprefix $(OUT)/,$(addsuffix .html,$(basename $(SOURCES))))
COPIED := $(addprefix $(OUT)/,$(ASSETS))

# Standalone page, GitHub-flavoured input, a floating table of contents, and
# syntax highlighting. The per-page --css (with the right ../ depth to reach the
# shared stylesheet) is appended in the build rules below.
PANDOC_FLAGS := --standalone --from=gfm --to=html5 \
                --toc --toc-depth=3 \
                --highlight-style=tango \
                --metadata=lang:en \
                --lua-filter=$(LINK_FILTER)

# css_rel / nav_rel: relative paths from a page's stem back to the shared
# style.css and the landing index.html. Each directory segment of the stem
# becomes '..' (top-level → style.css / index.html, archive/x → ../style.css /
# ../index.html). The backlink is injected below via --include-before-body.
css_rel = $(shell echo '$*' | sed 's|[^/]*/|../|g; s|[^/]*$$|style.css|')
nav_rel = $(shell echo '$*' | sed 's|[^/]*/|../|g; s|[^/]*$$|index.html|')
# Backlink banner prepended to every page (a sub-page → main-TOC "up" link).
backlink = <nav class="docnav"><a href="$(nav_rel)">&#8617; Docs index</a></nav>

.PHONY: all open clean check
.DEFAULT_GOAL := all

all: check $(OUT)/style.css $(HTML) $(COPIED) $(OUT)/index.html
	@echo "Docs HTML written to $(OUT)/ — open $(OUT)/index.html"

# Guard: fail early with an install hint if pandoc is missing.
check:
	@command -v $(PANDOC) >/dev/null 2>&1 || { \
	  echo "error: '$(PANDOC)' not found on PATH."; \
	  echo "  install it from https://pandoc.org/installing.html"; \
	  echo "  (Debian/Ubuntu: sudo apt install pandoc)"; \
	  exit 1; }

# One rule per source extension. $* is the stem (path without extension), so the
# relative depth to style.css is derived by turning each dir segment into '..'.
$(OUT)/%.html: %.md $(LINK_FILTER) style.css
	@mkdir -p $(dir $@)
	printf '%s\n' '$(backlink)' | $(PANDOC) $(PANDOC_FLAGS) --css=$(css_rel) \
	  --include-before-body=/dev/stdin \
	  --metadata=title:"$(notdir $*)" \
	  -o $@ $<

$(OUT)/%.html: %.txt $(LINK_FILTER) style.css
	@mkdir -p $(dir $@)
	printf '%s\n' '$(backlink)' | $(PANDOC) $(PANDOC_FLAGS) --css=$(css_rel) \
	  --include-before-body=/dev/stdin \
	  --metadata=title:"$(notdir $*)" \
	  -o $@ $<

# Copy image assets verbatim.
$(OUT)/%: %
	@mkdir -p $(dir $@)
	cp $< $@

# A generated landing page linking every rendered doc (grouped by directory).
$(OUT)/index.html: $(HTML) gen-index.sh
	@sh gen-index.sh $(OUT) $(SOURCES) > $@
	@echo "generated $@"

# Ship a small default stylesheet if the repo doesn't provide one.
$(OUT)/style.css: style.css
	@mkdir -p $(OUT)
	cp $< $@

# Provide a minimal stylesheet the first time (kept in-tree, editable).
style.css:
	@printf '%s\n' \
	  'body{max-width:52rem;margin:2rem auto;padding:0 1rem;line-height:1.55;' \
	  '  font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1a1a1a}' \
	  'a{color:#0b6bcb}code,pre{font-family:ui-monospace,Menlo,monospace}' \
	  'pre{background:#f6f8fa;padding:.8rem;border-radius:6px;overflow:auto}' \
	  'code{background:#f6f8fa;padding:.1rem .3rem;border-radius:4px}' \
	  'pre code{background:none;padding:0}' \
	  'table{border-collapse:collapse}th,td{border:1px solid #d0d7de;padding:.4rem .6rem}' \
	  'h1,h2,h3{line-height:1.25}#TOC{font-size:.9rem}' \
	  'blockquote{border-left:3px solid #d0d7de;margin:0;padding-left:1rem;color:#555}' \
	  '.docnav{font-size:.85rem;margin-bottom:1.5rem;padding-bottom:.5rem;' \
	  '  border-bottom:1px solid #eaecef}.docnav a{text-decoration:none}' \
	  > $@

open: all
	@xdg-open $(OUT)/index.html >/dev/null 2>&1 || open $(OUT)/index.html 2>/dev/null \
	  || echo "open $(OUT)/index.html manually"

clean:
	rm -rf $(OUT)
