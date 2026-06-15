"""
Minimal asyncio RCON client (Source RCON protocol, as spoken by
Factorio's --rcon-port). No external dependencies.

Packet wire format (little-endian):
    int32 size      (of everything after this field)
    int32 id        (request id; -1 in auth response = auth failed)
    int32 type      (3=AUTH, 2=EXECCOMMAND / AUTH_RESPONSE, 0=RESPONSE_VALUE)
    bytes body      (null-terminated)
    byte  0x00      (trailing null)

Commands are serialized through a lock — Factorio responses carry the
request id, but one-at-a-time keeps reasoning simple and our commands
are tiny ("ok"). Large multi-packet responses are not handled; mod
remote calls return short strings by design.
"""

import asyncio
import struct
from typing import Optional

TYPE_AUTH = 3
TYPE_EXECCOMMAND = 2
TYPE_AUTH_RESPONSE = 2
TYPE_RESPONSE_VALUE = 0

MAX_PACKET = 1024 * 1024  # sanity bound


class RconError(Exception):
    pass


class RconAuthError(RconError):
    pass


class RconClient:
    def __init__(self, host: str, port: int, password: str,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # ── wire helpers ────────────────────────────────────────────────

    def _request_id(self) -> int:
        self._next_id = (self._next_id % 1_000_000) + 1
        return self._next_id

    @staticmethod
    def _encode(req_id: int, ptype: int, body: str) -> bytes:
        payload = (struct.pack("<ii", req_id, ptype)
                   + body.encode("utf-8") + b"\x00\x00")
        return struct.pack("<i", len(payload)) + payload

    async def _read_packet(self):
        size_raw = await self._reader.readexactly(4)
        (size,) = struct.unpack("<i", size_raw)
        if size < 10 or size > MAX_PACKET:
            raise RconError(f"bad packet size {size}")
        payload = await self._reader.readexactly(size)
        req_id, ptype = struct.unpack("<ii", payload[:8])
        body = payload[8:-2].decode("utf-8", errors="replace")
        return req_id, ptype, body

    # ── public api ──────────────────────────────────────────────────

    async def connect(self):
        """Open the TCP connection and authenticate."""
        async with self._lock:
            await self._connect_locked()

    async def _connect_locked(self):
        await self._close_locked()
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.timeout)
        auth_id = self._request_id()
        self._writer.write(self._encode(auth_id, TYPE_AUTH, self.password))
        await self._writer.drain()
        # Some servers send an empty RESPONSE_VALUE before the auth
        # response; read until we see type 2.
        while True:
            req_id, ptype, _ = await asyncio.wait_for(
                self._read_packet(), timeout=self.timeout)
            if ptype == TYPE_AUTH_RESPONSE:
                if req_id == -1:
                    await self._close_locked()
                    raise RconAuthError("RCON password rejected")
                return

    async def command(self, cmd: str) -> str:
        """Run a command; reconnects once on connection failure."""
        async with self._lock:
            if not self.connected:
                await self._connect_locked()
            try:
                return await self._command_locked(cmd)
            except (RconAuthError, asyncio.CancelledError):
                raise
            except Exception:
                # One transparent retry on a fresh connection (covers
                # the game restarting between commands).
                await self._connect_locked()
                return await self._command_locked(cmd)

    async def _command_locked(self, cmd: str) -> str:
        req_id = self._request_id()
        self._writer.write(self._encode(req_id, TYPE_EXECCOMMAND, cmd))
        await self._writer.drain()
        while True:
            resp_id, ptype, body = await asyncio.wait_for(
                self._read_packet(), timeout=self.timeout)
            if ptype == TYPE_RESPONSE_VALUE and resp_id == req_id:
                return body

    async def close(self):
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self):
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
