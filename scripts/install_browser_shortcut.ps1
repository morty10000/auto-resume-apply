[CmdletBinding()]
param(
    [switch]$NoDesktopShortcut
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot "browser_launcher.cmd"
$urlShortcut = Join-Path $root "启动看板.url"
$protocol = "autoapply"
$command = '"' + (Join-Path $env:SystemRoot "System32\cmd.exe") + '" /d /c ""' + $launcher + '""'

New-Item -Path "HKCU:\Software\Classes\$protocol" -Force | Out-Null
Set-ItemProperty -Path "HKCU:\Software\Classes\$protocol" -Name "(Default)" -Value "URL:AutoApply Dashboard Launcher"
Set-ItemProperty -Path "HKCU:\Software\Classes\$protocol" -Name "URL Protocol" -Value ""
New-Item -Path "HKCU:\Software\Classes\$protocol\DefaultIcon" -Force | Out-Null
Set-ItemProperty -Path "HKCU:\Software\Classes\$protocol\DefaultIcon" -Name "(Default)" -Value "$env:SystemRoot\System32\SHELL32.dll,13"
New-Item -Path "HKCU:\Software\Classes\$protocol\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path "HKCU:\Software\Classes\$protocol\shell\open\command" -Name "(Default)" -Value $command

$shortcutText = "[InternetShortcut]`r`nURL=autoapply://start`r`n"
[System.IO.File]::WriteAllText($urlShortcut, $shortcutText, [System.Text.UTF8Encoding]::new($false))

if (-not $NoDesktopShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if ($desktop) {
        $desktopShortcut = Join-Path $desktop "启动投递看板.url"
        [System.IO.File]::WriteAllText($desktopShortcut, $shortcutText, [System.Text.UTF8Encoding]::new($false))
    }
}

Write-Host "Browser shortcut installed."
Write-Host "Protocol: autoapply://start"
Write-Host "Project shortcut: $urlShortcut"