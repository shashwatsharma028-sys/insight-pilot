"""
Hardened dispatcher (P3.2b): the function bound to
agent.nodes.executor.execute_code in hardened mode.

It keeps V1's call signature and return tuple
(success, output, chart_path, error_type) so executor_node, the retry loop
and both execution paths work unchanged.

- The V1 AST scanner runs first, unchanged: same verdicts, same message,
  same retry behaviour. Defence in depth only.
- Code that passes is handed to the injected ExecutionRuntime. The dispatcher
  itself never executes code, and it never calls the V1 executor.
- Runtime errors, protocol violations and unexpected results raise
  (SandboxUnavailableError / SandboxProtocolError). Nothing falls back to V1.
- Artifact extraction is not implemented yet, so a result carrying a chart is
  rejected as a protocol error.

P5.1A (shared by both callers: executor_node and the P5 ContainerExecutionPort):
- Dataset identity: after the scan and the size check, the dataset is checked
  READ-ONLY with DatasetAuthorizer.identify (demo-only policy, descriptor-
  relative open, digest == pin). A policy failure raises
  DatasetNotAuthorizedError; nothing reaches the runtime. The request carries
  only a DatasetIdentity (name, digest, size): never the host path.
- expected_sha256 (optional, P5): the digest the caller profiled. It must
  equal the verified digest, or DatasetNotAuthorizedError is raised.
- prelude (optional): a PreludeId for the trusted in-image runner.
"""

import secrets
import threading

from v2.execution.errors import DatasetNotAuthorizedError, SandboxProtocolError, SandboxUnavailableError
from v2.execution.isolation import IsolationLevel
from v2.execution.limits import DEFAULT_LIMITS, MIB, Limits
from v2.execution.results import ExecutionOutcome, ExecutionResult
from v2.execution.runtime import DatasetIdentity, ExecutionRequest, PreludeId

_CHART_MARKER = "CHART_SAVED:"


class HardenedDispatcher:
    def __init__(self, runtime, limits: Limits = DEFAULT_LIMITS, authorizer=None):
        if getattr(runtime, "isolation", None) is not IsolationLevel.ROOTLESS_CONTAINER:
            raise SandboxUnavailableError("the hardened dispatcher requires an OS-isolated container runtime")
        if not isinstance(limits, Limits):
            raise TypeError("limits must be Limits")
        if authorizer is None:
            from v2.execution.datasets import DatasetAuthorizer
            authorizer = DatasetAuthorizer(limits=limits)
        self.runtime = runtime
        self.limits = limits
        self.authorizer = authorizer
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(self, code, csv_path, task_id, *, expected_sha256=None, prelude=PreludeId.V1):
        from agent.rules_engine import scan_code_safety
        from agent.state import ErrorType

        with self._lock:
            self.calls += 1

        violation = scan_code_safety(code)
        if violation:
            return False, violation, None, ErrorType.RUNTIME

        if not isinstance(code, str) or len(code.encode("utf-8")) > self.limits.code_bytes:
            return (False, f"Generated code exceeds the {self.limits.code_bytes // MIB} MiB size limit.",
                    None, ErrorType.RUNTIME)

        if not isinstance(prelude, PreludeId):
            raise TypeError("prelude must be a PreludeId")
        authorized = self.authorizer.identify(csv_path)      # policy -> DatasetNotAuthorizedError
        if expected_sha256 is not None and expected_sha256 != authorized.sha256:
            raise DatasetNotAuthorizedError(
                "Dataset not authorised: its verified SHA-256 differs from the profiled identity")
        request = ExecutionRequest(run_id=secrets.token_hex(16), task_id=str(task_id),
                                   code=code, limits=self.limits,
                                   dataset=DatasetIdentity(authorized.name, authorized.sha256, authorized.size),
                                   prelude=prelude)
        try:
            result = self.runtime.execute(request)
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # any runtime failure stops the run; never a fallback
            raise SandboxUnavailableError(
                f"hardened runtime {getattr(self.runtime, 'name', '?')!r} failed: "
                f"{type(exc).__name__}") from exc

        self._check_result(request, result)
        return self._to_v1_tuple(result, ErrorType)

    def _check_result(self, request, result) -> None:
        if not isinstance(result, ExecutionResult):
            raise SandboxProtocolError("runtime returned something other than an ExecutionResult")
        if result.run_id != request.run_id:
            raise SandboxProtocolError("runtime result belongs to a different run")
        if result.isolation is not IsolationLevel.ROOTLESS_CONTAINER:
            raise SandboxProtocolError("runtime result does not report OS isolation")
        if result.outcome is ExecutionOutcome.BLOCKED_BY_SCANNER:
            raise SandboxProtocolError("only the dispatcher may report a scanner block")
        if result.chart_path is not None:
            raise SandboxProtocolError("artifact extraction is not implemented; charts are not accepted yet")

    def _to_v1_tuple(self, result: ExecutionResult, ErrorType):
        import sandbox.executor as v1   # constants and classifier only; never v1.execute_code

        if result.outcome is ExecutionOutcome.SUCCEEDED:
            lines = [line for line in result.stdout.strip().splitlines()
                     if not line.startswith(_CHART_MARKER)]
            output = "\n".join(lines).strip()
            return True, output or "Execution completed successfully.", None, None
        if result.outcome is ExecutionOutcome.CODE_ERROR:
            stderr = result.stderr.strip()
            return False, stderr, None, v1.classify_error(stderr)
        if result.outcome is ExecutionOutcome.TIMED_OUT:
            return False, f"Execution timed out after {v1.MAX_EXECUTION_TIME}s.", None, ErrorType.RUNTIME
        if result.outcome is ExecutionOutcome.MEMORY_LIMIT:
            return (False, f"Execution exceeded the memory limit ({self.limits.memory_bytes // MIB} MiB).",
                    None, ErrorType.RUNTIME)
        raise SandboxProtocolError(f"unhandled execution outcome {result.outcome!r}")
