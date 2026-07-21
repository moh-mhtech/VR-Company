# Getting Started — VR-Company AutoGen Experiment

This guide covers the **manual steps** you must complete before the experiment can run. The repository already contains the seed company, runtime, and CLIs.

## Prerequisites

- Python **3.11+** (3.12 is fine)
- An OpenAI API key (or OpenAI-compatible endpoint credentials)
- Git (optional, for syncing with [moh-mhtech/VR-Company](https://github.com/moh-mhtech/VR-Company))
- GitHub CLI `gh` (optional, only if you want to push from this machine)

## 1. Open a terminal in the project folder

```powershell
cd "c:\Users\mlhil\Desktop\Research Projects\VR Company"
```

## 2. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 3. Install AutoGen and dependencies

Either:

```powershell
pip install -U pip
pip install -r requirements.txt
pip install -e .
```

Or:

```powershell
pip install -U pip
pip install -e ".[dev]"
```

This installs:

- `autogen-agentchat` / `autogen-ext[openai]` — agent runtime
- `pyyaml`, `python-dotenv` — config and secrets loading
- `pytest` — smoke tests (via requirements or `[dev]`)

## 4. Configure the model secret

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.4-mini
AGENTOPS_API_KEY=...
```

Notes:

- Never commit `.env` (it is gitignored).
- Default model is **`gpt-5.4-mini`**. With OpenAI **traffic sharing**, mini/nano models get up to **2.5M free tokens/day** (including `gpt-5.4-mini` and `gpt-5.4-nano`). Full models like `gpt-5.4` share a smaller **250k/day** free pool.
- Prefer **`gpt-5.4-mini`** for experiment quality within the large free tier. Switch to **`gpt-5.4-nano`** only if you need to stretch token budget. Use **`gpt-5.4`** sparingly when you need higher quality (burns the 250k pool faster with multi-agent tool loops).
- Usage beyond free quotas is billed at [standard rates](https://developers.openai.com/api/docs/pricing).
- **AgentOps (optional):** create an API key at [app.agentops.ai](https://app.agentops.ai), set `AGENTOPS_API_KEY`, then restart the runtime. Live LLM/tool traces appear in the AgentOps drilldown. Without the key, the runtime still runs normally.
- Restart `python -m runtime.main` after changing `.env`.
- For Azure OpenAI or other compatible gateways, you may need additional env vars supported by `autogen-ext` OpenAI clients.

## 5. (Optional) Verify offline pieces

Smoke tests do **not** call the model:

```powershell
pytest -q
```

## 6. Start the central runtime

In terminal A:

```powershell
python -m runtime.main
```

You should see a log line that the runtime is listening on `127.0.0.1:8765`.

If `AGENTOPS_API_KEY` is set, you should also see `AgentOps enabled` and can open [app.agentops.ai/drilldown](https://app.agentops.ai/drilldown) while you chat in the board/client CLIs.

Leave this process running.

## 7. Open the board CLI (human = board of directors)

In terminal B (venv activated):

```powershell
python -m interfaces.board_cli
```

**Important:** the interactive prompt submits on every Enter. Do not paste multi-line text. Type or paste **one single line**, then press Enter once.

Example first instruction (Phase 1 of the experiment plan) — paste as one line:

```text
Start a software development company. Hire a software developer and a sales agent. Create their agent specification YAML files under company/agents/, start them, and update company/organization.md. Keep the structure minimal.
```

Useful commands inside the board CLI:

- `/agents` — list agents
- `/quit` — exit

For a longer message without interactive pasting, use one-shot mode (`-m` is still one line):

```powershell
python -m interfaces.board_cli -m "Start a software development company. Hire a software developer and a sales agent. Create their agent specification YAML files under company/agents/, start them, and update company/organization.md. Keep the structure minimal."
```

Other one-shot example:

```powershell
python -m interfaces.board_cli -m "List current company status from organization.md"
```

## 8. Client CLI (after Sales exists)

When the CEO has hired a sales agent (for example `sales_001`):

```powershell
python -m interfaces.client_cli --recipient sales_001
```

Same rule as the board CLI: **one line per message** — Enter submits immediately.

Example client message (single line):

```text
Hi, I need a small internal tool that tracks project proposals. Can you prepare a short proposal and price estimate?
```

Commands:

- `/to sales_001` — change recipient
- `/agents` — list agents
- `/quit` — exit

One-shot:

```powershell
python -m interfaces.client_cli --recipient sales_001 -m "Hi, I need a small internal tool that tracks project proposals. Can you prepare a short proposal and price estimate?"
```

## 9. Wire this folder to GitHub (optional)

This machine did **not** have `gh` logged in when the project was scaffolded. To push:

```powershell
gh auth login
```

If this folder is not yet a git repo linked to the remote:

```powershell
git init
git remote add origin https://github.com/moh-mhtech/VR-Company.git
git fetch origin
git checkout -b main
git add .
git status
git commit -m "Scaffold AutoGen virtual company runtime and seed configuration"
git push -u origin main
```

If `main` already exists on the remote with only README/LICENSE, you may need to pull/rebase or force-with-lease after reviewing remote history. Ask before force-pushing.

## Experiment phases (once running)

Use **single-line** board/client messages for each step (or `-m "..."`).

1. **Formation** — board: `Hire a software developer and a sales agent, create their YAML specs, start them, and update organization.md.` Then client via `client_cli` with a one-line project request.
2. **Quality** — board: `Hire an ISO 9001 specialist and have them work with you to document quality policies and processes.`
3. **Finance (later)** — board: `Hire a financial controller to review token usage and improve company/accounting/accounting_plugin.py without altering raw usage logs.`

## Troubleshooting

| Symptom | What to check |
|--------|----------------|
| `Cannot connect to runtime` | Start `python -m runtime.main` first |
| `OPENAI_API_KEY is not set` | Create `.env` from `.env.example` and restart the runtime |
| No AgentOps sessions | Set `AGENTOPS_API_KEY`, `pip install agentops`, restart runtime, then send a board/client message |
| `Access denied` / protected path | Agents cannot touch `runtime/`, `.env`, or raw `runtime-data/accounting/raw-usage.jsonl` |
| Import errors for `autogen_*` | Confirm venv is active and `pip install -r requirements.txt` succeeded |

## What was already set up for you

- Seed company docs, access control, CEO agent YAML
- Revised immutable / base / CEO prompts
- Runtime (message router, permissions, model gateway, accounting plugin hook)
- `board_cli` / `client_cli`
- Smoke tests without live API calls

Docker-based hard isolation is **not** required for v1; path-based permissions are enforced in-process. Container isolation can be added later without changing company documents.
