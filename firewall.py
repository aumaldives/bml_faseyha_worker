"""Read/modify the Windows Firewall allow-list for the worker port.

The server runs as SYSTEM (Highest), so it can edit the inbound rule
`BMLAPI Worker (port 8000) - allowed IPs` via PowerShell. Only the /faseyha
admin layer (cookie-authenticated) calls this.

Every IP/CIDR/range is validated with the stdlib `ipaddress` module before being
passed to PowerShell, and the rule DisplayName is a fixed constant — so nothing
user-supplied is interpolated unvalidated into the command (no shell injection).
"""

import os
import ipaddress
import subprocess

from logutil import log

RULE = os.environ.get("FASEYHA_FW_RULE", "BMLAPI Worker (port 8000) - allowed IPs")


def valid_ip(value):
    """Accept a single IPv4/IPv6 address, a CIDR network, or an a.b.c.d-e.f.g.h range."""
    s = str(value).strip()
    if not s:
        return False
    try:
        if "-" in s:
            a, b = s.split("-", 1)
            ipaddress.ip_address(a.strip())
            ipaddress.ip_address(b.strip())
        elif "/" in s:
            ipaddress.ip_network(s, strict=False)
        else:
            ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _run_ps(script):
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def get_allowed():
    """Return (list_of_ips, None) or (None, error_message)."""
    script = ("$ErrorActionPreference='Stop';"
              f"((Get-NetFirewallRule -DisplayName '{RULE}' "
              "| Get-NetFirewallAddressFilter).RemoteAddress)")
    rc, out, err = _run_ps(script)
    if rc != 0:
        return None, (err.strip() or "failed to read firewall rule")
    ips = []
    for line in out.splitlines():
        v = line.strip()
        if v and v.lower() != "any" and v not in ips:
            ips.append(v)
    return ips, None


def set_allowed(ips):
    """Replace the rule's RemoteAddress with `ips`. Returns (ok, error)."""
    clean = []
    for ip in ips:
        s = str(ip).strip()
        if s and valid_ip(s) and s not in clean:
            clean.append(s)
    if not clean:
        return False, "refusing to set an empty allow-list (would change exposure)"
    arr = ",".join("'" + s + "'" for s in clean)   # each entry already ipaddress-validated
    script = ("$ErrorActionPreference='Stop';"
              f"Set-NetFirewallRule -DisplayName '{RULE}' -RemoteAddress @({arr})")
    rc, out, err = _run_ps(script)
    if rc != 0:
        log(f"firewall: set failed: {err.strip()}")
        return False, (err.strip() or "failed to set firewall rule")
    log(f"firewall: allow-list set to {clean}")
    return True, None
