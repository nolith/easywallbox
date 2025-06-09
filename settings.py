import asyncio

class Settings:
    KEY_BLE_RETRY_INTERVAL = "ble_retry_interval"
    KEY_BLE_REFRESH_INTERVAL = "ble_refresh_interval"

    def __init__(self):
        self._dict = {
            self.KEY_BLE_RETRY_INTERVAL: 10,
            self.KEY_BLE_REFRESH_INTERVAL: 10,
        }
        self._lock = asyncio.Lock()

    async def get(self, key, default=None):
        async with self._lock:
            return self._dict.get(key, default)

    async def set(self, key, value):
        async with self._lock:
            self._dict[key] = value

    async def keys(self):
        return [
            self.KEY_BLE_RETRY_INTERVAL,
            self.KEY_BLE_REFRESH_INTERVAL
        ]
