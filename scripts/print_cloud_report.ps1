# Print the official AutoCount Cloud invoice report to a named Windows printer.
#
# This does not rebuild the invoice. Chrome opens the Cloud report URL (the
# same report the mobile "Open Cloud Report" button uses) with a dedicated
# profile so the office AutoCount Cloud login can persist, writes that page
# to a temporary PDF, then sends the PDF to the named Epson.
#
# The Windows default printer is never read or changed. Jobs always go to
# the exact printer name passed in (office printer: EPSONE85FF0 (L6460 Series)).
#
# One-time setup: start Chrome with this profile, log into AutoCount Cloud,
# then close Chrome before running the agent:
#   "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%LOCALAPPDATA%\AutocountPrintAgent\ChromeProfile"

param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$PrinterName,
    [int]$WaitSeconds = 30,
    [string]$UserDataDir = "",
    [string]$ChromePath = ""
)

$ErrorActionPreference = "Stop"

if ($Url -notmatch '^https://') {
    throw "Cloud report URL must be https"
}

# Exact-name lookup only. Do not inspect or change the Windows default printer.
$printer = Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue
if (-not $printer) {
    throw "Printer not found: $PrinterName"
}

$chromeCandidates = @(
    $ChromePath,
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromeCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
    Select-Object -First 1
if (-not $chrome) {
    throw "Google Chrome was not found. Expected C:\Program Files\Google\Chrome\Application\chrome.exe"
}

if (-not $UserDataDir) {
    $UserDataDir = Join-Path $env:LOCALAPPDATA "AutocountPrintAgent\ChromeProfile"
}
New-Item -ItemType Directory -Force -Path $UserDataDir | Out-Null

$workDir = Join-Path $env:TEMP "AutocountPrintAgent"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null
$pdfPath = Join-Path $workDir ("invoice-" + [guid]::NewGuid().ToString() + ".pdf")

function Send-PdfToNamedPrinter {
    param(
        [Parameter(Mandatory = $true)][string]$PdfPath,
        [Parameter(Mandatory = $true)][string]$PrinterName
    )

    # FolderItem.InvokeVerbEx("printto", name) targets that printer without
    # touching the Windows default. Start-Process -Verb PrintTo is the same
    # contract if the shell verb is missing on the COM object.
    $folderPath = Split-Path -LiteralPath $PdfPath
    $fileName = Split-Path -LiteralPath $PdfPath -Leaf
    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.NameSpace($folderPath)
    $item = $folder.ParseName($fileName)
    if ($item) {
        try {
            $item.InvokeVerbEx("printto", $PrinterName) | Out-Null
            return
        } catch {
            # Fall through to Start-Process PrintTo, still with the exact name.
        }
    }

    try {
        Start-Process -FilePath $PdfPath -Verb PrintTo -ArgumentList $PrinterName -Wait -ErrorAction Stop
    } catch {
        throw "Windows could not PrintTo printer '$PrinterName'. The agent never uses the Windows default printer."
    }
}

$chromeArgs = @(
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

$proc = Start-Process -FilePath $chrome -ArgumentList $chromeArgs -PassThru -Wait
if ($proc.ExitCode -ne 0) {
    throw "Chrome print-to-pdf failed with exit code $($proc.ExitCode). Log into AutoCount Cloud in the print-agent Chrome profile and try again."
}

$deadline = (Get-Date).AddSeconds([Math]::Max($WaitSeconds, 5))
while (-not (Test-Path -LiteralPath $pdfPath) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
}
if (-not (Test-Path -LiteralPath $pdfPath) -or (Get-Item -LiteralPath $pdfPath).Length -lt 100) {
    throw "Chrome did not write a Cloud report PDF. Confirm the print-agent Chrome profile is logged into AutoCount Cloud."
}

try {
    Send-PdfToNamedPrinter -PdfPath $pdfPath -PrinterName $PrinterName
} finally {
    Start-Sleep -Seconds 2
    Remove-Item -LiteralPath $pdfPath -Force -ErrorAction SilentlyContinue
}
