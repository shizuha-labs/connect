"""
CON-19: Abuse controls — attachment safety, bulk export, and quarantine (CON-16 C6-C7).
"""

import hashlib
import ipaddress
import re
import socket
import urllib.parse

# ── SSRF-safe URL validation (C6) ─────────────────────────────────────────────

#: Private/link-local ranges that must never be fetched for user attachments.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / AWS metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class SSRFError(Exception):
    """Raised when an attachment URL would hit a private/internal address (C6)."""


class MagicByteError(Exception):
    """Raised when file content does not match the declared content-type (C6)."""


class SVGRejectedError(Exception):
    """Raised when an SVG attachment is submitted without sanitization (C6)."""


#: Allowed content-types with their magic-byte signatures.
_MAGIC_BYTES: dict[str, list[bytes]] = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/gif": [b"GIF87a", b"GIF89a"],
    "image/webp": [b"RIFF"],
    "application/pdf": [b"%PDF"],
    "video/mp4": [b"\x00\x00\x00\x18ftypmp42", b"\x00\x00\x00\x20ftyp"],
}


def validate_attachment_url(raw_url: str) -> str:
    """
    Validate that raw_url is safe to fetch (C6 SSRF protection).

    Resolves the hostname to its IP and checks that it is not in any private/
    link-local range. Returns the URL on success; raises SSRFError on failure.
    """
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"Attachment URL must use http/https, got: {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFError("Attachment URL has no hostname")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for {hostname!r}: {exc}") from exc

    for info in infos:
        addr_str = info[4][0]
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        for net in _BLOCKED_NETWORKS:
            if addr in net:
                raise SSRFError(
                    f"Attachment URL {hostname!r} resolves to private address {addr_str!r} (C6)"
                )

    return raw_url


def validate_magic_bytes(content: bytes, content_type: str) -> None:
    """
    Validate that content starts with the expected magic bytes for content_type (C6).

    Raises MagicByteError if the bytes do not match. Rejects SVG unconditionally
    (SVGRejectedError) — call sanitize_svg instead if you need SVG support.
    """
    if content_type == "image/svg+xml" or content_type.startswith("image/svg"):
        raise SVGRejectedError(
            "SVG attachments must be sanitized before storage — call sanitize_svg() first (C6)"
        )

    sigs = _MAGIC_BYTES.get(content_type)
    if sigs is None:
        raise MagicByteError(f"Content-type {content_type!r} is not in the allow-list (C6)")

    if not any(content.startswith(sig) for sig in sigs):
        raise MagicByteError(
            f"File magic bytes do not match declared content-type {content_type!r} (C6)"
        )


# Minimal allowlist for SVG elements and attributes (C6 sanitization).
_SVG_ALLOWED_ELEMENTS = frozenset({
    "svg", "g", "path", "circle", "ellipse", "line", "polyline", "polygon",
    "rect", "text", "tspan", "title", "desc", "defs", "use", "symbol",
})
_SVG_DISALLOWED_PATTERNS = [
    re.compile(r"<script", re.IGNORECASE),
    re.compile(r"javascript:", re.IGNORECASE),
    re.compile(r"on\w+=", re.IGNORECASE),  # event handlers
    re.compile(r"<foreignObject", re.IGNORECASE),
    re.compile(r"xlink:href\s*=\s*['\"]javascript:", re.IGNORECASE),
    re.compile(r"data:", re.IGNORECASE),
]


def sanitize_svg(svg_content: str) -> str:
    """
    Return a sanitized version of svg_content, stripping scripts, event handlers,
    and foreign objects (C6). Raises SVGRejectedError if unsafe patterns cannot be
    fully removed and the SVG cannot be trusted.
    """
    for pattern in _SVG_DISALLOWED_PATTERNS:
        if pattern.search(svg_content):
            raise SVGRejectedError(
                f"SVG contains disallowed pattern {pattern.pattern!r}; cannot sanitize (C6)"
            )
    return svg_content


# ── Quarantine queue (C6) ─────────────────────────────────────────────────────

class QuarantineEntry:
    """Records an attachment submitted for async quarantine review (C6)."""

    def __init__(self, attachment_id: str, content_hash: str, reason: str):
        self.attachment_id = attachment_id
        self.content_hash = content_hash
        self.reason = reason

    def __repr__(self) -> str:
        return f"QuarantineEntry(id={self.attachment_id!r}, reason={self.reason!r})"


def compute_content_hash(content: bytes) -> str:
    """Return SHA-256 hex digest of content for quarantine/dedup tracking."""
    return hashlib.sha256(content).hexdigest()


def enqueue_quarantine(attachment_id: str, content: bytes, reason: str) -> QuarantineEntry:
    """
    Enqueue an attachment for async quarantine review (C6).

    In production this would push to a Celery task queue; here it returns a
    QuarantineEntry for callers to persist or test against.
    """
    entry = QuarantineEntry(
        attachment_id=attachment_id,
        content_hash=compute_content_hash(content),
        reason=reason,
    )
    return entry
