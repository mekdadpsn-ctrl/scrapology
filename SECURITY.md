# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on the repository:
https://github.com/mekdadpsn-ctrl/scrapology/security/advisories/new

Please do not open a public issue for a security problem. Include the version, the target and
command that show the problem, and what you expected. You will get an acknowledgement within a
week and a fix or a decision as soon as one is ready.

## Scope notes

- Scrapology executes no code from fetched pages outside the browser sandbox Playwright provides,
  in a fresh context with service workers blocked. Allowed subresources are fetched by the
  interceptor and handed to the browser, so the browser's own cross-origin protections are
  preserved by refusing what they would have stopped: cross-origin fetch, XHR and EventSource
  calls, non-GET requests made by pages, and requests from public pages to private, loopback,
  link-local or unspecified addresses (checked before any connection, including robots.txt, is
  made). XML and HTML are parsed with entity expansion, DTD loading and network access switched off.
- The Unpaywall email address is sent to api.unpaywall.org only and never reaches an output file,
  a receipt or an error message.
- Output files are written exactly where `--out` points, under validated names that cannot leave
  that folder. Do not point `--out` at a folder whose contents are served or executed.
- Responses are capped at 50 MB by default and redirects at 5 hops, http and https only; every
  redirect, every browser navigation and every request a rendered page makes (in any frame or
  page, on any host, redirect hops included) is checked against robots.txt before it is sent, and
  refused ones are aborted. WebSockets are never connected. Known limit: the private-network check
  resolves a host name once per render, so a DNS rebinding between that lookup and the browser's
  connection is not caught.
- Plugins (the `scrapology.commands` entry-point group) are ordinary installed packages and run with
  the user's rights; `python -m scrapology` puts the current folder on `sys.path` like any Python
  program. Install only plugins you trust; the maintainers do not review them.
