# Print the official AutoCount Cloud invoice report to a named Windows printer.
#
# This does not rebuild the invoice. Headed Google Chrome opens the Cloud
# report URL (the same report the mobile "Open Cloud Report" button uses)
# with a dedicated profile so the office AutoCount Cloud login can persist.
# After the generated report is showing (not the AutoCount login page), the
# script clicks AutoCount Print Report and prints that printout to the named
# Epson. A headed Chrome window is required; dumping the Cloud URL to PDF
# printed the AutoCount login page and is not used.
#
# The Windows default printer is never read or changed. Jobs always go to
# the exact printer name passed in (office printer: EPSONE85FF0 (L6460 Series)).
# The Cloud report URL is never logged (the account-book path lives in it).
#
# One-time setup: start Chrome with this profile, log into AutoCount Cloud,
# open any invoice report once, then close Chrome before running the agent:
#   "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%LOCALAPPDATA%\AutocountPrintAgent\ChromeProfile"
#
# Keep this a simple script: no Parameter attributes and no CmdletBinding.
# A nested advanced helper inside an advanced script caused Windows
# PowerShell 5.1 AmbiguousParameterSet at the helper call site. Helpers below
# use a plain param list with no Parameter attributes and no CmdletBinding.

param(
    [string]$Url,
    [string]$PrinterName,
    [int]$WaitSeconds = 30,
    [string]$UserDataDir = "",
    [string]$ChromePath = ""
)

$ErrorActionPreference = "Stop"

if (-not $Url) {
    throw "Url is required"
}
if (-not $PrinterName) {
    throw "PrinterName is required"
}

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

# Default Chrome profile path we must never stop: %LOCALAPPDATA%\Google\Chrome\User Data
$defaultChromeUserData = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"

function Get-ChromeProcessRows {
    try {
        return @(Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction Stop)
    } catch {
        return @(Get-WmiObject Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue)
    }
}

function Get-ProfileChromePids($profileDir) {
    $resolved = [string]$profileDir
    try {
        $resolved = [System.IO.Path]::GetFullPath($profileDir)
    } catch {
        $resolved = [string]$profileDir
    }
    $resolved = $resolved.TrimEnd('\')
    $resolvedLower = $resolved.ToLowerInvariant()
    $defaultLower = $defaultChromeUserData.TrimEnd('\').ToLowerInvariant()
    $pids = @()
    foreach ($proc in (Get-ChromeProcessRows)) {
        $cmd = [string]$proc.CommandLine
        if (-not $cmd) { continue }
        $flagMatch = [regex]::Match($cmd, '--user-data-dir(?:=|\s+)(?:"([^"]+)"|(\S+))')
        if (-not $flagMatch.Success) { continue }
        $dir = $flagMatch.Groups[1].Value
        if (-not $dir) { $dir = $flagMatch.Groups[2].Value }
        try {
            $dir = [System.IO.Path]::GetFullPath($dir.TrimEnd('\'))
        } catch {
            $dir = $dir.TrimEnd('\')
        }
        $dirLower = $dir.ToLowerInvariant()
        if ($dirLower -eq $defaultLower -and $resolvedLower -ne $defaultLower) {
            continue
        }
        if ($dirLower -eq $resolvedLower) {
            $pids += [int]$proc.ProcessId
        }
    }
    return $pids
}

function Stop-ProfileChrome($profileDir) {
    foreach ($pid in @(Get-ProfileChromePids $profileDir)) {
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        $left = @(Get-ProfileChromePids $profileDir)
        if ($left.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    }
}

function Set-ChromePrintPrefs($profileDir, $namedPrinter, $downloadDir) {
    # Best-effort. Do not throw: Print Report / kiosk-printing can still
    # target the named Epson if sticky prefs are missing.
    try {
        $defaultDir = Join-Path $profileDir "Default"
        New-Item -ItemType Directory -Force -Path $defaultDir | Out-Null
        $prefsPath = Join-Path $defaultDir "Preferences"
        $helper = Join-Path $PSScriptRoot "set_chrome_print_prefs.py"
        if (-not (Test-Path -LiteralPath $helper)) {
            return
        }
        $python = $null
        foreach ($candidate in @("python.exe", "python3.exe", "python", "python3")) {
            $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
            if ($cmd -and $cmd.Source) {
                $python = [string]$cmd.Source
                break
            }
        }
        if (-not $python) { return }
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $python
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.Arguments = (
            ('"{0}" "{1}" "{2}" "{3}"' -f $helper, $prefsPath, $namedPrinter, $downloadDir)
        )
        $proc = [System.Diagnostics.Process]::Start($psi)
        if ($proc) {
            $proc.WaitForExit(15000) | Out-Null
            if (-not $proc.HasExited) {
                try { $proc.Kill() } catch { }
            }
        }
    } catch {
        return
    }
}

function Ensure-Win32Helper {
    if ("AutocountPrintWin32" -as [type]) { return }
    Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public class AutocountPrintWin32 {
    public class WindowInfo {
        public long Hwnd;
        public uint Pid;
        public string Title;
    }

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    public const uint KEYEVENTF_KEYUP = 2;
    public const byte VK_CONTROL = 0x11;
    public const byte VK_P = 0x50;

    private static List<WindowInfo> _windows;

    private static bool EnumCallback(IntPtr hWnd, IntPtr lParam) {
        if (!IsWindowVisible(hWnd)) return true;
        uint pid;
        GetWindowThreadProcessId(hWnd, out pid);
        StringBuilder sb = new StringBuilder(1024);
        GetWindowText(hWnd, sb, sb.Capacity);
        string title = sb.ToString();
        if (string.IsNullOrWhiteSpace(title)) return true;
        WindowInfo info = new WindowInfo();
        info.Hwnd = hWnd.ToInt64();
        info.Pid = pid;
        info.Title = title;
        _windows.Add(info);
        return true;
    }

    public static List<WindowInfo> GetVisibleWindows() {
        _windows = new List<WindowInfo>();
        EnumWindows(EnumCallback, IntPtr.Zero);
        return _windows;
    }
}
"@
}

function Get-ProfileChromeWindows($profileDir) {
    Ensure-Win32Helper
    $pidSet = @{}
    foreach ($pid in @(Get-ProfileChromePids $profileDir)) {
        $pidSet[[int]$pid] = $true
    }
    $matches = @()
    if ($pidSet.Count -eq 0) { return $matches }
    foreach ($win in [AutocountPrintWin32]::GetVisibleWindows()) {
        if ($pidSet.ContainsKey([int]$win.Pid)) {
            $matches += $win
        }
    }
    return $matches
}

function Test-LoginWindowTitle($title) {
    if (-not $title) { return $false }
    $t = $title.ToLowerInvariant()
    if ($t -match 'log\s*in') { return $true }
    if ($t -match 'sign\s*in') { return $true }
    if ($t -match '(^|[\s\|\-])login([\s\|\-]|$)') { return $true }
    return $false
}

function Test-ReadyReportTitle($title) {
    if (-not $title) { return $false }
    if (Test-LoginWindowTitle $title) { return $false }
    $t = $title.ToLowerInvariant()
    if ($t -eq 'google chrome') { return $false }
    if ($t -match 'new tab') { return $false }
    if ($t -match 'untitled') { return $false }
    if ($t -match 'about:blank') { return $false }
    return $true
}

function Wait-ForCloudReport($profileDir, $timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    $sawLogin = $false
    $bestWindow = $null
    $printControl = $null
    $chromeEl = $null
    while ((Get-Date) -lt $deadline) {
        foreach ($win in @(Get-ProfileChromeWindows $profileDir)) {
            $title = [string]$win.Title
            if (Test-LoginWindowTitle $title) {
                $sawLogin = $true
                continue
            }
            if (-not (Test-ReadyReportTitle $title)) { continue }
            $bestWindow = $win
            try {
                $el = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$win.Hwnd)
                $found = Find-PrintReportControl $el
                if ($found) {
                    $printControl = $found
                    $chromeEl = $el
                    break
                }
                $chromeEl = $el
            } catch {
            }
        }
        if ($printControl) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $bestWindow) {
        if ($sawLogin) {
            throw "Cloud report is still the AutoCount login page (Log In). Log into AutoCount Cloud in the print-agent Chrome profile, open any invoice report once, then close Chrome."
        }
        throw "Chrome did not show the generated Cloud report in time. Confirm the print-agent Chrome profile is logged into AutoCount Cloud."
    }
    if (Test-LoginWindowTitle $bestWindow.Title) {
        throw "Cloud report is still the AutoCount login page (Log In). Log into AutoCount Cloud in the print-agent Chrome profile, open any invoice report once, then close Chrome."
    }
    $result = @{
        Window = $bestWindow
        PrintControl = $printControl
        Element = $chromeEl
    }
    return $result
}

function Get-UiaElementByName($root, $name) {
    if ($null -eq $root -or -not $name) { return $null }
    try {
        $cond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            $name)
        return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
    } catch {
        return $null
    }
}

function Get-ControlTextBlob($el) {
    $name = ""
    $help = ""
    try { $name = [string]$el.Current.Name } catch { $name = "" }
    try { $help = [string]$el.Current.HelpText } catch { $help = "" }
    return ($name + " " + $help)
}

function Find-PrintReportControl($root) {
    if ($null -eq $root) { return $null }
    $exact = Get-UiaElementByName $root "Print Report"
    if ($exact) { return $exact }
    $printButton = $null
    $cetak = Get-UiaElementByName $root "Cetak"
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($root)
    $visited = 0
    while ($queue.Count -gt 0 -and $visited -lt 12000) {
        $el = $queue.Dequeue()
        $visited++
        try {
            $blob = Get-ControlTextBlob $el
            $type = $el.Current.ControlType
            if ($blob -match 'Print Report') {
                return $el
            }
            if (-not $cetak -and $blob -match 'Cetak') {
                $cetak = $el
            }
            $name = ""
            try { $name = [string]$el.Current.Name } catch { $name = "" }
            if (-not $printButton -and $type -eq [System.Windows.Automation.ControlType]::Button -and $name -replace '&','' -match '^\s*Print\s*$') {
                $printButton = $el
            }
        } catch {
        }
        try {
            $child = $walker.GetFirstChild($el)
            while ($child) {
                $queue.Enqueue($child)
                $child = $walker.GetNextSibling($child)
            }
        } catch {
        }
    }
    if ($printButton) { return $printButton }
    if ($cetak) { return $cetak }
    return $null
}

function Invoke-UiaControl($el) {
    if ($null -eq $el) { return $false }
    try {
        $pattern = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $pattern.Invoke()
        return $true
    } catch {
    }
    try {
        $legacy = $el.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
        $legacy.DoDefaultAction()
        return $true
    } catch {
    }
    return $false
}

function Find-PrintDialogWindow($profileDir) {
    $pidSet = @{}
    foreach ($pid in @(Get-ProfileChromePids $profileDir)) {
        $pidSet[[int]$pid] = $true
    }
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $windows = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Children,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Window)))
    foreach ($w in $windows) {
        $n = ""
        try { $n = [string]$w.Current.Name } catch { $n = "" }
        # AutoCount "Print Report" is the report-viewer control, not the printer dialog.
        if ($n -ne "Print" -and $n -ne "Cetak") { continue }
        try {
            $procId = [int]$w.Current.ProcessId
            if ($pidSet.ContainsKey($procId)) { return $w }
        } catch {
        }
    }
    return $null
}

function Get-SelectedPrinterFromDialog($dialog) {
    if ($null -eq $dialog) { return "" }
    $combos = $dialog.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::ComboBox)))
    foreach ($c in $combos) {
        $val = ""
        try {
            $vp = $c.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $val = [string]$vp.Current.Value
        } catch {
            try { $val = [string]$c.Current.Name } catch { $val = "" }
        }
        if ($val) { return $val }
    }
    $items = $dialog.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::ListItem)))
    foreach ($item in $items) {
        try {
            $sp = $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
            if ($sp.Current.IsSelected) {
                return [string]$item.Current.Name
            }
        } catch {
        }
    }
    return ""
}

function Select-PrinterInDialog($dialog, $namedPrinter) {
    if ($null -eq $dialog) { return $false }
    $items = $dialog.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::ListItem)))
    foreach ($item in $items) {
        $name = ""
        try { $name = [string]$item.Current.Name } catch { $name = "" }
        if ($name -eq $namedPrinter) {
            try {
                $sp = $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
                $sp.Select()
                return $true
            } catch {
            }
        }
    }
    $combos = $dialog.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::ComboBox)))
    foreach ($c in $combos) {
        try {
            $vp = $c.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $vp.SetValue($namedPrinter)
            return $true
        } catch {
        }
    }
    return $false
}

function Confirm-PrintDialogForNamedPrinter($namedPrinter, $waitSeconds, $profileDir) {
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    $dialog = $null
    while ((Get-Date) -lt $deadline) {
        $dialog = Find-PrintDialogWindow $profileDir
        if ($dialog) { break }
        Start-Sleep -Milliseconds 400
    }
    if (-not $dialog) { return $false }
    $selected = Get-SelectedPrinterFromDialog $dialog
    if ($selected -ne $namedPrinter) {
        Select-PrinterInDialog $dialog $namedPrinter | Out-Null
        Start-Sleep -Milliseconds 400
        $selected = Get-SelectedPrinterFromDialog $dialog
    }
    if ($selected -ne $namedPrinter) {
        throw "Print dialog is not set to printer '$namedPrinter' (it was '$selected'). The agent never prints to a different printer and never uses the Windows default printer."
    }
    $printBtn = $null
    $buttons = $dialog.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button)))
    foreach ($btn in $buttons) {
        $n = ""
        try { $n = ([string]$btn.Current.Name).Replace("&", "") } catch { $n = "" }
        if ($n -eq "Print" -or $n -eq "Cetak") {
            $printBtn = $btn
            break
        }
    }
    if (-not $printBtn) {
        throw "Print dialog for '$namedPrinter' has no Print button."
    }
    if (-not (Invoke-UiaControl $printBtn)) {
        throw "Could not click Print on the dialog for printer '$namedPrinter'."
    }
    return $true
}

function Send-CtrlPToWindow($hwnd) {
    # Ctrl+P to this Chrome window only. Do not SendKeys to whatever happens
    # to be in the foreground.
    Ensure-Win32Helper
    $ptr = [IntPtr]$hwnd
    $pidOut = [uint32]0
    $windowThread = [AutocountPrintWin32]::GetWindowThreadProcessId($ptr, [ref]$pidOut)
    $currentThread = [AutocountPrintWin32]::GetCurrentThreadId()
    $attached = $false
    if ($windowThread -ne 0 -and $windowThread -ne $currentThread) {
        $attached = [AutocountPrintWin32]::AttachThreadInput($currentThread, $windowThread, $true)
    }
    [AutocountPrintWin32]::ShowWindow($ptr, 9) | Out-Null
    [AutocountPrintWin32]::SetForegroundWindow($ptr) | Out-Null
    Start-Sleep -Milliseconds 200
    $fg = [AutocountPrintWin32]::GetForegroundWindow()
    if ($fg -ne $ptr) {
        if ($attached) {
            [AutocountPrintWin32]::AttachThreadInput($currentThread, $windowThread, $false) | Out-Null
        }
        throw "Could not focus the Cloud report Chrome window to send Ctrl+P. Refusing to send keys to some other window."
    }
    [AutocountPrintWin32]::keybd_event([AutocountPrintWin32]::VK_CONTROL, 0, 0, [UIntPtr]::Zero)
    [AutocountPrintWin32]::keybd_event([AutocountPrintWin32]::VK_P, 0, 0, [UIntPtr]::Zero)
    [AutocountPrintWin32]::keybd_event([AutocountPrintWin32]::VK_P, 0, [AutocountPrintWin32]::KEYEVENTF_KEYUP, [UIntPtr]::Zero)
    [AutocountPrintWin32]::keybd_event([AutocountPrintWin32]::VK_CONTROL, 0, [AutocountPrintWin32]::KEYEVENTF_KEYUP, [UIntPtr]::Zero)
    if ($attached) {
        [AutocountPrintWin32]::AttachThreadInput($currentThread, $windowThread, $false) | Out-Null
    }
}

function Send-PdfToNamedPrinter($pdfPath, $PrinterName) {
    # FolderItem.InvokeVerbEx("printto", name) targets that printer without
    # touching the Windows default. ProcessStartInfo printto is the fallback
    # if the shell verb is missing on the COM object. Never combine
    # Start-Process -Verb with -ArgumentList (different 5.1 parameter sets).
    # Only used when AutoCount/Chrome exported a real report PDF after login
    # and Print Report — never a headless dump of the login page.
    $sentToPrinter = $false
    $folderPath = Split-Path -LiteralPath $pdfPath
    $fileName = Split-Path -LiteralPath $pdfPath -Leaf
    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.NameSpace($folderPath)
    $item = $folder.ParseName($fileName)
    if ($item) {
        try {
            $item.InvokeVerbEx("printto", $PrinterName) | Out-Null
            $sentToPrinter = $true
        } catch {
            # Fall through to ProcessStartInfo printto, still with the exact name.
        }
    }
    if (-not $sentToPrinter) {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $pdfPath
        $psi.UseShellExecute = $true
        $psi.Verb = "printto"
        $psi.Arguments = '"' + $PrinterName + '"'
        try {
            $printProc = [System.Diagnostics.Process]::Start($psi)
            if ($printProc) {
                $printProc.WaitForExit()
            }
        } catch {
            throw "Windows could not printto printer '$PrinterName'. The agent never uses the Windows default printer."
        }
    }
}

function Get-NamedPrinterJobIds($namedPrinter) {
    $ids = @()
    try {
        foreach ($job in @(Get-PrintJob -PrinterName $namedPrinter -ErrorAction SilentlyContinue)) {
            $ids += [string]$job.Id
        }
    } catch {
    }
    try {
        $prefix = $namedPrinter + ","
        foreach ($job in @(Get-CimInstance Win32_PrintJob -ErrorAction SilentlyContinue)) {
            $name = [string]$job.Name
            if ($name.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                $ids += [string]$job.JobId
            }
        }
    } catch {
    }
    return $ids
}

function Wait-ForNamedPrinterJob($namedPrinter, $beforeIds, $waitSeconds) {
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    while ((Get-Date) -lt $deadline) {
        foreach ($id in @(Get-NamedPrinterJobIds $namedPrinter)) {
            if ($beforeIds -notcontains $id) { return $true }
        }
        Start-Sleep -Milliseconds 250
    }
    foreach ($id in @(Get-NamedPrinterJobIds $namedPrinter)) {
        if ($beforeIds -notcontains $id) { return $true }
    }
    return $false
}

function Get-NewPdfFiles($folders, $since) {
    $found = @()
    foreach ($dir in $folders) {
        if (-not $dir) { continue }
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        Get-ChildItem -LiteralPath $dir -Filter *.pdf -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.LastWriteTime -ge $since -and $_.Length -ge 100) {
                $found += $_
            }
        }
    }
    return $found
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Stop-ProfileChrome $UserDataDir
try {
    Set-ChromePrintPrefs $UserDataDir $PrinterName $workDir
} catch {
    # Prefs merge is best-effort; continue to Print Report.
}

$chromePsi = New-Object System.Diagnostics.ProcessStartInfo
$chromePsi.FileName = $chrome
$chromePsi.UseShellExecute = $false
# Headed Chrome only (no headless PDF dump of the Cloud URL).
# --force-renderer-accessibility exposes AutoCount Print Report to UI Automation.
# --hide-crash-restore-bubble avoids a restore prompt after leftover profile Chrome was stopped.
$chromePsi.Arguments = (
    '--user-data-dir="{0}" --kiosk-printing --no-first-run --no-default-browser-check --start-maximized --force-renderer-accessibility --hide-crash-restore-bubble "{1}"' -f $UserDataDir, $Url
)
$chromeProc = [System.Diagnostics.Process]::Start($chromePsi)
if (-not $chromeProc) {
    throw "Google Chrome did not start for the print-agent profile."
}

try {
    $waitForReport = [Math]::Max($WaitSeconds, 90)
    $report = Wait-ForCloudReport $UserDataDir $waitForReport
    $reportWindow = $report.Window
    $printControl = $report.PrintControl
    $chromeEl = $report.Element
    if (Test-LoginWindowTitle $reportWindow.Title) {
        throw "Cloud report is still the AutoCount login page (Log In). Log into AutoCount Cloud in the print-agent Chrome profile, open any invoice report once, then close Chrome."
    }

    $jobsBefore = @(Get-NamedPrinterJobIds $PrinterName)
    $pdfWatchStart = (Get-Date).AddSeconds(-1)
    $usedPrintReport = $false
    if ($printControl) {
        Ensure-Win32Helper
        [AutocountPrintWin32]::SetForegroundWindow([IntPtr]$reportWindow.Hwnd) | Out-Null
        Start-Sleep -Milliseconds 300
        if (Invoke-UiaControl $printControl) {
            $usedPrintReport = $true
        }
    }

    if (-not $usedPrintReport) {
        if (Test-LoginWindowTitle $reportWindow.Title) {
            throw "Cloud report is still the AutoCount login page (Log In). Refusing to send Ctrl+P."
        }
        Send-CtrlPToWindow $reportWindow.Hwnd
    }

    $watchFolders = @(
        $workDir,
        (Join-Path $UserDataDir "Default\Downloads")
    )
    $printedViaDialog = $false
    $exported = @()
    $sawNamedJob = $false
    $observeDeadline = (Get-Date).AddSeconds(16)
    while ((Get-Date) -lt $observeDeadline) {
        foreach ($id in @(Get-NamedPrinterJobIds $PrinterName)) {
            if ($jobsBefore -notcontains $id) { $sawNamedJob = $true }
        }
        if ($sawNamedJob) { break }
        if (-not $printedViaDialog) {
            $dialog = Find-PrintDialogWindow $UserDataDir
            if ($dialog) {
                $printedViaDialog = Confirm-PrintDialogForNamedPrinter $PrinterName 2 $UserDataDir
            }
        }
        if ($exported.Count -eq 0) {
            $exported = @(Get-NewPdfFiles $watchFolders $pdfWatchStart)
        }
        Start-Sleep -Milliseconds 250
    }

    $usedPrintTo = $false
    if (-not $sawNamedJob -and $exported.Count -gt 0) {
        Send-PdfToNamedPrinter $exported[0].FullName $PrinterName
        $usedPrintTo = $true
        $sawNamedJob = Wait-ForNamedPrinterJob $PrinterName $jobsBefore 12
        try {
            if ($exported[0].DirectoryName -eq $workDir) {
                Start-Sleep -Seconds 2
                Remove-Item -LiteralPath $exported[0].FullName -Force -ErrorAction SilentlyContinue
            }
        } catch {
        }
    } elseif (-not $sawNamedJob) {
        $sawNamedJob = Wait-ForNamedPrinterJob $PrinterName $jobsBefore 8
    }

    if (-not $sawNamedJob) {
        throw "No print job reached printer '$PrinterName'. The Cloud report was not sent to that named Epson (login pages and other printers are never used)."
    }

    Start-Sleep -Seconds 6
} finally {
    Stop-ProfileChrome $UserDataDir
}
