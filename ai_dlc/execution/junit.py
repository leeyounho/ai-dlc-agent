"""Bounded JUnit evidence. Process exit zero alone never implies tested success."""

from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET

from ..errors import AgentError
from .workspace import files_under, matches, read_file


@dataclass(frozen=True)
class JUnitResult:
    reports: int
    tests: int
    failures: int
    errors: int
    skipped: int

    @property
    def executed(self):
        return self.tests - self.skipped

    @property
    def passed(self):
        return self.executed > 0 and self.failures == 0 and self.errors == 0

    def document(self):
        return {"reports": self.reports, "tests": self.tests, "executed": self.executed,
                "failures": self.failures, "errors": self.errors, "skipped": self.skipped, "passed": self.passed}


def report_paths(root, patterns):
    return tuple(path for path in files_under(root) if any(matches(path, pattern) for pattern in patterns))


def _parse(data: bytes):
    try:
        text = data.decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError
        declaration = re.match(r"\s*<\?xml\s+[^?]*encoding=['\"]([^'\"]+)['\"]", text, re.I)
        if declaration and declaration.group(1).lower() not in {"utf-8", "utf8"}:
            raise ValueError
        root = ET.fromstring(text)
        if root.tag not in {"testsuite", "testsuites"}:
            raise ValueError
        totals = [0, 0, 0, 0]
        nodes = 0
        pending = [(root, 0, None)]
        while pending:
            node, depth, parent = pending.pop()
            nodes += 1
            if nodes > 50000 or depth > 32:
                raise ValueError
            pending.extend((child, depth + 1, node.tag) for child in node)
            if node.tag == "testcase":
                if parent != "testsuite" or not node.get("name"):
                    raise ValueError
                flags = [any(child.tag == tag for child in node) for tag in ("failure", "error", "skipped")]
                if sum(flags) > 1:
                    raise ValueError
                totals = [totals[0] + 1, *(totals[i + 1] + int(flags[i]) for i in range(3))]
            if node.tag in {"testsuite", "testsuites"}:
                if parent not in {None, "testsuite", "testsuites"}:
                    raise ValueError
                cases = list(node.iter("testcase"))
                counts = [len(cases), *[sum(any(c.tag == tag for c in case) for case in cases) for tag in ("failure", "error", "skipped")]]
                for field, count in zip(("tests", "failures", "errors", "skipped"), counts):
                    if field in node.attrib and (not re.fullmatch(r"[0-9]+", node.attrib[field]) or int(node.attrib[field]) != count):
                        raise ValueError
        return totals
    except (ValueError, UnicodeError, ET.ParseError, RecursionError):
        raise AgentError("JUNIT_INVALID", "JUnit XML is unsafe, inconsistent, or unsupported.") from None


def collect_junit(root, patterns) -> JUnitResult:
    paths = report_paths(root, patterns)
    if not paths:
        raise AgentError("JUNIT_MISSING", "No reports were produced for the configured JUnit patterns.")
    if len(paths) > 256:
        raise AgentError("JUNIT_LIMIT", "Too many JUnit reports.")
    totals, size = [0, 0, 0, 0], 0
    for path in paths:
        data = read_file(root, path, limit=2 * 1024 * 1024)
        size += len(data)
        if size > 16 * 1024 * 1024:
            raise AgentError("JUNIT_LIMIT", "JUnit evidence exceeds the total size limit.")
        totals = [a + b for a, b in zip(totals, _parse(data))]
    return JUnitResult(len(paths), *totals)
