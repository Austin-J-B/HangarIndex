$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
  $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($pythonLauncher) {
    & $pythonLauncher.Source -3 -m venv .venv
  } else {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
      throw 'Python 3.11 or newer is required. Install it from https://www.python.org/downloads/ and try again.'
    }
    & $pythonCommand.Source -m venv .venv
  }
  if ($LASTEXITCODE -ne 0) {
    throw 'Could not create the project virtual environment.'
  }
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$pythonVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]'3.11') {
  throw "Python 3.11 or newer is required; the virtual environment uses Python $pythonVersion. Remove .venv and run this script again after installing a newer Python."
}

& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
  throw 'Dependency installation failed. Check your network connection and try again.'
}

& $python app.py
exit $LASTEXITCODE
