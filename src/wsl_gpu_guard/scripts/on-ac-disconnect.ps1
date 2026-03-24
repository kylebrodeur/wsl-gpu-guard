<#
.SYNOPSIS
    Proactive GPU hot-removal guard for WSL2 (NVIDIA Optimus laptops).

.DESCRIPTION
    Runs on Windows via Task Scheduler when AC power is disconnected (Kernel-Power
    EventID 105) or the machine enters sleep/hibernate (EventID 42).

    On Optimus laptops the discrete GPU is powered off when AC is unplugged.
    That makes /dev/dxg disappear in WSL2, which can crash any process that has
    an open CUDA context.  This script sends SIGUSR1 to the wsl-gpu-guard daemon
    running inside WSL2 *before* the GPU powers down, giving it time to signal
    watched processes and let them release their CUDA contexts gracefully.

    The guard daemon writes its PID to $env:TEMP\wsl-gpu-guard.pid (via a WSL2
    path) when it starts.  This script reads that file to find the daemon.

.NOTES
    Install via: wsl-gpu-guard install-task
    Requires:    powershell.exe, wsl.exe in PATH (standard on Windows 10/11)
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# The PID file is written by the wsl-gpu-guard daemon (Linux path).
$PidFile = "/tmp/.wsl-gpu-guard.pid"

# Grace period: how long (ms) to wait after sending SIGUSR1 before returning.
# Gives CUDA processes time to finish in-flight work and release the GPU context.
# Task Scheduler has a 1-minute execution limit — keep this well under 60s.
$GraceMs = 8000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Output "[$ts] wsl-gpu-guard: $Message"
}

function Show-Toast {
    param([string]$Title, [string]$Body)
    # Use Windows Runtime toast notifications (no extra modules required).
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

        $xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
        $xml.LoadXml(@"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>$Title</text>
      <text>$Body</text>
    </binding>
  </visual>
</toast>
"@)
        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("wsl-gpu-guard")
        $notifier.Show($toast)
    } catch {
        # Toast is best-effort — don't fail the script if the WinRT API is unavailable.
        Write-Log "Toast notification unavailable: $_"
    }
}

function Get-WslRunning {
    <# Return $true if WSL2 is running (wsl.exe --list reports a running distro). #>
    try {
        $out = & wsl.exe --list --running 2>&1
        return ($LASTEXITCODE -eq 0) -and ($out -match '\S')
    } catch {
        return $false
    }
}

function Read-WslPidFile {
    <# Read and validate the PID from $PidFile inside WSL2. Returns int or $null. #>
    try {
        $raw = & wsl.exe -- cat $PidFile 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        $raw = $raw.Trim()
        if ($raw -match '^\d+$') { return [int]$raw }
    } catch {}
    return $null
}

function Send-WslSignal {
    param([int]$Pid, [string]$Signal = "USR1")
    try {
        & wsl.exe -- kill "-s" $Signal $Pid 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-Log "AC disconnect / sleep event received."

Show-Toast `
    -Title "GPU powering down" `
    -Body "Unplugged from AC — CUDA processes are releasing the GPU. This takes up to $($GraceMs / 1000)s."

if (-not (Get-WslRunning)) {
    Write-Log "WSL2 is not running — nothing to do."
    exit 0
}

$guardPid = Read-WslPidFile
if ($null -eq $guardPid) {
    Write-Log "No wsl-gpu-guard PID file found at $PidFile — daemon may not be running."
    exit 0
}

Write-Log "Sending SIGUSR1 to wsl-gpu-guard PID $guardPid (pre-emptive GPU removal)."
$ok = Send-WslSignal -Pid $guardPid -Signal "USR1"

if ($ok) {
    Write-Log "Signal sent. Waiting ${GraceMs}ms for processes to release CUDA contexts."
    Start-Sleep -Milliseconds $GraceMs
    Write-Log "Grace period complete — GPU can now power down safely."
} else {
    Write-Log "WARNING: kill failed — PID $guardPid may no longer exist."
}

exit 0
