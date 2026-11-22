"""
Error contracts for hardened execution (P3.1).

Two families, never mixed:

- Policy errors (SandboxPolicyError): the request was refused on purpose and
  nothing was executed. Example: a dataset path that is not an authorised
  demo dataset.
- Infrastructure errors (SandboxUnavailableError): isolation cannot be
  provided or verified. They subclass InsecureConfigurationError because the
  only safe reaction is the same: stop, never fall back to the V1 executor.

Neither family is retried by asking the LLM for new code.
"""

from config.feature_flags import InsecureConfigurationError


# ── Policy errors ────────────────────────────────────────────────────────────

class SandboxPolicyError(RuntimeError):
    """A request was refused by policy. Nothing was executed."""


class DatasetNotAuthorizedError(SandboxPolicyError):
    """The dataset path is not an authorised dataset (demo-only in early P3)."""


# Reason codes for a rejected chart artifact. Fixed set: reasons never carry
# attacker-controlled text such as file names.
ARTIFACT_REJECT_REASONS = frozenset({
    "not_regular_file",
    "symlink",
    "hardlink",
    "empty",
    "too_large",
    "bad_signature",
    "bad_crc",
    "disallowed_chunk",
    "bad_ihdr",
    "dimensions_exceeded",
    "decompression_mismatch",
    "trailing_data",
    "decode_failed",
    "truncated",          # P3.4: a chunk or the file ends early
    "too_many_chunks",    # P3.4: chunk-count cap
})


class ArtifactRejectedError(SandboxPolicyError):
    """An output artifact failed validation and was discarded."""

    def __init__(self, reason: str):
        if reason not in ARTIFACT_REJECT_REASONS:
            raise ValueError(f"unknown artifact reject reason: {reason!r}")
        super().__init__(f"artifact rejected: {reason}")
        self.reason = reason


# ── Infrastructure errors ────────────────────────────────────────────────────

class SandboxUnavailableError(InsecureConfigurationError):
    """Isolation is unavailable or could not be verified. Fail closed."""


class SandboxProtocolError(SandboxUnavailableError):
    """The sandbox result could not be decoded within its declared bounds."""
