"""The visitor's IP address, read the same way everywhere (rate limits, login lockout, internal traffic).

`X-Forwarded-For` is a list the client can start itself: a proxy that appends to it (rather than replacing it) leaves
whatever the client sent at the front. Trusting the first entry therefore lets anyone pick their own IP, which
defeats per-IP limits and can mark a visitor as office traffic. Which entry is real depends on the proxies in front of
this service, so it is configured, and GET /api/admin/ops/client-ip shows the live headers to choose from:

- CLIENT_IP_HEADER: a header a trusted edge proxy always overwrites (e.g. cloudfront-viewer-address on AWS CloudFront,
  cf-connecting-ip, true-client-ip). A trailing :port is removed.
  Used when present. Only set this when every request passes through that proxy, or clients can forge it too.
- TRUSTED_PROXY_HOPS: how many entries at the right of X-Forwarded-For were added by your own proxies. The entry
  just left of those is the client: 1 means the last entry. Anything the client prepended is ignored.
- Neither set: the first X-Forwarded-For entry, as before (spoofable; kept so nothing changes until configured).
"""
from __future__ import annotations

import os
from typing import List, Optional

from fastapi import Request


def _hops() -> Optional[int]:
    raw = (os.environ.get("TRUSTED_PROXY_HOPS") or "").strip()
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n >= 1 else None


def _without_port(value: str) -> str:
    """CloudFront-Viewer-Address is "ip:port" ("198.51.100.7:46532", "2001:db8::1:46532"); others may be "[v6]:port"."""
    import ipaddress
    if not value:
        return value
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    try:
        ipaddress.ip_address(value)
        return value  # already a bare address (an IPv6 address has colons of its own)
    except ValueError:
        pass
    head, _, tail = value.rpartition(":")
    if head and tail.isdigit():
        try:
            ipaddress.ip_address(head)
            return head
        except ValueError:
            return value
    return value


def forwarded_chain(request: Request) -> List[str]:
    # Several X-Forwarded-For headers are one list, in order.
    parts: List[str] = []
    for value in request.headers.getlist("x-forwarded-for"):
        parts.extend(p.strip() for p in value.split(",") if p.strip())
    return parts


def _peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def client_ip(request: Request) -> str:
    header = (os.environ.get("CLIENT_IP_HEADER") or "").strip().lower()
    if header:
        value = (request.headers.get(header) or "").split(",")[0].strip()
        # CloudFront always appends the port, and "2001:db8::1:443" would otherwise read as a valid IPv6 address.
        value = value.rpartition(":")[0] if header == "cloudfront-viewer-address" and ":" in value else _without_port(value)
        if value:
            return value
    chain = forwarded_chain(request)
    if not chain:
        return _peer(request)
    hops = _hops()
    if hops is None:
        return chain[0]
    # Fewer entries than proxies configured means the request skipped one of them: use the nearest address we have.
    return chain[-hops] if len(chain) >= hops else chain[0]


def describe(request: Request) -> dict:
    """What this request's address headers look like, and what each setting would pick (for the admin check)."""
    chain = forwarded_chain(request)
    return {
        "resolved": client_ip(request),
        "config": {"CLIENT_IP_HEADER": os.environ.get("CLIENT_IP_HEADER") or None,
                   "TRUSTED_PROXY_HOPS": os.environ.get("TRUSTED_PROXY_HOPS") or None},
        "peer": _peer(request),
        "x_forwarded_for": chain,
        "candidate_headers": {h: request.headers.get(h) for h in
                              ("cloudfront-viewer-address", "cf-connecting-ip", "true-client-ip", "x-real-ip", "x-client-ip", "fly-client-ip")
                              if request.headers.get(h)},
        "by_hops": {str(n): chain[-n] for n in range(1, len(chain) + 1)},
    }
