from contextlib import ExitStack
from unittest.mock import patch

from ..errors import AgentError


class OfflineNetworkGuard:
    """Intercept Python socket creation and resolution during local evaluation.

    No fixture requires a listener or DNS. This regression tripwire is not the
    RHEL execution sandbox, and does not claim to constrain arbitrary native code.
    """

    def __init__(self):
        self.attempts = 0
        self._stack = ExitStack()

    def _deny(self, *args, **kwargs):
        self.attempts += 1
        raise AgentError("NETWORK_ATTEMPT_BLOCKED", "Local evaluation cannot create network connections or resolve hosts.")

    def __enter__(self):
        for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            self._stack.enter_context(patch("socket." + name, self._deny))
        return self

    def __exit__(self, *args):
        return self._stack.__exit__(*args)
