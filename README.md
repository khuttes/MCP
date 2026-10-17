# Helpyard local dashboard

Helpyard is a single-page HTML/CSS/JavaScript dashboard that connects to an optional Windows PowerShell bridge. The bridge provides local system information, explicitly approved named actions, and access to a locally running Ollama service.

## Start on Windows

1. Open a regular PowerShell terminal (administrator rights are not needed for normal dashboard use).
2. From this project folder, run:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\bridge.ps1
   ```

   The execution-policy bypass applies only to this new PowerShell process. The bridge listens only on `127.0.0.1` and prints its URL and temporary bearer token.
3. Open `pc-control-dashboard.html` in a current browser. For best browser compatibility, serve the folder on loopback, for example:

   ```powershell
   python -m http.server 8000 --bind 127.0.0.1
   ```

   Then browse to `http://127.0.0.1:8000/pc-control-dashboard.html`.
4. In **Settings**, enter the bridge URL and token printed by the bridge, then select **Save & connect**.
5. Keep the bridge terminal open while using local system features. Stop it with `Ctrl+C`.

Some actions require administrator rights and may be rejected when the bridge runs as a standard user. Elevate only when you intentionally need those actions.

### Start from VS Code

1. Open **Run and Debug** and select **Helpyard: Full Local Development**.
2. The compound starts the local PowerShell bridge and opens the dashboard in Edge. The bridge selects an available port and prints its URL and temporary token in the PowerShell debug session.
3. In dashboard **Settings**, enter the exact URL and token printed by the bridge, then choose **Save & connect**.
4. Stop the compound from VS Code when finished. Do not add the printed token to `launch.json` or any workspace file.

You can also launch **Helpyard: Start Local Bridge** or **Helpyard: Open Dashboard** independently.

### If the dashboard will not connect

- Match the URL and token printed by the **same running bridge**. If the bridge says its preferred port is in use, it switches to another port; update the dashboard URL to the printed URL before connecting.
- An `HTTP 401` message means the token is missing or does not belong to the bridge at that URL. A failed network request usually means the bridge is no longer running or the URL/port is wrong.
- The token is kept in memory only. Enter it again after reloading the dashboard, and keep that bridge's terminal or VS Code debug session open.
- If multiple bridge sessions are open, stop the ones you no longer need with `Ctrl+C` in their own terminals, then start one bridge and use its printed URL/token pair.

## Security and scope

- The bridge is intended for local workstation use and binds to loopback only. Do not expose its port to a LAN or the public internet.
- The bearer token is a local access credential. Do not share it or publish it in screenshots, logs, or source control.
- The dashboard keeps the bridge token in memory only; after reloading the page, paste the current token again in Settings.
- Arbitrary PowerShell execution through `/api/run` is disabled. Terminal and skill screens are intentionally non-executable until reviewed, bounded named tasks are implemented.
- MCP process startup is disabled until a reviewed server registry and fixed executable/argument policies exist.
- Process, service, and high-impact action requests require explicit approval at the bridge API. Do not treat a browser confirmation or bearer token as a substitute for least privilege.
- Project discovery is limited to the folder containing `bridge.ps1`; it does not crawl the user profile or Desktop.
- Cloud provider API keys are used directly by the browser for the current session and are not stored in localStorage. Cloud prompts are sent to the selected provider.
- Ollama is optional. Local AI requires an Ollama service reachable at `http://127.0.0.1:11434` and at least one installed model.
- A public demo should use simulated data and must not connect to a real workstation-control bridge.

## Validation

The bridge can be syntax-checked with the Windows PowerShell parser:

```powershell
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path .\bridge.ps1), [ref]$null, [ref]$errors) | Out-Null
$errors
```

The dashboard's inline JavaScript can be checked with Node.js. Runtime checks should include bridge authentication (valid and invalid tokens), dashboard navigation, offline/error states, and the target browsers actually supported by the release. Do not describe a browser or environment as tested unless it was run.

Run the bridge smoke checks from the project folder in Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\bridge-smoke.ps1
```

The smoke checks use a temporary random token and a local test bridge process. They cover valid and invalid authentication, disabled unsafe operations, approval gates, malformed JSON, and unknown routes. They do not exercise operations that mutate Windows services, processes, configuration, or power state.

Check the dashboard script syntax and its navigation, DOM, and API route connections with Node.js:

```powershell
node .\tests\dashboard-check.js
```

## Current implementation limits

- This is still a local prototype, not a public-release build.
- Persistent audit history, a reviewed MCP server registry, project-folder approval, and named terminal tasks are not implemented.
- Cross-browser, accessibility, and release-security qualification must be performed and recorded separately.
