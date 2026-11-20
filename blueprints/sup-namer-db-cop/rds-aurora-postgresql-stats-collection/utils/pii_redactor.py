"""PII redaction utility for collected database statistics.

Runs on the customer's EC2 instance **before** json.dump writes to disk,
so the local JSON file and S3 upload are both clean.

Usage:
    from utils.pii_redactor import PiiRedactor
    redactor = PiiRedactor()
    redacted_data, query_hash_map = redactor.redact(collected_data)
    # collected_data is modified in-place and also returned

What is redacted:
    - Database endpoints → <masked-endpoint>
    - Client IP addresses → SHA-256 hash (first 8 chars)
    - KMS key ARNs → key ID only
    - Password hashes in pg_health_insights → removed
    - Query text gets a sibling query_hash field (query itself is NOT removed —
      pg_stat_statements stores parameterized form only, no PII)

What is NOT redacted:
    - Query text (parameterized $1/$2 form, no customer data)
    - Table/column/schema names (metadata, not customer data)
    - All metric values (numeric performance data)
    - Parameter names and settings (configuration, not PII)
"""

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple


class PiiRedactor:
    """Redact PII from collected database statistics."""

    ENDPOINT_KEYS = frozenset({
        'endpoint', 'reader_endpoint', 'db_host',
        'pgsnapper_output_dir', 'analysis_database',
    })

    IP_KEYS = frozenset({
        'client_address', 'client_addr', 'client_ip',
    })

    KMS_KEYS = frozenset({
        'kms_key_id',
    })

    # Regex for RDS/Aurora endpoints
    _ENDPOINT_RE = re.compile(
        r'[a-zA-Z0-9._-]+\.(?:cluster-(?:ro-)?)?[a-z0-9]+\.[a-z0-9-]+\.rds\.amazonaws\.com'
    )

    # Regex for IP addresses (v4)
    _IPV4_RE = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')

    # Regex for KMS ARNs
    _KMS_ARN_RE = re.compile(
        r'arn:aws:kms:[a-z0-9-]+:\d{12}:key/([a-f0-9-]+)'
    )

    def __init__(self):
        self._query_hash_map: Dict[str, str] = {}

    @staticmethod
    def _hash_short(value: str, length: int = 8) -> str:
        """SHA-256 hash truncated to `length` hex chars."""
        return hashlib.sha256(value.encode('utf-8')).hexdigest()[:length]

    def redact(self, data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Redact PII from collected data dict (in-place).

        Returns:
            (data, query_hash_map) — data is the same dict (modified in-place),
            query_hash_map maps query_hash → original query text.
        """
        self._query_hash_map = {}
        self._walk_and_redact(data)
        return data, dict(self._query_hash_map)

    def _walk_and_redact(self, obj: Any, parent_key: str = '') -> Any:
        """Recursively walk the data structure and apply redactions."""
        if isinstance(obj, dict):
            keys = list(obj.keys())
            for key in keys:
                value = obj[key]

                # Endpoint masking
                if key in self.ENDPOINT_KEYS and isinstance(value, str):
                    obj[key] = self._mask_endpoint(value)
                    continue

                # Client IP hashing
                if key in self.IP_KEYS and isinstance(value, str):
                    obj[key] = self._hash_ip(value)
                    continue

                # KMS ARN trimming
                if key in self.KMS_KEYS and isinstance(value, str):
                    obj[key] = self._trim_kms_arn(value)
                    continue

                # Query hashing — add sibling field, don't remove query
                if key == 'query' and isinstance(value, str) and len(value) > 10:
                    qhash = self._hash_short(value, 16)
                    obj['query_hash'] = qhash
                    self._query_hash_map[qhash] = value

                # Password hash removal (pg_health_insights)
                if key == 'password_hash' or key == 'rolpassword':
                    obj[key] = '<redacted>'
                    continue

                # Recurse
                self._walk_and_redact(value, parent_key=key)

        elif isinstance(obj, list):
            for item in obj:
                self._walk_and_redact(item, parent_key=parent_key)

        return obj

    def _mask_endpoint(self, value: str) -> str:
        """Replace RDS/Aurora endpoints with <masked-endpoint>."""
        return self._ENDPOINT_RE.sub('<masked-endpoint>', value)

    def _hash_ip(self, value: str) -> str:
        """Replace IP addresses with SHA-256 hash (8 chars)."""
        if self._IPV4_RE.match(value.strip()):
            return self._hash_short(value.strip())
        return value

    def _trim_kms_arn(self, value: str) -> str:
        """Trim KMS ARN to key ID only."""
        match = self._KMS_ARN_RE.match(value)
        if match:
            return match.group(1)
        return value
