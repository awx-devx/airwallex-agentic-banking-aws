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
from botocore.exceptions import ClientError

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
            raise self._session.fail(kwargs["SecretString"])
        return {"VersionId": "v-1"}

    def describe_stacks(self, **kwargs):
        assert self._name == "cloudformation"
        self._session.calls.append(("describe_stacks", kwargs))
        if self._session.no_stack:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": "Stack with id AwxSpine does not exist",
                    }
                },
                "DescribeStacks",
            )
        outputs = [{"OutputKey": "AirwallexSecretArn", "OutputValue": self._session.stack_arn}]
        return {"Stacks": [{"Outputs": outputs if self._session.stack_arn else []}]}


class _Session:
    """Stands in for boto3.Session and records every call."""

    def __init__(self, stack_arn=ARN, fail=None, no_stack=False):
        self.calls, self.made, self.stack_arn, self.fail = [], [], stack_arn, fail
        self.no_stack = no_stack

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


def _echo_runtime_error(secret_string):
    return RuntimeError(f"AccessDenied for {secret_string}")


def _echo_client_error(secret_string):
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"denied {secret_string}"}},
        "PutSecretValue",
    )


@pytest.mark.parametrize(
    ("cid", "key"),
    [
        (CID, KEY),
        # The shorter value inside the longer one.
        ("abc", "XYZabcQRS"),
        # Values that JSON or repr escape when an error echoes them.
        ('cid"q', "key\\back"),
        ("cid-plain", "k\u00e9y-zz"),
        ("c", "k"),
    ],
    ids=["plain", "overlap", "json-escaped", "non-ascii", "one-character"],
)
@pytest.mark.parametrize(
    ("fail", "shown"),
    [
        (_echo_runtime_error, "RuntimeError"),
        (_echo_client_error, "ClientError (AccessDeniedException)"),
    ],
    ids=["runtime-error", "client-error"],
)
def test_an_aws_error_is_one_line_with_no_value_in_it(tmp_path, capsys, cid, key, fail, shown):
    session = _Session(fail=fail)
    code, _ = _run(["--outputs", str(_outputs(tmp_path))], answers=(cid, key), session=session)
    assert code == 1
    out, err = capsys.readouterr()
    assert err.strip() == f"seed_airwallex_secret: {shown}"
    assert out == ""


def test_ctrl_c_during_the_upload_says_it_may_have_completed(tmp_path, capsys):
    session = _Session(fail=lambda _: KeyboardInterrupt())
    code, _ = _run(["--outputs", str(_outputs(tmp_path))], session=session)
    assert code == 130
    assert capsys.readouterr().err.strip() == "cancelled; the upload may have completed"


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


def test_an_outputs_file_from_another_region_falls_back_to_the_stack(tmp_path, capsys):
    other = "arn:aws:secretsmanager:ap-southeast-2:111122223333:secret:awx-poc/airwallex-X"
    session = _Session(stack_arn=other)
    outputs = _outputs(tmp_path, region="us-west-2")
    code, _ = _run(["--region", "ap-southeast-2", "--outputs", str(outputs)], session=session)
    assert code == 0
    assert session.made[0]["region_name"] == "ap-southeast-2"
    assert ("describe_stacks", {"StackName": "AwxSpine"}) in session.calls
    assert [put["SecretId"] for put in _puts(session)] == [other]
    err = capsys.readouterr().err
    assert f"{outputs} belongs to a deployment in us-west-2" in err
    assert "looking up the AwxSpine stack in ap-southeast-2" in err


def test_with_no_spine_stack_in_the_region_the_script_refuses(tmp_path, capsys):
    session = _Session(no_stack=True)
    outputs = _outputs(tmp_path, region="us-west-2")
    code, _ = _run(["--region", "ap-southeast-2", "--outputs", str(outputs)], session=session)
    assert code == 2
    assert _puts(session) == []
    err = capsys.readouterr().err
    assert "no AwxSpine stack in ap-southeast-2: pass --region with the Region" in err
    assert "or seed after the spine stack has deployed" in err
    assert "Traceback" not in err


def test_a_spine_stack_with_no_secret_output_is_refused(tmp_path, capsys):
    session = _Session(stack_arn="")
    outputs = _outputs(tmp_path, region="us-west-2", arn=None)
    code, _ = _run(["--outputs", str(outputs)], session=session)
    assert code == 2
    assert _puts(session) == []
    err = capsys.readouterr().err
    assert "stack AwxSpine in us-west-2 has no AirwallexSecretArn output; wait for" in err
    assert "--secret-id <ARN>" in err


def test_an_outputs_arn_with_no_region_is_refused(tmp_path, capsys):
    outputs = _outputs(tmp_path, region="us-west-2", arn="awx-poc/airwallex")
    code, session = _run(["--outputs", str(outputs)])
    assert code == 2
    assert _puts(session) == []
    err = capsys.readouterr().err
    assert "holds a secret ARN with no Region in it; pass --secret-id <ARN>" in err
    assert "--region" not in err


@pytest.mark.parametrize(
    ("secret_id", "message"),
    [
        (
            "arn:aws:secretsmanager:ap-southeast-2:111122223333:secret:awx-poc/airwallex",
            "--secret-id names the secret of a deployment in ap-southeast-2, not us-west-2; "
            "pass --region ap-southeast-2 to seed that deployment, or --secret-id <ARN> to "
            "seed the us-west-2 one",
        ),
        ("arn:aws:secretsmanager", "--secret-id holds a secret ARN with no Region in it"),
    ],
    ids=["other-region", "no-region"],
)
def test_a_secret_id_arn_outside_the_region_is_refused(tmp_path, capsys, secret_id, message):
    code, session = _run(["--secret-id", secret_id, "--outputs", str(_outputs(tmp_path))])
    assert code == 2
    assert _puts(session) == []
    assert message in capsys.readouterr().err


def test_a_secret_id_arn_in_the_region_is_seeded(tmp_path):
    code, session = _run(["--secret-id", ARN, "--outputs", str(_outputs(tmp_path))])
    assert code == 0
    assert [put["SecretId"] for put in _puts(session)] == [ARN]


def test_an_outputs_file_that_is_not_json_names_a_next_step(tmp_path, capsys):
    path = tmp_path / "cdk-outputs.json"
    path.write_text("{")
    code, session = _run(["--outputs", str(path)])
    assert code == 2
    assert session.made == []
    assert "fix or delete it, or pass --outputs with another file" in capsys.readouterr().err


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
    assert [put["SecretString"] for put in _puts(session)] == [
        json.dumps({"client_id": CID, "api_key": KEY})
    ]
    assert environ == {}
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
    assert (
        "no terminal to prompt on; set AWX_CLIENT_ID and AWX_API_KEY and pass --from-env"
        in capsys.readouterr().err
    )


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
            # A shell started with SIGINT ignored keeps it ignored, so Ctrl-C would never
            # reach the script. make, nohup and some CI runners start processes that way.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
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
    assert [c for c in record["calls"] if "SecretString" in c] == [
        {"SecretId": ARN, "SecretString": json.dumps({"client_id": CID, "api_key": KEY})}
    ]
    assert b"Airwallex client ID" not in out
    assert CID.encode() not in out
