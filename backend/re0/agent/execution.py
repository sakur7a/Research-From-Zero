"""Small shared cancellation/deadline/request boundary for one durable Agent turn."""


class UpstreamRequestBudgetExceeded(RuntimeError):
    """A task-local provider request was refused so one model call remains for a final report."""


class RequestBoundaryStop(RuntimeError):
    """Cancellation, deadline or cumulative budget stopped a request before it was sent."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
