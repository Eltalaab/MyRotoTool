$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
Push-Location $project
try {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $runner = @("uv", "run", "--python", "3.11", "--with", "pyinstaller", "pyinstaller")
    } else {
        $runner = @("py", "-3.11", "-m", "PyInstaller")
    }
    & $runner[0] $runner[1..($runner.Count - 1)] --noconfirm --clean --onefile --windowed `
        --name MyRotoTool-Setup `
        --distpath "$project\installer\dist" `
        "$project\installer\bootstrap.py"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
    Write-Host "Built: $project\installer\dist\MyRotoTool-Setup.exe"
}
finally {
    Pop-Location
}
