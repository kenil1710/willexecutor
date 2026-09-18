# What happened on chain

A run of `test/seed.mjs` against GenLayer Studio Dev on **2026-09-18**, 17:54:23 to 17:59:50 UTC. Machine-readable form: [`evidence.json`](evidence.json). Raw
console output: [`seed-run.log`](seed-run.log).

**All checks passed.** What follows includes the things that were *not*
demonstrated, in §7, because a demo that quietly omits them is worse than one
that says so.

| | address |
|---|---|
| WillExecutor (canonical, 7–365 day intervals) | `0xa3eF4Ee2a69b38d20866A80be054c79282b4c03e` |
| WillExecutorDemo (same source, clock in seconds) | `0x766897eb88F5D7dbb2734A0501fA97eFDb3cd1d0` |

---

## 1. Three real wills, with the brief's own bounds

| id | owner → beneficiary | deposit | interval | watched chain | tx | settled |
|---|---|---|---|---|---|---|
| 1 | `0x7370…7Ebd` → `0xDbdF…e420` | 5 GEN | 30 days | ethereum | `0xf0fc4bc5f987c62e47…` | 7s |
| 2 | `0x150d…8FDb` → `0xE97A…769d` | 12 GEN | 90 days | base | `0x453c4b61a32e72b696…` | 7s |
| 3 | `0x6a34…a338` → `0x0dc1…F8F1` | 3 GEN | 365 days | arbitrum | `0x0136192e659653b020…` | 9s |

20 GEN locked. `get_stats` afterwards:

```
balance_wei 20000000000000000000 == locked_wei 20000000000000000000 + payable_wei 0
identity_holds: true      protocol_revenue_wei: "0"
```

These are the three wills the brief asks for, on the contract that enforces the
brief's real rules. They become claimable in 60, 180 and 730 days respectively —
which is exactly why §3 onwards happens on the second instance.

## 2. A heartbeat, and a premature claim that is refused rather than reverted

```
heartbeat(1)        last_heartbeat 1789754075 → 1789754100, count 1
                    tx 0xe460a1e3d4648e8226…
claim_inactive(1)   REJECTED — "will #1 is not overdue; it becomes claimable in 5183989s"
                    reverted: false
                    tx 0xaaa2e5965155ad6db1…
```

Three things were asserted about that refusal, and two of them were asserted
against **contract state** rather than against the return value:

- the transaction did **not** revert (rule 2 — a refusal returns, it does not raise);
- the will is byte-identical afterwards;
- `claim_attempts` is still `0` (rule 3 — no counter moves before a refusal).

## 3. A heartbeat saves an estate

On the demo instance, will #2 (4 GEN, 60-second interval) was allowed to pass
its threshold, and then its owner checked in.

```
heartbeat(2)        OK
claim_inactive(2)   REJECTED — "will #2 is not overdue; it becomes claimable in 112s"
                    will #2: status ACTIVE, deposit 4.00 GEN, claim_attempts 0
```

This is the case a timestamp-only dead man's switch gets right too. The next one
is not.

The assertion the script actually makes here is *"the estate survived"*, not
*"a particular gate fired"* — because two different protections can save this
will and which one does is a race against how fast Studio settles a transaction
today. At a 60-second interval the heartbeat buys 120 seconds; on a slow run the
claim lands after that window has reopened, a real round runs, and it declines
to release instead. Both outcomes are correct. An earlier version of this check
demanded the first one and reported a healthy contract as broken on a slow run —
which is the failure mode a test suite must not have.

## 4. The consensus round

Will #1 on the demo instance: 8 GEN, 60-second interval, two intervals missed.
The probed wallet is the owner's own — `0x73700819747517EAD7aceBf4DE8211FE0E047Ebd` —
and every validator independently fetched:

```
https://polygon.blockscout.com/api?module=account&action=txlist
    &address=0x73700819747517EAD7aceBf4DE8211FE0E047Ebd&sort=desc&page=1&offset=10
```

Polygon rather than Ethereum for an operational reason that is itself part of
the story: `eth.blockscout.com` rate-limits at roughly three rapid requests per
IP, and a round fires one fetch *per validator* from one datacentre range, so
repeated demo runs against one host walk into a 429 — which is, correctly,
`INCONCLUSIVE`. See §7.

**The wallet is a freshly generated key that has never signed anything on any
of these chains**, so the live answer is genuine rather than mocked:

```json
{"message":"No transactions found","result":[],"status":"0"}
```

Settled on the first attempt in **6 seconds**, transaction `0xf7e6f84a8eacde6cb8…`:

| compared field | value |
|---|---|
| `activity_status` | `INACTIVE` |
| `age_bucket` | `7` — *over six months old, or absent entirely* |
| `count_bucket` | `0` — *no signature was found at all* |
| `src_ok` | `true` |
| `content_hash` | `43feae0a7f5cd9d5` |

Canonical projection the hash commits to:

```
v1.0.0|will=1|chain=polygon|wallet=0x73700819747517ead7acebf4de8211fe0e047ebd
      |anchor=1789754121|now=1789754305|window=10|status=INACTIVE|age=7|count=0|src=1
```

Stored reasoning, composed from the agreed vector rather than written by the
leader:

> No sign of life: the polygon explorer shows no transaction signed by this
> wallet since the owner's last heartbeat. The newest signature is over six
> months old, or absent entirely, and no signature was found at all in the last
> 10 transactions examined. Inbound transfers were ignored: only a signature
> proves the key holder is present.

`src_ok: true` is what makes this verdict evidence-backed. Had the explorer not
answered, `_coherent` would have refused any verdict but `INCONCLUSIVE`.

## 5. The release, and that it conserves

```
deposit           8.000000 GEN
to beneficiary    7.600000 GEN   (0xDbdF…e420)
to finder         0.400000 GEN   (0xCC84…e41d — the wallet that pulled the switch)
                  ─────────────
                  8.000000 GEN   exactly, no remainder
finder_fee_bps    500
```

`verify_claim(1)` then re-derived the whole thing **from storage alone**, with no
network and no model:

```
✔ content_hash re-derives from the stored vector
✔ the stored reasoning recomposes from the stored vector
✔ the stored vector is internally coherent
✔ the split re-derives and conserves exactly
✔ a release was authorised by an INACTIVE reading from a source that answered
```

## 6. Withdrawal, and the freeze

```
claim_payout()  heir1    7.60 GEN   1 internal message posted   tx 0xf161fc4761a5fc70f9…
claim_payout()  finder   0.40 GEN   1 internal message posted   tx 0xa94dd420c87e144ac2…
```

Exactly one internal transfer per payout, to the right address, for the right
amount — one message, one recipient, which is the shape proved to work.

**Balances did not move, and that is the network, not the contract.** Studio Dev
queues an `on="finalized"` value transfer and never executes it; two previous
projects measured the same thing three ways each. Rather than hide it,
`get_stats` publishes the gap:

```
locked 4.00 GEN   payable 0.00 GEN   undelivered_wei 8000000000000000000
```

On a network that delivers, `undelivered_wei` stays at zero.

Then, against the executed will:

```
claim_inactive(1)  REJECTED — "will 1 is executed and can no longer change"
cancel_will(1)     REJECTED — "will 1 is executed and can no longer change"
→ the will is byte-identical; still EXECUTED; settlement unchanged
```

And a cancellation on the surviving will:

```
cancel_will(2)     OK — owner2 is owed the full 4.00 GEN back
demo ledger: balance 4.00 == locked 0.00 + payable 4.00   identity_holds: true
```

Every GEN that entered either contract is either still locked in a live will or
sitting in somebody's claimable balance. There is no third bucket.

---

## 7. What was *not* demonstrated on chain, and why

**~~The `ALIVE` verdict.~~ DEMONSTRATED — see §9.** This section previously
recorded ALIVE as unprovable on chain. That was true of the wallets in the main
run and is no longer true of the contract's evidence base; §9 is the proof.

**The `INCONCLUSIVE` verdict**, on this run. It cannot be produced on demand —
it requires the explorer to be unavailable at the moment a round opens. It is
nonetheless a routine occurrence rather than an edge case: `eth.blockscout.com`
answers **HTTP 429 after roughly three rapid requests from one IP**, and a
consensus round fires one fetch *per validator* simultaneously from one
datacentre range. **Two earlier runs of this same script, against earlier deploys,
went `INCONCLUSIVE` for exactly this reason — one of them five times in a row —
and the contract did the right thing every time**: nothing moved, the will
stayed `ACTIVE` with its 8 GEN intact, and the recorded reason said *"Nothing
was changed and this claim can be made again once the explorer answers."* That
is what moved the demo onto a less-hammered host; the contract needed no change,
because declining to act on evidence it could not read is the behaviour, not a
workaround for it. `seed.mjs` therefore retries up to five times
with a 45-second back-off, and asserts on every `INCONCLUSIVE` round that state
did not change.

**`settle_stalled`.** Structurally undemonstrable, and that is a property worth
stating rather than working around. The in-flight marker is only set inside
`claim_inactive`, and a round that never settles rolls back the whole
transaction *including the marker*. The only way to leave one set deliberately
would be a method that sets it — which is an owner who can freeze a will, which
is the exact power rule 6 exists to deny. The recovery path is proved offline by
setting the marker through the storage stub; the refusal path is exercised on
chain.

---

## 8. Reproducing this

```bash
cd test && npm install
node accounts.mjs          # fresh keys — and therefore genuinely dormant wallets
node deploy.mjs --both
node seed.mjs              # ~6 minutes, rewrites docs/evidence.json
```

```bash
./tools/audit.sh --chain   # 38 static checks + the live contracts
python3 test/test_logic.py # 440 offline tests
```

Note that `node accounts.mjs` preserves existing roles unless `--force` is
passed. If you regenerate the keys you get *different* dormant wallets, which is
fine — but if you reuse wallets that have since transacted on the chain a will
names, the honest verdict becomes `ALIVE` and no release will happen. That is
the contract working.

---

## 9. The ALIVE verdict, on chain

`test/alive.mjs`, one invocation, 2026-09-18 19:02:23 → 19:15:35 UTC.
Machine-readable: [`alive-evidence.json`](alive-evidence.json). Console:
[`alive-run.log`](alive-run.log).

### Why it took a different setup

The validators probe the will's **owner** — nobody can point them at a wallet
they do not control — and `create_will` must be signed by that owner. So ALIVE
needs one wallet that both holds a signable key *and* has genuine outbound
history on an allowlisted chain. Every wallet in §1–§6 is a freshly generated
key with no history anywhere, which is exactly what makes the INACTIVE reading
there genuine, and exactly why it cannot produce an ALIVE.

`sepolia` is in the allowlist, so the resolution is to use a wallet funded
there and create the signature *after* the anchor is fixed. The key used is
**testnet-only** — the script asserts a zero balance on ethereum, base,
arbitrum and polygon mainnets before it will run — and it is read at run time
from an existing dev `.env`, never copied, printed or committed.

### The sequence, in order

| step | fact |
|---|---|
| before | the wallet's newest signature is `1789757688`; nothing newer exists |
| **create_will** | `0x5dbe233c35274a48f43a6bdf018d297d3fe60b82af2d218fda26e950e581d938` → **will #6**, 6 GEN, 90 s interval, watching `sepolia` |
| anchor | `last_heartbeat = 1789758155`, claimable at `1789758335` |
| assertion | *every existing signature is OLDER than the anchor* — so nothing yet says ALIVE |
| **sepolia tx** | `0xec39fa7ea1169b6abcc0314fb8d714bdf4d4a0044809d19f45f8c2f96f0959ca`, block `11732605`, status success |
| indexed | Blockscout reports it at `1789758168` — **13 s after the anchor** |
| threshold | expires; the owner deliberately does **not** check in |
| **claim_inactive** | `0xc5af390ae344f318bd13d6f0df120cefc69de56d7692c1fa645c600be20de82e` |

The order is the point. The anchor is fixed *before* the signature exists, so
the only way a validator can answer ALIVE is by actually fetching the wallet's
history and finding something newer than a timestamp that was already on chain.

### The verdict

Settled in **16 seconds**, first attempt:

| compared field | value |
|---|---|
| `activity_status` | **`ALIVE`** |
| `age_bucket` | `0` — *less than a day old* |
| `count_bucket` | `3` — *four to seven signatures were found* |
| `src_ok` | `true` |
| `content_hash` | `b3698b76cf9fc279` |

```
v1.0.0|will=6|chain=sepolia|wallet=0xbe9ee23694b69d287fbe096ab3e37f61cff7b802
      |anchor=1789758155|now=1789758493|window=10|status=ALIVE|age=0|count=3|src=1
```

> Still alive: this wallet signed at least one transaction AFTER the owner's
> last heartbeat, according to the sepolia explorer. The newest signature is
> less than a day old, and four to seven signatures were found in the last 10
> transactions examined. Missing a check-in is not the same as being gone, so
> the deposit stays where it is.

`count_bucket = 3` matters as much as the verdict: the validators did not just
answer "alive", they **counted signatures**, and the count is on the compared
axis. An answer nobody could have produced without reading the chain.

### And the money did not move

```
will #6 status            ACTIVE      (not EXECUTED)
deposit                   6.000000 GEN, unchanged
contract locked total     6000000000000000000 → 6000000000000000000
paid to beneficiary       0
paid to finder            0
beneficiary owed          0
ledger identity           holds
verdict counters          alive=3  inactive=1  inconclusive=0
```

**The owner missed every check-in and the estate stayed locked**, because the
wallet was still signing. That is the half a timestamp-only dead man's switch
cannot do, and it is now a transaction anyone can look up rather than a claim
in a README.

### Two things this run also showed, unplanned

**A 429 is not a verdict.** The polling loop that waits for Blockscout to index
the transaction tripped the explorer's rate limit — roughly three requests per
window per IP — on several attempts. The script reports those as *"the explorer
did not answer — rate limited, which is NOT 'no transaction'"* and keeps them
out of its conclusions, which is the same distinction `_parse` enforces inside
the contract: a 429 carries `result: null`, so it can only ever produce
`INCONCLUSIVE`, never a payout.

**The observer's quota is not the validators'.** The local confirmation check is
advisory for exactly this reason: the validators fetch from their own address
range during the round, so a laptop that cannot currently read Blockscout says
nothing about whether they can. Aborting on it would be letting the observer's
rate limit decide the experiment.

### Re-running it

```bash
cd test
node alive-reset.mjs   # cancel the owner's active will, withdraw the refund
node alive.mjs         # create, sign on sepolia, wait, claim — ~6 minutes
```

`alive.mjs` is resumable, which is right for recovering a half-finished run but
wrong for producing evidence: a resumed run reuses a will whose post-anchor
signature it had already sent, so the ordering that makes the result meaningful
is not visible in that run's own log. `alive-reset.mjs` exists so the evidence
run can be a single clean invocation — which is what the table above is.
