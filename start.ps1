param([ValidateSet('run','discover','doctor')][string] $Mode = 'run')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$assistantPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $assistantPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Creating Python environment failed' }
}
& $assistantPython -c 'import lark_oapi, dotenv, tzdata' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $assistantPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Installing dependencies failed' }
}
if (-not (Test-Path -LiteralPath '.env')) {
    & (Join-Path $PSScriptRoot 'configure.ps1')
}
& $assistantPython -m assistant $Mode
exit $LASTEXITCODE
