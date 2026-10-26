"""TrackingProvider Protocol and shared exception for all provider adapters."""
from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """Sanitised provider error — never contains upstream body, keys, or raw messages."""


@runtime_checkable
class TrackingProvider(Protocol):
    """Structural protocol every provider adapter must satisfy."""

    def register(self, number: str, carrier: int | None = None) -> dict: ...

    def gettrackinfo(self, number: str, carrier: int | None = None) -> dict: ...

    def stoptrack(self, number: str, carrier: int | None = None) -> dict: ...

    def deletetrack(self, number: str, carrier: int | None = None) -> dict: ...

    def getquota(self) -> dict: ...
