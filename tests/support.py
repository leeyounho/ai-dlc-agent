from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import uuid


@contextmanager
def temporary_directory():
    """Test-only workspace with inherited Windows ACLs and verified cleanup."""
    root = (Path(__file__).resolve().parents[1] / "var" / "test-tmp").resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("test-" + uuid.uuid4().hex)
    target.mkdir(mode=0o700 if os.name == "posix" else 0o777)
    try:
        yield target
    finally:
        # Never recursively remove a computed path before resolving its boundary.
        resolved = target.resolve()
        if target.is_symlink() or resolved.parent != root or not resolved.name.startswith("test-"):
            raise RuntimeError("Refusing cleanup outside the test workspace.")
        # Convert only the already-verified local target, so Windows can remove
        # nested long names without following an untrusted device/UNC input.
        native = "\\\\?\\" + str(resolved) if os.name == "nt" else resolved
        def make_writable(function, path, _error):
            os.chmod(path, stat.S_IRWXU)
            function(path)
        shutil.rmtree(native, onexc=make_writable)
