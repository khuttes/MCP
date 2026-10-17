# Helpyard local dashboard

Helpyard is a single-page HTML/CSS/JavaScript dashboard with a local Python bridge for Windows system data and explicitly approved operations. The bridge serves the dashboard and API from one loopback-only HTTP server. Python uses only its standard library; no pip packages or frontend build step are required.

## Start with one double-click

1. Install Python 3.10 or newer for Windows. Enable the Python launcher (`py`) if offered; the launcher can also use a `python` command on `PATH`.
2. Double-click [`Start-Helpyard.bat`](./Start-Helpyard.bat).
3. Helpyard starts its minimized bridge console and opens the dashboard in your default browser. Leave the bridge running while you use the dashboard; close its console or press `Ctrl+C` there to stop it.

The dashboard is served at `http://127.0.0.1:<selected-port>/pc-control-dashboard.html`. The browser receives a short-lived one-time connection code in the URL fragment; the frontend removes it from the address bar, exchanges it for a bearer token, and keeps that token in memory only. The token is not printed, saved to browser storage, or included in server request logs. If the browser does not open automatically, use the **Dashboard** or **Connection URL** printed in the bridge console.

## Start from VS Code or a terminal

- In VS Code, run **Terminal > Run Task... > Helpyard: Start Bridge**. The task starts the same Python bridge and opens the dashboard.
- From PowerShell in the project folder, run `py -3 .\bridge.py --open-browser`.
- For a static UI preview only, open `pc-control-dashboard.html` directly; it will not connect to local system APIs.

The bridge checks a short list of localhost ports starting at 8787 and selects the first available port. It runs one instance per project folder. The dashboard discovers and uses the actual selected port.

## Dashboard appearance and resources

- Overview and all dashboard sections provide contextual safety tips. Disable them in **Settings** if preferred.
- Choose a dashboard accent in **Settings**, or switch between dark and light themes from the header. These non-secret preferences are saved in this browser.
- Overview displays responsive CPU/memory metrics and history, processor core/thread/maximum-clock details, disk capacity, and section-linked suggestions. CPU/memory history is a short, in-memory view sampled every five seconds while Overview is open; it is not persisted or a long-term capacity report.
- Process-list CPU values are cumulative processor time in seconds, not utilization percentages.
- Local AI distinguishes an unavailable Ollama service from an available service with no installed models. The Skills page provides 13 fixed, read-only diagnostic checks and locally saved declarative custom guidance. Built-in checks use backend allowlists; editing their text does not alter the operation. Custom guidance is never executed, and no skill is automatically added to AI prompts or granted permissions.
- Saved Skills use schema version 2 in `%APPDATA%\Helpyard\skills.json` (or `~\.helpyard\skills.json` when `APPDATA` is unavailable). The authenticated `GET /api/skills` route reads the catalog; `POST /api/skills` accepts `create`, `update`, `delete`, `pin`, and built-in-only `run` actions. Catalog mutations create timestamped `.bak-*` copies before atomic replacement; invalid, oversized, or unsupported data is reported and preserved. The JSON file is plaintext under the current user's profile; do not store secrets in skill descriptions or instructions.
- No Python pip packages or third-party frontend assets are required. The dashboard uses native HTML, CSS, JavaScript, and canvas, keeping startup offline-capable and avoiding unreviewed downloads.
- The [`resources`](./resources/) folder contains the [implementation strategy](./resources/Helpyard-Implementation-Strategy.md), its [PDF version](./resources/Helpyard-Implementation-Strategy.pdf), and resource/dependency notes. No third-party package or asset was downloaded for the redesign.

## Troubleshooting

- If the launcher says Python 3.10 or newer is missing, install Python for Windows and ensure either `py -3` or `python` works in a new terminal.
- Keep the minimized **Helpyard Bridge** console open. Its startup failures and runtime errors are printed there.
- A `401` means the browser session is missing or the bootstrap code was already consumed. Close the bridge console and start `Start-Helpyard.bat` again to create a fresh session.
- If a port is occupied, the bridge tries the next configured loopback port automatically.
- Ollama is optional. Local chat requires Ollama running at `http://127.0.0.1:11434` and at least one installed model.
- Cloud chat calls the selected provider directly from the browser and requires a provider API key entered for the current session.

## Security and scope

- The Python bridge binds only to `127.0.0.1`; do not expose it to a LAN or the public internet.
- The one-time bootstrap code expires after 60 seconds and can be redeemed once from the exact local browser origin. The resulting bearer token remains in JavaScript memory only.
- Arbitrary PowerShell execution through `/api/run` and MCP process startup remain disabled. Terminal commands and custom Skills are non-executable; the built-in Skills Run buttons invoke only fixed read-only checks.
- Process, service, Claude configuration, and high-impact action requests require explicit API approval. A browser confirmation is not a substitute for least privilege.
- Windows operations use fixed PowerShell scripts launched as subprocesses. Browser input is validated and is not passed as an arbitrary script.
- Project discovery is limited to this project folder and immediate Git repositories.
- Cloud provider API keys are not persisted in browser storage; requests are sent to the selected provider.
- A public demo should use simulated data, not a real workstation-control bridge.
- `bridge.ps1` remains available as a legacy/manual alternative, but the one-click launcher, VS Code task, and current dashboard bootstrap flow use `bridge.py`.

## Validation

Run from the project folder:

```powershell
py -3 -m unittest discover -s tests -p test_bridge.py -v
node .\tests\dashboard-check.js
```

The dashboard check also verifies that every navigation section has a contextual tip and that customization does not change the secret-storage safeguards.

The Python unit tests cover dashboard serving, bootstrap expiry/replay protections, authentication, route behavior, approval gates, malformed/oversized input, Skills schema persistence and corruption handling, and keeping ping responsive during a simulated slow Ollama chat. They stub Windows-specific and Ollama calls where appropriate; they do not mutate processes, services, configuration, or power state.

The legacy PowerShell bridge has a separate smoke suite:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\bridge-smoke.ps1
```

That suite tests only `bridge.ps1`. Use the Python unit tests for the default launcher and runtime.

## Current implementation limits

- This is a local prototype, not a public-release build.
- Persistent audit history, a reviewed MCP server registry, project-folder approval, and named terminal tasks are not implemented.
- Cloud provider access requires the user's own key. Cross-browser, accessibility, and release-security qualification must be performed and recorded separately.
