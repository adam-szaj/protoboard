-- Rewrite intra-doc links so the generated HTML is browsable:
--   foo.md            -> foo.html
--   sub/bar.md#anchor -> sub/bar.html#anchor
--   baz.txt           -> baz.html   (the .txt spec is rendered too)
-- Absolute URLs (http:, https:, mailto:, //) and pure #anchors are left alone.
-- Used by the docs Makefile via `pandoc --lua-filter`.

local function is_external(url)
  return url:match("^%a[%w+.-]*:") ~= nil  -- has a scheme (http:, mailto:, …)
      or url:match("^//") ~= nil            -- protocol-relative
end

local function rewrite(url)
  if url == "" or is_external(url) or url:sub(1, 1) == "#" then
    return url
  end
  -- Split off a trailing #anchor or ?query, rewrite only the path part.
  local path, tail = url:match("^([^#?]*)(.*)$")
  path = path:gsub("%.md$", ".html"):gsub("%.txt$", ".html")
  return path .. tail
end

function Link(el)
  el.target = rewrite(el.target)
  return el
end
