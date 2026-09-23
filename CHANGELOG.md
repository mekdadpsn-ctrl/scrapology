# Changelog

All notable changes to Scrapology are recorded here. The format follows Keep a Changelog and the
project uses semantic versioning.

## 0.1.0 (2026-09-23)

First release.

- `scrapology fetch`: one target or a batch file; web pages through a plain GET first and a
  Playwright browser only when the page needs it; robots.txt always obeyed with RFC 9309 matching
  (`*`, `$`, longest match, Allow wins ties) on the first URL and on every redirect hop and browser
  navigation; per-host delay for every page request; block detection that stops at challenge pages
  and at 401/403/429/503 without retrying; http and https only; at most 5 redirect hops; bodies
  capped at 50 MB (`--max-bytes`).
- Browser engine: a stock headless Chromium, Chrome or Edge in a fresh context with service
  workers blocked; every request of every frame and page, navigation or subresource, on any host,
  is checked against robots.txt and the http/https rule before it is sent, redirect hops included
  (the interceptor fetches the hops itself), and the browser's cross-origin protections are
  preserved: every cross-origin request in CORS mode (fetch, XHR, EventSource, module scripts,
  fonts, preloads, `crossorigin` resources), CORS-mode requests from opaque origins, non-GET page
  requests and private-network requests from public pages (IPv4-embedding IPv6 forms included) are
  refused; a refusal after the page has loaded keeps the page and reports a warning listing the
  refused URLs by reason; popups are never followed;
  WebSockets are refused.
- Official routes: Europe PMC full text for PMC ids and PubMed Central URLs; Europe PMC and
  Unpaywall for DOIs (Wiley and doi.org URLs included); EU CELLAR for CELEX numbers, EUR-Lex
  CELEX URLs and ELI links. Hosts matched on the parsed hostname.
- Outputs: `.raw.*` (byte-exact), `.rendered.html` (browser DOM), `.main.md`, optional
  `.structured.json`, and one JSON line per fetch in `sources.jsonl` with the access time,
  redirects, versions, outcome and the sha256 of the raw and rendered files. Output names are
  validated, kept inside the output folder, and never overwrite an earlier fetch's files.
- `scrapology search` (Europe PMC), `scrapology cite` (numbered source list) and
  `scrapology doctor` (environment, browsers, plugins, Europe PMC, CELLAR, a test fetch).
- Plugin subcommands through the `scrapology.commands` entry-point group (a plugin can never take
  the CLI down) and a public `scrapology.record()` helper for plugin receipts that validates names,
  extra fields and file locations.
- Library API: `scrapology.fetch()`, `scrapology.search()`, `scrapology.record()`.
- Exit codes 0 fetched, 1 failed, 2 usage, 3 blocked, 130 interrupted.
- Crawl4AI is not used: its browser launcher forces automation-hiding and certificate-ignoring
  flags that cannot be switched off.
