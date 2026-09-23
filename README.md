# Scrapology

Fetch web pages, open-access papers and EU law the polite way, with a receipt for every page.

## Why

Most scrapers do one of two things at a wall: push through it, or fail without saying why.
Scrapology does neither. It reads what is openly served, takes the official route where one exists
(Europe PMC for papers, EU CELLAR for legislation, Unpaywall for open copies of a DOI), stops at
walls and says so, and keeps a receipt for every page: the bytes as served, the extracted main text,
the URL, every redirect, the access time, the versions used and a hash.

It exists for research work where the source list matters as much as the text.

## What it will never do

- Read a page robots.txt closes to it. There is no override flag. Matching follows RFC 9309,
  including `*` and `$`, and every redirect hop, every browser navigation (main page, iframes,
  popups) and every request a rendered page makes (scripts, styles, images, XHR and fetch calls,
  prefetch links, beacons, on any host, redirects included) is checked, not only the first URL.
  WebSockets are refused.
- Bypass the browser's cross-origin protections. Every cross-origin request a page makes in CORS
  mode (fetch, XHR and EventSource calls, module scripts, web fonts, preloads and any resource
  requested with the `crossorigin` attribute), every request from an opaque origin (a sandboxed
  frame), non-GET requests made by pages (POST fetch, beacons, form POST auto-submits) and requests
  from public pages to private, loopback, link-local or unspecified addresses are refused. A
  cross-origin `fetch(..., {mode: "no-cors"})` is refused too, which is stricter than a browser: a
  browser sends it and hands the page a response it cannot read. Pages that depend on cross-origin
  API calls, module scripts or web fonts may render incompletely or with system fonts; that is the
  price of not bypassing those protections.
- Retry a refusal. A 401, 403, 429 or 503, or a challenge page, ends the fetch. It is never retried
  with a browser, another User-Agent, another address or a second request.
- Hide what it is. Every request carries `Scrapology/<version> (+repository URL)`, and the browser
  it opens is a stock headless Chromium, Chrome or Edge with no automation-hiding flags.
- Use stealth patches, fingerprint masking, proxy rotation or CAPTCHA solving. Crawl4AI is not used
  for that reason: its browser launcher forces automation-hiding and certificate-ignoring flags that
  cannot be switched off.
- Reuse your browser profile or cookies. The browser context is fresh, headless, and has service
  workers blocked.
- Bulk-download from Europe PMC. It fetches single open-access articles only.
- Hammer a host. One page request per host every 2 seconds by default, more if robots.txt asks
  (Crawl-delay, capped at 60 s). Subresources of a rendered page (scripts, images, styles) are
  loaded by the browser as part of that one page: each is checked against its host's robots.txt
  (one robots.txt request per host, cached) but not individually throttled.
- Follow a redirect to anything but http or https, or more than 5 hops. Follow a popup. Open a
  WebSocket.

## Install

There is no PyPI release yet. Install from the repository:

```
pip install "scrapology @ git+https://github.com/mekdadpsn-ctrl/scrapology"
python -m playwright install chromium
```

With the structured-data extra:

```
pip install "scrapology[structured] @ git+https://github.com/mekdadpsn-ctrl/scrapology"
```

The second command downloads Playwright's own Chromium. If it is missing (Playwright's installer
can delete builds that no other Playwright copy on the machine has claimed), Scrapology falls back
to the installed Google Chrome, then Microsoft Edge. `scrapology doctor` shows which one will run.

Extras: `structured` (extruct, for `--structured`). A PyPI release may follow. Python 3.10 or newer.

## Quick start

```
scrapology fetch https://www.rfc-editor.org/rfc/rfc9309.html
scrapology fetch pmc:PMC10663573
scrapology fetch doi:10.1038/s41598-023-47754-w        # add --email for the Unpaywall step
scrapology fetch celex:32016R0679
scrapology fetch https://eur-lex.europa.eu/eli/reg/2016/679/oj
scrapology fetch -i targets.txt -o research/sources
scrapology search "antimicrobial resistance surveillance" --n 5
scrapology cite research/sources
scrapology doctor
```

`fetch` writes into `./scrapology-out` unless `-o` says otherwise. Options: `--name` (base name of
the output files, default a slug of the target), `--engine auto|http|browser` (default `auto`),
`--timeout 60`, `--delay 2`, `--max-bytes 52428800` (50 MB), `--structured`, `--email`.

In batch mode (`-i targets.txt`, one target per line, `#` starts a comment) names are derived from
each target and the per-host delay holds across the whole batch.

## Routes

| Target | Route | What happens |
|---|---|---|
| `pmc:PMC123`, PubMed Central or Europe PMC article URL | Europe PMC | Open-access full text as JATS XML, converted to markdown. Not open access, or no full text: blocked. |
| `doi:10.x/...`, `https://doi.org/...`, Wiley URL with a DOI | Europe PMC, then Unpaywall | Open copy in Europe PMC if there is one. Otherwise Unpaywall (needs `--email` or `SCRAPOLOGY_EMAIL`): a PDF is saved as `.raw.pdf`, a landing page goes through the web route. Nothing open: blocked. |
| `celex:32016R0679`, EUR-Lex URL with a CELEX number or an ELI path | EU CELLAR | English XHTML from the Publications Office, converted to markdown with annex tables kept as rows. |
| Anything else | Web | robots.txt check, plain GET, browser render only when the page needs it (see below). |

Hosts are matched on the parsed hostname, never on substrings. Wiley Online Library, PubMed
Central and EUR-Lex refuse headless browsers. Scrapology never tries them directly: it uses the
official routes above, and a Wiley, EUR-Lex or PubMed Central URL without an identifier is reported
as blocked with the identifier to use instead.

The documented APIs (Europe PMC, CELLAR, Unpaywall) are called under their own terms and are not
gated on robots.txt; the per-host delay applies to them too. robots.txt governs every web resource:
pages, PDFs, Unpaywall landing pages, and every redirect, navigation or subresource request on the way.

### The web route in detail

1. robots.txt is fetched once per host and applied for the `Scrapology` agent token (falling back
   to `*`), with RFC 9309 matching: `*` and `$`, longest match wins, Allow wins a tie, percent
   encoding normalised, query string included. `Crawl-delay` is honoured up to 60 s. If robots.txt
   cannot be fetched: a 4xx other than 429 means there are no rules; a 429, a 5xx or a network
   error means the host is treated as closed for this run.
2. A plain HTTP GET. Redirects are followed by hand, at most 5 hops, http and https only, and each
   hop is checked against robots.txt before it is requested. A refused hop ends the fetch as
   blocked and nothing from the refused page is read.
3. A 401, 403, 429 or 503, or a challenge page (Cloudflare, AWS WAF, Incapsula, a CAPTCHA, "verify
   you are human"), is a block. The run stops there.
4. With `--engine auto`, a 200 whose main text is thin on a page that runs scripts is rendered in a
   headless browser (bundled Chromium, then Chrome, then Edge). The browser applies the same rules
   to every navigation of every frame and every page in its context: each navigation URL is checked
   against robots.txt, the scheme allow-list and the 5-hop cap before it is sent. Browsers follow
   server redirects internally, so the interceptor reads each hop and the final page itself first
   and only then lets the browser load the final URL (a redirected page is requested one more time
   than a plain browser would request it). A refusal during the first navigation ends the fetch as
   blocked with nothing kept. A refusal after the page has loaded (a meta refresh, a script
   navigation, an iframe, a popup) leaves the loaded page in place: it is kept, and the refused URL
   is reported as a warning. Popups are never followed. Every other request the page makes (scripts,
   styles, images, XHR and fetch calls, prefetch links, beacons, on any host) passes four checks
   before it is sent: only GET and HEAD; cross-origin requests in CORS mode are refused, which the
   browser marks with an Origin header (fetch, XHR and EventSource calls, module scripts, web fonts,
   preloads, anything with the `crossorigin` attribute), and so are CORS-mode requests from a
   sandboxed frame (same-origin ones and plain images, classic scripts and stylesheets are not);
   from a page on a public address, no request may reach a private, loopback, link-local or
   unspecified address; then that host's robots.txt and the http/https rule. Every redirect hop of
   such a request passes the same checks (the interceptor fetches the hops itself and hands the
   browser the final response). A refused request is aborted and listed in the warning by reason,
   so nothing a page may not have can reach `.rendered.html`. WebSockets are closed without
   connecting and listed in the warning. The final URL is checked again after load. Later
   navigations are throttled, each URL once; subresources are not throttled. A short static page is
   not rendered: it is fetched as is with a warning.
5. trafilatura extracts the main text as markdown.

One retry on a network error or on a 5xx other than 503, never on a 4xx. Bodies over `--max-bytes`
are abandoned and the fetch fails, in the plain GET and in the browser alike.

`--engine http` never opens a browser. `--engine browser` skips the plain GET.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Fetched. Warnings (a very short page, a refused later navigation, refused subresources) are allowed. |
| 1 | Failed: network error, timeout, HTTP error, oversized body, no usable browser. |
| 2 | Usage error: bad arguments, a bad `--name`, a missing extra. |
| 3 | Blocked or not permitted: robots.txt, refused redirect, challenge page, 401/403/429/503, no open copy. |
| 130 | Interrupted (Ctrl-C). |

Batch mode: 1 if any target failed, else 3 if any was blocked, else 0, with a summary line.

## Output files

For each fetch, in the output folder:

- `<name>.raw.html` (plain GET), `<name>.raw.xml` (Europe PMC), `<name>.raw.xhtml` (CELLAR) or
  `<name>.raw.pdf` (Unpaywall): byte-exact as served; its sha256 goes into the receipt as `sha256_raw`.
- `<name>.rendered.html` for pages read in a browser (`--engine browser`, or `auto` when it
  rendered): the DOM after rendering, not the bytes served, hashed as `sha256_rendered`.
- `<name>.main.md`: the main text as markdown, headed by a comment with the source, URL, access
  time (UTC), route and tool. Web pages carry a title, date, author and site line.
- `<name>.structured.json` with `--structured`: JSON-LD, microdata and OpenGraph via extruct.
- `sources.jsonl`: one JSON line per fetch, appended, also for blocked and failed ones.

Names: letters, digits, `.`, `_` and `-`, starting with a letter or digit, at most 120 characters,
no Windows device names, never outside the output folder. A name whose files already exist in the
folder (case-insensitively) is never overwritten: the new fetch gets `<name>-<8 hex of the target>`,
then `-2`, `-3`. A slug cut to 80 characters ends in that hash too.

`sources.jsonl` fields: `target`, `name`, `url`, `final_url`, `redirects`, `route`, `engine` (`http`,
`browser`, `api`), `browser` (which one rendered), `http_status`, `accessed_utc`, `tool`, `versions`
(scrapology, playwright, trafilatura, lxml, extruct), `outcome` (`fetched`, `blocked`, `failed`),
`note`, `warning`, `title`, `files`, `sha256_raw`, `sha256_rendered`, plus route extras such as
`pmcid`, `doi`, `celex`, `license` and `citation_url` (the DOI or EUR-Lex link to cite instead of the
API URL). URLs in notes never carry a query string.

`scrapology cite <folder>` turns the fetched lines into a numbered markdown source list, one line
per source (a page fetched twice is listed once), using `citation_url` where the route set one.

## Library use

```python
from scrapology import fetch, search

result = fetch("https://www.rfc-editor.org/rfc/rfc9309.html", out_dir="sources")
print(result.outcome, result.files, result.warning)

papers = search("antimicrobial resistance surveillance", n=5, oa_only=True)
for paper in papers:
    print(paper.pmcid, paper.year, paper.title)
```

`fetch(target, out_dir="./scrapology-out", name=None, structured=False, engine="auto", timeout=60,
delay=2.0, email=None, *, session=None, max_bytes=52428800)` returns a `FetchResult` with
`outcome`, `route`, `url`, `final_url`, `redirects`, `files`, `note`, `warning`, `http_status`,
`browser`, `engine`, `title`, `name` and `exit_code`. It raises `ValueError` for bad arguments (a bad
name, a missing extra, a bad engine) before any network work and never raises for a block or a
failure. Calls in one process share a per-host rate limiter and robots.txt cache.

`search(query, n=10, oa_only=False)` returns a list of `Paper` (pmcid, pmid, doi, title, authors,
journal, year, open_access, cited_by). Europe PMC query syntax works.

## Plugins

Other packages can add subcommands through the `scrapology.commands` entry-point group. Each entry
point names a callable that receives the argparse subparsers object, adds one subparser and sets a
handler:

```python
def register(subparsers):
    parser = subparsers.add_parser("hello", help="an example command")
    parser.add_argument("--code", type=int, default=0)
    parser.set_defaults(func=lambda args: args.code)  # returns the exit code (an int, or None for 0)
```

```toml
[project.entry-points."scrapology.commands"]
hello = "my_package.cli:register"
```

Plugins are loaded when the CLI starts; one that fails to import or register (whatever it raises)
prints a single warning line and the built-in commands keep working. A handler returns an int exit
code or None (taken as 0); one that raises ends with exit code 1 and one line on stderr.
`scrapology doctor` lists what loaded. A plugin that fetches something should write the same
receipt as the built-in routes with
`scrapology.record(out_dir, target=..., url=..., route=..., outcome=..., files={...}, ...)`, which
appends one line to `sources.jsonl` with the standard fields and computes `sha256_raw` and
`sha256_rendered` from the `raw` and `rendered` entries of `files` (which must exist); it validates
the name, refuses `extra` keys that collide with standard fields and refuses file paths outside
`out_dir`. Plugins are ordinary installed packages that run with your rights and are bound by the
same principles as the tool itself; the maintainers do not review or endorse them.

## Responsible use

Scrapology grants no rights. You remain responsible for the terms of every site you read, for
copyright in what you keep, and for the rate at which you read. The defaults are polite; keep them
unless a site's robots.txt or terms say otherwise. The Unpaywall email address is sent only to
Unpaywall, as its API requires, and never reaches an output file, a receipt or an error message.

## Limitations

- No PDF text extraction yet. An open PDF is saved, not converted.
- JavaScript-heavy sites need a browser, which is slower and needs a Chromium, Chrome or Edge.
- Sites that block automated readers stay blocked. That is the point.
- Only the English version of EU acts is fetched, and only acts CELLAR serves as XHTML (older acts
  may be PDF only).
- Europe PMC returns only what is open access.
- A rendered page is a snapshot of a DOM, not the bytes the server sent; `.rendered.html` says so.
- Pages that depend on cross-origin API calls (fetch, XHR), cross-origin module scripts or web
  fonts render without them; fonts fall back to the system's.
- The private-network rule resolves a host once per render, and the browser connects on its own
  afterwards, so a name re-pointed at a private address in between (DNS rebinding) is not caught.

## Licence

Apache License 2.0. See `LICENSE`.
