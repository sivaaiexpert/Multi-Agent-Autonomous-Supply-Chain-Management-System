"""Start / stop the mock environment on a free local port (used by tests, the demo and the failure suite)."""
from __future__ import annotations

import threading
import time

import uvicorn

from .server import World, create_app


class MockServer:
    def __init__(self, token: str = "mock-token", timeout_delay: float = 3.0):
        self.app = create_app(token, timeout_delay)
        self.token = token
        self.world: World = self.app.state.world
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def start(self) -> MockServer:
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if self._server.started and self._server.servers and self._server.servers[0].sockets:
                port = self._server.servers[0].sockets[0].getsockname()[1]
                self.url = f"http://127.0.0.1:{port}"
                return self
            time.sleep(0.02)
        raise RuntimeError("mock server did not start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def set_scenario(self, name: str) -> None:
        self.world.scenario = name

    def __enter__(self) -> MockServer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
