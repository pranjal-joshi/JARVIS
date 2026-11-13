"""ps_classify.ps1 must never rate a changing command look-only (it then runs without approval)."""
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PWSH = shutil.which("pwsh") or shutil.which("powershell")
# the way the bridge runs it: -EncodedCommand, so execution policy for script files doesn't apply
SCRIPT = base64.b64encode(Path(__file__).with_name("ps_classify.ps1").read_text(encoding="utf-8").encode("utf-16-le")).decode()
pytestmark = pytest.mark.skipif(not PWSH, reason="no PowerShell")


def classify(cmd: str) -> dict:
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-EncodedCommand", SCRIPT], capture_output=True,
                         text=True, env={**os.environ, "JARVIS_PS_CMD": cmd}, timeout=60).stdout
    return json.loads(out.strip().splitlines()[-1])


@pytest.mark.parametrize("cmd", [
    r"Get-ChildItem C:\x | ForEach-Object -MemberName Delete",
    "Get-Process notepad | % Kill",
    r'(Get-ChildItem C:\x).ForEach("Delete")',
    "(Get-Item a.txt).IsReadOnly = $true",
])
def test_changes_are_not_read(cmd):
    assert classify(cmd)["read"] is False


@pytest.mark.parametrize("cmd", [
    "Get-Process | ForEach-Object { $_.Name }",
    "Get-ChildItem | Where-Object { $_.Length -gt 1kb }",
    "(Get-Process).ForEach({ $_.Id })",
    "$n = (Get-Process).Count; $n",
])
def test_lookups_stay_read(cmd):
    assert classify(cmd)["read"] is True
