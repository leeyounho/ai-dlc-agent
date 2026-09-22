class AgentError(Exception):
    """An error with a stable code and a safe, value-free public message."""

    def __init__(self, code: str, message: str, *, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.field is not None:
            result["field"] = self.field
        return result
