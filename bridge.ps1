<#
  PC Control Bridge  -  local PowerShell server for the HTML dashboard
  Run:   powershell -ExecutionPolicy Bypass -File .\bridge.ps1
  The window prints a URL and a TOKEN. The URL serves the dashboard and API.
  Listens ONLY on 127.0.0.1 (your own computer). API requests need the token.
#>
param(
  [int]$Port = 8787,
  [string]$Token = '',
  [string]$OllamaUrl = 'http://127.0.0.1:11434',
  [switch]$OpenDashboard
)

$script:MaxRequestBytes = 1048576
$script:DashboardPath = Join-Path $PSScriptRoot 'pc-control-dashboard.html'
if (-not (Test-Path -LiteralPath $script:DashboardPath -PathType Leaf)) {
  throw "Dashboard file not found: $script:DashboardPath"
}

if (-not $Token) {
  $bytes = New-Object byte[] 16
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $Token = ([BitConverter]::ToString($bytes) -replace '-', '').ToLower()
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$script:mcp = @{}
$script:ActionRegistry = @{
  'lock' = @{ name='lock'; description='Lock the current Windows session'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=10; outputLimit=2048; audit='security screen lock' }
  'sleep' = @{ name='sleep'; description='Suspend the computer'; permission='standard'; risk='medium'; requiresApproval=$true; timeoutSec=15; outputLimit=1024; audit='power management' }
  'restart' = @{ name='restart'; description='Restart the computer after a short delay'; permission='admin'; risk='high'; requiresApproval=$true; timeoutSec=20; outputLimit=1024; audit='system reboot' }
  'shutdown' = @{ name='shutdown'; description='Shut down the computer after a short delay'; permission='admin'; risk='high'; requiresApproval=$true; timeoutSec=20; outputLimit=1024; audit='system shutdown' }
  'cancel' = @{ name='cancel'; description='Cancel the pending shutdown or restart'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=10; outputLimit=1024; audit='shutdown cancellation' }
  'flushdns' = @{ name='flushdns'; description='Flush the Windows DNS cache'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=15; outputLimit=1024; audit='network refresh' }
  'taskmgr' = @{ name='taskmgr'; description='Open Task Manager'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=10; outputLimit=512; audit='system utility open' }
  'explorer' = @{ name='explorer'; description='Open File Explorer'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=10; outputLimit=512; audit='file explorer launch' }
  'settings' = @{ name='settings'; description='Open Windows Settings'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=10; outputLimit=512; audit='system settings' }
  'devmgr' = @{ name='devmgr'; description='Open Device Manager'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=10; outputLimit=512; audit='device manager launch' }
  'emptybin' = @{ name='emptybin'; description='Empty the Recycle Bin'; permission='standard'; risk='medium'; requiresApproval=$true; timeoutSec=15; outputLimit=1024; audit='file deletion' }
  'cleantemp' = @{ name='cleantemp'; description='Clean the user temporary folder'; permission='standard'; risk='medium'; requiresApproval=$true; timeoutSec=20; outputLimit=1024; audit='temporary file cleanup' }
  'perf_high' = @{ name='perf_high'; description='Enable the high performance power scheme'; permission='admin'; risk='medium'; requiresApproval=$true; timeoutSec=15; outputLimit=1024; audit='power policy change' }
  'perf_balanced' = @{ name='perf_balanced'; description='Enable the balanced power scheme'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=15; outputLimit=1024; audit='power policy change' }
  'perf_saver' = @{ name='perf_saver'; description='Enable the power saver scheme'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=15; outputLimit=1024; audit='power policy change' }
  'winupdate' = @{ name='winupdate'; description='Open Windows Update settings'; permission='standard'; risk='low'; requiresApproval=$false; timeoutSec=10; outputLimit=512; audit='update settings' }
}

function Send-Json($ctx, $obj, [int]$code = 200) {
  $json = if ($null -eq $obj) { 'null' } else { ConvertTo-Json -InputObject $obj -Depth 8 -Compress }
  $b = [Text.Encoding]::UTF8.GetBytes($json)
  $ctx.Response.StatusCode = $code
  $ctx.Response.ContentType = 'application/json; charset=utf-8'
  $ctx.Response.Headers['Cache-Control'] = 'no-store'
  $ctx.Response.OutputStream.Write($b, 0, $b.Length)
  $ctx.Response.Close()
}

function Send-Dashboard($ctx) {
  $bytes = [IO.File]::ReadAllBytes($script:DashboardPath)
  $ctx.Response.StatusCode = 200
  $ctx.Response.ContentType = 'text/html; charset=utf-8'
  $ctx.Response.Headers['Cache-Control'] = 'no-store'
  $ctx.Response.Headers['X-Content-Type-Options'] = 'nosniff'
  $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
  $ctx.Response.Close()
}

function Read-Body($ctx) {
  if ($ctx.Request.ContentLength64 -gt $script:MaxRequestBytes) {
    throw 'Request body exceeds the 1 MiB limit.'
  }

  $stream = $ctx.Request.InputStream
  $buffer = New-Object byte[] 8192
  $memory = New-Object IO.MemoryStream
  try {
    while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
      if (($memory.Length + $read) -gt $script:MaxRequestBytes) {
        throw 'Request body exceeds the 1 MiB limit.'
      }
      $memory.Write($buffer, 0, $read)
    }
    $encoding = New-Object System.Text.UTF8Encoding($false, $true)
    try {
      return $encoding.GetString($memory.ToArray())
    } catch {
      throw 'Request body must contain valid UTF-8.'
    }
  } finally {
    $memory.Dispose()
  }
}

function Read-JsonBody($ctx) {
  $raw = Read-Body $ctx
  if ([string]::IsNullOrWhiteSpace($raw)) {
    throw 'A JSON request body is required.'
  }
  try {
    $body = ConvertFrom-Json -InputObject $raw -ErrorAction Stop
  } catch {
    throw "Invalid JSON request body: $($_.Exception.Message)"
  }
  if ($null -eq $body -or $body -is [string] -or $body -is [ValueType] -or $body -is [Array]) {
    throw 'The JSON request body must be an object.'
  }
  return $body
}

function Assert-AllowedProperties($body, [string[]]$allowedFields) {
  foreach ($property in $body.PSObject.Properties) {
    if ($property.Name -notin $allowedFields) {
      throw "Unexpected request field '$($property.Name)'."
    }
  }
}

function Get-AvailablePort([int]$PreferredPort = 8787) {
  $candidatePorts = @($PreferredPort, 8788, 8789, 8790, 8791, 8792, 8800, 9000, 9001)
  foreach ($candidate in $candidatePorts) {
    $probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $candidate)
    try {
      $probe.Start()
      return $candidate
    }
    catch {
      continue
    }
    finally {
      $probe.Stop()
    }
  }
  throw 'No free localhost port was available in the default range.'
}

function Test-LocalRequest([System.Net.HttpListenerRequest]$req) {
  $hostHeader = [string]$req.Headers['Host']
  $origin = [string]$req.Headers['Origin']
  $remoteIp = $req.RemoteEndPoint.Address.ToString()

  $loopbackIps = @('127.0.0.1', '::1', '[::1]', '::ffff:127.0.0.1', '[::ffff:127.0.0.1]')
  if ($remoteIp -notin $loopbackIps) {
    throw 'Only localhost requests are allowed.'
  }

  if ($hostHeader) {
    $hostHeader = $hostHeader.Trim()
    $validHost = ($hostHeader -match '^(localhost|127\.0\.0\.1|\[::1\]|\[::ffff:127\.0\.0\.1\]|::1|::ffff:127\.0\.0\.1)(:\d+)?$')
    if (-not $validHost) {
      throw 'Invalid Host header.'
    }
  }

  if ($origin -and $origin -ne 'null') {
    $validOrigin = ($origin -match '^https?://(localhost|127\.0\.0\.1|\[::1\]|\[::ffff:127\.0\.0\.1\]|::1|::ffff:127\.0\.0\.1)(:\d+)?$')
    if (-not $validOrigin) {
      throw 'Invalid Origin header.'
    }
  }
}

function Assert-Authorized([System.Net.HttpListenerRequest]$req) {
  $auth = [string]$req.Headers['Authorization']
  if (-not $auth) {
    throw 'Unauthorized: wrong or missing token.'
  }

  $normalized = $auth.Trim()
  if ($normalized -match '^Bearer\s+(.+)$') {
    $normalized = $matches[1].Trim()
  }

  if (-not $normalized -or $normalized -ne $Token) {
    throw 'Unauthorized: wrong or missing token.'
  }
}

function Get-Projects() {
  $roots = @($PSScriptRoot)

  $items = @()
  foreach ($root in ($roots | Select-Object -Unique)) {
    if (-not $root) { continue }
    if (-not (Test-Path $root)) { continue }
    $items += [pscustomobject]@{
      name = Split-Path $root -Leaf
      path = $root
      kind = 'folder'
      accessible = $true
    }
  }

  $gitDirs = @()
  foreach ($root in ($roots | Select-Object -Unique)) {
    if (-not $root -or -not (Test-Path $root)) { continue }
    $gitDirs += Get-ChildItem -Path $root -Directory -ErrorAction Stop | Where-Object { Test-Path (Join-Path $_.FullName '.git') }
  }

  foreach ($d in ($gitDirs | Select-Object -Unique -ExpandProperty FullName)) {
    $name = Split-Path $d -Leaf
    $items += [pscustomobject]@{
      name = $name
      path = $d
      kind = 'git'
      accessible = $true
      branch = (git -C $d rev-parse --abbrev-ref HEAD 2>$null)
      status = (git -C $d status --short --branch 2>$null)
    }
  }

  return @($items | Sort-Object name -Unique)
}

$actions = @{
  'lock' = { rundll32.exe user32.dll,LockWorkStation }
  'sleep' = { rundll32.exe powrprof.dll,SetSuspendState 0,1,0 }
  'restart' = { shutdown.exe /r /t 30 }
  'shutdown' = { shutdown.exe /s /t 30 }
  'cancel' = { shutdown.exe /a }
  'flushdns' = { ipconfig /flushdns }
  'taskmgr' = { Start-Process taskmgr.exe }
  'explorer' = { Start-Process explorer.exe }
  'settings' = { Start-Process 'ms-settings:' }
  'devmgr' = { Start-Process devmgmt.msc }
  'emptybin' = { Clear-RecycleBin -Force -ErrorAction SilentlyContinue; 'Recycle Bin emptied' }
  'perf_high' = { powercfg /setactive SCHEME_MIN; 'High performance plan active' }
  'perf_balanced' = { powercfg /setactive SCHEME_BALANCED; 'Balanced plan active' }
  'perf_saver' = { powercfg /setactive SCHEME_MAX; 'Power saver plan active' }
  'winupdate' = { Start-Process 'ms-settings:windowsupdate' }
  'cleantemp' = { Get-ChildItem $env:TEMP -Recurse -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; 'User temp folder cleaned' }
}

try {
  $SelectedPort = Get-AvailablePort -PreferredPort $Port
  if ($SelectedPort -ne $Port) {
    Write-Host "  Port $Port was already in use; switching to $SelectedPort" -ForegroundColor Yellow
  }

  $listener = New-Object System.Net.HttpListener
  $listener.Prefixes.Add("http://127.0.0.1:$SelectedPort/")
  $listener.Start()

  $dashboardUrl = "http://127.0.0.1:$SelectedPort/pc-control-dashboard.html"
  Write-Host ""
  Write-Host "  PC Control Bridge is running" -ForegroundColor Green
  Write-Host "  Bridge URL : http://127.0.0.1:$SelectedPort"
  Write-Host "  Dashboard  : $dashboardUrl"
  Write-Host "  TOKEN : $Token" -ForegroundColor Yellow
  Write-Host "  Admin : $isAdmin"
  Write-Host "  Press Ctrl+C to stop."
  Write-Host ""

  if ($OpenDashboard) {
    Start-Process $dashboardUrl
  }

  while ($listener.IsListening) {
    $ctx = $listener.GetContext()
    $req = $ctx.Request
    $res = $ctx.Response
    $origin = [string]$req.Headers['Origin']
      try {
        Test-LocalRequest $req
      } catch {
        if ($req.HttpMethod -eq 'OPTIONS') {
          $res.StatusCode = 403
          $res.Close()
        } else {
          Send-Json $ctx @{ error = $_.Exception.Message } 403
        }
        continue
      }
      if ($origin) { $res.Headers['Access-Control-Allow-Origin'] = $origin }
      $res.Headers['Vary'] = 'Origin'
      $res.Headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
      $res.Headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'

      if ($req.HttpMethod -eq 'OPTIONS') { $res.StatusCode = 204; $res.Close(); continue }
      if ($req.HttpMethod -eq 'GET' -and $req.Url.AbsolutePath -eq '/api/bridge-probe') {
        $res.StatusCode = 204
        $res.Close()
        continue
      }

      if ($req.HttpMethod -eq 'GET' -and $req.Url.AbsolutePath -in @('/', '/pc-control-dashboard.html')) {
        Send-Dashboard $ctx
        continue
      }

      try {
        Assert-Authorized $req

      $route = "$($req.HttpMethod) $($req.Url.AbsolutePath)"
      switch ($route) {
        'GET /api/ping' {
          Send-Json $ctx @{ ok = $true; admin = $isAdmin; host = $env:COMPUTERNAME; user = $env:USERNAME }
        }

        'GET /api/health' {
          $os = Get-CimInstance Win32_OperatingSystem
          Send-Json $ctx @{
            ok = $true
            admin = $isAdmin
            host = $env:COMPUTERNAME
            user = $env:USERNAME
            os = $os.Caption
            build = $os.BuildNumber
            uptimeHours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
            time = (Get-Date).ToString('o')
          }
        }

        'GET /api/actions' {
          $registry = @()
          foreach ($entry in $script:ActionRegistry.Values) {
            $registry += [pscustomobject]$entry
          }
          Send-Json $ctx @($registry)
        }

        'GET /api/projects' {
          Send-Json $ctx @(Get-Projects)
        }

        'GET /api/stats' {
          $os   = Get-CimInstance Win32_OperatingSystem
          $processors = @(Get-CimInstance Win32_Processor)
          $cpu  = ($processors | Measure-Object LoadPercentage -Average).Average
          $cpuN = ($processors | Select-Object -First 1).Name
          $cpuCores = ($processors | Measure-Object NumberOfCores -Sum).Sum
          $cpuThreads = ($processors | Measure-Object NumberOfLogicalProcessors -Sum).Sum
          $cpuMaxMHz = ($processors | Measure-Object MaxClockSpeed -Maximum).Maximum
          $disks = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
            @{ name = $_.DeviceID; totalGB = [math]::Round($_.Size/1GB,1); freeGB = [math]::Round($_.FreeSpace/1GB,1) }
          }
          Send-Json $ctx @{
            host = $env:COMPUTERNAME; user = $env:USERNAME; admin = $isAdmin
            os = $os.Caption; build = $os.BuildNumber; cpuName = $cpuN
            cpuCores = [int]$cpuCores; cpuThreads = [int]$cpuThreads; cpuMaxMHz = [int]$cpuMaxMHz
            cpu = [int]$cpu
            ramTotalGB = [math]::Round($os.TotalVisibleMemorySize/1MB,1)
            ramFreeGB  = [math]::Round($os.FreePhysicalMemory/1MB,1)
            uptimeHours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours,1)
            disks = @($disks)
            processCount = (Get-Process).Count
          }
        }

        'GET /api/processes' {
          $p = Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 60 | ForEach-Object {
            @{ id = $_.Id; name = $_.ProcessName; cpu = [math]::Round([double]$_.CPU,1); memMB = [math]::Round($_.WorkingSet64/1MB,0) }
          }
          Send-Json $ctx @($p)
        }

        'POST /api/process/stop' {
          $b = Read-JsonBody $ctx
          Assert-AllowedProperties $b @('id', 'approved')
          if ($b.id -isnot [ValueType] -or [int]$b.id -le 0) { throw 'A valid process ID is required.' }
          if ($b.approved -isnot [bool] -or -not $b.approved) { throw 'Stopping a process requires explicit approval.' }
          if ([int]$b.id -eq $PID) { throw 'The bridge process cannot stop itself.' }
          Stop-Process -Id ([int]$b.id) -Force -ErrorAction Stop
          Send-Json $ctx @{ ok = $true }
        }

        'GET /api/services' {
          $s = Get-Service | ForEach-Object {
            @{ name = $_.Name; display = $_.DisplayName; status = "$($_.Status)"; start = "$($_.StartType)" }
          }
          Send-Json $ctx @($s)
        }

        'POST /api/service' {
          $b = Read-JsonBody $ctx
          Assert-AllowedProperties $b @('name', 'action', 'approved')
          if ([string]$b.name -notmatch '^[A-Za-z0-9_.-]{1,128}$') { throw 'A valid exact service name is required.' }
          if ($b.approved -isnot [bool] -or -not $b.approved) { throw 'Changing a service requires explicit approval.' }
          $service = Get-Service -Name ([string]$b.name) -ErrorAction Stop
          switch ($b.action) {
            'start'   { Start-Service   -InputObject $service -ErrorAction Stop }
            'stop'    { Stop-Service    -InputObject $service -Force -ErrorAction Stop }
            'restart' { Restart-Service -InputObject $service -Force -ErrorAction Stop }
            default   { throw "Unknown service action" }
          }
          Send-Json $ctx @{ ok = $true }
        }

        'POST /api/action' {
          $b = Read-JsonBody $ctx
          Assert-AllowedProperties $b @('name', 'approved')
          if (-not $b -or -not $b.name) { throw 'Action name is required.' }
          if (-not $actions.ContainsKey($b.name)) { throw "Unknown action '$($b.name)'" }

          $rule = $script:ActionRegistry[$b.name]
          if (-not $rule) { throw "Action '$($b.name)' is not registered." }
          if (($rule.permission -eq 'admin') -and (-not $isAdmin)) { throw "Action '$($b.name)' requires administrator rights." }
          if ($rule.requiresApproval -and ($b.approved -isnot [bool] -or -not $b.approved)) { throw "Action '$($b.name)' requires explicit approval before execution." }

          $out = & $actions[$b.name] 2>&1 | Out-String
          Send-Json $ctx @{ ok = $true; output = $out.Trim(); action = $b.name; permission = $rule.permission; risk = $rule.risk }
        }

        'POST /api/run' {
          Send-Json $ctx @{ error = 'Arbitrary PowerShell execution is disabled. Use reviewed named operations instead.' } 403
        }

        'GET /api/ollama/tags' {
          $r = Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 10
          Send-Json $ctx $r
        }

        'POST /api/ollama/chat' {
          $requestBody = Read-JsonBody $ctx
          Assert-AllowedProperties $requestBody @('model', 'messages', 'stream', 'options')
          if ([string]::IsNullOrWhiteSpace([string]$requestBody.model)) { throw 'Ollama model is required.' }
          if ($null -eq $requestBody.messages) { throw 'Ollama messages are required.' }
          foreach ($message in @($requestBody.messages)) {
            if ($message -isnot [System.Management.Automation.PSCustomObject] -or $message.role -notin @('system', 'user', 'assistant') -or $message.content -isnot [string]) {
              throw 'Ollama messages must be objects with a supported role and text content.'
            }
          }
          $requestBody | Add-Member -MemberType NoteProperty -Name stream -Value $false -Force
          $jsonBody = ConvertTo-Json -InputObject $requestBody -Depth 20 -Compress
          $r = Invoke-RestMethod -Uri "$OllamaUrl/api/chat" -Method Post -Body $jsonBody -ContentType 'application/json' -TimeoutSec 600
          Send-Json $ctx $r
        }

        'GET /api/mcp/status' {
          $list = @()
          foreach ($k in @($script:mcp.Keys)) {
            $p = $script:mcp[$k]
            $list += @{ id = $k; pid = $p.Id; running = (-not $p.HasExited) }
          }
          Send-Json $ctx @($list)
        }

        'POST /api/mcp/start' {
          Send-Json $ctx @{ error = 'MCP startup is disabled until a reviewed server registry is configured.' } 403
        }

        'POST /api/mcp/stop' {
          $b = Read-JsonBody $ctx
          Assert-AllowedProperties $b @('id')
          if ($script:mcp.ContainsKey($b.id)) {
            $p = $script:mcp[$b.id]
            if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
            $script:mcp.Remove($b.id)
          }
          Send-Json $ctx @{ ok = $true }
        }

        'GET /api/claude-config' {
          $path = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
          $txt = if (Test-Path $path) { Get-Content $path -Raw } else { '' }
          Send-Json $ctx @{ path = $path; exists = (Test-Path $path); content = $txt }
        }

        'POST /api/claude-config' {
          $b = Read-JsonBody $ctx
          Assert-AllowedProperties $b @('content', 'approved')
          if ($b.approved -isnot [bool] -or -not $b.approved) { throw 'Updating Claude configuration requires explicit approval.' }
          $null = $b.content | ConvertFrom-Json
          $dir  = Join-Path $env:APPDATA 'Claude'
          $path = Join-Path $dir 'claude_desktop_config.json'
          if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
          if (Test-Path $path) { Copy-Item $path "$path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" }
          Set-Content -Path $path -Value $b.content -Encoding UTF8
          Send-Json $ctx @{ ok = $true; path = $path }
        }

        default { Send-Json $ctx @{ error = "Not found: $route" } 404 }
      }
    } catch {
      try {
        $msg = $_.Exception.Message
        $status = 500
        if ($msg -like 'Unauthorized:*') { $status = 401 }
        elseif ($msg -like '*requires explicit approval*' -or $msg -like '*requires administrator rights*') { $status = 403 }
        elseif ($msg -like 'Arbitrary PowerShell execution is disabled.*' -or $msg -like 'MCP startup is disabled*') { $status = 403 }
        elseif ($msg -like 'Invalid Host header.*' -or $msg -like 'Invalid Origin header.*' -or $msg -like 'Only localhost requests are allowed.*') { $status = 403 }
        elseif ($msg -like 'Action name is required.*' -or $msg -like 'Unknown action*' -or $msg -like 'A valid *' -or $msg -like 'A JSON request body is required.*' -or $msg -like 'The JSON request body must be an object.*' -or $msg -like 'Invalid JSON request body:*' -or $msg -like 'Request body must contain valid UTF-8.*' -or $msg -like 'Unexpected request field*' -or $msg -like 'Ollama * are required.*' -or $msg -like 'Ollama messages must*') { $status = 400 }
        elseif ($msg -like 'Request body exceeds*') { $status = 413 }
        Send-Json $ctx @{ error = $msg } $status
      } catch {
        Write-Warning "Unable to send request error response: $($_.Exception.Message)"
      }
    }
  }
} finally {
  foreach ($p in $script:mcp.Values) { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
  if ($listener) { $listener.Stop() }
}