from __future__ import annotations

import argparse

import uvicorn

from .api import app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the runc-edge-api service.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", default=8000, type=int, help="Bind port")
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
