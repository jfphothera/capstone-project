from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "deploy" / "windows"


def _script(name: str) -> str:
    return (WINDOWS / name).read_text(encoding="utf-8")


def test_update_uses_the_configured_listen_address_for_health_gate():
    update = _script("Update-Capstone.ps1")
    assert "config\\appsettings.json" in update
    assert '"http://$($settings.listen_host):$($settings.listen_port)/api/health"' in update
    assert "http://127.0.0.1:8000/api/health" not in update


def test_stop_terminates_install_owned_processes_and_waits_for_port_release():
    stop = _script("Stop-Capstone.ps1")
    assert "Win32_Process" in stop
    assert "$_.ExecutablePath" in stop
    assert "StartsWith($installPrefix,[StringComparison]::OrdinalIgnoreCase)" in stop
    assert "Stop-Process" in stop
    assert "Get-NetTCPConnection -LocalPort $listenPort -State Listen" in stop
    assert "port $listenPort is still listening" in stop


def test_all_lifecycle_callers_forward_custom_roots_to_stop():
    for name in ("Update-Capstone.ps1", "Rollback-Capstone.ps1"):
        script = _script(name)
        calls = [line for line in script.splitlines() if "Stop-Capstone.ps1" in line]
        assert calls
        assert all("-InstallRoot $InstallRoot -DataRoot $DataRoot" in line for line in calls)


def test_lab_heads_emits_each_result_before_the_final_report():
    script = _script("Test-CapstoneLabHeads.ps1")
    loop = script.index("foreach($head in")
    per_head = script.index("$record|ConvertTo-Json -Compress", loop)
    final_report = script.index("targets=$output.Count", per_head)
    assert loop < per_head < final_report


def test_release_version_and_gateway_only_legacy_api_message():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert version == "6.0.0-rc.29"
    assert (ROOT / "docs" / f"RELEASE_NOTES_{version}.md").exists()
    router = (ROOT / "src" / "capstone_rag" / "webapp" / "routers" / "ai.py").read_text(encoding="utf-8")
    assert "not available: Capstone runs through the Inference Gateway only" in router
    assert not (ROOT / "src" / "capstone_rag" / "webapp" / "static" / "js" / "ai_configuration.js").exists()

