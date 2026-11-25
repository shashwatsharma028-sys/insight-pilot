"""
InsightPilot V2 security controls (deterministic; none of them relies on a model following instructions).

- policy:   the RuntimeAuthority (policy engine): capability matrix, approval binding and one-time nonces,
            the approved-task grant, operation authorization, and a runtime-keyed integrity tag over run state.
- outbound: what may leave the application for the provider (prompt guard, insight-request view with label
            pseudonymisation and local-only results), and what model output may be accepted.
- detect:   prompt-injection and sensitive-pattern SIGNALS. Detection is defence in depth only: a signal never
            grants anything, and no decision to allow depends on the absence of a signal.
- audit:    structured, privacy-preserving security events (codes, ids and timings only; never payloads).
"""

from multi_agent.security.audit import POLICY_VERSION  # noqa: F401
from multi_agent.security.policy import (IntegrityViolation, PolicyRefused, RuntimeAuthority,  # noqa: F401
                                         SecurityViolation)
