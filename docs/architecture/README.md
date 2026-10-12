# Reference architecture diagrams

Each diagram is drawn from the code and the synthesised CloudFormation templates, not from a
design plan. `generate.py` writes every `.drawio` file; draw.io renders each `.png` from its
`.drawio`.

| Diagram | Shows |
|---|---|
| `stacks` | The AWS Cloud Development Kit (CDK) stacks `AwxSpine`, `AwxKitK1` to `AwxKitK8` and `AwxAgentStack`, what each holds, and the deploy order (spine, then kit stacks, then the agent stack) |
| `spine` | The shared spine end to end: sign-in, the agent and its tools, reads, proposals, approval, the mutation adapter and state |
| `kit-workflow` | The generic kit state machine from `infra/constructs/kit_workflow.py`, including approval, the per-step send and finality loop, and kit 6's fan-out |
| `k1_treasury` .. `k8_settlement` | One reference architecture per kit: its tools, state machines, the Airwallex endpoints it writes, who approves, tenancy, the model and code split, and its acceptance test |
| `overview/all-kits` | Kits 1 to 8 on the shared spine, grouped by tenancy, for the top of the root README |
| `overview/k1_treasury` .. `overview/k8_settlement` | One overview per kit for the top of its README: who asks, the kit's tools and actions, who approves and which actions can run AUTO, and what the kit writes to Airwallex |

## Reading a diagram

The lanes run from the front door (a person with the `awxpoc` CLI, the Amazon Cognito user pool
and the approval API) through the agent boundary (read and propose only) and deterministic
control (Step Functions and the kit-step Lambdas) to execution and state (the mutation adapter,
Amazon DynamoDB, AWS Secrets Manager and Airwallex). Each edge carries a step number; the notes
panel on the right lists the steps in order. Red edges are the approval path, orange edges are
the write path, and dashed edges are secondary paths: secret and evidence reads, reads during policy, and
operator actions.

The deployment has no Amazon EventBridge schedules. An operator starts the schedule-driven
actions (kit 6 payroll, kit 7 collection and reconciliation, kit 8 settlement and
reconciliation) with `awx-deploy trigger`.

## Regenerating

```
python3.12 docs/architecture/generate.py --drawio /path/to/drawio
```

Without `--drawio` the script writes only the `.drawio` files. `--only spine k6_payroll` limits
the run to the named diagrams. Name an overview with its folder, for example
`--only overview/all-kits overview/k4_dispute`. When no display is set, the script runs draw.io
under `xvfb-run`. Generation fails if an edge crosses a node, two edges overlap, or a node label
is longer than three lines.

The overviews in `overview/` use larger type, no notes panel and no step numbers: each edge
carries a short label instead, and the lane titles say what each lane does. Each overview edge
follows an explicit route. Generation also fails if an overview edge label leaves its lane, an
edge runs through a lane title, or edges run side by side closer than 18 pixels.

The PNGs in this directory were rendered with draw.io desktop 31.7.0 for Linux arm64 (the
`drawio-arm64-31.7.0.AppImage` release asset, SHA-256 checked against the release's
`Files-SHA256-Hashes.txt`, extracted with `--appimage-extract`), at scale 2 with a 20 pixel
border:

```
xvfb-run -a drawio --no-sandbox --disable-gpu -x -f png -s 2 -b 20 -o out.png in.drawio
```

`optimise_pngs.py` then converts each PNG here and in `overview/` (or only the paths it is given)
to a 256-colour palette in place, which keeps them at about 500 KB each with no visible change
(the diagrams use flat colours). `make diagrams DRAWIO=/path/to/drawio` runs the generator and
the optimiser.

## Icons

The service icons are the AWS Architecture Icons that ship with draw.io (the `mxgraph.aws4`
shape library). Amazon Bedrock AgentCore Runtime and AgentCore Gateway, and the Airwallex API,
are drawn as labelled boxes.
