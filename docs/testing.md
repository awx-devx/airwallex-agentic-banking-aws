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
| `make sandbox-test` | `pytest -m sandbox -rs` (calls the real sandbox, and lists each skipped test with its reason) |
| `make lint` | `ruff check` and `ruff format --check` |
| `make hooks` | sets `core.hooksPath` to `.githooks`, so the secret scan runs on every commit |

`make check` and `make synth-test` take about 4 to 5 minutes each. pytest prints only dots
while it runs, so a quiet terminal does not mean a hung run.

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

`make sandbox-test` passes `-rs`, so pytest lists every skipped test with its reason. A
sandbox run that skipped contract tests still exits 0, and ends with a line that starts
`CONTRACT SUITE SKIPPED` (every contract test skipped) or `CONTRACT SUITE PARTLY SKIPPED`.
Without the fixture ids below, a green `make sandbox-test` reaches the sandbox from no
contract test: its passes are kit acceptance tests on `FakeAirwallex`.

### Contract fixtures

The contract suite (`tests/contract/test_sandbox_contract.py`) uses long-lived objects in your
sandbox account, named by environment variables. The whole module skips, naming the missing
variables, until all of them are set:

| Variable | Sandbox object | Finding its id |
|---|---|---|
| `AWX_CONTRACT_ACCT_A` | an ACTIVE connected account (`acct_...`) | `GET /api/v1/accounts`, or the connected accounts list in the sandbox web app |
| `AWX_CONTRACT_ACCT_B` | a second ACTIVE connected account; the tests move 1.00 USD to it from the platform and charge it back | as above |
| `AWX_CONTRACT_BENEFICIARY` | a platform USD LOCAL beneficiary, which the transfer tests pay 1.00 USD at a time | `GET /api/v1/beneficiaries`, its `id` |
| `AWX_CONTRACT_CARDHOLDER` | an issuing cardholder | `GET /api/v1/issuing/cardholders`, its `cardholder_id` |
| `AWX_CONTRACT_CARD` | an active card on that cardholder; each run sets its per-transaction limit to 0.01 USD | `GET /api/v1/issuing/cards`, its `card_id` |

The tests also spend small USD amounts from the platform balance. The repository has no
helper that creates these objects. Make them once in the sandbox web app, or with the API: the
connected account and beneficiary calls are in "Sandbox objects to create first" in
[deployment.md](deployment.md), and a cardholder and card come from
`POST /api/v1/issuing/cardholders/create` and `POST /api/v1/issuing/cards/create`. Give each a
`poc-` label, then export the ids before the run:

```sh
export AWX_CONTRACT_ACCT_A=acct_... AWX_CONTRACT_ACCT_B=acct_...
export AWX_CONTRACT_BENEFICIARY=<beneficiary id> AWX_CONTRACT_CARDHOLDER=<cardholder id>
export AWX_CONTRACT_CARD=<card id>
make sandbox-test
```

### Kit live switches

A kit's live variant runs only when its own switch is set as well as the `sandbox` marker.
The contract fixture ids do not switch any of them on.

| Kit | Switch | Without it |
|---|---|---|
| 1 | `AWX_LIVE=1` | the scenario runs on `FakeAirwallex` |
| 2 | `AWX_K2_LIVE=1` | the scenario runs on `FakeAirwallex` |
| 3 | `AWX_K3_LIVE=1` | the live test is skipped |
| 6 | `AWX_K6_LIVE=1`, plus `AWX_K6_ACCOUNTS` naming the owner-only connected-accounts file | the scenario runs on `FakeAirwallex` |

Kits 5, 7 and 8 commit no live wiring: their sandbox scenarios always run on `FakeAirwallex`,
and their READMEs describe one-off live sandbox runs driven by code that is not part of this
repository. With `AWX_K5_LIVE` set the kit 5 live test skips. With `AWX_K7_LIVE=1` the kit 7
live probes run and raise `NotImplementedError`; they are not implemented. Kit 4's live
variant is skipped, because the sandbox account does not have online payments enabled. Each
kit README gives its exact command.

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
