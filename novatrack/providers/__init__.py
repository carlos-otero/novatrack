"""TrackingProvider Protocol and shared exception for all provider adapters."""

from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """Sanitised provider error — never contains upstream body, keys, or raw messages.

    retryable_without_quota: True when the rejection did not consume a tracking
    quota slot (e.g. missing required parameter codes -18010018/-18010019/-18010020).
    The worker uses this flag to reset next_attempt_at immediately instead of
    applying the normal exponential backoff, so that re-filling the missing field
    and waiting for the next worker run triggers a quick retry.
    """

    def __init__(self, message: str = "", *, retryable_without_quota: bool = False) -> None:
        super().__init__(message)
        self.retryable_without_quota = retryable_without_quota


@runtime_checkable
class TrackingProvider(Protocol):
    """Structural protocol every provider adapter must satisfy."""

    def register(self, number: str, carrier: int | None = None, *, param: str | None = None) -> dict: ...

    def gettrackinfo(self, number: str, carrier: int | None = None) -> dict: ...

    def stoptrack(self, number: str, carrier: int | None = None) -> dict: ...

    def deletetrack(self, number: str, carrier: int | None = None) -> dict: ...

    def getquota(self) -> dict: ...

    def changecarrier(self, number: str, carrier_new: int, carrier_old: int | None = None) -> dict: ...
