# Office print agent

The iPhone PWA queues a print job. This agent runs on the **always-on office
Windows PC**, claims the job, opens the official AutoCount Cloud invoice
report in **Google Chrome**, and sends it to **EPSONE85FF0 (L6460 Series)**
by that exact printer name. It never reads or changes the Windows default
printer.

The phone never receives the Cloud URL or the account-book path. The agent
does, after authenticating with `PRINT_AGENT_TOKEN`.

## One-time setup

1. Install Python 3.11+ on the office PC.
2. Copy [`print-agent.local.json.example`](print-agent.local.json.example) to
   `scripts/print-agent.local.json` (gitignored) and fill in the same
   `PRINT_AGENT_TOKEN` you set on Vercel.
3. Confirm the printer name is exactly `EPSONE85FF0 (L6460 Series)`
   (Windows Settings → Printers). Chrome is expected at
   `C:\Program Files\Google\Chrome\Application\chrome.exe`.
4. **Log Chrome into AutoCount Cloud in the agent's profile**, then close
   Chrome:

   ```bat
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%LOCALAPPDATA%\AutocountPrintAgent\ChromeProfile"
   ```

   Sign in, open any invoice report once, then quit Chrome. The Cloud report
   host needs that session cookie. If prints later fail with a login/PDF
   error, repeat this step.

Leave that profile window **closed** while the agent runs; Chrome will not
share a locked user-data directory.

## Run

From a Command Prompt or PowerShell in the repo (or a copy of `scripts/`):

```bat
set PRINT_API_BASE_URL=https://autocount-mobile-quick-invoice.vercel.app
python scripts\print_agent.py
```

Or rely on `print-agent.local.json` and just:

```bat
python scripts\print_agent.py
```

Useful flags:

| Flag | Purpose |
|---|---|
| `--once` | Claim at most one job and exit |
| `--dry-run` | Mark the job printed without sending it to the Epson |
| `--config PATH` | Use a JSON file other than `scripts/print-agent.local.json` |

Environment variables override the JSON file: `PRINT_API_BASE_URL`,
`PRINT_AGENT_TOKEN`, `OFFICE_PRINTER_NAME`, `PRINT_POLL_INTERVAL_SECONDS`,
`PRINT_WAIT_SECONDS`, `PRINT_CHROME_USER_DATA_DIR`, `PRINT_DRY_RUN`.

## Start with Windows

**Startup folder (simplest):** create a shortcut to:

```bat
pythonw.exe C:\path\to\repo\scripts\print_agent.py
```

and put it in `shell:startup`. `pythonw` hides the console; use `python.exe`
instead if you want a log window.

**Scheduled Task (survives logoff if you pick "Run whether user is logged on
or not" and store the Cloud login in that user's Chrome profile):**

```powershell
$python = (Get-Command python.exe).Source
$script = "C:\path\to\repo\scripts\print_agent.py"
schtasks /Create /TN "AutocountOfficePrintAgent" /SC ONLOGON /RL LIMITED /TR "`"$python`" `"$script`"" /F
```

The PC should stay on, the Epson should stay `Normal`, and the task/user
should have permission to print.

## How it prints

The agent does **not** call an AutoCount PDF API (there isn't one) and does
not scrape Cloud HTML. It:

1. `POST /api/print-agent/jobs/next` with `Authorization: Bearer …`
2. Receives the server-resolved Cloud report HTTPS URL
3. Has Chrome at `C:\Program Files\Google\Chrome\Application\chrome.exe`
   render that official report to a temporary PDF
4. Sends the PDF to `EPSONE85FF0 (L6460 Series)` with Windows `PrintTo` /
   `printto` using that exact name (never the Windows default printer)
5. `POST /api/print-agent/jobs/{id}/complete` with `printed` or `failed`

If the Cloud session has expired, the job fails and the iPhone shows the
error. Log into AutoCount Cloud in the print-agent Chrome profile again.
