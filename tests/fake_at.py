"""A fake Africa's Talking messaging endpoint with fault injection, for tests and the simulator."""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs

from aiohttp import web


@dataclass
class FakeAT:
    api_key: str = "k" * 20
    username: str = "sandbox"
    fail_rate: float = 0.0  # fraction of requests that fail
    fail_mode: str = "500"  # 500 | drop | slow | balance | auth | redirect | garbage | reject
    latency: float = 0.0
    seed: int = 1
    sent: list = field(default_factory=list)  # accepted messages: (to, message)
    requests: int = 0
    _rng: random.Random = field(default_factory=random.Random)
    runner: Optional[web.AppRunner] = None
    port: int = 0

    def __post_init__(self) -> None:
        self._rng.seed(self.seed)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/version1/messaging"

    async def start(self) -> "FakeAT":
        app = web.Application()
        app.router.add_post("/version1/messaging", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return self

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    def duplicates(self) -> int:
        return len(self.sent) - len(set(self.sent))

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests += 1
        if self.latency:
            await asyncio.sleep(self.latency)
        if request.headers.get("apiKey") != self.api_key or self.fail_mode == "auth":
            return web.Response(status=401, text="The supplied authentication is invalid")
        form = {k: v[0] for k, v in parse_qs((await request.read()).decode()).items()}
        if not form.get("to"):
            return web.json_response({"SMSMessageData": {"Message": "Invalid number", "Recipients": []}}, status=400)
        if self._rng.random() < self.fail_rate:
            if self.fail_mode == "500":
                return web.Response(status=500, text="boom")
            if self.fail_mode == "drop":
                # Message is processed, but the connection dies before the client sees the answer.
                self.sent.append((form["to"], form.get("message", "")))
                request.transport.close()
                return web.Response(status=200)
            if self.fail_mode == "slow":
                await asyncio.sleep(5)
            if self.fail_mode == "balance":
                return self._json(form, 405, "InsufficientBalance")
            if self.fail_mode == "redirect":
                return web.Response(status=302, headers={"Location": "http://169.254.169.254/"})
            if self.fail_mode == "garbage":
                return web.Response(status=200, text="<html>nope</html>")
            if self.fail_mode == "reject":
                return self._json(form, 406, "UserInBlacklist")
        self.sent.append((form["to"], form.get("message", "")))
        return self._json(form, 101, "Sent", ok=True)

    def _json(self, form: dict, code: int, status: str, ok: bool = False) -> web.Response:
        rec = {"statusCode": code, "number": form["to"], "status": status, "cost": "KES 0.8000" if ok else "0",
               "messageId": f"ATXid_{len(self.sent):06d}" if ok else "None"}
        return web.Response(status=201, content_type="application/json",
                            text=json.dumps({"SMSMessageData": {"Message": "Sent", "Recipients": [rec]}}))
