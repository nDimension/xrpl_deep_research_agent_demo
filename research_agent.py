"""A research agent that pays paywalls with x402 over XRPL.

Demonstrates the core thesis: an AI agent that can make quick on-ledger
micropayments reaches far more full-text sources than one limited to free
abstracts, with little friction.

The agent runs the SAME research question two ways against the mock provider
(journal_server.py):

  Run A  — no wallet. It can read only free abstracts.
  Run B  — x402 wallet. It hits each paywall, pays autonomously via XRPL, and
           reads full text.

It then prints a before/after comparison (sources reachable, XRP spent,
latency) and an AI-synthesized answer from whatever it could access.

LLM backend is selectable via --llm (default: openai):

  --llm claude   Uses claude-opus-4-8 via the Anthropic SDK (ANTHROPIC_API_KEY)
  --llm openai   Uses gpt-5.5 via the OpenAI SDK (OPENAI_API_KEY)

Start the provider first (in another terminal):

    py -3.12 journal_server.py server

Then:

    py -3.12 research_agent.py "How does XRPL stay safe under Byzantine validators?"
    py -3.12 research_agent.py --llm openai "How does XRPL stay safe under Byzantine validators?"

Required .env values:

    ANTHROPIC_API_KEY=...    (for --llm claude)
    OPENAI_API_KEY=...       (for --llm openai)
    BUYER_SEED=...
    BUYER_ADDRESS=...        (optional cross-check)
    XRPL_SEED=...            (used by the provider; not needed by the agent)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import os

import requests

# Reuse the provider's .env loader and defaults so both sides agree.
from journal_server import load_local_env, getenv_first, DEFAULT_RPC_URL

CLAUDE_MODEL = "claude-opus-4-8"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
DEFAULT_PROVIDER = "http://localhost:8000"
DEFAULT_QUESTION = "How does the XRP Ledger remain safe and live under Byzantine validators?"
AUDIT_FILE = Path("payments_audit.jsonl")

# Testnet network identifier (CAIP-2 format, confirmed from server source and x402 docs).
XRPL_TESTNET_NETWORK = "xrpl:1"


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------
def get_llm_client(llm: str):
    """Return an opaque client object for the chosen backend."""
    if llm == "claude":
        try:
            import anthropic
        except ImportError:
            sys.exit(
                "ERROR: the `anthropic` package is not installed for this interpreter.\n"
                "Install it with: pip install anthropic"
            )
        api_key = getenv_first(("ANTHROPIC_API_KEY",))
        if not api_key:
            sys.exit("ERROR: ANTHROPIC_API_KEY is not set (put it in .env or the environment).")
        return ("claude", anthropic.Anthropic(api_key=api_key))

    if llm == "openai":
        try:
            import openai
        except ImportError:
            sys.exit(
                "ERROR: the `openai` package is not installed for this interpreter.\n"
                "Install it with: pip install openai"
            )
        api_key = getenv_first(("OPENAI_API_KEY",))
        if not api_key:
            sys.exit("ERROR: OPENAI_API_KEY is not set (put it in .env or the environment).")
        return ("openai", openai.OpenAI(api_key=api_key))

    sys.exit(f"ERROR: unknown --llm value '{llm}'. Choose 'claude' or 'openai'.")


def llm_text(client_pair, system: str, user: str, max_tokens: int = 4000) -> str:
    """One LLM call, normalised across backends."""
    backend, client = client_pair
    if backend == "claude":
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            # thinking requires type="enabled" with a budget_tokens ceiling.
            # It is incompatible with output_config json_schema, so only used here
            # for free-form synthesis (not rank_relevance which uses structured output).
            thinking={"type": "enabled", "budget_tokens": min(max_tokens // 2, 2000)},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    # openai
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        max_completion_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Provider access
# ---------------------------------------------------------------------------
def fetch_catalogue(provider: str) -> list[dict]:
    r = requests.get(f"{provider}/papers", timeout=30)
    r.raise_for_status()
    return r.json()["papers"]


def rank_relevance(client_pair, question: str, catalogue: list[dict]) -> list[str]:
    """Ask the LLM which papers are relevant, from titles + abstracts only.

    This is the decision an agent makes BEFORE paying: it sees the free
    abstracts and picks what's worth unlocking. Returns paper ids, most
    relevant first.
    """
    listing = "\n".join(
        f"- id={p['id']} | {p['title']}\n  abstract: {p['abstract']}" for p in catalogue
    )
    system = (
        "You are a research agent triaging a paywalled catalogue. Given a "
        "question and the free abstracts, return the ids of papers whose "
        "FULL TEXT is worth paying to unlock, most relevant first. Include "
        "only genuinely relevant papers. Respond with ONLY a JSON object of "
        "the form {\"relevant_ids\": [\"id1\", \"id2\", ...]} and nothing else."
    )
    user = f"Question: {question}\n\nCatalogue:\n{listing}"

    backend, client = client_pair
    if backend == "claude":
        schema = {
            "type": "object",
            "properties": {
                "relevant_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["relevant_ids"],
            "additionalProperties": False,
        }
        # output_config json_schema is incompatible with thinking — omit thinking here.
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next(b.text for b in resp.content if b.type == "text")
    else:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_completion_tokens=1000,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = resp.choices[0].message.content

    ids = json.loads(text)["relevant_ids"]
    valid = {p["id"] for p in catalogue}
    return [pid for pid in ids if pid in valid]


def make_x402_session(max_drops: int | None = None):
    """A requests-style session that auto-pays x402 challenges via XRPL.

    Passes network_filter="xrpl:1" to pin to testnet and max_value=max_drops
    so the library refuses any 402 whose declared amount exceeds the cap
    before a payment is ever signed.
    """
    from xrpl.wallet import Wallet
    from x402_xrpl.clients import x402_requests

    seed = getenv_first(("BUYER_SEED", "XRPL_BUYER_SEED"))
    if not seed:
        sys.exit("ERROR: BUYER_SEED (or XRPL_BUYER_SEED) is not set in .env.")
    wallet = Wallet.from_seed(seed)

    expected = os.getenv("BUYER_ADDRESS")
    actual = getattr(wallet, "classic_address", None) or wallet.address
    if expected and actual != expected:
        sys.exit(f"ERROR: BUYER_SEED resolves to {actual}, but BUYER_ADDRESS is {expected}.")

    rpc_url = os.getenv("XRPL_TESTNET_RPC_URL", DEFAULT_RPC_URL)

    # max_value is the native per-payment guard in the x402_requests selector:
    # it filters out any 402 "accepts" entry whose amount > max_value, causing
    # the session to return the original 402 (no payment attempted) rather than
    # overspend. Pass as a string of drops to match the wire-format "amount" field.
    max_value_str = str(max_drops) if max_drops is not None else None

    try:
        session = x402_requests(
            wallet,
            rpc_url=rpc_url,
            network_filter=XRPL_TESTNET_NETWORK,
            scheme_filter="exact",
            max_value=max_value_str,
        )
    except TypeError:
        session = x402_requests(wallet, rpc_url=rpc_url)

    print(f"  [x402] network={XRPL_TESTNET_NETWORK}  max_value={max_value_str or 'unlimited'}")
    return session


# ---------------------------------------------------------------------------
# Payment audit trail
# ---------------------------------------------------------------------------
def _append_audit(record: dict) -> None:
    with AUDIT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# The two runs
# ---------------------------------------------------------------------------
def run_without_payment(provider: str, catalogue: list[dict], relevant_ids: list[str]) -> dict:
    """Agent has no wallet: it can read abstracts but every full-text fetch 402s."""
    by_id = {p["id"]: p for p in catalogue}
    sources, blocked = [], []
    for pid in relevant_ids:
        paper = by_id[pid]
        # Try the paywalled endpoint with a plain client — expect 402.
        try:
            r = requests.get(f"{provider}{paper['fulltext_url']}", timeout=15)
        except requests.RequestException as e:
            blocked.append({"id": pid, "title": paper["title"], "reason": str(e)})
            continue
        if r.status_code == 200:
            # Shouldn't happen for a gated path, but handle it.
            sources.append({"id": pid, "title": paper["title"],
                            "content": r.json().get("fulltext", ""), "kind": "fulltext"})
        else:
            # Paywalled. Fall back to the free abstract.
            sources.append({"id": pid, "title": paper["title"],
                            "content": paper["abstract"], "kind": "abstract"})
            blocked.append({"id": pid, "title": paper["title"], "status": r.status_code})
    return {"sources": sources, "blocked": blocked, "drops_spent": 0, "elapsed_s": 0.0}


def _probe_402_price(provider: str, fulltext_url: str) -> int | None:
    """Do a plain GET to read the price declared in the live 402 challenge.

    Returns the integer drops from accepts[0].amount, or None if unreachable.
    The 402 body shape is: {"accepts": [{"amount": "<drops>", ...}, ...]}
    """
    try:
        r = requests.get(f"{provider}{fulltext_url}", timeout=15)
        if r.status_code != 402:
            return None
        body = r.json()
        accepts = body.get("accepts") or []
        if accepts and "amount" in accepts[0]:
            return int(accepts[0]["amount"])
    except Exception:
        pass
    return None


def run_with_payment(provider: str, catalogue: list[dict], relevant_ids: list[str],
                     max_drops: int | None = None) -> dict:
    """Agent has an x402 wallet: it pays each paywall and reads full text.

    Spend-cap enforcement has two layers:
    1. max_value passed to x402_requests: the library refuses to sign any
       payment whose 402-declared amount exceeds this limit.
    2. Pre-flight probe: before each request, fetch the live 402 price and
       check drops_spent + actual_price > max_drops. Skip if it would breach.

    drops_spent is incremented by the price the 402 actually declared
    (from the live probe), not by the catalogue's advertised price. A warning
    is printed if the two differ. The receipt carries no settled-amount field —
    only the tx hash — so the 402-declared amount is the best on-wire figure
    available short of querying the ledger.
    """
    from x402_xrpl.clients import decode_payment_response

    session = make_x402_session(max_drops=max_drops)
    by_id = {p["id"]: p for p in catalogue}
    sources, payments, blocked = [], [], []
    drops_spent = 0
    t0 = time.time()

    for pid in relevant_ids:
        paper = by_id[pid]
        catalogue_price = paper["price_drops"]

        # --- pre-flight: read the price the 402 actually declares ---
        actual_price = _probe_402_price(provider, paper["fulltext_url"])
        if actual_price is None:
            # Can't determine price; fall back to catalogue price with a warning.
            actual_price = catalogue_price
            print(f"  [warn] Could not probe live 402 price for '{paper['title']}'; "
                  f"using catalogue price ({catalogue_price} drops).")
        elif actual_price != catalogue_price:
            print(f"  [warn] Live 402 price for '{paper['title']}' is {actual_price} drops "
                  f"but catalogue advertises {catalogue_price} drops.")

        # --- budget gate on the live price ---
        if max_drops is not None and drops_spent + actual_price > max_drops:
            print(f"  [budget] skipping '{paper['title']}' "
                  f"({actual_price} drops would exceed limit of {max_drops} drops "
                  f"/ {drops_to_xrp(max_drops):.6f} XRP)")
            blocked.append({"id": pid, "title": paper["title"], "reason": "budget limit"})
            continue

        tx_hash = None
        try:
            r = session.get(f"{provider}{paper['fulltext_url']}", timeout=180)
        except Exception as e:  # noqa: BLE001
            # Request was sent but outcome is uncertain — record as unknown so it
            # can be reconciled against the ledger rather than silently ignored.
            _append_audit({
                "id": pid,
                "title": paper["title"],
                "drops_paid": actual_price,
                "tx_hash": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "unknown",
                "error": str(e),
            })
            blocked.append({"id": pid, "title": paper["title"], "reason": str(e)})
            continue

        if r.status_code != 200:
            _append_audit({
                "id": pid,
                "title": paper["title"],
                "drops_paid": 0,
                "tx_hash": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "failed",
                "http_status": r.status_code,
            })
            blocked.append({"id": pid, "title": paper["title"], "status": r.status_code})
            continue

        # --- successful payment ---
        drops_spent += actual_price
        print(f"  [x402] Paid {drops_to_xrp(actual_price):.6f} XRP "
              f"({actual_price} drops) for \"{paper['title']}\" "
              f"— total spent: {drops_to_xrp(drops_spent):.6f} XRP")

        pr_header = r.headers.get("PAYMENT-RESPONSE")
        receipt = None
        if pr_header:
            try:
                receipt = decode_payment_response(pr_header)
                tx_hash = receipt.get("transaction")
            except Exception:
                receipt = "<undecodable>"

        payments.append({"id": pid, "receipt": receipt})
        sources.append({"id": pid, "title": paper["title"],
                        "content": r.json().get("fulltext", ""), "kind": "fulltext"})

        _append_audit({
            "id": pid,
            "title": paper["title"],
            "drops_paid": actual_price,
            "tx_hash": tx_hash,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "paid",
        })

    return {"sources": sources, "blocked": blocked, "payments": payments,
            "drops_spent": drops_spent, "elapsed_s": time.time() - t0}


def synthesize(client_pair, question: str, sources: list[dict]) -> str:
    if not sources:
        return "(no sources accessible)"
    blocks = []
    for s in sources:
        tag = "FULL TEXT" if s["kind"] == "fulltext" else "ABSTRACT ONLY"
        blocks.append(f"[{tag}] {s['title']}\n{s['content']}")
    corpus = "\n\n".join(blocks)
    return llm_text(
        client_pair,
        system=(
            "You are a research assistant. Answer the question using ONLY the "
            "provided sources. Note where a source is abstract-only and your "
            "answer is therefore limited. Be specific and cite paper titles."
        ),
        user=f"Question: {question}\n\nSources:\n{corpus}",
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def drops_to_xrp(drops: int) -> float:
    return drops / 1_000_000


def print_report(question, relevant_ids, no_pay, with_pay, answer_no_pay, answer_pay):
    line = "=" * 72
    full_no = sum(1 for s in no_pay["sources"] if s["kind"] == "fulltext")
    full_yes = sum(1 for s in with_pay["sources"] if s["kind"] == "fulltext")
    total = len(relevant_ids)

    print(f"\n{line}\nRESEARCH QUESTION\n{line}\n{question}")
    print(f"\nAgent judged {total} of the catalogue's papers relevant: {', '.join(relevant_ids)}")

    print(f"\n{line}\nRUN A — agent WITHOUT payment ability\n{line}")
    print(f"Full-text sources reached : {full_no}/{total}")
    print(f"Paywalls hit (fell back to abstract): {len(no_pay['blocked'])}")
    print(f"XRP spent                 : 0")

    print(f"\n{line}\nRUN B — agent WITH x402 XRPL wallet\n{line}")
    print(f"Full-text sources reached : {full_yes}/{total}")
    print(f"Paywalls paid             : {len(with_pay.get('payments', []))}")
    print(f"XRP spent                 : {drops_to_xrp(with_pay['drops_spent']):.6f} "
          f"({with_pay['drops_spent']} drops)")
    print(f"Time to pay+fetch all     : {with_pay['elapsed_s']:.1f}s")
    if with_pay["blocked"]:
        print(f"Still blocked             : {len(with_pay['blocked'])} "
              f"({with_pay['blocked']})")

    if AUDIT_FILE.exists():
        print(f"Payment audit trail       : {AUDIT_FILE}")

    expansion = "inf" if full_no == 0 and full_yes > 0 else (
        f"{full_yes / full_no:.1f}x" if full_no else "0x")
    print(f"\n{line}\nIMPACT OF BEING ABLE TO PAY\n{line}")
    print(f"Full-text access expanded : {full_no} -> {full_yes} sources ({expansion})")
    print(f"Cost of that expansion    : {drops_to_xrp(with_pay['drops_spent']):.6f} XRP, "
          f"{with_pay['elapsed_s']:.1f}s of added latency")

    print(f"\n{line}\nANSWER — abstract-only agent\n{line}\n{answer_no_pay}")
    print(f"\n{line}\nANSWER — paying agent (full text)\n{line}\n{answer_pay}")
    print(line)


def fetch_wallet_balance() -> tuple[str, float] | tuple[None, None]:
    """Return (address, xrp_balance) for the buyer wallet, or (None, None) on error."""
    try:
        from xrpl.wallet import Wallet
        from xrpl.clients import JsonRpcClient
        from xrpl.account import get_balance

        seed = getenv_first(("BUYER_SEED", "XRPL_BUYER_SEED"))
        if not seed:
            return None, None
        wallet = Wallet.from_seed(seed)
        address = getattr(wallet, "classic_address", None) or wallet.address
        rpc_url = os.getenv("XRPL_TESTNET_RPC_URL", DEFAULT_RPC_URL)
        client = JsonRpcClient(rpc_url)
        drops = int(get_balance(address, client))
        return address, drops / 1_000_000
    except Exception:
        return None, None


def prompt_llm_choice() -> str:
    print("\nSelect LLM backend:")
    print("  1) OpenAI  (OPENAI_API_KEY)")
    print("  2) Claude  (ANTHROPIC_API_KEY)")
    while True:
        choice = input("Choice [1/2]: ").strip()
        if choice == "1":
            return "openai"
        if choice == "2":
            return "claude"
        print("  Please enter 1 or 2.")


SPEND_PRESETS = [0.001, 0.005, 0.01, 0.05]


def prompt_spend_limit() -> float | None:
    print("\nXRP spend limit for this research job:")
    for i, xrp in enumerate(SPEND_PRESETS, 1):
        print(f"  {i}) {xrp:.3f} XRP")
    print(f"  {len(SPEND_PRESETS) + 1}) Enter amount")
    print(f"  {len(SPEND_PRESETS) + 2}) No limit")
    while True:
        raw = input(f"Choice [1-{len(SPEND_PRESETS) + 2}]: ").strip()
        if not raw.isdigit():
            print("  Please enter a number.")
            continue
        n = int(raw)
        if 1 <= n <= len(SPEND_PRESETS):
            return SPEND_PRESETS[n - 1]
        if n == len(SPEND_PRESETS) + 1:
            while True:
                amt = input("  Enter XRP amount: ").strip()
                try:
                    val = float(amt)
                    if val > 0:
                        return val
                except ValueError:
                    pass
                print("  Please enter a positive number.")
        if n == len(SPEND_PRESETS) + 2:
            return None
        print(f"  Please enter a number between 1 and {len(SPEND_PRESETS) + 2}.")


def main():
    parser = argparse.ArgumentParser(description="Paywall-paying research agent (x402/XRPL).")
    parser.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--llm", choices=["claude", "openai"], default=None,
                        help="LLM backend (omit to be prompted)")
    parser.add_argument("--max-spend", type=float, default=None, metavar="XRP",
                        help="Maximum XRP to spend on paywalls (omit to be prompted)")
    args = parser.parse_args()

    load_local_env()

    # 1. Wallet balance + network banner so mainnet misconfiguration is visible up front.
    address, balance_xrp = fetch_wallet_balance()
    if address is not None:
        print(f"\nBuyer wallet : {address}")
        print(f"Testnet balance: {balance_xrp:.6f} XRP")
        print(f"Network      : {XRPL_TESTNET_NETWORK}")
    else:
        print("\n(Could not retrieve wallet balance — check BUYER_SEED and network)")

    # 2. LLM selection
    llm = args.llm if args.llm is not None else prompt_llm_choice()
    client_pair = get_llm_client(llm)

    # 3. Spend limit selection
    if args.max_spend is not None:
        max_xrp = args.max_spend
    else:
        max_xrp = prompt_spend_limit()
    max_drops = int(max_xrp * 1_000_000) if max_xrp is not None else None
    if max_drops is not None:
        print(f"Spend limit  : {max_xrp} XRP ({max_drops} drops)")
    else:
        print("Spend limit  : none")

    print(f"\nConnecting to provider {args.provider} ...")
    try:
        catalogue = fetch_catalogue(args.provider)
    except requests.RequestException as e:
        sys.exit(f"ERROR: could not reach the provider ({e}). "
                 f"Start it with: py -3.12 journal_server.py server")
    print(f"Catalogue has {len(catalogue)} papers (abstracts free).")

    print(f"Asking {llm} which papers are worth unlocking ...")
    relevant_ids = rank_relevance(client_pair, args.question, catalogue)
    if not relevant_ids:
        sys.exit("LLM judged no papers relevant to the question.")

    print("Run A: reading abstracts only (no payment) ...")
    no_pay = run_without_payment(args.provider, catalogue, relevant_ids)
    answer_no_pay = synthesize(client_pair, args.question, no_pay["sources"])

    print("Run B: paying paywalls via x402/XRPL and reading full text ...")
    with_pay = run_with_payment(args.provider, catalogue, relevant_ids, max_drops=max_drops)
    answer_pay = synthesize(client_pair, args.question, with_pay["sources"])

    print_report(args.question, relevant_ids, no_pay, with_pay, answer_no_pay, answer_pay)


if __name__ == "__main__":
    main()
