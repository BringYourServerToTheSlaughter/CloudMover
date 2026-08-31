from src.cloudmover.providers import proton

def test_json_flag_is_command_level(monkeypatch):
    seen = {}
    class R:
        returncode = 0
        stdout = "[]"
        stderr = ""
    def fake_run(cmd, text, capture_output):
        seen["cmd"] = cmd
        return R()
    monkeypatch.setattr(proton.shutil, "which", lambda _: "/usr/local/bin/proton-drive")
    monkeypatch.setattr(proton.subprocess, "run", fake_run)
    proton.run(["filesystem", "list", "/my-files"], json_output=True)
    assert seen["cmd"] == [
        "/usr/local/bin/proton-drive",
        "filesystem",
        "list",
        "-j",
        "/my-files",
    ]
