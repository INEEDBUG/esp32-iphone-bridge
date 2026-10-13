"""Preview-first launcher for the separately approved LAN-only lab page."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

from phonelab import deploy
from phonelab.lan_lab import create_lan_proxy, validate_lan_proxy_config
from phonelab.network_plan import make_plan
from phonelab.paths import PROJECT_ROOT, resolve_pwsh

ROOT = PROJECT_ROOT
DATA = deploy.DATA
PRIVATE = DATA / "private"
AIRPLAY = DATA / "airplay-build"
PROPOSAL = PRIVATE / "network-proposal.json"
MARKER = AIRPLAY / "receiver-run-approval.json"
SCOPE_SCRIPT = ROOT / "scripts" / "Test-AirPlayScope.ps1"
PWSH = resolve_pwsh()
BACKEND_PORT = 8766
PROXY_PORT = 8767
RULES = {
    "UxPlay-Block-OutsideLAN-v4", "UxPlay-Block-v6", "UxPlay-Block-OtherInterfaces",
    "UxPlay-Allow-TCP", "UxPlay-Allow-UDP", "UxPlay-Allow-mDNS-v4", "LabPage-Allow",
    "UxPlay-Block-OutsideLAN-v4-Out", "UxPlay-Block-v6-Out", "UxPlay-Block-OtherInterfaces-Out",
    "UxPlay-Block-OtherTCPPorts", "UxPlay-Block-OtherUDPPorts",
}
RUNTIME_FILES = {"python.exe", "python313.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"}


def _load(path):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def _runtime_bound(data: Path, project_root: Path, executable: str) -> bool:
    expected = (data / "toolchain" / "lan-python" / "python.exe").resolve()
    try:
        actual = Path(executable).resolve()
        manifest = _load(data / "toolchain" / "lan-python" / "runtime-manifest.json")
        pth = data / "toolchain" / "lan-python" / "python._pth"
        pth_sha = hashlib.sha256(pth.read_bytes()).hexdigest()
        pth_lines = {line.strip().casefold() for line in pth.read_text(encoding="utf-8").splitlines() if line.strip()}
        file_records = manifest.get("files") if manifest else None
        if not isinstance(file_records, dict) or set(file_records) != RUNTIME_FILES:
            return False
        for name in RUNTIME_FILES:
            record = file_records.get(name)
            if not isinstance(record, dict) or set(record) != {"sha256", "signer"}:
                return False
            expected_hash = record.get("sha256")
            if not isinstance(expected_hash, str) or len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
                return False
            file_path = expected.parent / name
            if hashlib.sha256(file_path.read_bytes()).hexdigest() != expected_hash:
                return False
        return bool(actual == expected and manifest and manifest.get("python_pth_sha256") == pth_sha and
                    "import site" not in pth.read_text(encoding="utf-8").casefold() and
                    str(project_root.resolve()).casefold() in pth_lines)
    except (OSError, ValueError, TypeError):
        return False


def _proposal_scope(proposal: dict, marker: dict, bind_address: str, remote_subnet: str,
                    executable: str) -> bool:
    if proposal.get("execute_allowed") is not True or not marker.get("approved") is True or marker.get("firewallScopeVerified") is not True:
        return False
    rules = proposal.get("rules")
    if not isinstance(rules, list) or len(rules) != 12:
        return False
    if {r.get("name") for r in rules if isinstance(r, dict)} != RULES:
        return False
    if proposal.get("local_address") != bind_address or proposal.get("remote_subnet") != remote_subnet:
        return False
    try:
        addr = ipaddress.ip_address(bind_address)
        subnet = ipaddress.ip_network(remote_subnet, strict=True)
        prefix = subnet.prefixlen
    except ValueError:
        return False
    if not isinstance(addr, ipaddress.IPv4Address) or not isinstance(subnet, ipaddress.IPv4Network) or addr not in subnet:
        return False
    interface = proposal.get("interface")
    blocked = next((r for r in rules if isinstance(r, dict) and r.get("name") == "UxPlay-Block-OtherInterfaces"), None)
    uxplay = marker.get("programPath")
    if not isinstance(blocked, dict) or not isinstance(blocked.get("InterfaceAlias"), list) or not isinstance(uxplay, str):
        return False
    try:
        expected = make_plan(bind_address, prefix, interface,
                             [interface, *blocked["InterfaceAlias"]], uxplay, executable)
    except (TypeError, ValueError):
        return False
    def canonical(row):
        normalized = dict(row)
        # Windows image paths are case-insensitive. Path.resolve() may return
        # the stored directory casing even when the approved launch used AI.
        if isinstance(normalized.get("Program"), str):
            normalized["Program"] = str(Path(normalized["Program"]).resolve()).casefold()
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    if sorted(map(canonical, rules)) != sorted(map(canonical, expected["rules"])):
        return False
    return (marker.get("firewallProfile") == "Public" and
            marker.get("interfaceAlias") == interface and
            marker.get("localAddress") == bind_address and marker.get("remoteSubnet") == remote_subnet and
            marker.get("tcpPorts") == [53100, 53101, 53102] and
            marker.get("udpPorts") == [53100, 53101, 53102] and marker.get("mdnsUdpPort") == 5353)


def _fresh_scope_check(data: Path, project_root: Path, runner=subprocess.run, pwsh: Path = PWSH) -> bool:
    if not pwsh.is_file() or not (project_root / "scripts" / "Test-AirPlayScope.ps1").is_file():
        return False
    try:
        result = runner([str(pwsh), "-NoProfile", "-NonInteractive", "-File",
                         str(project_root / "scripts" / "Test-AirPlayScope.ps1"),
                         "-DataRoot", str(data / "airplay-build")],
                        capture_output=True, text=True, check=False, timeout=30)
        return result.returncode == 0 and result.stdout.strip() == "AIRPLAY_SCOPE_VERIFIED"
    except Exception:
        return False


def _verify_listener(local_address: str, port: int, expected_pid: int,
                     executable: str, runner=subprocess.run, pwsh: Path = PWSH) -> bool:
    """Read-only Windows listener/PID/image check before reporting running."""
    if not pwsh.is_file():
        return False
    env = dict(os.environ)
    env.update({"PHONELAB_LISTENER_ADDRESS": local_address,
                "PHONELAB_LISTENER_PORT": str(port),
                "PHONELAB_LISTENER_PID": str(expected_pid),
                "PHONELAB_LISTENER_IMAGE": str(Path(executable).resolve())})
    ps = r"""
$ErrorActionPreference='Stop'
$address=$env:PHONELAB_LISTENER_ADDRESS
$port=[int]$env:PHONELAB_LISTENER_PORT
$pidExpected=[int]$env:PHONELAB_LISTENER_PID
$imageExpected=[IO.Path]::GetFullPath($env:PHONELAB_LISTENER_IMAGE)
$listeners=@(Get-NetTCPConnection -State Listen -LocalAddress $address -LocalPort $port -ErrorAction Stop)
$ok=$false
if ($listeners.Count -eq 1 -and [int]$listeners[0].OwningProcess -eq $pidExpected) {
  $process=Get-CimInstance Win32_Process -Filter "ProcessId=$pidExpected" -ErrorAction Stop
  $ok=($null -ne $process -and [IO.Path]::GetFullPath([string]$process.ExecutablePath) -ieq $imageExpected)
}
ConvertTo-Json -InputObject ([bool]$ok) -Compress
"""
    try:
        result = runner([str(pwsh), "-NoProfile", "-NonInteractive", "-Command", ps],
                        capture_output=True, text=True, check=False, env=env, timeout=30)
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except Exception:
        return False


def run(*, execute=False, data: Path = DATA, project_root: Path = ROOT,
        executable: str = sys.executable, runner=subprocess.run,
        scope_check=None, listener_check=None, server_factory=None, serve=None, on_running=None):
    """Preview by default. Only explicit approved run can create a LAN listener."""
    if not execute:
        return {"state": "preview_only", "listener_started": False}
    runtime_root = data / "toolchain" / "lan-python"
    if not _runtime_bound(data, project_root, executable):
        return {"state": "blocked", "reason": "isolated_runtime_not_verified", "listener_started": False}
    proposal = _load(PROPOSAL if data == DATA else data / "private" / "network-proposal.json")
    marker = _load(MARKER if data == DATA else data / "airplay-build" / "receiver-run-approval.json")
    if proposal is None or marker is None:
        return {"state": "blocked", "reason": "approval_or_proposal_missing", "listener_started": False}
    local_address = proposal.get("local_address")
    remote_subnet = proposal.get("remote_subnet")
    if not isinstance(local_address, str) or not isinstance(remote_subnet, str) or not _proposal_scope(
            proposal, marker, local_address, remote_subnet, str((runtime_root / "python.exe").resolve())):
        return {"state": "blocked", "reason": "approval_scope_mismatch", "listener_started": False}
    try:
        validate_lan_proxy_config(local_address, remote_subnet, BACKEND_PORT,
            {"lan_test_page": True, "firewall_scope_verified": True,
             "local_address": local_address, "remote_subnet": remote_subnet})
    except ValueError:
        return {"state": "blocked", "reason": "proxy_scope_invalid", "listener_started": False}
    checker = scope_check or (lambda: _fresh_scope_check(data, project_root, runner))
    if not checker():
        return {"state": "blocked", "reason": "fresh_airplay_scope_check_failed", "listener_started": False}
    factory = server_factory or create_lan_proxy
    try:
        server = factory(local_address, remote_subnet, BACKEND_PORT,
                         {"lan_test_page": True, "firewall_scope_verified": True,
                          "local_address": local_address, "remote_subnet": remote_subnet}, PROXY_PORT)
    except Exception:
        return {"state": "blocked", "reason": "listener_bind_failed", "listener_started": False}
    try:
        verifier = listener_check or (lambda: _verify_listener(local_address, PROXY_PORT, os.getpid(), executable, runner))
        if not verifier():
            return {"state": "blocked", "reason": "listener_process_image_unverified",
                    "listener_started": True, "listener_closed": True}
        if on_running:
            on_running()
        if serve:
            serve(server)
        else:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return {"state": "stopped", "listener_started": True}
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Restricted LAN PhoneLab projection")
    parser.add_argument("--run", action="store_true", help="explicitly request the approved LAN listener")
    args = parser.parse_args(argv)
    result = run(execute=args.run, on_running=lambda: print("LAN_PROXY_RUNNING: restricted listener active.") if args.run else None)
    if result["state"] == "preview_only":
        print("PREVIEW_ONLY: no process or network listener started.")
        return 0
    if result["state"] == "blocked":
        print("RUN_BLOCKED: " + result["reason"])
        return 1
    print("LAN_PROXY_STOPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
