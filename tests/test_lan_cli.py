import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from phonelab import lan_cli
from phonelab.network_plan import make_plan


def setup(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "project"
    private = data / "private"
    airplay = data / "airplay-build"
    runtime = data / "toolchain" / "lan-python"
    for path in (private, airplay, runtime, root / "scripts"):
        path.mkdir(parents=True, exist_ok=True)
    exe = runtime / "python.exe"
    exe.write_bytes(b"signed-runtime-test-fixture")
    runtime_files = {"python.exe": exe}
    for name in sorted(lan_cli.RUNTIME_FILES - {"python.exe"}):
        path = runtime / name
        path.write_bytes(("fixture-" + name).encode())
        runtime_files[name] = path
    pth = runtime / "python._pth"
    pth.write_text(f"{root}\n")
    file_manifest = {name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "signer": "fixture"}
                     for name, path in runtime_files.items()}
    (runtime / "runtime-manifest.json").write_text(json.dumps({
        "files": file_manifest,
        "python_pth_sha256": hashlib.sha256(pth.read_bytes()).hexdigest()}))
    proposal_path = private / "network-proposal.json"
    marker_path = airplay / "receiver-run-approval.json"
    bind, subnet, interface = "192.168.1.4", "192.168.1.0/24", "Ethernet-Test"
    uxplay = str((data / "airplay-build" / "uxplay.exe").resolve())
    rules = make_plan(bind, 24, interface, [interface, "WLAN"], uxplay, str(exe.resolve()))["rules"]
    proposal = {"execute_allowed": True, "rules": rules, "local_address": bind,
        "remote_subnet": subnet, "interface": interface}
    marker = {"approved": True, "firewallScopeVerified": True, "firewallProfile": "Public",
        "interfaceAlias": interface, "localAddress": bind, "remoteSubnet": subnet,
        "programPath": uxplay, "tcpPorts": [53100, 53101, 53102],
        "udpPorts": [53100, 53101, 53102], "mdnsUdpPort": 5353}
    proposal_path.write_text(json.dumps(proposal))
    marker_path.write_text(json.dumps(marker))
    (root / "scripts" / "Test-AirPlayScope.ps1").write_text("# fixture")
    return data, root, exe, marker_path


def test_preview_is_default_and_never_invokes_checks_or_binds(tmp_path):
    data, root, exe, _ = setup(tmp_path)
    result = lan_cli.run(data=data, project_root=root, executable=str(exe),
        runner=lambda *a, **k: pytest.fail("preview must not run external checks"),
        server_factory=lambda *a: pytest.fail("preview must not bind"))
    assert result == {"state": "preview_only", "listener_started": False}
    assert lan_cli.main([]) == 0


def test_scope_accepts_windows_program_case_but_rejects_another_executable(tmp_path):
    data, root, exe, marker_path = setup(tmp_path)
    proposal_path = data / 'private' / 'network-proposal.json'
    proposal = json.loads(proposal_path.read_text())
    marker = json.loads(marker_path.read_text())
    for rule in proposal['rules']:
        if rule['name'] == 'LabPage-Allow':
            rule['Program'] = str(exe.resolve()).swapcase()
    assert lan_cli._proposal_scope(proposal, marker, proposal['local_address'],
                                   proposal['remote_subnet'], str(exe.resolve()))
    next(r for r in proposal['rules'] if r['name'] == 'LabPage-Allow')['Program'] = str(exe.parent / 'other.exe')
    assert not lan_cli._proposal_scope(proposal, marker, proposal['local_address'],
                                       proposal['remote_subnet'], str(exe.resolve()))


def test_run_requires_marker_and_fresh_scope_before_server_factory(tmp_path):
    data, root, exe, marker_path = setup(tmp_path)
    marker = json.loads(marker_path.read_text())
    marker["firewallScopeVerified"] = False
    marker_path.write_text(json.dumps(marker))
    result = lan_cli.run(execute=True, data=data, project_root=root, executable=str(exe),
        server_factory=lambda *a: pytest.fail("unapproved firewall marker must block"))
    assert result["state"] == "blocked" and result["reason"] == "approval_scope_mismatch"
    marker["firewallScopeVerified"] = True
    marker_path.write_text(json.dumps(marker))
    result = lan_cli.run(execute=True, data=data, project_root=root, executable=str(exe),
        scope_check=lambda: False,
        server_factory=lambda *a: pytest.fail("failed fresh check must block"))
    assert result["reason"] == "fresh_airplay_scope_check_failed"


def test_runtime_binding_rejects_mutated_dll_and_extra_or_traversal_keys(tmp_path):
    data, root, exe, _ = setup(tmp_path)
    dll = data / "toolchain" / "lan-python" / "python313.dll"
    dll.write_bytes(b"modified")
    assert not lan_cli._runtime_bound(data, root, str(exe))
    dll.write_bytes(b"fixture-python313.dll")
    manifest_path = data / "toolchain" / "lan-python" / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["extra.dll"] = {"sha256": "0" * 64, "signer": "fixture"}
    manifest_path.write_text(json.dumps(manifest))
    assert not lan_cli._runtime_bound(data, root, str(exe))
    del manifest["files"]["extra.dll"]
    manifest["files"]["..\\python.exe"] = manifest["files"].pop("python.exe")
    manifest_path.write_text(json.dumps(manifest))
    assert not lan_cli._runtime_bound(data, root, str(exe))


def test_verified_run_checks_listener_image_before_marking_running(tmp_path):
    data, root, exe, _ = setup(tmp_path)
    events = []
    class Server:
        def serve_forever(self): events.append("served")
        def server_close(self): events.append("closed")
    result = lan_cli.run(execute=True, data=data, project_root=root, executable=str(exe),
        scope_check=lambda: True, listener_check=lambda: True,
        server_factory=lambda *a: Server(), serve=lambda server: server.serve_forever(),
        on_running=lambda: events.append("running"))
    assert result == {"state": "stopped", "listener_started": True}
    assert events == ["running", "served", "closed"]


def test_failed_listener_identity_reports_transient_bind_and_closes(tmp_path):
    data, root, exe, _ = setup(tmp_path)
    events = []
    class Server:
        def serve_forever(self): pytest.fail("must not serve before PID check")
        def server_close(self): events.append("closed")
    result = lan_cli.run(execute=True, data=data, project_root=root, executable=str(exe),
        scope_check=lambda: True, listener_check=lambda: False,
        server_factory=lambda *a: Server())
    assert result == {"state": "blocked", "reason": "listener_process_image_unverified",
                      "listener_started": True, "listener_closed": True}
    assert events == ["closed"]


def test_keyboard_interrupt_closes_proxy_and_returns_stopped(tmp_path):
    data, root, exe, _ = setup(tmp_path)
    events = []
    class Server:
        def serve_forever(self): raise KeyboardInterrupt
        def server_close(self): events.append("closed")
    result = lan_cli.run(execute=True, data=data, project_root=root, executable=str(exe),
        scope_check=lambda: True, listener_check=lambda: True,
        server_factory=lambda *a: Server(), on_running=lambda: events.append("running"))
    assert result == {"state": "stopped", "listener_started": True}
    assert events == ["running", "closed"]


def test_listener_verifier_uses_exact_expected_pid_and_image_without_shell(tmp_path):
    pwsh = tmp_path / "pwsh.exe"
    pwsh.write_bytes(b"test")
    observed = {}
    def runner(args, **kwargs):
        observed.update(args=args, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="true\n")
    assert lan_cli._verify_listener("192.0.2.5", 8767, 4242, str(tmp_path / "python.exe"),
                                   runner=runner, pwsh=pwsh)
    assert observed["args"][:3] == [str(pwsh), "-NoProfile", "-NonInteractive"]
    env = observed["kwargs"]["env"]
    assert env["PHONELAB_LISTENER_PID"] == "4242"
    assert env["PHONELAB_LISTENER_ADDRESS"] == "192.0.2.5"
    assert observed["kwargs"]["timeout"] == 30


def test_fresh_scope_gate_requires_exact_success_token(tmp_path):
    data, root, _, _ = setup(tmp_path)
    pwsh = tmp_path / "pwsh.exe"
    pwsh.write_bytes(b"test")
    def runner_ok(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="AIRPLAY_SCOPE_VERIFIED\n")
    def runner_bad(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="AIRPLAY_SCOPE_BLOCKED: failure\n")
    assert lan_cli._fresh_scope_check(data, root, runner_ok, pwsh)
    assert not lan_cli._fresh_scope_check(data, root, runner_bad, pwsh)


def test_readonly_subprocess_timeouts_fail_closed(tmp_path):
    data, root, _, _ = setup(tmp_path)
    pwsh = tmp_path / "pwsh.exe"
    pwsh.write_bytes(b"test")
    def timeout_runner(*args, **kwargs):
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    assert not lan_cli._fresh_scope_check(data, root, timeout_runner, pwsh)
    assert not lan_cli._verify_listener("192.0.2.5", 8767, 9, str(tmp_path / "python.exe"),
                                        runner=timeout_runner, pwsh=pwsh)
