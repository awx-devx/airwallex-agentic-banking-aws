# Cross-cutting tests

Each package and kit keeps its own tests next to its code. This directory holds the suites
that span them.

| Directory | Holds | Markers |
|---|---|---|
| `contract/` | contract tests against the real Airwallex sandbox, one happy and one negative path per endpoint family the kits use, plus offline checks that pin shared spine constants (attempt lease, deduplication margin, the sandbox driver's home module), a crash inside the attempt lease end to end, and every kit's operator commands | `sandbox` for the sandbox tests; the rest run offline |
| `security/` | synth-time assertions over the CDK templates (secret access, adapter invoke rights, KMS encryption, no wildcard actions, no wildcard resources on write paths, the state machine layout, cdk-nag) and the cross-kit tenant tests for kits 5 to 8 | `synth` for the template tests; the rest run offline |

The sandbox tests are skipped unless the marker expression selects them or `AWX_SANDBOX=1` is
set. [docs/testing.md](../docs/testing.md) explains every test layer and how to run it.
