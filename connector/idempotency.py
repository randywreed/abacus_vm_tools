"""Small in-memory idempotency cache for a single student connector."""
import asyncio
import hmac
import time
from collections import OrderedDict


class TurnIdempotency:
    def __init__(self, ttl_seconds: float = 900, limit: int = 1000):
        self.ttl_seconds = ttl_seconds
        self.limit = limit
        self.entries = OrderedDict()
        self.lock = asyncio.Lock()

    async def run(self, key: str, fingerprint: str, work):
        now = time.monotonic()
        leader = False
        async with self.lock:
            while self.entries and next(iter(self.entries.values()))["expires"] < now:
                self.entries.popitem(last=False)
            entry = self.entries.get(key)
            if entry:
                if not hmac.compare_digest(str(entry["fingerprint"]), fingerprint):
                    raise ValueError("idempotency key cannot be reused for another prompt")
                future = entry["future"]
            else:
                future = asyncio.get_running_loop().create_future()
                self.entries[key] = {"expires": now + self.ttl_seconds, "fingerprint": fingerprint, "future": future}
                while len(self.entries) > self.limit:
                    self.entries.popitem(last=False)
                leader = True
        if not leader:
            return await asyncio.shield(future)
        try:
            result = await work()
            if not future.done():
                future.set_result(result)
            return result
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
                future.exception()
            async with self.lock:
                self.entries.pop(key, None)
            raise
