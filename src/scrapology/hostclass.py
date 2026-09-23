"""Which kind of network a host is on: public, loopback, private, link-local, or unspecified.

Used by the browser engine's private-network rule: a page on a public address may not make the
browser reach loopback, private (RFC 1918), CGNAT (100.64.0.0/10), unique-local IPv6, link-local
(169.254.0.0/16, which includes cloud metadata services, and fe80::/10) or unspecified addresses. A
page that is itself on such an address (the user asked for it directly) may reach the same class of
network and the public internet.

Resolution uses `socket.getaddrinfo` and is cached per host for one render. A DNS rebinding race
remains possible between this check and the browser's own connection: a name that resolves to a
public address here can be re-pointed at a private one before the browser connects.

IPv6 addresses that embed an IPv4 address (mapped, compatible, translated, NAT64, 6to4, Teredo)
take the stricter of their own class and the embedded address's class.
"""

import ipaddress
import socket
from collections.abc import Callable

PUBLIC = "public"
LOOPBACK = "loopback"
PRIVATE = "private"
LINK_LOCAL = "link-local"
UNSPECIFIED = "unspecified"
UNRESOLVABLE = "unresolvable"

_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# The four forms whose embedded IPv4 address sits in the low 32 bits. IPv4-compatible and
# IPv4-translated are distinct from ipaddress's own `ipv4_mapped` prefix (::ffff:0:0/96); NAT64
# well-known and local-use are distinct from each other and from the two above.
_COMPAT = ipaddress.ip_network("::/96")
_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_UNSPECIFIED_V6 = ipaddress.IPv6Address("::")
_LOOPBACK_V6 = ipaddress.IPv6Address("::1")

Classifier = Callable[[str], str]


def embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address `ip` embeds: mapped, IPv4-compatible, IPv4-translated, NAT64 (well-known
    and local-use), 6to4, and Teredo's server and client. Empty when it embeds none. `::` and `::1`
    fall inside the IPv4-compatible prefix by bit pattern alone but carry their own meaning
    (unspecified, loopback), so they are excluded here, not only in `classify_address`."""
    found: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip in _COMPAT and ip not in (_UNSPECIFIED_V6, _LOOPBACK_V6):
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip in _TRANSLATED:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip in _NAT64:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip in _NAT64_LOCAL:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)  # (server, client)
    return found


def classify_address(address: str) -> str:
    """The class of one IP address literal."""
    ip = ipaddress.ip_address(address)
    if ip.is_unspecified:
        return UNSPECIFIED
    if ip.is_loopback:
        return LOOPBACK
    if ip.is_link_local:
        return LINK_LOCAL
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return PRIVATE
    if isinstance(ip, ipaddress.IPv6Address):
        # `is_private` counts 6to4 (2002::/16) and Teredo (2001::/32) as private, so those forms are refused even
        # when the address they embed is public. Both mechanisms are deprecated and the direction is the safe one.
        own = PRIVATE if ip.is_private else PUBLIC
        classes = {own} | {classify_address(str(embedded)) for embedded in embedded_ipv4(ip)}
        return _strictest(classes)
    if ip.is_private:  # RFC 1918, unique-local IPv6 (fc00::/7) and the other reserved ranges
        return PRIVATE
    return PUBLIC


def _strictest(classes: set[str]) -> str:
    """A host that resolves to several addresses takes its least public class."""
    for candidate in (UNSPECIFIED, LINK_LOCAL, LOOPBACK, PRIVATE):
        if candidate in classes:
            return candidate
    return PUBLIC


def classify_host(host: str) -> str:
    """The class of a host name or address literal. `localhost` and `*.localhost` are loopback without
    a lookup. A name that does not resolve is `unresolvable`, which callers treat as not public."""
    name = host.strip().strip("[]").lower().rstrip(".")
    if not name:
        return UNRESOLVABLE
    if name == "localhost" or name.endswith(".localhost"):
        return LOOPBACK
    try:
        return classify_address(name)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(name, None)
    except (socket.gaierror, UnicodeError, OSError):
        return UNRESOLVABLE
    classes = set()
    for info in infos:
        address = info[4][0]
        if "%" in address:  # scoped IPv6 literal
            address = address.split("%", 1)[0]
        try:
            classes.add(classify_address(address))
        except ValueError:
            continue
    return _strictest(classes) if classes else UNRESOLVABLE


def may_reach(page_class: str, target_class: str) -> bool:
    """A public page reaches only public hosts; a non-public page reaches its own class and public hosts."""
    if target_class in (UNSPECIFIED, UNRESOLVABLE):
        return False
    if target_class == PUBLIC:
        return True
    return page_class == target_class


class CachedClassifier:
    """Per-render cache in front of a classifier, so each host is resolved once."""

    def __init__(self, classifier: Classifier | None = None) -> None:
        self._classifier = classifier or classify_host
        self._cache: dict[str, str] = {}

    def __call__(self, host: str) -> str:
        key = host.strip().lower()
        if key not in self._cache:
            self._cache[key] = self._classifier(host)
        return self._cache[key]
