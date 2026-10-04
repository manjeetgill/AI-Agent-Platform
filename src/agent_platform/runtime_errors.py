"""Stable public error codes; provider internals are never returned to clients."""


class RunFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)
