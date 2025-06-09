import asyncio

class RepeatingTimer:
    def __init__(self, callback):
        self.callback = callback
        self.task = None
        self.is_running = False

    async def _run(self):
        await self.callback()
        while self.is_running:
            await asyncio.sleep(self.interval)
            if self.is_running:  # Check again in case it was stopped
                await self.callback()

    def start(self,interval=None):
        if not self.is_running:
            self.is_running = True
            if interval:
                self.interval = interval
            self.task = asyncio.create_task(self._run())

    def stop(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
