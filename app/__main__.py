"""Run from the repository root with python -m app."""

from contextlib import contextmanager
import os
import signal

import uvicorn

from app.main import app, configure_logging


class GracefulServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Uvicorn drains requests via handle_exit. Do not re-raise SIGTERM after
        # the drain: this entry point intentionally exits with status zero.
        previous = {
            sig: signal.signal(sig, self.handle_exit)
            for sig in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main():
    configure_logging()
    server = GracefulServer(uvicorn.Config(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        log_config=None,
        access_log=False,
    ))
    server.run()


if __name__ == "__main__":
    main()
