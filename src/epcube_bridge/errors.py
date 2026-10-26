class BridgeError(Exception):
    """Only fixed, non-sensitive messages may cross the cloud boundary."""

    def __init__(self, code: str, retry_after: float = 0):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
