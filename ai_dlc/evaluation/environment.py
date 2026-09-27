"""Allowlisted runtime metadata, without uname, host/user names or environment."""

import ctypes
from pathlib import Path
import re
import sys

from ..errors import AgentError


FIELDS = ("python_implementation", "python_version", "os_family", "os_release", "machine_architecture")
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+()\-]{0,127}\Z")


def validate_environment(value):
    if (type(value) is not dict or set(value) != set(FIELDS)
            or any(item is not None and (type(item) is not str or not _VALUE.fullmatch(item))
                   for item in value.values())):
        raise AgentError("EVAL_ENVIRONMENT", "Expected only bounded allowlisted runtime fields or null.")
    return dict(value)


def _safe(value):
    return value if type(value) is str and _VALUE.fullmatch(value) else None


def _windows_architecture():
    # GetNativeSystemInfo exposes CPU/platform data, never host/user identity.
    class SystemInfo(ctypes.Structure):
        _fields_ = [("architecture", ctypes.c_ushort), ("reserved", ctypes.c_ushort),
            ("page_size", ctypes.c_uint32), ("minimum_address", ctypes.c_void_p),
            ("maximum_address", ctypes.c_void_p), ("processor_mask", ctypes.c_size_t),
            ("processor_count", ctypes.c_uint32), ("processor_type", ctypes.c_uint32),
            ("allocation_granularity", ctypes.c_uint32), ("processor_level", ctypes.c_ushort),
            ("processor_revision", ctypes.c_ushort)]
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    query = library.GetNativeSystemInfo
    query.argtypes, query.restype = [ctypes.POINTER(SystemInfo)], None
    value = SystemInfo()
    query(ctypes.byref(value))
    return {0: "x86", 5: "arm", 6: "ia64", 9: "x86_64", 12: "aarch64"}.get(value.architecture)


def _linux_architecture():
    # AT_PLATFORM is the kernel's execution-platform label. Calling uname or
    # platform.machine would also retrieve the hostname, even if later dropped.
    query = ctypes.CDLL(None).getauxval
    query.argtypes, query.restype = [ctypes.c_ulong], ctypes.c_void_p
    pointer = query(15)  # AT_PLATFORM
    value = ctypes.cast(pointer, ctypes.c_char_p).value.decode("ascii") if pointer else None
    # Some kernels report a CPU model here (for example a POWER generation).
    # Do not label that as a machine architecture or infer it from build paths.
    aliases = {"i386": "x86", "i486": "x86", "i586": "x86", "i686": "x86",
               "v7l": "armv7l", "v8l": "armv8l"}
    supported = {"x86_64", "aarch64", "armv7l", "armv8l", "ppc64", "ppc64le", "s390x", "riscv64", "mips", "mips64"}
    return aliases.get(value, value if value in supported else None)


def collect_environment():
    version = sys.version_info
    suffix = {"final": "", "alpha": "a", "beta": "b", "candidate": "rc"}.get(version.releaselevel)
    python_version = (f"{version.major}.{version.minor}.{version.micro}"
                      + (suffix + str(version.serial) if suffix else "")) if suffix is not None else None
    family = {"win32": "Windows", "linux": "Linux", "darwin": "Darwin"}.get(sys.platform)
    release, architecture = None, None
    try:
        if family == "Windows":
            windows = sys.getwindowsversion()
            release = f"{windows.major}.{windows.minor}.{windows.build}"
        elif family == "Linux":
            # Read only the release field; never os.uname/platform.uname.
            with Path("/proc/sys/kernel/osrelease").open("r", encoding="ascii") as stream:
                release = stream.read(129).strip()
    except (OSError, UnicodeError, AttributeError):
        pass
    try:
        if family == "Windows":
            architecture = _windows_architecture()
        elif family == "Linux":
            architecture = _linux_architecture()
    except (OSError, UnicodeError, AttributeError):
        pass
    return validate_environment({"python_implementation": _safe(sys.implementation.name),
        "python_version": _safe(python_version), "os_family": family,
        "os_release": _safe(release), "machine_architecture": _safe(architecture)})


def report_environment(report):
    if type(report) is not dict:
        raise AgentError("EVAL_ENVIRONMENT", "Expected an evaluation report object.")
    version = report.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        raise AgentError("CONFIG_VERSION", "Unsupported evaluation report schema.")
    if version == 1:
        if "runtime_environment" in report:
            raise AgentError("EVAL_ENVIRONMENT", "Legacy reports cannot claim runtime metadata.")
        return dict.fromkeys(FIELDS)
    return validate_environment(report.get("runtime_environment"))


def compare_environments(baseline, candidate):
    left, right = report_environment(baseline), report_environment(candidate)
    unknown = [field for field in FIELDS if left[field] is None or right[field] is None]
    differences = {field: {"baseline": left[field], "candidate": right[field]} for field in FIELDS
                   if field not in unknown and left[field] != right[field]}
    return {"status": "different" if differences else "unknown" if unknown else "same",
            "differences": differences, "unknown_fields": unknown, "baseline": left, "candidate": right,
            "performance_conclusion": "not_evaluated"}
