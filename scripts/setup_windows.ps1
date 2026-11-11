# راه‌اندازی ORCHESTRA روی ویندوز (بدون نیاز به Postgres؛ برای شروع با SQLite)
# اجرا: در PowerShell داخل پوشهٔ پروژه بزنید:  powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

Step "پیدا کردن پایتون"
$py = $null
foreach ($v in @("3.12", "3.13", "3.11")) {
    try { & py -$v -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = "-$v"; break } } catch {}
}
if (-not $py) {
    Write-Host "پایتون ۳.۱۱ یا جدیدتر پیدا نشد. از python.org نصبش کنید (تیک Add python.exe to PATH) و دوباره اجرا کنید." -ForegroundColor Red
    exit 1
}
Write-Host "پایتون $py"

if (-not (Test-Path ".venv")) {
    Step "ساخت محیط مجازی"
    & py $py -m venv .venv
}
$venvPy = ".venv\Scripts\python.exe"

Step "نصب کتابخانه‌ها (چند دقیقه طول می‌کشد)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -e ".[dev]" --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "نصب کتابخانه‌ها خطا داد." -ForegroundColor Red; exit 1 }

Step "بررسی مرورگر برای ساخت اسلاید"
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($edge) {
    Write-Host "مرورگر پیدا شد: $edge (نیازی به دانلود نیست)"
} else {
    Write-Host "Edge یا Chrome پیدا نشد؛ تلاش برای نصب Chromium (نیاز به VPN روشن)"
    & $venvPy -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Write-Host "Chromium نصب نشد. Microsoft Edge یا Chrome نصب کنید و دوباره اجرا کنید." -ForegroundColor Yellow }
}

if (-not (Test-Path ".env")) {
    Step "ساخت فایل .env (با SQLite)"
    New-Item -ItemType Directory -Force -Path "data" | Out-Null
    $env_text = Get-Content ".env.example" -Raw -Encoding UTF8
    $env_text = $env_text -replace "ORCHESTRA_DATABASE_URL=.*", "ORCHESTRA_DATABASE_URL=sqlite:///data/app.db"
    $env_text = $env_text -replace "ORCHESTRA_SYSTEM_DATABASE_URL=.*", "ORCHESTRA_SYSTEM_DATABASE_URL=sqlite:///data/dbos.sqlite"
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) ".env"), $env_text, (New-Object System.Text.UTF8Encoding $false))
}

Step "ساخت جدول‌ها و ثبت مقصدها"
& $venvPy -m orchestra.cli init

Step "اجرای تست‌ها"
& $venvPy -m pytest -q -o addopts=""
if ($LASTEXITCODE -ne 0) { Write-Host "بعضی تست‌ها رد شدند؛ خروجی بالا را برای Claude بفرستید." -ForegroundColor Yellow; exit 1 }

Write-Host "`nآماده است. حالا کلید مدل را در فایل .env بنویسید (ORCHESTRA_LLM_API_KEY و ORCHESTRA_LLM_MODEL)." -ForegroundColor Green
