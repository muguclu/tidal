"""In-memory ``Bus`` fake that records sent batches."""

from collections.abc import Sequence

from tidal.query.protocols import OutboundMessage


class FakeBus:
    """Records every ``send_batch`` call; ``fail_ping`` makes ``ping`` raise."""

    def __init__(self, *, fail_ping: bool = False) -> None:
        """Create an empty bus."""
        self.fail_ping = fail_ping
        self.batches: list[list[OutboundMessage]] = []

    @property
    def sent(self) -> list[OutboundMessage]:
        """All messages sent so far, in order."""
        return [m for batch in self.batches for m in batch]

    async def send_batch(self, messages: Sequence[OutboundMessage]) -> int:
        """Record one batch and return its size."""
        self.batches.append(list(messages))
        return len(messages)

    async def ping(self) -> None:
        """Raise ``ConnectionError`` when ``fail_ping`` is set."""
        if self.fail_ping:
            raise ConnectionError("fake bus down")
