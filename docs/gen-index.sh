#!/bin/sh
# gen-index.sh OUT SOURCE... — emit an index.html linking every rendered doc,
# grouped by directory. Invoked by the docs Makefile; not meant to be run alone.
set -eu

out=$1
shift

cat <<'HEAD'
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Documentation index</title>
<link rel="stylesheet" href="style.css">
</head><body>
<h1>Documentation</h1>
HEAD

# Group by directory. Prefix each path with its group key ("." for top level,
# sorted last so it heads the list), sort by that key, then emit grouped <ul>s.
printf '%s\n' "$@" | awk '
  { d = $0; if (!sub("/[^/]*$", "", d)) d = "."; print d "\t" $0 }
' | LC_ALL=C sort | awk -F'\t' '
  $1 != cur {
    if (cur != "") print "</ul>"
    cur = $1
    print "<h2>" (cur == "." ? "Top level" : cur "/") "</h2>"
    print "<ul>"
  }
  {
    html = $2; sub(/\.(md|txt)$/, ".html", html)
    label = $2; sub(/.*\//, "", label); sub(/\.(md|txt)$/, "", label)
    print "<li><a href=\"" html "\">" label "</a></li>"
  }
  END { if (cur != "") print "</ul>" }
'

echo '</body></html>'
