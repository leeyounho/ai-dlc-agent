"""Runs only bundled synthetic programs for local executor contract evaluation.

Not a production runner, not an isolation boundary, and not a repository script
executor. No arbitrary executable, Python code, shell, URL, or inherited secret
environment can be selected by an evaluation plan.
"""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from .. import validation as v
from ..errors import AgentError
from ..execution.ports import ProcessResult
from ..execution.workspace import contained

PROGRAM = r'''
from pathlib import Path
import sys
import time
mode = sys.argv[1]
if mode == "timeout":
    time.sleep(30)
elif mode == "output_limit":
    for _ in range(10000):
        print("x" * 1000)
else:
    if mode == "mutate":
        Path("input.txt").write_text("changed by command", encoding="utf-8")
    if mode == "unexpected_file":
        Path("unapproved.txt").write_text("new source", encoding="utf-8")
    if mode == "nonzero":
        raise SystemExit(7)
    if mode != "missing":
        Path("reports").mkdir(exist_ok=True)
        cases = "" if mode == "zero" else '<testcase name="synthetic">' + (
            '<failure message="fixture failure"/>' if mode == "failure" else
            '<skipped/>' if mode == "skipped" else '') + '</testcase>'
        Path("reports/result.xml").write_text('<testsuite>' + cases + '</testsuite>', encoding="utf-8")
    print("Synthetic command finished.")
'''
MODES = frozenset({"pass", "failure", "zero", "skipped", "missing", "nonzero", "mutate", "timeout", "output_limit", "unexpected_file"})


class _StreamDigest:
    def __init__(self, stream, exceeded):
        self.digest = hashlib.sha256()
        self.total = 0
        self.stream, self.exceeded = stream, exceeded
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while chunk := self.stream.read(4096):
                self.total += len(chunk)
                self.digest.update(chunk)
                if self.total > 65536:
                    self.exceeded.set()
        finally:
            self.stream.close()


class _FixtureHandle:
    def __init__(self, run_id, process):
        self.process = process
        self._identity = {"run_id": run_id, "pid": process.pid, "token": uuid.uuid4().hex, "scope": "bundled_synthetic_program"}
        self.termination = "completed"
        self.exceeded = threading.Event()
        self.stdout = _StreamDigest(process.stdout, self.exceeded)
        self.stderr = _StreamDigest(process.stderr, self.exceeded)

    @property
    def identity(self):
        return dict(self._identity)

    def poll(self):
        if self.exceeded.is_set():
            self.cancel("output_limit")
        code = self.process.poll()
        if code is None:
            return None
        for stream in (self.stdout, self.stderr):
            stream.thread.join(timeout=0.1)
            if stream.thread.is_alive():
                return None
        # The bundled fixture never spawns descendants. This property must not
        # be generalized to arbitrary repository code or a PID after restart.
        return ProcessResult(code, self.termination, self.stdout.digest.hexdigest(), self.stderr.digest.hexdigest(),
                             self.stdout.total, self.stderr.total, True)

    def cancel(self, reason):
        if self.termination == "completed":
            self.termination = reason
        if self.process.poll() is None:
            self.process.kill()


class FixtureProcessRunner:
    """Explicit local evaluation dependency; never selected by production config."""

    def __init__(self):
        self.handles = {}
        self.launch_count = 0

    def preflight(self, plan):
        if (plan.toolchain_id != "local-fixture-only" or plan.command.executable_id != "fixture-python"
                or len(plan.command.argv) != 1 or plan.command.argv[0] not in MODES
                or plan.command.cwd != "." or plan.generated_patterns != ("reports/**",)):
            raise AgentError("FIXTURE_ONLY", "This runner accepts only the bundled synthetic evaluation commands.")
        executable_digest = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
        return v.canonical_digest({"executable": executable_digest, "version": sys.version, "fixture": PROGRAM})

    def start(self, run_id, plan):
        self.preflight(plan)
        if run_id in self.handles:
            raise AgentError("RUN_COLLISION", "This fixture run was already launched.")
        environment = {}
        if os.name == "nt":
            environment["SystemRoot"] = os.environ["SystemRoot"]
        process = subprocess.Popen([sys.executable, "-I", "-S", "-c", PROGRAM, plan.command.argv[0]],
                    cwd=contained(plan.workspace.root, plan.command.cwd), env=environment,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    shell=False, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        handle = _FixtureHandle(run_id, process)
        self.handles[run_id] = handle
        self.launch_count += 1
        return handle

    def inspect(self, run_id, identity):
        handle = self.handles.get(run_id)
        if handle is None or (identity is not None and identity != handle.identity):
            return None
        return handle.poll()
