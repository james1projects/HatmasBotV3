"""
Simple time-based cache for API responses.
"""

import time


class Cache:
    def __init__(self):
        self._store = {}

    def get(self, key, ttl=60):
        if key in self._store:
            value, timestamp = self._store[key]
            if time.time() - timestamp < ttl:
                return value
            del self._store[key]
        return None

    def set(self, key, value):
        self._store[key] = (value, time.time())

    def clear(self, key=None):
        if key:
            self._store.pop(key, None)
        else:
            self._store.clear()
