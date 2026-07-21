"""Client terminal — talks privately to a chosen company agent (e.g. sales)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from interfaces.runtime_client import RuntimeClient, load_endpoint


async def interactive(client: RuntimeClient, recipient: str) -> None:
    print("VR-Company client CLI")
    print(f"Recipient: {recipient}")
    print("Commands: /agents  /to <agent_id>  /quit")
    print("Type a message and press Enter.\n")
    conversation_id: str | None = None
    current = recipient
    while True:
        try:
            line = input("client> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/quit", "/exit", ":q"}:
            break
        if line == "/agents":
            resp = await client.request({"op": "list_agents"})
            print(json.dumps(resp, indent=2))
            continue
        if line.startswith("/to "):
            current = line[4:].strip()
            conversation_id = None
            print(f"Recipient set to {current}")
            continue
        resp = await client.request(
            {
                "op": "message",
                "acting_as": "client",
                "recipient": current,
                "content": line,
                "conversation_id": conversation_id,
            }
        )
        if resp.get("conversation_id"):
            conversation_id = resp["conversation_id"]
        if not resp.get("ok"):
            print("ERROR:", resp.get("error") or resp)
            continue
        print(f"\n[{current}]\n{resp.get('content', '')}\n")


async def once(client: RuntimeClient, recipient: str, content: str) -> int:
    resp = await client.request(
        {
            "op": "message",
            "acting_as": "client",
            "recipient": recipient,
            "content": content,
        }
    )
    print(json.dumps(resp, indent=2, default=str))
    return 0 if resp.get("ok") else 1


def main() -> None:
    host, port = load_endpoint()
    parser = argparse.ArgumentParser(description="Client CLI for VR-Company")
    parser.add_argument("--host", default=host)
    parser.add_argument("--port", type=int, default=port)
    parser.add_argument(
        "--recipient",
        default="sales_001",
        help="Initial recipient agent id (use /to to change)",
    )
    parser.add_argument("--message", "-m", help="Send a single message and exit")
    args = parser.parse_args()

    async def _run() -> int:
        client = RuntimeClient(args.host, args.port)
        await client.connect()
        try:
            if args.message:
                return await once(client, args.recipient, args.message)
            await interactive(client, args.recipient)
            return 0
        finally:
            await client.close()

    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
