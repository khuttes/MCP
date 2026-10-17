<#
  PC Control Bridge  -  local PowerShell server for the HTML dashboard
  Run:   powershell -ExecutionPolicy Bypass -File .\bridge.ps1
  Admin: right-click PowerShell -> "Run as administrator" first (needed for services, etc.)
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

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://127.0.0.1:$Port/")
$listener.Start()

Write-Host ""
Write-Host "  PC Control Bridge is running" -ForegroundColor Green
Write-Host "  URL   : http://127.0.0.1:$Port"
Write-Host "  TOKEN : $Token" -ForegroundColor Yellow
Write-Host "  Admin : $isAdmin"
Write-Host "  Press Ctrl+C to stop."
Write-Host ""

# ---------- helpers ----------
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

# Allow-listed quick actions
$actions = @{
  'lock'         = { rundll32.exe user32.dll,LockWorkStation }
  'sleep'        = { rundll32.exe powrprof.dll,SetSuspendState 0,1,0 }
  'restart'      = { shutdown.exe /r /t 30 }
  'shutdown'     = { shutdown.exe /s /t 30 }
  'cancel'       = { shutdown.exe /a }
  'flushdns'     = { ipconfig /flushdns }
  'taskmgr'      = { Start-Process taskmgr.exe }
  'explorer'     = { Start-Process explorer.exe }
  'settings'     = { Start-Process 'ms-settings:' }
  'devmgr'       = { Start-Process devmgmt.msc }
  'emptybin'     = { Clear-RecycleBin -Force -ErrorAction SilentlyContinue; 'Recycle Bin emptied' }
  'perf_high'    = { powercfg /setactive SCHEME_MIN; 'High performance plan active' }
  'perf_balanced'= { powercfg /setactive SCHEME_BALANCED; 'Balanced plan active' }
  'perf_saver'   = { powercfg /setactive SCHEME_MAX; 'Power saver plan active' }
  'winupdate'    = { Start-Process 'ms-settings:windowsupdate' }
  'cleantemp'    = { Get-ChildItem $env:TEMP -Recurse -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; 'User temp folder cleaned' }
}

# ---------- main loop ----------
try {
  while ($listener.IsListening) {
    $ctx = $listener.GetContext()
    $req = $ctx.Request
    $res = $ctx.Response
    $res.Headers.Add('Access-Control-Allow-Origin', '*')
    $res.Headers.Add('Access-Control-Allow-Headers', 'Authorization, Content-Type')
    $res.Headers.Add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

    if ($req.HttpMethod -eq 'OPTIONS') { $res.StatusCode = 204; $res.Close(); continue }

    if ($req.Headers['Authorization'] -ne "Bearer $Token") {
      Send-Json $ctx @{ error = 'Unauthorized: wrong or missing token' } 401
      continue
    }

    $route = "$($req.HttpMethod) $($req.Url.AbsolutePath)"
    try {
      switch ($route) {

        'GET /api/ping' {
          Send-Json $ctx @{ ok = $true; admin = $isAdmin; host = $env:COMPUTERNAME; user = $env:USERNAME }
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
          if (-not $actions.ContainsKey($b.name)) { throw "Unknown action '$($b.name)'" }
          $out = & $actions[$b.name] 2>&1 | Out-String
          Send-Json $ctx @{ ok = $true; output = $out.Trim() }
        }

        'POST /api/run' {
          $b = (Read-Body $ctx) | ConvertFrom-Json
          $out = Run-Command $b.command
          Send-Json $ctx @{ ok = $true; output = $out }
        }

        # ----- Local AI (Ollama proxy) -----
        'GET /api/ollama/tags' {
          $r = Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 10
          Send-Json $ctx $r
        }
        'POST /api/ollama/chat' {
          $raw = Read-Body $ctx
          $r = Invoke-RestMethod -Uri "$OllamaUrl/api/chat" -Method Post -Body $raw -ContentType 'application/json' -TimeoutSec 600
          Send-Json $ctx $r
        }

        # ----- MCP servers -----
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
          } finally {
            foreach ($k in $old.Keys) { [Environment]::SetEnvironmentVariable($k, $old[$k], 'Process') }
          }
          $script:mcp[$b.id] = $p
          Start-Sleep -Milliseconds 1500
          Send-Json $ctx @{ ok = $true; pid = $p.Id; running = (-not $p.HasExited) }
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
          $null = $b.content | ConvertFrom-Json   # validates JSON
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
      try { Send-Json $ctx @{ error = $_.Exception.Message } 500 } catch {}
    }
  }
} finally {
  foreach ($p in $script:mcp.Values) { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
  $listener.Stop()
}
