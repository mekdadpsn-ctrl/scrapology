# Contributing

Thank you for taking the time.

## Ground rules

- Scrapology reads what is openly served and stops at walls. A change that adds stealth,
  fingerprint masking, proxy rotation, CAPTCHA solving, a robots.txt override, a retry of a
  refusal, or reuse of a user's browser profile will not be merged, whatever the use case. That
  applies to engines added later and to plugins shipped in this repository.
- Keep the dependency tree small. New runtime dependencies need a reason in the pull request.
- Every route and every engine needs offline tests with small hand-written fixtures, including a
  test that it obeys robots.txt on redirects and navigations. Live tests are welcome too, marked
  `@pytest.mark.live`.
- Examples in docs, tests and fixtures stay neutral: public standards, public law, open-access
  science from general journals.

## Setup

```
git clone https://github.com/mekdadpsn-ctrl/scrapology
cd scrapology
pip install -e ".[dev,structured]"
python -m playwright install chromium
```

## Before opening a pull request

```
ruff check .
pytest                  # offline suite, includes a local 127.0.0.1 test server
pytest -m live          # optional, needs network access and a browser
```

Python 3.10 is the floor: no `tomllib`, no 3.11-only syntax.

## Writing style

Plain English in code comments, docs and messages. No em dashes (use a comma, a colon, a full
stop or brackets), no apologies, no hype, no emoji.

## Reporting a bug

Open an issue with the target, the command, the `sources.jsonl` line the run produced and the
output of `scrapology doctor`. Security issues go through the channel in `SECURITY.md`, not the
issue tracker.
