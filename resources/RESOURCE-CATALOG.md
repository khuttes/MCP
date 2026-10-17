# Helpyard resource and dependency notes

## Current project requirements

| Area | Current choice | External package required? |
| --- | --- | --- |
| Local bridge | Python 3.10+ standard library | No |
| Dashboard interface | Native HTML, CSS, and JavaScript | No |
| CPU/memory graph | Browser canvas API | No |
| Local language models | Optional separately installed Ollama service | Not a Python package |
| Cloud language models | Browser requests to the selected provider; user supplies a session-only key | No project package |

This project intentionally does not require pip installation, an npm build, or a CDN to start. Do not download assets or run an installer just to apply visual styling.

## Download and review policy

No third-party package or asset was downloaded for this implementation. Before adding one in the future:

1. State which feature needs it and why the existing platform APIs are insufficient.
2. Verify the official project, current supported version, maintenance status, and license.
3. Prefer pinned, locally vendored assets over a runtime CDN dependency; include the license and a recorded integrity hash.
4. Review transitive dependencies and update the setup/troubleshooting documentation.
5. Test startup with network access disabled and run the bridge/dashboard checks.
6. Never execute downloaded skills, MCP servers, scripts, or installers automatically. Require source review and explicit user approval.

## Framework evaluation (not installed)

NiceGUI was reviewed as a possible Python-first browser UI framework for a future, explicitly approved migration. On 17 October 2026, official project metadata listed release 3.18.0, Python `>=3.10,<4` (including Python 3.10-3.14 classifiers), MIT licensing, and Production/Stable status. The latest GitHub release was published 7 October 2026; recent repository commits were dated 16 October 2026. Its documentation describes a browser UI backed by FastAPI/Starlette/Uvicorn and Vue/Quasar frontend components.

NiceGUI is not adopted for the current visual refresh. The current dashboard already has a custom browser SPA, local bridge API, and one-time bootstrap/token flow. Replacing that UI with NiceGUI would add and require reviewing a transitive Python dependency tree, frontend assets, and a second application-serving model without being necessary for these changes. See the [implementation strategy](./Helpyard-Implementation-Strategy.md) for the rationale and migration gate.

No charting package or other UI package is recommended or needed for the current short CPU/memory plot. Browser-native canvas is sufficient. Revisit third-party packages only if a measured product need exists and after verifying current official source, compatible versions, license/redistribution terms, maintenance, security advisories, dependencies, accessibility, and offline behavior.

## Verified safety boundary

This catalog is informational; it does not add an installer, arbitrary resource fetch endpoint, skill execution, MCP startup, or shell capability. The bridge remains loopback-only and existing approval rules remain in force.
