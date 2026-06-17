$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$SettingsPath = Join-Path $Root "voe_converter_settings.json"
$ConverterPath = Join-Path $Root "voe_converter.py"

function Get-DefaultSettings {
    return [ordered]@{
        download_dir = (Join-Path $Root "downloads")
        min_duration = 60
        media_settle_seconds = 15
        cloudflare_dns = $true
        reset_browser_profile = $false
        capture_log = $true
        auto_click_voe = $false
        strict_https = $false
        no_progress = $false
        container = "mkv"
        adblock = $true
    }
}

function ConvertTo-Hashtable {
    param($Object)
    $hash = [ordered]@{}
    foreach ($prop in $Object.PSObject.Properties) {
        $hash[$prop.Name] = $prop.Value
    }
    return $hash
}

function Load-Settings {
    $defaults = Get-DefaultSettings
    if (Test-Path -LiteralPath $SettingsPath) {
        try {
            $loaded = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
            $loadedHash = ConvertTo-Hashtable $loaded
            foreach ($key in @($loadedHash.Keys)) {
                $defaults[$key] = $loadedHash[$key]
            }
        } catch {
            Write-Host "Settings konnten nicht gelesen werden, nutze Defaults." -ForegroundColor Yellow
        }
    }
    return $defaults
}

function Save-Settings {
    param($Settings)
    $Settings | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $SettingsPath -Encoding UTF8
}

function Read-WithDefault {
    param(
        [string]$Prompt,
        [string]$Default
    )
    $value = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value
}

function Read-Boolean {
    param(
        [string]$Prompt,
        [bool]$Default
    )
    $defaultText = if ($Default) { "j" } else { "n" }
    while ($true) {
        $value = (Read-Host "$Prompt (j/n) [$defaultText]").Trim().ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($value)) {
            return $Default
        }
        if ($value -in @("j", "ja", "y", "yes")) {
            return $true
        }
        if ($value -in @("n", "nein", "no")) {
            return $false
        }
        Write-Host "Bitte j oder n eingeben." -ForegroundColor Yellow
    }
}

function Show-Settings {
    param($Settings)
    Write-Host ""
    Write-Host "Aktuelle Settings" -ForegroundColor Cyan
    Write-Host "1. Download-Ordner:        $($Settings.download_dir)"
    Write-Host "2. Mindestdauer Sekunden: $($Settings.min_duration)"
    Write-Host "3. Sammelzeit Sekunden:   $($Settings.media_settle_seconds)"
    Write-Host "4. Cloudflare DNS nutzen: $($Settings.cloudflare_dns)"
    Write-Host "5. Browserprofil reset:   $($Settings.reset_browser_profile)"
    Write-Host "6. Capture-Log schreiben: $($Settings.capture_log)"
    Write-Host "7. Auto-Click VOE:        $($Settings.auto_click_voe)"
    Write-Host "8. Strict HTTPS:          $($Settings.strict_https)"
    Write-Host "9. Fortschritt aus:       $($Settings.no_progress)"
    Write-Host "10. Container:            $($Settings.container)"
    Write-Host "11. Adblock/Popupschutz:  $($Settings.adblock)"
    Write-Host ""
}

function Edit-Settings {
    param($Settings)
    while ($true) {
        Show-Settings $Settings
        Write-Host "Welche Einstellung aendern? Enter = fertig"
        $choice = Read-Host "Nummer"
        if ([string]::IsNullOrWhiteSpace($choice)) {
            Save-Settings $Settings
            return
        }

        switch ($choice) {
            "1" {
                $Settings.download_dir = Read-WithDefault "Download-Ordner" $Settings.download_dir
            }
            "2" {
                $Settings.min_duration = [double](Read-WithDefault "Mindestdauer in Sekunden (0 = aus)" ([string]$Settings.min_duration))
            }
            "3" {
                $Settings.media_settle_seconds = [int](Read-WithDefault "Sammelzeit nach erstem Media-Fund" ([string]$Settings.media_settle_seconds))
            }
            "4" {
                $Settings.cloudflare_dns = Read-Boolean "Cloudflare DNS im Browser nutzen" ([bool]$Settings.cloudflare_dns)
            }
            "5" {
                $Settings.reset_browser_profile = Read-Boolean "Browserprofil vor Start resetten" ([bool]$Settings.reset_browser_profile)
            }
            "6" {
                $Settings.capture_log = Read-Boolean "Capture-Log schreiben" ([bool]$Settings.capture_log)
            }
            "7" {
                $Settings.auto_click_voe = Read-Boolean "VOE automatisch klicken (nur kontrollierte Seiten)" ([bool]$Settings.auto_click_voe)
            }
            "8" {
                $Settings.strict_https = Read-Boolean "Strikte HTTPS-Zertifikate erzwingen" ([bool]$Settings.strict_https)
            }
            "9" {
                $Settings.no_progress = Read-Boolean "Fortschrittsanzeige deaktivieren" ([bool]$Settings.no_progress)
            }
            "10" {
                while ($true) {
                    $container = (Read-WithDefault "Container (mkv/mp4)" ([string]$Settings.container)).Trim().ToLowerInvariant()
                    if ($container -in @("mkv", "mp4")) {
                        $Settings.container = $container
                        break
                    }
                    Write-Host "Bitte mkv oder mp4 eingeben." -ForegroundColor Yellow
                }
            }
            "11" {
                $Settings.adblock = Read-Boolean "Adblock/Popupschutz im Browser nutzen" ([bool]$Settings.adblock)
            }
            default {
                Write-Host "Unbekannte Auswahl." -ForegroundColor Yellow
            }
        }
    }
}

function Build-Args {
    param(
        $Settings,
        [string]$Url
    )

    $argsList = @(
        $ConverterPath,
        "--browser",
        "--download",
        "--download-dir", $Settings.download_dir,
        "--container", $Settings.container,
        "--media-settle-seconds", ([string]$Settings.media_settle_seconds),
        $Url
    )

    if ([double]$Settings.min_duration -gt 0) {
        $argsList += @("--min-duration", ([string]$Settings.min_duration))
    }
    if (-not [bool]$Settings.cloudflare_dns) {
        $argsList += "--no-cloudflare-dns"
    }
    if (-not [bool]$Settings.adblock) {
        $argsList += "--no-adblock"
    }
    if ([bool]$Settings.reset_browser_profile) {
        $argsList += "--reset-browser-profile"
    }
    if ([bool]$Settings.capture_log) {
        $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $argsList += @("--capture-log", (Join-Path $Root "capture-$timestamp.txt"))
    }
    if ([bool]$Settings.auto_click_voe) {
        $argsList += "--auto-click-voe"
    }
    if ([bool]$Settings.strict_https) {
        $argsList += "--strict-https"
    }
    if ([bool]$Settings.no_progress) {
        $argsList += "--no-progress"
    }

    return $argsList
}

$settings = Load-Settings

while ($true) {
    Clear-Host
    Write-Host "VOE Converter Starter" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "1. Link downloaden"
    Write-Host "2. Settings bearbeiten"
    Write-Host "3. DNS-Check"
    Write-Host "4. Beenden"
    Write-Host ""
    Show-Settings $settings

    $choice = Read-Host "Auswahl"
    switch ($choice) {
        "1" {
            $url = Read-Host "Link einfuegen"
            if ([string]::IsNullOrWhiteSpace($url)) {
                Write-Host "Kein Link eingegeben." -ForegroundColor Yellow
                pause
                continue
            }
            New-Item -ItemType Directory -Force -Path $settings.download_dir | Out-Null
            Save-Settings $settings

            $argsList = Build-Args $settings $url
            & python -X utf8 @argsList
            Write-Host ""
            Write-Host "Fertig. Taste druecken..." -ForegroundColor Green
            pause
        }
        "2" {
            Edit-Settings $settings
        }
        "3" {
            $dnsArgs = @($ConverterPath, "--dns-check")
            if ([bool]$settings.reset_browser_profile) {
                $dnsArgs += "--reset-browser-profile"
            }
            if (-not [bool]$settings.cloudflare_dns) {
                $dnsArgs += "--no-cloudflare-dns"
            }
            & python -X utf8 @dnsArgs
            pause
        }
        "4" {
            Save-Settings $settings
            return
        }
        default {
            Write-Host "Unbekannte Auswahl." -ForegroundColor Yellow
            pause
        }
    }
}
