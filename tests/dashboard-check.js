'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const dashboardPath = path.join(__dirname, '..', 'pc-control-dashboard.html');
const bridgePath = path.join(__dirname, '..', 'bridge.py');
const html = fs.readFileSync(dashboardPath, 'utf8');
const bridge = fs.readFileSync(bridgePath, 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(scripts.length === 1, `Expected one inline script; found ${scripts.length}.`);
const script = scripts[0][1];
new vm.Script(script, { filename: dashboardPath });
assert(/key !== 'token'/.test(script), 'Bridge token is being persisted instead of kept in memory.');
assert(!/cloudKey\s*:/.test(script), 'Cloud API key must not be part of persisted default state.');
assert(/id="runCmdBtn"[^>]*disabled/.test(html), 'Unrestricted terminal control must remain disabled.');
assert(/function detectBridgeUrl\(\)/.test(script), 'Dashboard does not detect the bridge URL when served by the bridge.');
assert(/function bootstrapBridge\(\)/.test(script) && /location\.hash\.slice\(1\)/.test(script), 'Dashboard does not consume the launcher connection code from the URL fragment.');
assert(/initializeBridge\(\)/.test(script), 'Dashboard does not automatically establish its initial bridge connection.');
assert(/\/api\/bridge-probe/.test(script), 'Dashboard URL discovery does not verify that the origin is the bridge.');

const pageNav = new Set([...html.matchAll(/data-page="([^"]+)"/g)].map((match) => match[1]));
const pageSections = new Set([...html.matchAll(/id="page-([^"]+)"/g)].map((match) => match[1]));
assert(pageNav.size === pageSections.size, 'Navigation and page section counts differ.');
for (const page of pageNav) assert(pageSections.has(page), `Navigation target page-${page} is missing.`);
for (const page of pageSections) assert(pageNav.has(page), `Page page-${page} has no navigation entry.`);

const suggestionKeys = new Set([...script.matchAll(/^\s{6}([a-z]+): \[/gm)].map((match) => match[1]));
for (const page of pageNav) assert(suggestionKeys.has(page), `Page ${page} has no contextual safe tip.`);
assert(/S\.showSuggestions = S\.showSuggestions !== false/.test(script), 'Suggestions preference is not safely defaulted.');
assert(/accentSelect/.test(html) && /suggestionsToggle/.test(html), 'Appearance customization controls are missing.');
assert(/key !== 'token'/.test(script) && !/cloudKey\s*:/.test(script), 'Appearance preferences must not persist secrets.');

const domIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]));
assert(domIds.size === [...html.matchAll(/\bid="([^"]+)"/g)].length, 'Dashboard contains duplicate DOM IDs.');
for (const [, id] of script.matchAll(/getElementById\(['"]([^'"]+)['"]\)/g)) {
  if (id !== 'toast') assert(domIds.has(id), `Dashboard script references missing element #${id}.`);
}

for (const id of ['cpuCores', 'cpuThreads', 'cpuMaxMHz', 'chartSummary', 'suggestionsOpen', 'overviewLocalSummary', 'localSummary']) {
  assert(domIds.has(id), `Redesigned dashboard is missing #${id}.`);
}
assert(/Suggestions for you/.test(html), 'Overview suggestions are missing.');
assert(/Local AI server/.test(html) && /Skill library/.test(html), 'Local AI server or skill library redesign is missing.');
for (const id of ['skillSearch', 'skillStatus', 'skillForm', 'skillName', 'skillCategory', 'skillInstructions', 'skillState', 'newSkillBtn']) {
  assert(domIds.has(id), `Skills Manager is missing #${id}.`);
}
assert(/\/api\/skills/.test(script) && /action: 'run'/.test(script) && /action: 'pin'/.test(script), 'Skills Manager is missing its authenticated read-only Run or Pin operation.');
assert(/esc\(skill\.instructions\)/.test(script), 'Saved skill guidance is not escaped before rendering.');
assert(/custom skills.*cannot be run, do not change permissions/i.test(html), 'Skills Manager does not disclose that custom guidance is non-executable.');
for (const skill of ['Show IP addresses', 'Top memory apps', 'Disk usage', 'Installed apps', 'Open ports', 'Startup programs', 'Defender status', 'Wi-Fi status', 'GPU info', 'Battery', 'Recent updates', 'Dev tool versions', 'Ollama models']) {
  assert(bridge.includes(skill), `Built-in diagnostic ${skill} is missing from the bridge catalog.`);
}
assert(/def run_skill_check\(/.test(bridge) && /RUNNABLE_SKILL_IDS/.test(bridge), 'Skill execution is not bound to the backend allowlist.');
assert(/CPU time \(s\)/.test(html), 'Process CPU usage is not labeled with its actual cumulative-time unit.');
const linkedPages = [...html.matchAll(/data-open-page="([^"]+)"/g)].map((match) => match[1]).filter((page) => !page.includes('${'));
for (const page of linkedPages) assert(pageNav.has(page), `Dashboard quick link points to unknown page ${page}.`);
assert(/NumberOfCores/.test(bridge) && /NumberOfLogicalProcessors/.test(bridge) && /MaxClockSpeed/.test(bridge), 'Bridge does not request processor topology and maximum clock details.');

const dashboardRoutes = new Set([...script.matchAll(/\bapi\(['"](\/api\/[^'"]+)['"]/g)].map((match) => match[1]));
const bridgeRoutes = new Set([...bridge.matchAll(/path == ["'](\/api\/[^"']+)["']/g)].map((match) => match[1]));
bridgeRoutes.add('/api/bridge-probe');
for (const route of dashboardRoutes) assert(bridgeRoutes.has(route), `Dashboard route ${route} is not implemented by the bridge.`);

const linkedAssets = [...html.matchAll(/(?:src|href)="([^"]+)"/g)]
  .map((match) => match[1])
  .filter((value) => !value.startsWith('#') && !value.startsWith('https://') && !value.startsWith('http://'));
assert(linkedAssets.length === 0, `Unexpected external/local assets found: ${linkedAssets.join(', ')}`);

console.log(`Dashboard checks passed: ${pageNav.size} navigation targets, ${dashboardRoutes.size} API routes, ${domIds.size} DOM IDs.`);
