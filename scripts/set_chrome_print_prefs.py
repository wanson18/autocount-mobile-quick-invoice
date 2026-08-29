"""Merge sticky print settings into a Chrome profile Preferences file.

Uses stdlib json only. PowerShell 5.1 must not round-trip this file through
JavaScriptSerializer or ConvertTo-Json (those wrap nested dicts and throw
on serialize). Prefs merge is best-effort for the office print agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    current = parent.get(key)
    if not isinstance(current, dict):
        current = {}
        parent[key] = current
    return current


def merge_chrome_print_prefs(
    prefs_path: str, printer_name: str, download_dir: str
) -> None:
    path = Path(prefs_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefs: dict[str, Any] = {}
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
        if raw.strip():
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                prefs = loaded

    app_state = json.dumps(
        {
            "recentDestinations": [
                {
                    "id": printer_name,
                    "origin": "local",
                    "account": "",
                    "displayName": printer_name,
                }
            ],
            "selectedDestinationId": printer_name,
            "version": 2,
            "isHeaderFooterEnabled": False,
        },
        separators=(",", ":"),
    )
    sticky = _ensure_dict(_ensure_dict(prefs, "printing"), "print_preview_sticky_settings")
    sticky["appState"] = app_state
    download = _ensure_dict(prefs, "download")
    download["default_directory"] = download_dir
    download["prompt_for_download"] = False
    savefile = _ensure_dict(prefs, "savefile")
    savefile["default_directory"] = download_dir
    profile = _ensure_dict(prefs, "profile")
    profile["exit_type"] = "Normal"
    profile["exited_cleanly"] = True

    path.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        return 2
    merge_chrome_print_prefs(args[0], args[1], args[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
