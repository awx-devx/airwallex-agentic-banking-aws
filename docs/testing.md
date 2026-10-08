# Testing

## Test layers

| Layer | What it runs against | Marker | How to run |
|---|---|---|---|
| Offline | `FakeAirwallex`, in-memory stores and `moto`; no network | none | `make test` (also part of `make check`) |
| Synth | `cdk synth` of the real CDK app, then assertions on the templates, IAM and cdk-nag | `synth` | `make synth-test` |
| Contract | the real Airwallex sandbox: one happy and one negative path per endpoint family the kits use (`tests/contract`) | `sandbox` | `make sandbox-test` |
| Kit live sandbox | each kit's acceptance scenario through `local_runner` against the real sandbox | `sandbox` plus the kit's switch | see each kit README |
| Deployed checks | a deployed stack, read-only | none (a script) | `scripts/deployed_security_checks.py` |

The offline layer is the one every change must pass. Most kit tests run the real workflow
engine through `awx_spine_aws.local_runner` and the real tool dispatcher through
`awx_agent.testing.KitToolHarness`, so they exercise the code that runs in AWS with only the
transport replaced. `packages/awx_spine_aws/tests/test_asl_equivalence.py` drives the
synthesised state machine definition and checks it reaches the same outcome as
`local_runner`.

Synth tests need no AWS credentials. The root `conftest.py` gives them the placeholder account
`111122223333` and `alerts@example.com`, so no real account or mailbox appears in a template.

## Running tests

| Command | Runs |
|---|---|
| `make check` | `sync`, then `lint`, `secret-scan` and `test` |
| `make test` | `pytest -m "not sandbox and not synth and not live"` |
| `make synth-test` | `pytest -m synth` |
| `make sandbox-test` | `pytest -m sandbox` (calls the real sandbox) |
| `make lint` | `ruff check` and `ruff format --check` |
| `make hooks` | sets `core.hooksPath` to `.githooks`, so the secret scan runs on every commit |

To run one package or kit, pass its directory:

```sh
uv run --all-packages pytest kits/k1_treasury -m "not sandbox and not synth and not live"
```

The `sandbox` marker means a test calls the Airwallex sandbox. The `live` marker is reserved
for tests that call deployed AWS resources. `tests/contract/conftest.py` skips sandbox tests
unless the marker expression selects them or `AWX_SANDBOX=1` is set, so a plain `pytest
tests/contract` never reaches the sandbox.

Sandbox runs need sandbox credentials: `AWX_CLIENT_ID` and `AWX_API_KEY` in the environment,
or the file `~/.config/airwallex-sandbox/credentials.env`. Nothing prints them.

A kit's live variant runs only when its switch is set as well as the marker: `AWX_LIVE=1` for
kit 1, and `AWX_K2_LIVE=1`, `AWX_K3_LIVE=1` and `AWX_K6_LIVE=1` for kits 2, 3 and 6. Without
the switch the same scenario runs on `FakeAirwallex`. Kits 5, 7 and 8 commit no live wiring:
their sandbox scenarios always run on `FakeAirwallex`, and their READMEs describe one-off live
sandbox runs driven by code that is not part of this repository. With `AWX_K7_LIVE=1` the kit 7
live probes run and raise `NotImplementedError`; they are not implemented. Kit 4's live variant
is skipped, because the sandbox account does not have online payments enabled.

Every kit shares one sandbox account, so anything that touches the sandbox holds
`/tmp/awx-sandbox.lock` for its whole run. The kit acceptance tests take the lock themselves
with a blocking `fcntl.flock`, so a `make sandbox-test` wrapped in `flock` on the same file
waits for itself forever. Run `make sandbox-test` unwrapped, and only when no other sandbox
run is in progress, because the contract tests do not take the lock. Wrap a manual sandbox
step (such as a deposit simulation in a deploy runbook) in the lock:

```sh
flock /tmp/awx-sandbox.lock <command that touches the sandbox>
```

Every object a test creates in the sandbox carries a `poc-` label, and tests never assume the
account is empty or change an object they did not create.

## FakeAirwallex

`awx_spine.fake.FakeAirwallex` is an in-memory model of every Airwallex endpoint kits 1 to 8
use: balances, beneficiaries, transfers with their status machine (including `CANCELLED` with a
failure block), FX quotes and conversions, connected accounts, charges and connected account
transfers, issuing cardholders and cards with authorisation simulation and limits, payment
intents, disputes and file uploads. It runs on simulated time, so finality polls and approval
windows run instantly in tests.

Fault injection (`awx_spine.fake.Fault`) covers a timeout after submit, duplicate delivery, a
stale read, a 429, a 5xx, insufficient funds, a definitive refusal, a platform movement that
ends in another state, and a write whose objects stay invisible to list reads for a while.
Kit tests run their scenarios under each fault to prove rule 9 and M1 hold.

Every behaviour the fake models cites the sandbox evidence behind it in
[packages/awx_spine/FAKE-FIDELITY.md](../packages/awx_spine/FAKE-FIDELITY.md). Where the
sandbox and the fake differ, that file and the kit README say so.

## Property tests

Every kit's allocators and money rules have [Hypothesis](https://hypothesis.readthedocs.io/)
property tests. Examples: any treasury plan keeps every wallet it touches at or above its
floor; a card limit is inclusive and cumulative limits count prior spend; a transfer still in
`SENT` never yields a replacement; the reserve allocator never commits more than its headroom;
no employer is ever funded from another employer's balance; a loan collection never exceeds
the amount due or the wallet. Money is `Decimal` throughout, and the transport tests check
that no float reaches a money path.
