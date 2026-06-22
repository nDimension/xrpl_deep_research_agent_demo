"""Mock paywalled research-data provider, gated with x402 over XRPL.

Simulates the kind of provider most likely to actually adopt x402 today:
a crypto / blockchain research-data API. Abstracts are free; full text is
behind an x402 paywall, priced per paper. An AI agent that holds an XRPL
wallet can pay the 402 and unlock the full text autonomously.

What is REAL here: the XRPL testnet payments and the x402 protocol flow.
What is SIMULATED: a data provider choosing to accept x402 (the adoption
assumption your idea depends on).

Run the provider:

    py -3.12 journal_server.py server

Then drive it with the agent:

    py -3.12 research_agent.py "your research question"

Required .env values:

    XRPL_SEED=...
    BUYER_SEED=...
    BUYER_ADDRESS=...

Optional .env values:

    XRPL_PAY_TO=...
    XRPL_FACILITATOR_URL=https://xrpl-facilitator-testnet.t54.ai
    XRPL_SOURCE_TAG=20260601
    XRPL_TESTNET_RPC_URL=https://s.altnet.rippletest.net:51234/
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Iterable


DEFAULT_FACILITATOR_URL = "https://xrpl-facilitator-testnet.t54.ai"
DEFAULT_RPC_URL = "https://s.altnet.rippletest.net:51234/"
DEFAULT_SOURCE_TAG = "20260601"


# ---------------------------------------------------------------------------
# Mock catalogue. Each paper has a free abstract and gated full text. Prices
# vary per paper to demonstrate per-resource x402 pricing (in XRP drops;
# 1 XRP = 1_000_000 drops). Keep prices tiny — this is testnet XRP.
# ---------------------------------------------------------------------------
PAPERS = [
    {
        "id": "xrpl-consensus",
        "title": "The XRP Ledger Consensus Protocol: Safety and Liveness",
        "keywords": ["consensus", "xrpl", "byzantine", "validators", "safety", "liveness", "ripple"],
        "price_drops": 1000,
        "abstract": (
            "We analyze the XRP Ledger Consensus Protocol, a low-latency "
            "agreement protocol over a partially trusted set of validators. "
            "We give conditions on overlap between Unique Node Lists under "
            "which safety and liveness hold."
        ),
        "fulltext": (
            "FULL TEXT — XRPL Consensus.\n"
            "The protocol proceeds in rounds; each validator proposes a set of "
            "candidate transactions and iteratively converges by raising its "
            "support threshold from 50% to 80%. Safety requires pairwise UNL "
            "overlap above ~90%; below that, two quorums can ratify conflicting "
            "ledgers (a fork). Liveness degrades gracefully under validator "
            "unavailability up to 20%. We prove that with >=90% overlap and "
            "<20% Byzantine validators, all honest nodes agree on the same "
            "ledger within a bounded number of rounds."
        ),
    },
    {
        "id": "amm-clob",
        "title": "Hybrid AMM and Central Limit Order Books on XRPL",
        "keywords": ["amm", "order book", "clob", "liquidity", "defi", "dex", "trading", "xrpl"],
        "price_drops": 1500,
        "abstract": (
            "XRPL combines an automated market maker (AMM) with a native "
            "central limit order book. We study how arbitrage between the two "
            "venues tightens spreads and how the continuous auction mechanism "
            "mitigates impermanent loss for liquidity providers."
        ),
        "fulltext": (
            "FULL TEXT — Hybrid AMM/CLOB.\n"
            "Each AMM pool is auto-bridged into the order book: when the AMM "
            "price diverges from resting CLOB orders, the protocol synthesizes "
            "an offer that lets takers sweep both venues atomically. The "
            "continuous auction auctions off the LP fee discount each ledger, "
            "so the most aggressive arbitrageur captures the imbalance instead "
            "of LPs bearing it. Empirically this reduces LP impermanent loss by "
            "30-45% versus a standalone constant-product AMM at equal volume."
        ),
    },
    {
        "id": "micropayments-agents",
        "title": "Machine-to-Machine Micropayments for Autonomous AI Agents",
        "keywords": ["micropayments", "agents", "x402", "http 402", "stablecoin", "machine-to-machine", "autonomous", "payment"],
        "price_drops": 2000,
        "abstract": (
            "Autonomous agents increasingly need to pay for data and compute "
            "without a human in the loop. We survey HTTP 402-based payment "
            "rails (x402) settling on fast ledgers, and quantify how removing "
            "paywall friction expands the set of resources an agent can reach."
        ),
        "fulltext": (
            "FULL TEXT — M2M Micropayments.\n"
            "An agent issues a normal request; the server replies 402 with a "
            "payment-required header naming amount, asset, network, and payee. "
            "The agent's wallet middleware constructs and submits an on-ledger "
            "payment, then retries with a proof-of-payment header. On XRPL the "
            "round trip settles in 3-4 seconds at sub-cent cost, making "
            "per-request pricing viable. In our crawl of paywalled sources, an "
            "agent with a funded wallet reached 3.4x more full-text resources "
            "than an abstract-only agent, with median added latency of 4.1s and "
            "median spend of 0.0018 units per unlocked document."
        ),
    },
    {
        "id": "tokenization-rwa",
        "title": "On-Ledger Tokenization of Real-World Assets",
        "keywords": ["tokenization", "rwa", "real-world assets", "compliance", "issuance", "stablecoin", "custody"],
        "price_drops": 1200,
        "abstract": (
            "Tokenizing real-world assets requires reconciling on-ledger "
            "transferability with off-ledger legal control. We describe an "
            "issuance model using authorized trust lines and clawback for "
            "regulatory compliance."
        ),
        "fulltext": (
            "FULL TEXT — RWA Tokenization.\n"
            "Issuers gate holders behind authorized trust lines (no token "
            "without KYC approval) and retain a clawback flag to satisfy court "
            "orders or sanctions. Redemption is modeled as a burn paired with "
            "an off-ledger settlement attestation. We show the trust-line model "
            "supports cap tables of 10^5 holders with O(1) transfer cost and no "
            "global state contention, unlike account-based smart-contract "
            "tokens that serialize on a single balance map."
        ),
    },
    {
        "id": "hooks-smart",
        "title": "Hooks: Lightweight Smart Contracts on XRPL",
        "keywords": ["hooks", "smart contracts", "wasm", "webassembly", "programmability", "xrpl"],
        "price_drops": 1800,
        "abstract": (
            "Hooks are small WebAssembly modules attached to XRPL accounts that "
            "execute before or after transactions. We evaluate their "
            "expressiveness and the deterministic-execution guarantees needed "
            "for consensus."
        ),
        "fulltext": (
            "FULL TEXT — Hooks.\n"
            "Each hook is a WASM module with a bounded instruction budget so "
            "execution time is deterministic across validators. Hooks can "
            "reject a transaction, emit new transactions, or maintain a small "
            "key-value state. Because the instruction budget is metered and "
            "floating point is banned, every validator computes byte-identical "
            "results — a prerequisite for the hook's effects to be part of "
            "consensus rather than node-local."
        ),
    },
    {
        "id": "cbdc-privacy",
        "title": "Privacy-Preserving Retail CBDC Settlement",
        "keywords": ["cbdc", "central bank", "privacy", "settlement", "zero-knowledge", "digital currency"],
        "price_drops": 1600,
        "abstract": (
            "Retail central bank digital currencies must balance auditability "
            "with consumer privacy. We propose a settlement design using "
            "zero-knowledge commitments that reveals aggregate flows to the "
            "central bank while hiding individual transactions."
        ),
        "fulltext": (
            "FULL TEXT — CBDC Privacy.\n"
            "Balances are Pedersen commitments; transfers prove (in zero "
            "knowledge) that inputs equal outputs and that no balance goes "
            "negative, without revealing amounts. The central bank holds a "
            "viewing key that decrypts only aggregate per-bank netting, not "
            "individual payments. Throughput in our prototype is 9,000 "
            "settlements/second with 180ms proof verification batched per "
            "ledger."
        ),
    },
    {
        "id": "quantum-sigs",
        "title": "Post-Quantum Signature Schemes for Distributed Ledgers",
        "keywords": ["post-quantum", "signatures", "cryptography", "lattice", "security", "quantum"],
        "price_drops": 2200,
        "abstract": (
            "Distributed ledgers rely on signatures vulnerable to quantum "
            "attack. We benchmark lattice- and hash-based signature schemes "
            "for ledger use, focusing on verification cost and signature size."
        ),
        "fulltext": (
            "FULL TEXT — Post-Quantum Signatures.\n"
            "Hash-based schemes (SPHINCS+) give the most conservative security "
            "but 8-50KB signatures, bloating ledgers. Lattice schemes "
            "(Dilithium) offer ~2.5KB signatures and fast verification, at the "
            "cost of larger public keys. We recommend a hybrid: Dilithium for "
            "live transactions plus periodic hash-based checkpoints, bounding "
            "ledger growth while retaining a conservative fallback if lattice "
            "assumptions weaken."
        ),
    },
]

PAPERS_BY_ID = {p["id"]: p for p in PAPERS}


def load_local_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


def getenv_first(names: Iterable[str]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required .env value: {name}")
    return value


def wallet_address(wallet) -> str:
    return getattr(wallet, "classic_address", None) or wallet.address


def get_pay_to_address() -> str:
    explicit = os.getenv("XRPL_PAY_TO")
    if explicit:
        return explicit
    from xrpl.wallet import Wallet

    return wallet_address(Wallet.from_seed(require_env("XRPL_SEED")))


def create_app():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from x402_xrpl.server import require_payment

    app = FastAPI(title="Mock Paywalled Crypto Research Data Provider")

    pay_to = get_pay_to_address()
    facilitator_url = os.getenv("XRPL_FACILITATOR_URL", DEFAULT_FACILITATOR_URL)
    source_tag = int(os.getenv("XRPL_SOURCE_TAG", DEFAULT_SOURCE_TAG))

    # Gate each paper's full-text path behind its own x402 price. Each call to
    # require_payment installs one middleware that guards one path at one price,
    # repeated per paper.
    for paper in PAPERS:
        app.middleware("http")(
            require_payment(
                path=f"/papers/{paper['id']}/fulltext",
                price=str(paper["price_drops"]),
                pay_to_address=pay_to,
                facilitator_url=facilitator_url,
                network="xrpl:1",
                asset="XRP",
                description=f"Full text: {paper['title']}",
                extra={"sourceTag": source_tag},
            )
        )

    @app.get("/papers")
    async def list_papers():
        """Free: the catalogue with abstracts and the price to unlock each."""
        return {
            "papers": [
                {
                    "id": p["id"],
                    "title": p["title"],
                    "abstract": p["abstract"],
                    "price_drops": p["price_drops"],
                    "fulltext_url": f"/papers/{p['id']}/fulltext",
                }
                for p in PAPERS
            ]
        }

    @app.get("/papers/{paper_id}/abstract")
    async def get_abstract(paper_id: str):
        """Free: a single abstract."""
        paper = PAPERS_BY_ID.get(paper_id)
        if not paper:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"id": paper["id"], "title": paper["title"], "abstract": paper["abstract"]}

    @app.get("/papers/{paper_id}/fulltext")
    async def get_fulltext(paper_id: str):
        """Paid: reached only after the x402 middleware accepts payment."""
        paper = PAPERS_BY_ID.get(paper_id)
        if not paper:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"id": paper["id"], "title": paper["title"], "fulltext": paper["fulltext"]}

    @app.get("/health")
    async def health():
        return {"status": "ok", "papers": len(PAPERS)}

    print("Mock paywalled crypto research provider")
    print(f"Pay to      : {pay_to}")
    print(f"Source tag  : {source_tag}")
    print(f"Facilitator : {facilitator_url}")
    print(f"Papers      : {len(PAPERS)} (abstracts free, full text behind x402)")
    print("Catalogue   : http://localhost:8000/papers")
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mock paywalled research data provider (x402/XRPL).")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server", help="Run the FastAPI provider.")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> None:
    load_local_env()
    args = parse_args()
    if args.command == "server":
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
