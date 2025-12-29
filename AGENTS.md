# Repository Guidelines

## Project Structure & Module Organization
- `app.py`: FastAPI service plus frontend template, device discovery, NextDNS/Tailscale checks, and network helpers.
- `config.yaml`: Default dashboard, discovery, and device definitions. Override path with `DASH_CONFIG=/path/to/config.yaml`.
- `requirements.txt`: Runtime dependencies (FastAPI, Uvicorn, requests, pyyaml, psutil).
- `status.json`: Example discovery output; useful as a reference shape for responses.
- `reproduce_tailscale_bug.py`: Minimal script to reproduce Tailscale status calls in main and `asyncio.to_thread`.

## Setup, Build, and Run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py                       # Serve on configured host/port (defaults 0.0.0.0:8080)
# or for dev hot reload:
uvicorn app:app --reload --host 0.0.0.0 --port 8080
```
Use `DASH_CONFIG` to point at an alternate YAML during local testing.

## Coding Style & Naming Conventions
- Python 3, 4-space indentation, and PEP 8 readability; keep line length reasonable for diffs (~100–120 chars).
- Prefer explicit type hints and dataclasses (already used for device models).
- Functions that shell out or touch the network should stay small and log return codes/stderr for troubleshooting.
- Name helpers descriptively (`parse_known_devices`, `_run_async`); keep private helpers prefixed with `_`.

## Testing Guidelines
- No automated suite yet; before pushing, run a local smoke test by starting the app and loading the dashboard UI.
- When adding tests, place them under `tests/` and use `pytest`; prefer fixtures over ad-hoc sleeps for async code.
- For network-sensitive changes, capture a sample `status.json` to verify expected fields remain stable.

## Commit & Pull Request Guidelines
- Write imperative, specific commit subjects (e.g., `Improve neighbor timeout handling`); keep commits scoped and reviewable.
- In PRs, include: what changed, why, and how you verified (commands run or manual steps). Add screenshots/gifs if UI changes.
- Link issues/tickets when available and call out any configuration assumptions (e.g., reliance on `DASH_CONFIG` overrides).

## Configuration & Security Notes
- Keep secrets out of `config.yaml`; prefer local overrides via `DASH_CONFIG`.
- Be cautious with discovery defaults (`bounded_sweep`, `max_sweep_hosts`) to avoid noisy scans on shared networks.
- When adjusting shell commands (ping, arp, tailscale), note platform-specific flags in code comments to avoid regressions.
