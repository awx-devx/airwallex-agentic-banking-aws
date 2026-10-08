# Kits

One package per kit from the
[Airwallex Agentic Banking Hackathon builder guide](https://airwallexdev.com/guide). Each is a
uv workspace member of pure domain code (policy, operation builders, finality, ledgers, tools,
operator commands) with its own tests. The spine runs it.
[docs/adding-a-kit.md](../docs/adding-a-kit.md) explains what a kit exports.

| Kit | Package | What it shows |
|---|---|---|
| 1 | [k1_treasury](k1_treasury/README.md) | Adaptive Treasury Controller: a fee-inclusive FX plan checked against the reserve floor across the whole plan, with rate locks and quote expiry |
| 2 | [k2_purchase](k2_purchase/README.md) | Intent-Bound Purchase Agent: card controls bound to a purchase decision, with per-transaction and cumulative limits |
| 3 | [k3_incident](k3_incident/README.md) | Payment Ops Incident Commander: replacing a failed transfer only once the provider reports it failed, behind a duplicate lock |
| 4 | [k4_dispute](k4_dispute/README.md) | Dispute Response Agent: dispute economics and deadlines, an evidence pack, and a prompt injection inside the evidence |
| 5 | [k5_platform_spend](k5_platform_spend/README.md) | Platform Spend Controller: a platform capital ledger and reserve allocator for connected accounts |
| 6 | [k6_payroll](k6_payroll/README.md) | Multi-Employer Payroll Executor: per-employer funding and one child run per employer |
| 7 | [k7_lending](k7_lending/README.md) | Portfolio Lending Agent: a loan subledger, advances and weekly collection |
| 8 | [k8_settlement](k8_settlement/README.md) | Marketplace Settlement Agent: a seller reserve ledger, the cycle identity check, receivables and recovery |

Each kit has a reference architecture diagram in
[docs/architecture/](../docs/architecture/README.md) and, for kits with deploy state, a deploy
file in [deploy/](../deploy).
