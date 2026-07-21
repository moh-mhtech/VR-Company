"""Entry point for the optional VR-Company web console."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from interfaces.runtime_client import load_endpoint


def main() -> None:
    runtime_host, runtime_port = load_endpoint()
    parser = argparse.ArgumentParser(description="VR-Company web console (optional)")
    parser.add_argument("--host", default="127.0.0.1", help="Web server bind host")
    parser.add_argument("--port", type=int, default=8787, help="Web server bind port")
    parser.add_argument("--runtime-host", default=runtime_host)
    parser.add_argument("--runtime-port", type=int, default=runtime_port)
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "Web dependencies missing. Install with: pip install -e \".[web]\""
        ) from exc

    from web.app import create_app

    app = create_app(runtime_host=args.runtime_host, runtime_port=args.runtime_port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
