"""Tell a block from a short page.

A block is a challenge page (Cloudflare, AWS WAF, Incapsula, a CAPTCHA, "verify you are human") or
one of the HTTP statuses a site uses to refuse automated readers. A genuinely short page (a stub, a
landing page, example.com at about 170 characters) is not a block: it is a warning.
"""

import re

BLOCK_STATUSES = frozenset({401, 403, 429, 503})

BLOCK_MARKERS = re.compile(
    r"just a moment\.\.\.|cf-chl|challenge-platform|attention required! \| cloudflare|verify you are human|"
    r"captcha|access denied|request unsuccessful\. incapsula|awswaf|aws-waf-token|enable javascript and cookies|"
    r"unusual traffic|are you a robot|bot protection|pardon our interruption",
    re.I,
)

# Main text shorter than this after a plain HTTP read is "thin": the auto engine renders the page
# in a browser when the page also carries scripts (a static page cannot get richer by rendering).
THIN_CHARS = 500
# Main text shorter than this in the final result earns a warning.
SHORT_CHARS = 1000
# A challenge marker only counts on a small page. A long article that mentions a CAPTCHA is not a wall.
MARKER_MAX_TEXT = 5000


def challenge_marker(html: str, text: str) -> str | None:
    """The first challenge marker found in the head of the page, or None."""
    head = (html or "")[:20000] + "\n" + (text or "")[:5000]
    match = BLOCK_MARKERS.search(head)
    if match and len(text or "") < MARKER_MAX_TEXT:
        return match.group(0)
    return None


def block_reason(status: int | None, html: str, text: str) -> str | None:
    """Why this response is a block, or None when it is not."""
    if status in BLOCK_STATUSES:
        marker = challenge_marker(html, text)
        return f"HTTP {status}" + (f" with challenge marker {marker!r}" if marker else "")
    marker = challenge_marker(html, text)
    if marker:
        return f"challenge page (marker {marker!r})"
    return None


def is_thin(text: str) -> bool:
    return len((text or "").strip()) < THIN_CHARS


def has_scripts(html: str) -> bool:
    return "<script" in (html or "")[:200000].lower()


def needs_render(html: str, text: str) -> bool:
    """True when a plain HTTP read gave thin text and the page runs scripts that may build the content."""
    return is_thin(text) and has_scripts(html)


def short_page_warning(text: str) -> str | None:
    n = len((text or "").strip())
    if n < SHORT_CHARS:
        return f"short page ({n} chars of main text): check it is the real page, not a stub or a login wall"
    return None
