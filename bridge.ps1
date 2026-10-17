<#
  PC Control Bridge  -  local PowerShell server for the HTML dashboard
  Run:   powershell -ExecutionPolicy Bypass -File .\bridge.ps1
  The window prints a URL and a TOKEN. Paste both into Dashboard -> Settings.
  Listens ONLY on 127.0.0.1 (your own computer). Every request needs the token.
#>
param(
  [int]$Port = 8787,
  [string]$Token = '',
  [string]$OllamaUrl = 'http://127.0.0.1:11434'
)

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
  $ctx.Response.OutputStream.Write($b, 0, $b.Length)
  $ctx.Response.Close()
}

function Read-Body($ctx) {
  $sr = New-Object IO.StreamReader($ctx.Request.InputStream, [Text.Encoding]::UTF8)
  $raw = $sr.ReadToEnd(); $sr.Close()
  return $raw
}

function Run-Command([string]$cmd) {
  $sb = [scriptblock]::Create($cmd)
  $out = & $sb 2>&1 | Out-String -Width 220
  return $out
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

  if (-not $normalized -or $normalized -ne $Token) "Bearer $Token") {
    throw 'Unauthorized: wrong or missing token.'
  }
}

function Assert-AuthorizedFixed([System.Net.HttpListenerRequest]$req) {
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
  $roots = @(
    $env:USERPROFILE,
    $env:USERPROFILE + '\Projects',
    $env:USERPROFILE + '\source\repos',
    $env:USERPROFILE + '\Desktop',
    $env:LOCALAPPDATA + '\Programs',
    (Get-Location).Path
  )

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
    try {
      $gitDirs += Get-ChildItem -Path $root -Directory -ErrorAction Stop | Where-Object { Test-Path (Join-Path $_.FullName '.git') }
    } catch {}
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

  Write-Host ""
  Write-Host "  PC Control Bridge is running" -ForegroundColor Green
  Write-Host "  URL   : http://127.0.0.1:$SelectedPort" 
  Write-Host "  TOKEN : $Token" -ForegroundColor Yellow
  Write-Host "  Admin : $isAdmin"
  Write-Host "  Press Ctrl+C to stop."
  Write-Host ""

  while ($listener.IsListening) {
    $ctx = $listener.GetContext()
    $req = $ctx.Request
    $res = $ctx.Response
    $origin = [string]$req.Headers['Origin']
    $allowOrigin = if ($origin -and ($origin -eq 'null' -or $origin -match '^https?://(localhost|127\.0\.0\.1)(:\d+)?$')) { $origin } else { '*' }
    $res.Headers.Add('Access-Control-Allow-Origin', $allowOrigin)
    $res.Headers.Add('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-CSRF-Token')
    $res.Headers.Add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

    if ($req.HttpMethod -eq 'OPTIONS') { $res.StatusCode = 204; $res.Close(); continue }

    try {
      Test-LocalRequest $req
      Assert-AuthorizedFixed $req

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
          $cpu  = (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average
          $cpuN = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
          $disks = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
            @{ name = $_.DeviceID; totalGB = [math]::Round($_.Size/1GB,1); freeGB = [math]::Round($_.FreeSpace/1GB,1) }
          }
          Send-Json $ctx @{
            host = $env:COMPUTERNAME; user = $env:USERNAME; admin = $isAdmin
            os = $os.Caption; build = $os.BuildNumber; cpuName = $cpuN
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
          $b = (Read-Body $ctx) | ConvertFrom-Json
          Stop-Process -Id ([int]$b.id) -Force
          Send-Json $ctx @{ ok = $true }
        }

        'GET /api/services' {
          $s = Get-Service | ForEach-Object {
            @{ name = $_.Name; display = $_.DisplayName; status = "$($_.Status)"; start = "$($_.StartType)" }
          }
          Send-Json $ctx @($s)
        }

        'POST /api/service' {
          $b = (Read-Body $ctx) | ConvertFrom-Json
          switch ($b.action) {
            'start'   { Start-Service   -Name $b.name }
            'stop'    { Stop-Service    -Name $b.name -Force }
            'restart' { Restart-Service -Name $b.name -Force }
            default   { throw "Unknown service action" }
          }
          Send-Json $ctx @{ ok = $true }
        }

        'POST /api/action' {
          $b = (Read-Body $ctx) | ConvertFrom-Json
          if (-not $b -or -not $b.name) { throw 'Action name is required.' }
          if (-not $actions.ContainsKey($b.name)) { throw "Unknown action '$($b.name)'" }

          $rule = $script:ActionRegistry[$b.name]
          if (-not $rule) { throw "Action '$($b.name)' is not registered." }
          if (($rule.permission -eq 'admin') -and (-not $isAdmin)) { throw "Action '$($b.name)' requires administrator rights." }
          if ($rule.requiresApproval -and -not $b.approved) { throw "Action '$($b.name)' requires explicit approval before execution." }

          $out = & $actions[$b.name] 2>&1 | Out-String
          Send-Json $ctx @{ ok = $true; output = $out.Trim(); action = $b.name; permission = $rule.permission; risk = $rule.risk }
        }

        'POST /api/run' {
          $b = (Read-Body $ctx) | ConvertFrom-Json
          if (-not $b.command) { throw 'Command is required.' }
          $out = Run-Command $b.command
          Send-Json $ctx @{ ok = $true; output = $out }
        }

        'GET /api/ollama/tags' {
          $r = Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 10
          Send-Json $ctx $r
        }

        'POST /api/ollama/chat' {
          $raw = Read-Body $ctx
          $r = Invoke-RestMethod -Uri "$OllamaUrl/api/chat" -Method Post -Body $raw -ContentType 'application/json' -TimeoutSec 600
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
          $b = (Read-Body $ctx) | ConvertFrom-Json
          $old = @{}
          if ($b.env) {
            foreach ($prop in $b.env.PSObject.Properties) {
              $old[$prop.Name] = [Environment]::GetEnvironmentVariable($prop.Name, 'Process')
              [Environment]::SetEnvironmentVariable($prop.Name, [string]$prop.Value, 'Process')
            }
          }
          try {
            $argList = @($b.args)
            if ($argList.Count -gt 0) { $p = Start-Process -FilePath $b.command -ArgumentList $argList -WindowStyle Hidden -PassThru }
            else { $p = Start-Process -FilePath $b.command -WindowStyle Hidden -PassThru }
            $script:mcp[$b.id] = $p
            Start-Sleep -Milliseconds 1500
            Send-Json $ctx @{ ok = $true; pid = $p.Id; running = (-not $p.HasExited) }
          } finally {
            foreach ($k in $old.Keys) { [Environment]::SetEnvironmentVariable($k, $old[$k], 'Process') }
          }
        }

        'POST /api/mcp/stop' {
          $b = (Read-Body $ctx) | ConvertFrom-Json
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
          $b = (Read-Body $ctx) | ConvertFrom-Json
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
        elseif ($msg -like 'Invalid Host header.*' -or $msg -like 'Invalid Origin header.*' -or $msg -like 'Only localhost requests are allowed.*') { $status = 403 }
        elseif ($msg -like 'Action name is required.*' -or $msg -like 'Unknown action*' -or $msg -like 'Command is required.*') { $status = 400 }
        Send-Json $ctx @{ error = $msg } $status
      } catch {}
    }
  }
} finally {
  foreach ($p in $script:mcp.Values) { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
  if ($listener) { $listener.Stop() }
}