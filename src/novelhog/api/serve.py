"""Run the API under uvicorn so that open event streams end cleanly on shutdown.

Uvicorn never tells an ASGI app that it is shutting down while requests are open; it cancels them
only when `timeout_graceful_shutdown` runs out (and logs errors). `Server` therefore raises the
app's `stopping` flag first: event streams see it within one poll and end by themselves.
"""

import socket

import uvicorn
from fastapi import FastAPI


class Server(uvicorn.Server):
    """A uvicorn server that sets `app.state.stopping` when it begins to shut down."""

    def __init__(self, config: uvicorn.Config, app: FastAPI) -> None:
        super().__init__(config)
        self._app = app

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """Tell the app to stop its long-lived streams, then shut down as usual."""
        self._app.state.stopping.set()
        await super().shutdown(sockets)


def build_server(app: FastAPI, *, host: str, port: int, graceful_s: int) -> Server:
    """Build the server; open requests are cancelled after `graceful_s` seconds at the latest."""
    config = uvicorn.Config(app, host=host, port=port, timeout_graceful_shutdown=graceful_s)
    return Server(config, app)


def run(app: FastAPI, *, host: str, port: int, graceful_s: int) -> None:
    """Serve `app` until a stop signal arrives (blocking)."""
    build_server(app, host=host, port=port, graceful_s=graceful_s).run()
