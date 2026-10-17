# Helpyard local dashboard

Helpyard is a single-page HTML/CSS/JavaScript dashboard that connects to an optional Windows PowerShell bridge. The bridge provides local system information, approved actions, and access to a locally running Ollama service.

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

## Security and scope

- The bridge is intended for local workstation use and binds to loopback only. Do not expose its port to a LAN or the public internet.
- The bearer token is a local access credential. Do not share it or publish it in screenshots, logs, or source control.
- The dashboard can execute PowerShell through the bridge. Treat commands and the browser profile as sensitive; only run commands you understand.
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
