# نصب ORCHESTRA روی یک ویندوز تازه. با دو بار کلیک روی setup.bat اجرا می‌شود؛ دوباره اجرا کردنش بی‌خطر است.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`n$text" -ForegroundColor Red; exit 1 }

function Find-Python {
    foreach ($v in @("3.12", "3.13", "3.11")) {
        try { & py -$v -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { return "-$v" } } catch {}
    }
    return $null
}

Step "۱ از ۶ — پایتون"
$py = Find-Python
if (-not $py) {
    Write-Host "پایتون نصب نیست؛ با winget نصب می‌کنم (چند دقیقه)…"
    try { winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements } catch {}
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $py = Find-Python
    if (-not $py) { Fail "پایتون نصب نشد. از python.org نسخهٔ 3.12 را نصب کنید (تیک «Add python.exe to PATH») و دوباره setup.bat را اجرا کنید." }
}
Write-Host "پایتون $py آماده است."

Step "۲ از ۶ — محیط برنامه و کتابخانه‌ها (بار اول چند دقیقه)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & py $py -m venv .venv }
$venvPy = ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $venvPy -m pip install -e ".[dev]" --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "نصب کتابخانه‌ها نشد. اینترنت (یا VPN) را بررسی کنید و دوباره setup.bat را اجرا کنید." }

Step "۳ از ۶ — مرورگر برای ساخت اسلاید"
$browser = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
             "$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") |
           Where-Object { Test-Path $_ } | Select-Object -First 1
if ($browser) { Write-Host "مرورگر پیدا شد: $browser" }
else {
    Write-Host "Edge یا Chrome پیدا نشد؛ نصب Chromium (VPN لازم است)…"
    & $venvPy -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Write-Host "Chromium نصب نشد؛ Microsoft Edge را نصب کنید. بقیهٔ کار ادامه دارد." -ForegroundColor Yellow }
}

Step "۴ از ۶ — تنظیمات (کلید Gemini، ربات تلگرام، کانال تست)"
New-Item -ItemType Directory -Force -Path "data" | Out-Null
& $venvPy scripts\configure.py
if ($LASTEXITCODE -ne 0) { Fail "تنظیمات کامل نشد؛ دوباره setup.bat را اجرا کنید." }

Step "۵ از ۶ — آماده‌سازی پایگاه‌داده"
& $venvPy -m orchestra.cli init
if ($LASTEXITCODE -ne 0) { Fail "پایگاه‌داده آماده نشد؛ متن خطای بالا را برای Claude بفرستید." }

Step "۶ از ۶ — روشن شدن خودکار ربات کنترل"
$envText = Get-Content ".env" -Raw -Encoding UTF8
if ($envText -match "(?m)^ORCHESTRA_CONTROL_BOT_TOKEN=\S+") {
    $answer = Read-Host "ربات کنترل هر بار که وارد ویندوز می‌شوید خودش روشن شود؟ (Enter = بله، n = نه)"
    if ($answer -ne "n") {
        # The user's Startup folder needs no administrator rights (a logon task in Task Scheduler does).
        $bat = Join-Path (Get-Location) "run_service.bat"
        $startup = [Environment]::GetFolderPath("Startup")
        $launcher = Join-Path $startup "ORCHESTRA control bot.cmd"
        $text = "@echo off`r`ncd /d `"$(Get-Location)`"`r`nstart `"ORCHESTRA`" /min cmd /c `"$bat`"`r`n"
        try {
            [System.IO.File]::WriteAllText($launcher, $text, (New-Object System.Text.ASCIIEncoding))
            Write-Host "ثبت شد. برای برداشتن: uninstall_autostart.bat"
        } catch { Write-Host "ثبت نشد؛ run_service.bat را دستی اجرا کنید." -ForegroundColor Yellow }
    }
} else { Write-Host "ربات کنترل تنظیم نشده؛ این مرحله رد شد." }

Write-Host "`nنصب تمام شد." -ForegroundColor Green
Write-Host "  گزارش امروز:            run_ask.bat"
Write-Host "  تأیید و انتشار:          approve_publish.bat"
Write-Host "  ربات کنترل روی گوشی:     run_service.bat"
