"""Tamper-evident audit log: hash-chained rows the application role cannot alter or delete.

See docs/adr/ADR-007-tamper-evident-audit.md for what this protects and, as importantly,
what it does not.
"""

from warden.audit.log import GENESIS_HASH, VerifyResult, append, verify

__all__ = ["GENESIS_HASH", "VerifyResult", "append", "verify"]
