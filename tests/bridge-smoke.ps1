param(
  [int]$Port = 18917
)

$ErrorActionPreference = 'Stop'
$bridgePath = Join-Path $PSScriptRoot '..\bridge.ps1'
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $bridgePath), [ref]$null, [ref]$parseErrors) | Out-Null
if ($parseErrors) {
  throw "Bridge parser errors: $($parseErrors -join '; ')"
}

$tokenBytes = New-Object byte[] 16
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($tokenBytes)
$token = [BitConverter]::ToString($tokenBytes).Replace('-', '').ToLowerInvariant()
$tempDirectory = Join-Path $env:TEMP ('helpyard-smoke-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $tempDirectory
$stdoutPath = Join-Path $tempDirectory 'bridge.stdout.log'
$stderrPath = Join-Path $tempDirectory 'bridge.stderr.log'
$server = $null
$baseUrl = "http://127.0.0.1:$Port"

function Invoke-BridgeRequest([string]$Path, [string]$RequestToken, [string]$Method = 'GET', [string]$Body = '', [string]$Origin = '', [string]$HostHeader = '') {
  $request = [System.Net.HttpWebRequest]::Create($baseUrl + $Path)
  $request.Method = $Method
  $request.Timeout = 5000
  $request.ReadWriteTimeout = 5000
  if ($RequestToken) { $request.Headers['Authorization'] = "Bearer $RequestToken" }
  if ($Origin) { $request.Headers['Origin'] = $Origin }
  if ($HostHeader) { $request.Host = $HostHeader }
  if ($Body) {
    $request.ContentType = 'application/json'
    $bytes = [Text.Encoding]::UTF8.GetBytes($Body)
    $request.ContentLength = $bytes.Length
    $stream = $request.GetRequestStream()
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
  }

  try {
    $response = $request.GetResponse()
  } catch [System.Net.WebException] {
    if (-not $_.Exception.Response) { throw }
    $response = $_.Exception.Response
  }
  try {
    $reader = New-Object IO.StreamReader($response.GetResponseStream())
    try { $content = $reader.ReadToEnd() } finally { $reader.Dispose() }
    return [pscustomobject]@{ Status = [int]$response.StatusCode; Body = $content; ContentType = $response.ContentType; AllowOrigin = $response.Headers['Access-Control-Allow-Origin'] }
  } finally { $response.Dispose() }
}

function Assert-Status($Response, [int]$Expected, [string]$Name) {
  if ($Response.Status -ne $Expected) {
    throw "$Name expected HTTP $Expected, received $($Response.Status): $($Response.Body)"
  }
  Write-Output "PASS $Name (HTTP $Expected)"
}

try {
  $powershell = Join-Path $PSHOME 'powershell.exe'
  $server = Start-Process -FilePath $powershell -PassThru -WindowStyle Hidden `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$((Resolve-Path $bridgePath).Path)`"", '-Port', "$Port", '-Token', $token) `
    -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

  $ready = $false
  for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($server.HasExited) { throw "Bridge exited during startup. See $stderrPath" }
    try {
      $response = Invoke-BridgeRequest '/api/ping' $token
      if ($response.Status -eq 200) { $ready = $true; break }
    } catch {}
    Start-Sleep -Milliseconds 300
  }
  if (-not $ready) { throw "Bridge did not become ready. See $stdoutPath and $stderrPath" }

  $dashboard = Invoke-BridgeRequest '/pc-control-dashboard.html' ''
  Assert-Status $dashboard 200 'unauthenticated dashboard page'
  if ($dashboard.ContentType -notlike 'text/html*' -or $dashboard.Body -notmatch '<title>Helpyard') { throw 'Dashboard route did not return the HTML dashboard.' }
  Assert-Status (Invoke-BridgeRequest '/' '') 200 'dashboard root page'
  Assert-Status (Invoke-BridgeRequest '/api/bridge-probe' '') 204 'unauthenticated bridge probe'

  $ping = Invoke-BridgeRequest '/api/ping' $token
  Assert-Status $ping 200 'authorized ping'
  if (-not ((ConvertFrom-Json $ping.Body).ok)) { throw 'Authorized ping did not return ok=true.' }

  $health = Invoke-BridgeRequest '/api/health' $token
  Assert-Status $health 200 'health route'
  if (-not ((ConvertFrom-Json $health.Body).ok)) { throw 'Health route did not return ok=true.' }
  $stats = Invoke-BridgeRequest '/api/stats' $token
  Assert-Status $stats 200 'system metrics route'
  $statsBody = ConvertFrom-Json $stats.Body
  if ($null -eq $statsBody.disks) { throw 'System metrics response is missing disks.' }
  foreach ($field in @('cpuCores', 'cpuThreads', 'cpuMaxMHz')) {
    if ($statsBody.PSObject.Properties.Name -notcontains $field) { throw "System metrics response is missing $field." }
  }
  foreach ($route in @('/api/actions', '/api/projects', '/api/processes', '/api/services', '/api/mcp/status')) {
    Assert-Status (Invoke-BridgeRequest $route $token) 200 "$route read route"
  }

  Assert-Status (Invoke-BridgeRequest '/api/ping' '') 401 'missing-token rejection'
  Assert-Status (Invoke-BridgeRequest '/api/ping' 'invalid-token') 401 'invalid-token rejection'
  $preflight = Invoke-BridgeRequest '/api/ping' '' 'OPTIONS' '' 'http://localhost:8000'
  Assert-Status $preflight 204 'localhost CORS preflight'
  if ($preflight.AllowOrigin -ne 'http://localhost:8000') { throw 'Trusted preflight did not echo its exact localhost origin.' }
  Assert-Status (Invoke-BridgeRequest '/api/ping' '' 'OPTIONS' '' 'https://attacker.example') 403 'hostile-origin preflight rejection'
  $badOrigin = Invoke-BridgeRequest '/api/ping' $token 'GET' '' 'https://attacker.example'
  Assert-Status $badOrigin 403 'hostile-origin request rejection'
  if (-not ((ConvertFrom-Json $badOrigin.Body).error -like 'Invalid Origin*')) { throw 'Hostile-origin response did not return a useful JSON error.' }
  $badHost = Invoke-BridgeRequest '/api/ping' $token 'GET' '' '' 'attacker.example'
  Assert-Status $badHost 403 'hostile Host rejection'
  if (-not ((ConvertFrom-Json $badHost.Body).error -like 'Invalid Host*')) { throw 'Hostile Host response did not return a useful JSON error.' }
  Assert-Status (Invoke-BridgeRequest '/api/run' $token 'POST' '{"command":"throw ''must not execute''"}') 403 'arbitrary PowerShell disabled'
  Assert-Status (Invoke-BridgeRequest '/api/mcp/start' $token 'POST' '{}') 403 'unregistered MCP startup disabled'
  Assert-Status (Invoke-BridgeRequest '/api/action' $token 'POST' '{"name":"restart","approved":false}') 403 'high-impact action requires approval'
  Assert-Status (Invoke-BridgeRequest '/api/action' $token 'POST' '{"name":"restart","approved":1}') 403 'approval must be a JSON boolean'
  Assert-Status (Invoke-BridgeRequest '/api/process/stop' $token 'POST' ('{"id":' + $PID + ',"approved":false}')) 403 'process mutation requires approval'
  Assert-Status (Invoke-BridgeRequest '/api/service' $token 'POST' '{"name":"Spooler","action":"stop","approved":false}') 403 'service mutation requires approval'
  Assert-Status (Invoke-BridgeRequest '/api/claude-config' $token 'POST' '{"approved":false,"content":"{}"}') 403 'configuration mutation requires approval'
  Assert-Status (Invoke-BridgeRequest '/api/process/stop' $token 'POST' '[]') 400 'non-object JSON rejected'
  Assert-Status (Invoke-BridgeRequest '/api/process/stop' $token 'POST' '{"id":123,"approved":false,"extra":true}') 400 'unexpected request field rejected'
  Assert-Status (Invoke-BridgeRequest '/api/ollama/chat' $token 'POST' '{"model":"test","messages":[1]}') 400 'invalid AI message shape rejected'
  Assert-Status (Invoke-BridgeRequest '/api/claude-config' $token 'POST' '{"approved":true,"content":') 400 'malformed JSON rejected'
  Assert-Status (Invoke-BridgeRequest '/api/not-a-route' $token) 404 'unknown route'
  Write-Output 'Bridge smoke tests passed.'
} finally {
  if ($server -and -not $server.HasExited) {
    Stop-Process -Id $server.Id -Force
    $server.WaitForExit()
  }
  if (Test-Path -LiteralPath $tempDirectory) {
    Remove-Item -LiteralPath $tempDirectory -Recurse -Force
  }
}
