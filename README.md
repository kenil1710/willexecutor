# WillExecutor

**A dead man's switch that checks whether you are actually dead.**

You deposit GEN, name a beneficiary, and set a check-in interval. Call
`heartbeat()` and the timer resets. Miss enough check-ins and *anyone* may call
`claim_inactive()` — and be paid 5% of the estate for doing it.

That call does not release the money. It opens a consensus round in which
**every validator independently fetches your wallet's transaction history from
Blockscout** and votes on what it found. If your wallet has *signed* anything
since your last check-in, you are alive, the claim fails, and nothing moves —
however many check-ins you missed.

That is not a design sketch. Both halves are on chain: a dormant wallet's estate
released in [tx `0xf7e6f84a…`](docs/EVIDENCE.md), and a wallet that had signed
one Sepolia transaction after its anchor keeping its GEN — verdict `ALIVE`,
settled in seconds, with the validators reporting how many signatures they
counted.

### The bug this contract was rejected for, and what replaced it

**A review found that the probe could read a living owner as gone.** It fetched
the newest ten transactions of an *account* and filtered them by signer — so ten
inbound transfers arriving after the owner's own last signature pushed that
signature off the page, the filter found nothing, and the estate was released.
Ten dust transfers stage it on purpose. Ordinary chain noise does it by accident:
measured on 2026-09-21, the ten newest transactions of `vitalik.eth` are all
inbound while its newest outbound transaction is a month old.

The fix is not a bigger page — fifty inbound transfers hide a signature exactly
as well as ten. It is two things:

1. **Ask for outbound history at the source.**
   `/api/v2/addresses/{owner}/transactions?filter=from` is server-side and
   authoritative, so the newest entry *is* the newest signature and inbound
   traffic cannot displace it.
2. **Refuse to prove a negative from evidence that cannot contain it.**
   `INACTIVE` now additionally requires `cov_ok`: the fetched history must be a
   complete history, or must reach back past the anchor. Where two bounded
   fetches cannot establish that, the verdict is `INCONCLUSIVE` — which changes
   nothing and can be retried by anyone — rather than a release that cannot be
   recalled.

Only the second makes a wrong release impossible, and it holds **whether or not
the explorer honoured the filter** — which matters, because the legacy endpoint
measurably accepts `filterby=from` and then silently ignores it. The first is
what keeps the contract *useful*: it is why a dormant wallet buried in inbound
traffic still settles instead of going `INCONCLUSIVE` for ever.

[**The attack is staged on a public chain in `docs/EVIDENCE.md` §10.**](docs/EVIDENCE.md)
The owner signs, twelve inbound transfers bury it, and the two endpoints are
asked the same question at the same instant: the rejected probe sees ten inbound
transactions and zero signatures and concludes `INACTIVE`; the deployed probe
sees the signature and concludes `ALIVE`. The deposit does not move.

---

## Why this needs GenLayer

A conventional contract can compare `last_heartbeat` to a deadline. That is all
it can do, and it is not enough: **forgetting is not dying.** A timestamp-only
dead man's switch pays out your estate because you were in hospital, or on a
boat, or had simply stopped thinking about it.

This contract asks a question no single-node chain can answer — *is this person
still signing transactions somewhere?* — and answers it with the one thing
GenLayer provides that nothing else does: **many independent nodes reading the
outside world and having to agree about what they saw.**

```
        conventional contract                 WillExecutor
   ┌──────────────────────────┐      ┌──────────────────────────────┐
   │ now > last_heartbeat + N │      │ now > last_heartbeat + N      │
   │            ↓             │      │            AND                │
   │        PAY OUT           │      │ every validator asks          │
   └──────────────────────────┘      │ Blockscout: has this wallet   │
                                     │ SIGNED anything since then?   │
     forgets ⇒ your heirs            │            ↓                  │
     inherit while you are           │  ALIVE → nothing moves        │
     still alive                     │  INACTIVE → release           │
                                     │  UNREADABLE → try later       │
                                     └──────────────────────────────┘
```

---

## Live on GenLayer Studio Dev

| | address |
|---|---|
| **WillExecutor** (canonical — the brief: 7–365 day intervals) | [`0x8862e1CcB90529Ff7e17DC80e0A13148d3530d98`](https://explorer-studio-dev.genlayer.com/) |
| **WillExecutorDemo** (same source, clock in seconds) | [`0xDb7ED7C86412d73D469f4f6F8148E76324fB9518`](https://explorer-studio-dev.genlayer.com/) |

Two instances because the canonical contract is correct and completely
un-watchable: the earliest a release could be demonstrated on it is fourteen
days away. The demo instance is the *same source file* with `interval_unit_s`
set to 1, so the whole lifecycle — create, check in, miss, claim, release,
withdraw — runs in about three minutes. Every rule, every gate and every line of
consensus logic is identical.

Exact addresses, transactions and constructor arguments: `deployments.json`.
What actually happened on chain: `docs/EVIDENCE.md` and `docs/evidence.json`.

---

## The consensus design

When `claim_inactive(will_id)` is called, every validator runs the same probe
and votes on a **feature vector** — not on a verdict.

```
  on chain, before the round            each validator, independently
  ──────────────────────────            ─────────────────────────────
  last_heartbeat  (the anchor)   ──┐
  owner wallet                     ├──►  GET blockscout /api/v2/addresses/
  chain                            │        <owner>/transactions?filter=from
  block time                     ──┘                  │
                                                      │   ← OUTBOUND ONLY,
                                                      │     server-side
                                                      ▼
                                       count only tx signed by the owner
                                                      │
                                    ┌─────────────────┴─────────────────┐
                                    │ found one after the anchor? ALIVE │
                                    │ found none? then this page may    │
                                    │ only answer if it REACHES BACK    │
                                    │ past the anchor — else it says so │
                                    └─────────────────┬─────────────────┘
                                                      ▼
                  ┌──────────────┬──────────────┬─────┴──────┬──────────┐
                  ▼              ▼              ▼            ▼          ▼
           activity_status   age_bucket   count_bucket    src_ok     cov_ok
           ALIVE/INACTIVE/     0..7          0..7        did it     did it
           INCONCLUSIVE      (in DAYS)   (signatures)    answer      reach
                  └──────────────┴──────────────┴─────┬──────┴──────────┘
                                                      ▼
                                      content_hash = FNV-1a(canonical projection)
```

**All six fields are compared.** A field the validators did not compare is a
field the leader can forge, and a forged `INACTIVE` empties a living person's
estate. The contract also stores *nothing it did not compare* — there is
deliberately no raw-body digest in storage.

**`cov_ok` is the field that makes a wrong release impossible.** A page of the
newest *account* transactions can be pushed past the anchor by inbound traffic
the owner does not control — ten dust transfers do it on purpose, and ordinary
chain noise does it by accident. So `ALIVE` is treated as positive evidence that
needs no coverage, while `INACTIVE` is a negative, provable only by evidence that
could have contained the counterexample. Where two bounded fetches cannot prove
it, the verdict is `INCONCLUSIVE` — which changes nothing and can be retried by
anyone.

Asking the outbound-only endpoint is what keeps that from being the *usual*
answer: it is why a dormant wallet buried in inbound traffic still settles.
The two jobs are deliberately separate — **the endpoint makes the contract
useful, the coverage test makes it safe** — and the coverage test holds whether
or not the explorer honoured the filter. See `contracts/NOTES.md` §3–§4.

Three design decisions are worth naming:

**The buckets are the point.** A previous project in this series measured
validators disagreeing about one round in four on a raw content digest, because
Blockscout is a load-balanced cluster whose replicas index at different rates.
Raw values cannot go on a consensus axis. Day-scale buckets can — a replica half
a block behind cannot move a wallet from "under a week" to "over a month". The
content hash is taken over the *canonical projection* (the bucketed vector plus
the question: will id, chain, wallet, anchor, block time, window), so it is a
pure function of already-compared fields and can never diverge on its own.

**Only a signature proves life.** A dead wallet still *receives* — airdrops,
dust, refunds. If an inbound transfer counted as a heartbeat, any stranger could
keep any will locked for ever for the price of one wei. `_probe` filters on
`from == owner`. The brief did not ask for this; the contract would have been
griefable without it.

**There is no language model.** "Did this wallet sign a transaction after this
timestamp" is a matter of fact, not of judgement. A model on the consensus axis
would add a disagreement source to a question that has a right answer. The
non-determinism here is the *web fetch*, performed independently by every
validator — which is precisely what GenLayer exists to make trustworthy.
`get_config` publishes `uses_language_model: false` rather than leaving anyone
to guess.

### Failing safe

Every ambiguity resolves towards **not moving the money**:

| what happened | verdict | what moves | demonstrated on chain |
|---|---|---|---|
| wallet signed something after the last check-in | `ALIVE` | nothing | [tx `0xc5af390a…`](docs/EVIDENCE.md) |
| wallet signed nothing, explorer answered | `INACTIVE` | estate released | [tx `0xf7e6f84a…`](docs/EVIDENCE.md) |
| explorer 429 / 5xx / unparseable / `result: null` | `INCONCLUSIVE` | nothing — retry later | yes, repeatedly |
| validators disagree | *no round* | nothing — retry later | offline suite |

`INCONCLUSIVE` is **on the consensus axis**, deliberately: validators must
*agree* the source was unavailable, or one node's bad minute silently becomes
everybody's payout.

---

## Methods

**Write**

| method | who | notes |
|---|---|---|
| `create_will(beneficiary, check_in_days, chain)` | anyone | payable — the GEN sent is the estate. 7–365 days, beneficiary ≠ self, one active will per wallet |
| `heartbeat(will_id)` | owner | free, resets the timer, works while paused |
| `top_up(will_id)` | owner | payable; counts as a check-in; refused while a claim is in flight |
| `claim_inactive(will_id)` | **anyone** | opens the consensus round; 5% finder fee on a release |
| `cancel_will(will_id)` | owner | returns the whole deposit; works while paused |
| `change_beneficiary(will_id, new)` | owner | refused while a claim is in flight |
| `settle_stalled(will_id)` | **anyone** | clears a round stuck > 48h; **works while paused** |
| `claim_payout()` | **anyone owed** | the only exit; ungated on pause |
| `set_paused`, `transfer_ownership` | owner | pause stops *new wills and top-ups only* |

**View** — `get_will`, `get_wills_by_owner`, `get_wills_by_beneficiary`,
`get_claimable_wills`, `get_stats`, `get_config`, `verify_claim`, `payout_of`,
`get_wills`, `preview_will`.

`verify_claim(will_id)` re-derives the content hash, the stored reasoning and
the money split **from storage alone** — no network, no model — so anybody can
check any claim, for ever, without trusting the node that answered.

---

## Money

One identity, asserted after every operation offline and published live by
`get_stats`:

```
balance_wei == locked_wei + payable_wei
```

Everything held is either locked in a live will or already somebody's to claim.
There is no third bucket and **no protocol revenue**: the finder fee is paid out
of the deposit to the caller, never to the owner, so the owner has **no withdraw
method at all** — not a gated one, none.

Releases conserve exactly: `to_beneficiary + to_finder == deposit`, with the
remainder wei going to the beneficiary rather than the bounty hunter.

---

## The ten rules

Every one is a past rejection written down so it cannot happen again. Full
reasoning in [`contracts/NOTES.md`](contracts/NOTES.md); executable form in
[`tools/audit.sh`](tools/audit.sh).

1. **Consensus binds every stored value** — and nothing is stored that was not compared.
2. **No public write ever raises.** Not one `raise` in the file. A revert rolls back storage but not the value that came with the call.
3. **No counter moves before a path that can still refuse.**
4. **The deposit and the finder fee are snapshotted**; the deploy-time settings have no setter.
5. **A will is frozen the moment it reaches a terminal status.**
6. **The owner cannot freeze user money.** Pause stops new wills; `settle_stalled` and `claim_payout` work regardless.
7. **Value the contract accepts is value somebody can get back out.**
8. **Conservative when the data is not there.**
9. **Only a signature proves life.**
10. **The explorer URL comes from an allowlist, never from a caller.**

---

## Running it

```bash
python3 test/test_logic.py     # 494 offline tests — no chain, no network, stdlib only
./tools/audit.sh               # 51 static checks, each one a past rejection
./tools/audit.sh --chain       # and assert the live deploy

cd test && npm install
node accounts.mjs              # a stable pool of signing keys
node deploy.mjs --both         # canonical + demo instance
node seed.mjs                  # the full live lifecycle, writing docs/evidence.json
node alive-reset.mjs && node alive.mjs   # the ALIVE path, end to end (~6 min)
node flood.mjs                 # the rejected attack, staged on chain (~15 min)
```

The offline suite needs nothing installed. It drives the real contract class
through a storage stub that reproduces the runner's semantics **including the
ones that bite** — a `TreeMap` that raises `KeyError` on a missing key, a
scalar-valued map that answers zero rather than `None`, and an `emit()` that
records nothing because on the real runner it posts no message.

---

## Layout

```
contracts/WillExecutor.py   the contract — ten rules in the header, reasoning in NOTES.md
contracts/NOTES.md          design notes and every hazard worth knowing
test/test_logic.py          494 offline tests + the runtime stub
test/deploy.mjs             deploys both instances, records deployments.json
test/seed.mjs               the live demonstration (INACTIVE → release)
test/alive.mjs              the ALIVE demonstration (signature after the anchor → nothing moves)
test/alive-reset.mjs        resets it so the evidence run is one clean invocation
tools/audit.sh              51 static checks
tools/audit_chain.mjs       asserts the live contracts, not the source
tools/verify_artifact.mjs   proves the deployed bytes are this source
docs/EVIDENCE.md            what happened on chain, including what did not
```
