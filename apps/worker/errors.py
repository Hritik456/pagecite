"""Stage error types. P2: a PermanentError fails the document at once (no retry). P3 adds TransientError."""


class PermanentError(Exception):
    """The input can never succeed (corrupt, encrypted, unsupported, no text, over a limit)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
