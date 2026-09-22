"""Scrapology: fetch web pages, open-access papers and EU law the polite way, with a receipt for every page.

    from scrapology import fetch, search
    result = fetch("https://example.com/", out_dir="out")
    papers = search("antimicrobial resistance surveillance", n=5)
"""

from scrapology._version import __version__
from scrapology.europepmc import Paper, search
from scrapology.fetch import FetchResult, fetch
from scrapology.provenance import record

__all__ = ["FetchResult", "Paper", "__version__", "fetch", "record", "search"]
