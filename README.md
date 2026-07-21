# VR-Company

Virtual Reality Company experiment — using [AutoGen](https://github.com/microsoft/autogen) agents to operate a persistent virtual company.

The company starts with a single CEO agent and a small seed configuration. A human acts as the **board of directors** (and later as a **client**). Agents hire peers, write policies, and invent workflows through documents — the runtime only supplies technical mechanisms (messaging, storage, permissions, model access, token accounting).

Design source: [ChatGPT session — Simulering af AI-agenter](https://chatgpt.com/share/6a5f65f6-c27c-83eb-89d2-ea22bf619d40).

## Quick start

See **[GETTING_STARTED.md](GETTING_STARTED.md)** for install, API key setup, and how to run the runtime + CLIs.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
Copy-Item .env.example .env   # then set OPENAI_API_KEY
python -m runtime.main experiment create my-run --start
python -m runtime.main --experiment my-run   # terminal A
python -m interfaces.board_cli               # terminal B
```

Optional live web console (does not replace the runtime or CLIs):

```powershell
pip install -e ".[web]"
python -m runtime.main --experiment my-run   # supervisor: AutoGen worker + web at http://127.0.0.1:8787
python -m runtime.main --no-web              # AutoGen worker only (uses experiments/.active)
```

`python -m runtime.autogen_server --experiment my-run` runs the simulation worker alone. `python -m web.main` still works as a standalone console against an existing worker.

## Layout

```text
runtime/              Supervisor + AutoGen worker + immutable orchestration
seed/                 Immutable experiment template (company, agents, shared)
experiments/<name>/   Mutable per-run copy (gitignored)
interfaces/           board_cli.py, client_cli.py, runtime TCP client
web/                  FastAPI console (live feed + experiments menu)
tests/                Offline smoke tests
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
