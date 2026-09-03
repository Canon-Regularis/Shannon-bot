from __future__ import annotations


class FakeLiveness:
    """What /health asks the process about itself, with each answer dictated."""

    def __init__(
        self,
        *,
        database: bool = True,
        worker: bool = True,
        bot: bool = True,
        poller: bool = True,
    ) -> None:
        self.database = database
        self.worker = worker
        self.bot = bot
        self.poller = poller

    async def database_reachable(self) -> bool:
        return self.database

    def worker_running(self) -> bool:
        return self.worker

    def bot_connected(self) -> bool:
        return self.bot

    def poller_running(self) -> bool:
        return self.poller
