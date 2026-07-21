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
python -m runtime.main        # terminal A
python -m interfaces.board_cli  # terminal B
```

## Layout

```text
runtime/           Immutable experimental environment (orchestration)
interfaces/        board_cli.py, client_cli.py
company/           Mutable virtual company (docs, prompts, agents, accounting)
shared/            Shared projects, decisions, deliverables
agents/<id>/       Private persistent memory per agent
runtime-data/      Conversations, raw usage logs, runtime state
tests/             Offline smoke tests
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
