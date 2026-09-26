<#
.SYNOPSIS
    Opens (or closes) the Windows Firewall for Flipee's relay on TCP 5024.

.DESCRIPTION
    flipee_relay.py binds 0.0.0.0:5024, but Windows Firewall blocks inbound
    connections by default, so Flipee's POST to /reflect is dropped before
    Flask ever sees it. The device can't tell that apart from "relay is
    down" -- it just times out after RELAY_TIMEOUT_MS and silently writes a
    templated reflection instead.

    The rule this adds is deliberately narrow:
      -Profile Private        never applies on a public/coffee-shop network
      -RemoteAddress LocalSubnet   only hosts on your own LAN can connect

    Right-click this file and pick "Run with PowerShell", or run it from any
    PowerShell window -- it re-launches itself elevated if it needs to.

.PARAMETER Remove
    Delete the rule instead of creating it.

.EXAMPLE
    .\allow-relay-firewall.ps1
    .\allow-relay-firewall.ps1 -Remove
#>

[CmdletBinding()]
param(
    [switch]$Remove
)

$RuleName = 'Flipee relay (TCP 5024)'
$Port     = 5024

# --- re-launch elevated if we aren't already admin -------------------------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin   = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host 'Needs administrator rights -- asking Windows to elevate...' -ForegroundColor Yellow
    $scriptPath = $MyInvocation.MyCommand.Path
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$scriptPath`"")
    if ($Remove) { $argList += '-Remove' }
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs
    } catch {
        Write-Host ''
        Write-Host 'Elevation was declined or failed.' -ForegroundColor Red
        Write-Host 'Open an Administrator PowerShell and run this script again.'
        Read-Host 'Press Enter to close'
    }
    return
}

# --- do the work -----------------------------------------------------------
$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue

if ($Remove) {
    if ($existing) {
        Remove-NetFirewallRule -DisplayName $RuleName
        Write-Host "Removed firewall rule: $RuleName" -ForegroundColor Green
    } else {
        Write-Host "No rule named '$RuleName' found -- nothing to remove." -ForegroundColor Yellow
    }
    Read-Host 'Press Enter to close'
    return
}

if ($existing) {
    Write-Host "Rule '$RuleName' already exists -- leaving it alone." -ForegroundColor Yellow
} else {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress LocalSubnet -Description 'Lets Flipee (ESP32) reach flipee_relay.py on the home LAN' | Out-Null
    Write-Host "Created firewall rule: $RuleName" -ForegroundColor Green
}

# --- report what's actually in effect --------------------------------------
Write-Host ''
Write-Host 'Rule now in effect:' -ForegroundColor Cyan
Get-NetFirewallRule -DisplayName $RuleName |
    Select-Object DisplayName, Enabled, Direction, Action, Profile |
    Format-List

Write-Host 'Listener check (expect 0.0.0.0:5024 LISTENING if the relay is running):' -ForegroundColor Cyan
$listening = netstat -ano | Select-String ":$Port\s"
if ($listening) {
    $listening | ForEach-Object { Write-Host "  $($_.Line.Trim())" }
} else {
    Write-Host "  nothing listening on $Port yet -- start it with:" -ForegroundColor Yellow
    Write-Host '    python flipee_relay.py'
}

Write-Host ''
Write-Host 'Done. Flip Flipee and watch the serial monitor for:' -ForegroundColor Green
Write-Host '    [relay] ok, NNN chars of Claude'
Write-Host 'To undo this later:  .\allow-relay-firewall.ps1 -Remove'
Write-Host ''
Read-Host 'Press Enter to close'
