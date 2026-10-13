"""scripts/seed_airwallex_secret.py: what it uploads, what it refuses, and the documented line.

boto3 is replaced by a recording session, so no test reaches AWS. The terminal tests paste the
command from docs/deployment.md into bash and zsh under a pseudo-terminal, with the `uv` it
starts replaced by a wrapper that runs the script against the same recording session.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import select
import shutil
import signal
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "seed_airwallex_secret.py"
ARN = "arn:aws:secretsmanager:us-west-2:111122223333:secret:awx-poc/airwallex-AbCdEf"
CID, KEY = "cid-typed-123", "key-typed-456"


def _load():
    spec = importlib.util.spec_from_file_location("_awx_seed_airwallex_secret", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = _load()


class _Client:
    def __init__(self, session, name):
        self._session, self._name = session, name

    def put_secret_value(self, **kwargs):
        assert self._name == "secretsmanager"
        self._session.calls.append(("put_secret_value", kwargs))
        if self._session.fail:
            raise RuntimeError(f"AccessDenied for {kwargs['SecretString']}")
        return {"VersionId": "v-1"}

    def describe_stacks(self, **kwargs):
        assert self._name == "cloudformation"
        self._session.calls.append(("describe_stacks", kwargs))
        outputs = [{"OutputKey": "AirwallexSecretArn", "OutputValue": self._session.stack_arn}]
        return {"Stacks": [{"Outputs": outputs if self._session.stack_arn else []}]}


class _Session:
    """Stands in for boto3.Session and records every call."""

    def __init__(self, stack_arn=ARN, fail=False):
        self.calls, self.made, self.stack_arn, self.fail = [], [], stack_arn, fail

    def __call__(self, **kwargs):
        self.made.append(kwargs)
        return self

    def client(self, name):
        return _Client(self, name)


def _outputs(tmp_path, region="us-west-2", arn=ARN):
    path = tmp_path / "cdk-outputs.json"
    body = {"AwxAgentStack": {"AwxRegion": region}} if region else {}
    if arn:
        body["AwxSpine"] = {"AirwallexSecretArn": arn}
    path.write_text(json.dumps(body))
    return path


def _run(argv, *, answers=(CID, KEY), environ=None, session=None, terminal=True):
    session = session or _Session()
    replies = list(answers)

    def prompt(text):
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    code = seed.main(
        argv,
        environ=environ or {},
        prompt=prompt,
        has_terminal=lambda: terminal,
        session_factory=session,
    )
    return code, session


def _puts(session):
    return [kwargs for name, kwargs in session.calls if name == "put_secret_value"]


def test_the_prompted_values_are_uploaded_as_one_json_object(tmp_path, capsys):
    code, session = _run(["--profile", "awx-deploy", "--outputs", str(_outputs(tmp_path))])
    assert code == 0
    assert session.made == [{"profile_name": "awx-deploy", "region_name": "us-west-2"}]
    assert _puts(session) == [
        {"SecretId": ARN, "SecretString": json.dumps({"client_id": CID, "api_key": KEY})}
    ]
    out, err = capsys.readouterr()
    assert CID not in out + err
    assert KEY not in out + err


@pytest.mark.parametrize(
    "answers", [("", KEY), (CID, ""), (" ", "\t")], ids=["client-id", "api-key", "blank"]
)
def test_an_empty_value_is_refused_and_nothing_is_uploaded(tmp_path, capsys, answers):
    code, session = _run(["--outputs", str(_outputs(tmp_path))], answers=answers)
    assert code == 2
    assert _puts(session) == []
    assert "must not be empty; nothing was uploaded" in capsys.readouterr().err


@pytest.mark.parametrize(
    "answers",
    [(KeyboardInterrupt(),), (CID, KeyboardInterrupt()), (EOFError(),), (CID, EOFError())],
    ids=["ctrl-c-first", "ctrl-c-second", "eof-first", "eof-second"],
)
def test_ctrl_c_or_end_of_file_uploads_nothing_and_prints_no_traceback(tmp_path, capsys, answers):
    code, session = _run(["--outputs", str(_outputs(tmp_path))], answers=answers)
    assert code == 130
    assert _puts(session) == []
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err
    assert err.strip() == "cancelled; nothing was uploaded"
    assert CID not in out + err


def test_an_aws_error_is_one_line_with_no_value_in_it(tmp_path, capsys):
    session = _Session(fail=True)
    code, _ = _run(["--outputs", str(_outputs(tmp_path))], session=session)
    assert code == 1
    out, err = capsys.readouterr()
    assert len(err.strip().splitlines()) == 1
    assert "AccessDenied" in err
    assert CID not in out + err
    assert KEY not in out + err


def test_the_region_flag_wins_over_the_outputs_file(tmp_path):
    other = "arn:aws:secretsmanager:ap-southeast-2:111122223333:secret:awx-poc/airwallex-X"
    outputs = _outputs(tmp_path, region="us-west-2", arn=None)
    session = _Session(stack_arn=other)
    code, _ = _run(["--region", "ap-southeast-2", "--outputs", str(outputs)], session=session)
    assert code == 0
    assert session.made[0]["region_name"] == "ap-southeast-2"
    assert ("describe_stacks", {"StackName": "AwxSpine"}) in session.calls
    assert _puts(session)[0]["SecretId"] == other


def test_with_no_region_flag_the_outputs_file_gives_the_region(tmp_path):
    code, session = _run(["--outputs", str(_outputs(tmp_path, region="us-west-2"))])
    assert code == 0
    assert session.made[0]["region_name"] == "us-west-2"


@pytest.mark.parametrize("region_flag", [[], ["--region", ""], ["--region", "  "]])
def test_with_no_region_anywhere_the_script_refuses(tmp_path, capsys, region_flag):
    missing = tmp_path / "absent.json"
    code, session = _run([*region_flag, "--outputs", str(missing)])
    assert code == 2
    assert session.made == []
    assert "no Region: pass --region" in capsys.readouterr().err


def test_an_outputs_file_from_another_region_is_refused(tmp_path, capsys):
    code, session = _run(
        ["--region", "ap-southeast-2", "--outputs", str(_outputs(tmp_path, region="us-west-2"))]
    )
    assert code == 2
    assert _puts(session) == []
    assert "a deployment in us-west-2, not ap-southeast-2" in capsys.readouterr().err


def test_the_secret_id_flag_names_the_secret(tmp_path):
    code, session = _run(
        ["--secret-id", "my-secret", "--outputs", str(_outputs(tmp_path, region="us-west-2"))]
    )
    assert code == 0
    assert _puts(session)[0]["SecretId"] == "my-secret"
    assert [name for name, _ in session.calls] == ["put_secret_value"]


def test_from_env_reads_the_environment_and_prompts_for_nothing(tmp_path, capsys):
    environ = {"AWX_CLIENT_ID": CID, "AWX_API_KEY": KEY}
    code, session = _run(
        ["--from-env", "--outputs", str(_outputs(tmp_path))],
        answers=(),
        environ=environ,
        terminal=False,
    )
    assert code == 0
    assert _puts(session)[0]["SecretString"] == json.dumps({"client_id": CID, "api_key": KEY})
    out, err = capsys.readouterr()
    assert CID not in out + err


def test_from_env_refuses_a_missing_variable(tmp_path, capsys):
    code, session = _run(
        ["--from-env", "--outputs", str(_outputs(tmp_path))],
        answers=(),
        environ={"AWX_CLIENT_ID": CID},
    )
    assert code == 2
    assert _puts(session) == []
    err = capsys.readouterr().err
    assert "AWX_API_KEY must not be empty" in err
    assert CID not in err


def test_with_no_terminal_and_no_from_env_the_script_refuses(tmp_path, capsys):
    code, session = _run(["--outputs", str(_outputs(tmp_path))], answers=(), terminal=False)
    assert code == 2
    assert _puts(session) == []
    assert "pass --from-env" in capsys.readouterr().err


def test_the_script_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    outputs = _outputs(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    code, _ = _run(["--outputs", str(outputs)])
    assert code == 0
    assert sorted(tmp_path.rglob("*")) == before


# The documented commands, pasted into an interactive shell under a pseudo-terminal.

WRAPPER = """\
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("seed", {script!r})
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
calls = []
class Client:
    def __init__(self, name):
        self.name = name
    def put_secret_value(self, **kwargs):
        calls.append(kwargs)
        return {{"VersionId": "v-1"}}
class Session:
    def __init__(self, **kwargs):
        calls.append({{"session": kwargs}})
    def client(self, name):
        return Client(name)
seed.DEFAULT_OUTPUTS = seed.Path({outputs!r})
try:
    code = seed.main(sys.argv[2:], session_factory=Session)
finally:
    open({record!r}, "w").write(json.dumps({{"argv": sys.argv[1:], "calls": calls}}))
sys.exit(code)
"""


def _documented_line(from_env: bool) -> str:
    text = (REPO / "docs" / "deployment.md").read_text()
    lines = [
        line
        for block in re.findall(r"```\n(.*?)```", text, re.S)
        for line in block.splitlines()
        if "scripts/seed_airwallex_secret.py" in line
    ]
    (line,) = [line for line in lines if ("--from-env" in line) is from_env]
    return line


# pty.fork warns that pytest's process has threads. The child only changes directory and execs.
_FORK_WARNING = "ignore:This process .* is multi-threaded:DeprecationWarning"


class _Pty:
    def __init__(self, argv, env, cwd):
        import pty

        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)  # noqa: S606 - fixed argv, no shell string
        self.out = b""

    def expect(self, needle: bytes, timeout=15.0) -> bool:
        end = time.monotonic() + timeout
        while needle not in self.out and time.monotonic() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if ready:
                try:
                    self.out += os.read(self.fd, 65536)
                except OSError:
                    break
        return needle in self.out

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)
        os.close(self.fd)


def _paste(tmp_path, shell, bracketed, keys, from_env=False):
    record = tmp_path / "record.json"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        WRAPPER.format(script=str(SCRIPT), outputs=str(_outputs(tmp_path)), record=str(record))
    )
    # `uv run --all-packages python <script> <args>` runs the wrapper with the same arguments.
    setup = f'uv() {{ shift 3; "{sys.executable}" "{wrapper}" "$@"; echo "seed-""exit=$?"; }}'
    if shell == "bash":
        argv = ["bash", "--norc", "--noprofile", "-i"]
        mode = "on" if bracketed else "off"
        setup += f"; bind 'set enable-bracketed-paste {mode}'"
    else:
        argv = ["zsh", "-f", "-i"]
        if not bracketed:
            setup += "; unset zle_bracketed_paste"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS")}
    env.update(TERM="xterm", REGION="us-west-2", HOME=str(tmp_path))
    if from_env:
        env.update(AWX_CLIENT_ID=CID, AWX_API_KEY=KEY)
    term = _Pty(argv, env, REPO)
    try:
        term.send(setup.encode() + b'; echo "ready-""marker"\r')
        assert term.expect(b"ready-marker"), term.out.decode(errors="replace")
        line = _documented_line(from_env).encode()
        term.send(b"\x1b[200~" + line + b"\x1b[201~\r" if bracketed else line + b"\r")
        for prompt, key in keys:
            assert term.expect(prompt), term.out.decode(errors="replace")
            term.send(key)
        assert term.expect(b"seed-exit="), term.out.decode(errors="replace")
        term.expect(b"\n", timeout=1)
    finally:
        term.close()
    return json.loads(record.read_text()), term.out


SHELLS = [
    pytest.param(s, marks=pytest.mark.skipif(shutil.which(s) is None, reason=s))
    for s in ("bash", "zsh")
]
BRACKETED = pytest.mark.parametrize("bracketed", [True, False], ids=["bracketed", "plain"])


@pytest.mark.filterwarnings(_FORK_WARNING)
@pytest.mark.parametrize("shell", SHELLS)
@BRACKETED
def test_the_pasted_line_prompts_and_uploads_once(tmp_path, shell, bracketed):
    keys = [
        (b"Airwallex client ID: ", f"{CID}\r".encode()),
        (b"Airwallex API key: ", f"{KEY}\r".encode()),
    ]
    record, out = _paste(tmp_path, shell, bracketed, keys)
    assert record["argv"][1:] == ["--profile", "awx-deploy", "--region", "us-west-2"]
    assert record["calls"] == [
        {"session": {"profile_name": "awx-deploy", "region_name": "us-west-2"}},
        {"SecretId": ARN, "SecretString": json.dumps({"client_id": CID, "api_key": KEY})},
    ]
    assert CID.encode() not in out
    assert KEY.encode() not in out
    assert b"seed-exit=0" in out


@pytest.mark.filterwarnings(_FORK_WARNING)
@pytest.mark.parametrize("shell", SHELLS)
@BRACKETED
def test_ctrl_c_at_the_pasted_prompt_uploads_nothing(tmp_path, shell, bracketed):
    record, out = _paste(tmp_path, shell, bracketed, [(b"Airwallex client ID: ", b"\x03")])
    assert [c for c in record["calls"] if "SecretString" in c] == []
    assert b"Traceback" not in out
    assert b"cancelled; nothing was uploaded" in out


@pytest.mark.filterwarnings(_FORK_WARNING)
@pytest.mark.parametrize("shell", SHELLS)
@BRACKETED
def test_the_pasted_from_env_line_uploads_with_no_prompt(tmp_path, shell, bracketed):
    record, out = _paste(tmp_path, shell, bracketed, [], from_env=True)
    assert record["calls"][-1] == {
        "SecretId": ARN,
        "SecretString": json.dumps({"client_id": CID, "api_key": KEY}),
    }
    assert b"Airwallex client ID" not in out
    assert CID.encode() not in out
