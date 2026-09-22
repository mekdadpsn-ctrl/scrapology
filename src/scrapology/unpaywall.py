"""Unpaywall: finds a legal open copy of a DOI when Europe PMC holds none.

Unpaywall requires an email address on every call (`--email` or the SCRAPOLOGY_EMAIL variable).
The address goes only to api.unpaywall.org and never reaches an output file, a receipt or an error
message: every URL in a message is the record URL without its query string. The API is called under
its own terms and is not gated on robots.txt; the per-host delay still applies.
API reference: https://unpaywall.org/products/api
"""

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from scrapology import transport
from scrapology.ratelimit import HostRateLimiter

UNPAYWALL = "https://api.unpaywall.org/v2/"


@dataclass
class OpenCopy:
    doi: str
    title: str | None
    is_oa: bool
    pdf_url: str | None
    landing_url: str | None
    host_type: str | None
    license: str | None
    version: str | None

    @property
    def any_url(self) -> str | None:
        return self.pdf_url or self.landing_url


def lookup_url(doi: str) -> str:
    """The record URL without the email: this is the form that goes into the receipt and into messages."""
    return UNPAYWALL + urllib.parse.quote(doi.strip(), safe="/")


_EMAIL_PARAM = re.compile(r"[?&]email=[^\s&:'\"]*")


def _scrub(message: str, email: str) -> str:
    """Belt and braces: even if a message somehow carried the address or the query, neither leaves this module."""
    cleaned = _EMAIL_PARAM.sub("", message)
    for form in (email, email.lower(), urllib.parse.quote(email, safe=""), urllib.parse.quote_plus(email)):
        cleaned = cleaned.replace(form, "<email>")
    return cleaned


def lookup(
    doi: str, email: str, *, timeout: float = 60, limiter: HostRateLimiter | None = None
) -> OpenCopy | None:
    """The Unpaywall record for a DOI, or None when Unpaywall does not know the DOI (HTTP 404).
    Any other non-2xx answer raises NetworkError."""
    if not email or not email.strip():
        raise ValueError("Unpaywall needs a contact email address")
    where = lookup_url(doi)
    url = where + "?" + urllib.parse.urlencode({"email": email})
    try:
        response = transport.get(url, accept="application/json", timeout=timeout, limiter=limiter)
    except transport.TransportError as err:
        detail = _scrub(str(err), email)
        raise transport.NetworkError(f"Unpaywall: {type(err).__name__} for {where}: {detail}") from None
    if response.status == 404:
        return None
    if not response.ok:
        raise transport.NetworkError(f"Unpaywall answered HTTP {response.status} for {where}")
    try:
        data = json.loads(response.body)
    except json.JSONDecodeError as err:
        raise transport.NetworkError(f"Unpaywall returned no JSON for {where}") from err
    return open_copy_from_record(data)


def open_copy_from_record(data: dict[str, Any]) -> OpenCopy:
    best: dict[str, Any] = data.get("best_oa_location") or {}
    return OpenCopy(
        doi=data.get("doi") or "",
        title=data.get("title") or None,
        is_oa=bool(data.get("is_oa")),
        pdf_url=best.get("url_for_pdf") or None,
        landing_url=best.get("url_for_landing_page") or best.get("url") or None,
        host_type=best.get("host_type") or None,
        license=best.get("license") or None,
        version=best.get("version") or None,
    )
