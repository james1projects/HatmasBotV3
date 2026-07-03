#!/usr/bin/env python3
"""
DEV tool to serve FindIt page and proxy its WebSocket to a locally-running worker.
Usage: python findit_devserver.py
Then open http://127.0.0.1:8090/FindIt
"""
import argparse
import asyncio
from pathlib import Path

import aiohttp
from aiohttp import web, WSMsgType


async def proxy_websocket(client_ws, backend_url, worker_port):
    """Proxy messages between client and backend WebSocket."""
    async with aiohttp.ClientSession() as session:
        try:
            backend_ws = await session.ws_connect(
                backend_url,
                max_msg_size=8 * 1024 * 1024
            )
        except Exception as e:
            print(f"worker not running on port {worker_port} - start it first")
            await client_ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"worker not running")
            return

        print("Client connected")

        async def pump_client_to_backend():
            try:
                async for msg in client_ws:
                    if msg.type == WSMsgType.TEXT:
                        await backend_ws.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await backend_ws.send_bytes(msg.data)
                    elif msg.type == WSMsgType.CLOSE:
                        break
                    elif msg.type == WSMsgType.ERROR:
                        break
            finally:
                await backend_ws.close()
                await client_ws.close()

        async def pump_backend_to_client():
            try:
                async for msg in backend_ws:
                    if msg.type == WSMsgType.TEXT:
                        await client_ws.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await client_ws.send_bytes(msg.data)
                    elif msg.type == WSMsgType.CLOSE:
                        break
                    elif msg.type == WSMsgType.ERROR:
                        break
            finally:
                await backend_ws.close()
                await client_ws.close()

        try:
            await asyncio.gather(pump_client_to_backend(), pump_backend_to_client())
        except Exception as e:
            print(f"Error in proxy: {e}")
        finally:
            print("Client disconnected")


async def handle_findit(request):
    root = Path(request.app["root"])
    findit_path = root / "public" / "findit.html"
    if not findit_path.exists():
        return web.Response(status=404, text="findit.html not found")
    content = findit_path.read_text(encoding="utf-8")
    return web.Response(text=content, content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


async def handle_ws(request):
    client_ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8 * 1024 * 1024)
    await client_ws.prepare(request)
    worker_port = request.app["worker_port"]
    backend_url = f"ws://127.0.0.1:{worker_port}/ws"
    await proxy_websocket(client_ws, backend_url, worker_port)
    return client_ws


async def handle_root(request):
    raise web.HTTPFound("/FindIt")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8090, help="HTTP port to listen on (default 8090)")
    parser.add_argument("--worker-port", type=int, default=8475, help="port of an already-running detection worker (default 8475)")
    parser.add_argument("--root", type=str, help="repo root containing public/findit.html (default: parent of this script's directory)")
    args = parser.parse_args()

    if not args.root:
        args.root = str(Path(__file__).parent.parent)

    app = web.Application()
    app["worker_port"] = args.worker_port
    app["root"] = args.root

    app.router.add_get("/", handle_root)
    app.router.add_get("/FindIt", handle_findit)
    app.router.add_get("/ws/findit", handle_ws)

    web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
