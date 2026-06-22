# Paywall-Busting Research Agent — x402 over XRPL

**An AI research agent that autonomously pays paywalls with on-ledger
micropayments — and reaches far more full-text sources because it can.**

The agent runs the same research question two ways against a mock
research-data provider and shows the difference:

- **Run A — no wallet.** The agent can read only free abstracts. Every
  full-text fetch returns `402 Payment Required` and it falls back to the
  abstract.
- **Run B — x402 XRPL wallet.** The agent hits each paywall, pays a few drops
  of testnet XRP autonomously via the [x402](https://x402.org) protocol, reads
  the full text, and keeps a signed audit trail of every payment.

It then prints a before/after report — full-text sources reached, XRP spent,
added latency — and an LLM-synthesized answer from each run, so you can read how
much thinner the abstract-only answer is.

```
Full-text access expanded : 0 -> 1 sources (inf)
Cost of that expansion    : 0.001000 XRP, 13.0s of added latency
```

---

## Why this is interesting

Today an AI agent that hits a paywall just stops. Giving it a wallet and an
HTTP-native payment rail (`402 Payment Required` → pay → retry) lets it transact
for data with no human in the loop, settling in ~3–4 seconds at sub-cent cost on
the XRP Ledger. This is a working proof of that loop end-to-end: relevance
triage, autonomous payment with a hard spend cap, full-text retrieval, and
synthesis.

## What's real vs. simulated

| Part | Status |
|------|--------|
| XRPL testnet payments | **Real.** Actual on-ledger payments via `xrpl-py` + the t54 testnet facilitator. |
| x402 protocol flow | **Real.** Genuine HTTP 402 challenge → pay → retry, using `x402-xrpl`. |
| Agent relevance ranking + synthesis | **Real.** `claude-opus-4-8` (Anthropic) or `gpt-5.5` (OpenAI), selectable at runtime. |
| The papers and the provider | **Mock.** A small in-memory catalogue of crypto/blockchain research. |
| A data provider *choosing to accept x402* | **Simulated.** This is the adoption assumption the idea depends on — journals/APIs would need to adopt x402. The demo stands in for that. |

The domain is crypto/blockchain research on purpose: that's where x402 is
actually being adopted today (agent tooling, on-chain data APIs, paid MCP
endpoints), so it's the most plausible early adopter — not academic journals,
which remain aspirational for x402.

## How it works

```
                 ┌──────────────────────┐
                 │   research_agent.py   │
                 │  (LLM triage + pay)   │
                 └───────────┬───────────┘
       1. GET /papers (free) │  ▲  4. synthesize answer from full text
       2. rank relevance ────┘  │
                                │
       3. GET /papers/{id}/fulltext
                                │
                 ┌──────────────▼───────────┐
                 │     journal_server.py     │
                 │  402 Payment Required ────┼──► x402 challenge (amount, payee, network)
                 └──────────────┬───────────┘
                                │
                 agent's wallet pays on XRPL testnet
                 (spend-capped), retries with proof-of-payment,
                 server returns 200 + full text
```

Spend is capped two ways: the `x402-xrpl` client refuses to sign any payment
whose 402-declared amount exceeds `--max-spend`, and the agent pre-probes each
paper's live 402 price and skips anything that would breach the running budget.
Every payment (paid / failed / unknown) is appended to `payments_audit.jsonl`
for reconciliation against the ledger.

## Prerequisites

- **Python 3.12** (the deps are tested there). Run everything with `py -3.12`
  on Windows, or `python3.12` elsewhere.
- A funded XRPL **buyer** wallet (the agent pays from it) and a **payee** wallet
  (the provider receives to it), both on testnet.
- An **Anthropic** API key (for `--llm claude`) and/or an **OpenAI** key
  (for `--llm openai`).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in your keys and seeds
```

Fund both testnet wallets at the
[XRP Testnet Faucet](https://xrpl.org/xrp-testnet-faucet.html) — generate a
wallet there, drop its seed into `.env` (`BUYER_SEED` for the agent,
`XRPL_SEED` for the provider), and the faucet pre-funds it with test XRP.

## Run it

**Terminal 1 — start the provider:**

```bash
py -3.12 journal_server.py server
```

**Terminal 2 — run the agent:**

```bash
py -3.12 research_agent.py "How does the XRP Ledger stay safe under Byzantine validators?"
```

Useful flags:

- `--llm claude|openai` — pick the backend (omit to be prompted).
- `--max-spend 0.01` — cap total spend, in XRP (omit to be prompted).
- `--provider http://host:port` — point at a different provider host.
- Omit the question to use the built-in default.

## Sample output

Captured from a real run (`--llm openai --max-spend 0.01`) against the mock
provider. Note how the abstract-only answer can only say "sufficient overlap,"
while the paid full-text answer recovers the actual thresholds:

> **What's real here:** the wallet, the payment, and the XRPL testnet
> settlement. **What's not:** the "papers" and their findings are fictional
> demo content from the mock catalogue in `journal_server.py` — they are *not*
> real research and should not be cited. The point of the demo is the payment
> loop and the access gap it closes, not the paper contents.

```
Buyer wallet : rGeNNvUHDXiX6poC65G6KWMTRv5mi3BzHV
Testnet balance: 99.995960 XRP
Network      : xrpl:1
Spend limit  : 0.01 XRP (10000 drops)

Connecting to provider http://localhost:8000 ...
Catalogue has 7 papers (abstracts free).
Asking openai which papers are worth unlocking ...
Run A: reading abstracts only (no payment) ...
Run B: paying paywalls via x402/XRPL and reading full text ...
  [x402] network=xrpl:1  max_value=10000
  [x402] Paid 0.001000 XRP (1000 drops) for "The XRP Ledger Consensus Protocol: Safety and Liveness" — total spent: 0.001000 XRP

========================================================================
RESEARCH QUESTION
========================================================================
How does the XRP Ledger stay safe under Byzantine validators?

Agent judged 1 of the catalogue's papers relevant: xrpl-consensus

========================================================================
RUN A — agent WITHOUT payment ability
========================================================================
Full-text sources reached : 0/1
Paywalls hit (fell back to abstract): 1
XRP spent                 : 0

========================================================================
RUN B — agent WITH x402 XRPL wallet
========================================================================
Full-text sources reached : 1/1
Paywalls paid             : 1
XRP spent                 : 0.001000 (1000 drops)
Time to pay+fetch all     : 13.0s
Payment audit trail       : payments_audit.jsonl

========================================================================
IMPACT OF BEING ABLE TO PAY
========================================================================
Full-text access expanded : 0 -> 1 sources (inf)
Cost of that expansion    : 0.001000 XRP, 13.0s of added latency
```

**Answer — abstract-only agent (Run A)** *(synthesized from the fictional mock papers):*

> The XRP Ledger stays safe under Byzantine validators by using a **partially
> trusted validator model** in which each participant relies on a **Unique Node
> List (UNL)**. The key safety condition is that there must be sufficient
> **overlap between different participants' UNLs** [...] because the source is
> abstract-only, the exact overlap requirements, Byzantine fault thresholds, and
> proof mechanisms are not available here.

**Answer — paying agent, full text (Run B)** *(synthesized from the fictional mock papers):*

> [...] Validators proceed in **rounds**, iteratively converging by raising the
> required support threshold from **50% up to 80%**. Safety requires pairwise
> UNL overlap above roughly **90%**. With **≥90% UNL overlap** and **<20%
> Byzantine validators**, the protocol proves all honest nodes agree on the same
> ledger within a bounded number of rounds; below that, two quorums can ratify
> **conflicting ledgers** (a fork). Validator unavailability affects
> **liveness** rather than safety, degrading gracefully up to **20%** unavailable.

The paying agent recovers the concrete thresholds (≥90% overlap, <20% Byzantine,
50%→80% quorum escalation) that the abstract-only agent simply cannot see — for
0.001 XRP and ~13s.

## Files

- `research_agent.py` — the agent. Ranks relevance and synthesizes with the
  chosen LLM; pays paywalls with an `x402_requests` wallet session.
- `journal_server.py` — the mock provider. Abstracts free; each paper's full
  text gated behind its own per-resource x402 price.
- `.env.example` — template for the keys and seeds you need to supply.

## About this project

The design and architecture of this project are my own. The implementation was
written by Claude (Anthropic) based on my direction. I have tested the demo and
it works as described, but it is a **proof of concept and not production-ready**
— it has not been audited, hardened, or reviewed for security, and should not be
used to handle real funds or in a production setting.

*Built by Jonathan Schneider.*

## License

MIT — see [LICENSE](LICENSE).
