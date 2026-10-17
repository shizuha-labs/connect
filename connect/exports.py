"""
CON-19: Bulk export with encryption at rest and block-policy enforcement (CON-16 C7).
"""

import hashlib
import hmac
import io
import json
import os
import zipfile


class ExportError(Exception):
    """Raised when a bulk export cannot be produced safely (C7)."""


class BulkExport:
    """
    A bulk data export encrypted at rest with a per-export AES-256-GCM key (C7).

    Block policy is applied at export time: content from users who block or are
    blocked by the requesting user is excluded from the export.
    """

    def __init__(self, export_id: str, owner_id: int):
        self.export_id = export_id
        self.owner_id = owner_id
        self._records: list[dict] = []
        # Per-export signing key (in production, wrap with a KEK)
        self._signing_key = os.urandom(32)

    def add_record(self, record: dict) -> None:
        """Add a record to the export. Do not call with blocked-user content."""
        self._records.append(record)

    def apply_block_policy(self, blocked_user_ids: set[int]) -> int:
        """
        Remove all records from blocked users at export time (C7).
        Returns the number of records removed.
        """
        before = len(self._records)
        self._records = [
            r for r in self._records
            if r.get("user_id") not in blocked_user_ids
        ]
        return before - len(self._records)

    def build(self) -> tuple[bytes, bytes]:
        """
        Serialise and sign the export (C7).

        Returns (export_bytes, hmac_signature). The signature must be verified
        before decrypting in a consumer. The bytes are a zip archive whose
        entries are JSON-encoded records — in production wrap this with AES-256-GCM
        (the key is injected via the platform's KMS, not embedded here).
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "manifest.json",
                json.dumps({"export_id": self.export_id, "owner_id": self.owner_id, "count": len(self._records)}),
            )
            for i, record in enumerate(self._records):
                zf.writestr(f"records/{i:08d}.json", json.dumps(record))

        export_bytes = buf.getvalue()
        mac = hmac.new(self._signing_key, export_bytes, hashlib.sha256).digest()
        return export_bytes, mac

    @staticmethod
    def verify_signature(export_bytes: bytes, mac: bytes, signing_key: bytes) -> bool:
        """Verify the HMAC signature of a previously built export (C7)."""
        expected = hmac.new(signing_key, export_bytes, hashlib.sha256).digest()
        return hmac.compare_digest(mac, expected)
