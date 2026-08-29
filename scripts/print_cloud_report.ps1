# Print the official AutoCount Cloud invoice report to a named Windows printer.
#
# This does not rebuild the invoice. Edge opens the Cloud report URL (the same
# report the mobile "Open Cloud Report" button uses) with a dedicated profile
# so the office AutoCount Cloud login can persist, writes that page to a
# temporary PDF, then sends the PDF to the Epson.
#
# One-time setup: start Edge with this profile, log into AutoCount Cloud,
# then close Edge before running the agent:
#   msedge --user-data-dir="%LOCALAPPDATA%\AutocountPrintAgent\EdgeProfile"

param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$PrinterName,
    [int]$WaitSeconds = 30,
    [string]$UserDataDir = ""
)

$ErrorActionPreference = "Stop"

if ($Url -notmatch '^https://') {
    throw "Cloud report URL must be https"
}

$printer = Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue
if (-not $printer) {
    throw "Printer not found: $PrinterName"
}

$edgeCandidates = @(
    "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)
$edge = $edgeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) {
    throw "Microsoft Edge was not found. Install Edge to print the Cloud report."
}

if (-not $UserDataDir) {
    $UserDataDir = Join-Path $env:LOCALAPPDATA "AutocountPrintAgent\EdgeProfile"
}
New-Item -ItemType Directory -Force -Path $UserDataDir | Out-Null

$workDir = Join-Path $env:TEMP "AutocountPrintAgent"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null
$pdfPath = Join-Path $workDir ("invoice-" + [guid]::NewGuid().ToString() + ".pdf")

$edgeArgs = @(
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-pdf-header-footer",
    "--virtual-time-budget=20000",
    "--timeout=30000",
    "--user-data-dir=$UserDataDir",
    "--print-to-pdf=$pdfPath",
    $Url
)

$proc = Start-Process -FilePath $edge -ArgumentList $edgeArgs -PassThru -Wait
if ($proc.ExitCode -ne 0) {
    throw "Edge print-to-pdf failed with exit code $($proc.ExitCode). Log into AutoCount Cloud in the print-agent Edge profile and try again."
}

$deadline = (Get-Date).AddSeconds([Math]::Max($WaitSeconds, 5))
while (-not (Test-Path $pdfPath) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
}
if (-not (Test-Path $pdfPath) -or (Get-Item $pdfPath).Length -lt 100) {
    throw "Edge did not write a Cloud report PDF. Confirm the print-agent Edge profile is logged into AutoCount Cloud."
}

try {
    $printed = $false
    try {
        Start-Process -FilePath $pdfPath -Verb PrintTo -ArgumentList "`"$PrinterName`"" -Wait -ErrorAction Stop
        $printed = $true
    } catch {
        $printed = $false
    }
    if (-not $printed) {
        $previous = (Get-CimInstance -ClassName Win32_Printer | Where-Object { $_.Default }).Name
        try {
            $target = Get-CimInstance -ClassName Win32_Printer | Where-Object { $_.Name -eq $PrinterName }
            Invoke-CimMethod -InputObject $target -MethodName SetDefaultPrinter | Out-Null
            Start-Process -FilePath $pdfPath -Verb Print -Wait
        } finally {
            if ($previous) {
                $restore = Get-CimInstance -ClassName Win32_Printer | Where-Object { $_.Name -eq $previous }
                if ($restore) {
                    Invoke-CimMethod -InputObject $restore -MethodName SetDefaultPrinter | Out-Null
                }
            }
        }
    }
} finally {
    Start-Sleep -Seconds 2
    Remove-Item -Force -ErrorAction SilentlyContinue $pdfPath
}
