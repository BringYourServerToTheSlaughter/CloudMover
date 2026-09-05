from src.cloudmover.providers import proton

def test_json_flag_is_command_level(monkeypatch):
    seen = {}
    class R:
        returncode = 0
        stdout = "[]"
        stderr = ""
    def fake_run_cli(cmd, timeout=120.0):
        seen["cmd"] = cmd
        return "[]","",0
    monkeypatch.setattr(proton.shutil, "which", lambda _: "/usr/local/bin/proton-drive")
    monkeypatch.setattr(proton, "_run_cli_process", fake_run_cli)
    proton.run(["filesystem", "list", "/my-files"], json_output=True)
    assert seen["cmd"] == [
        "/usr/local/bin/proton-drive",
        "filesystem",
        "list",
        "-j",
        "/my-files",
    ]
