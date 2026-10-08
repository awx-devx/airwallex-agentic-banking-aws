import subprocess

from awx_agent import staging


def test_uv_calls_never_colour_their_output(monkeypatch):
    """`npx cdk synth` forces colour; ANSI codes in the exported requirements broke staging."""
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="pyjwt==2.15.1\n", stderr="")

    monkeypatch.setattr(staging.subprocess, "run", fake_run)
    monkeypatch.setenv("FORCE_COLOR", "1")
    staging._runtime_requirements()
    assert seen
    for argv in seen:
        assert argv[1:3] == ["--color", "never"]


def test_runtime_exclusions_are_anchored_to_awx_agent():
    """Only awx_agent/testing.py and awx_agent/handlers are dropped."""
    names = ["testing.py", "handlers", "app.py", "__pycache__"]
    assert staging._RUNTIME_IGNORE(str(staging._PKG_DIR), names) == {
        "testing.py",
        "handlers",
        "__pycache__",
    }
    other = staging.package_dir("awx_common")
    assert staging._RUNTIME_IGNORE(str(other), names) == {"__pycache__"}
