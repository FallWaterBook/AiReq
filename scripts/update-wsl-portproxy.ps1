param(
    [int]$ListenPort = 8000,
    [int]$TargetPort = 8000,
    [string]$ListenAddress = "0.0.0.0",
    [string]$DistroName = "",
    [string]$FirewallRuleName = "WSL Django 8000"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Admin {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script in an elevated PowerShell session (Run as Administrator)."
    }
}

function Get-WslIPv4 {
    param([string]$Name)

    $raw = ""
    if ([string]::IsNullOrWhiteSpace($Name)) {
        $raw = (& wsl hostname -I 2>$null) | Out-String
    } else {
        $raw = (& wsl -d $Name -- hostname -I 2>$null) | Out-String
    }

    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw "Could not get WSL IP. Ensure your distro is running."
    }

    $ipv4 = $raw.Split([char[]]" `t`r`n", [System.StringSplitOptions]::RemoveEmptyEntries) |
        Where-Object { $_ -match '^(\d{1,3}\.){3}\d{1,3}$' } |
        Select-Object -First 1

    if ([string]::IsNullOrWhiteSpace($ipv4)) {
        throw "No IPv4 address found in WSL output: $raw"
    }

    return $ipv4
}

function Reset-PortProxy {
    param(
        [string]$ListenAddr,
        [int]$ListenPrt,
        [string]$ConnectAddr,
        [int]$ConnectPrt
    )

    & netsh interface portproxy delete v4tov4 listenaddress=$ListenAddr listenport=$ListenPrt *> $null
    & netsh interface portproxy add v4tov4 listenaddress=$ListenAddr listenport=$ListenPrt connectaddress=$ConnectAddr connectport=$ConnectPrt *> $null
}

function Ensure-FirewallRule {
    param(
        [string]$RuleName,
        [int]$Port
    )

    $existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port *> $null
    } else {
        Set-NetFirewallRule -DisplayName $RuleName -Enabled True *> $null
    }
}

try {
    Assert-Admin

    $wslIp = Get-WslIPv4 -Name $DistroName
    Reset-PortProxy -ListenAddr $ListenAddress -ListenPrt $ListenPort -ConnectAddr $wslIp -ConnectPrt $TargetPort
    Ensure-FirewallRule -RuleName $FirewallRuleName -Port $ListenPort

    Write-Host "[ok] portproxy updated"
    Write-Host "  listen:  $ListenAddress`:$ListenPort"
    Write-Host "  connect: $wslIp`:$TargetPort"

    Write-Host ""
    Write-Host "[info] current rules"
    & netsh interface portproxy show v4tov4

    $tailscaleCmd = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if ($tailscaleCmd) {
        $tailscaleIps = (& tailscale.exe ip -4 2>$null) -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        if ($tailscaleIps.Count -gt 0) {
            Write-Host ""
            Write-Host "[info] access URLs"
            foreach ($ip in $tailscaleIps) {
                Write-Host "  http://$ip`:$ListenPort/jobs"
            }
        }
    }
}
catch {
    Write-Error $_
    exit 1
}
