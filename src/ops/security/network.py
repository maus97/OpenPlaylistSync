"""Trusted network-boundary helpers for proxy-aware client identity."""

from ipaddress import ip_address, ip_network

from starlette.requests import Request

from ops.config import Settings


def client_address(request: Request, settings: Settings) -> str:
    """Honor forwarding headers only when the immediate peer is explicitly trusted."""

    peer = request.client.host if request.client else "unknown"
    try:
        peer_address = ip_address(peer)
    except ValueError:
        return peer
    trusted = []
    for value in settings.trusted_proxy_ips.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            trusted.append(ip_network(value, strict=False))
        except ValueError:
            continue
    if not any(peer_address in network for network in trusted):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    try:
        return str(ip_address(forwarded))
    except ValueError:
        return peer
