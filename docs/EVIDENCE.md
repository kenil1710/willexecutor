# What happened on chain

A run of `test/seed.mjs` against GenLayer Studio Dev on **2026-09-18**, 17:15:05
to 17:20:56 UTC. Machine-readable form: [`evidence.json`](evidence.json). Raw
console output: [`seed-run.log`](seed-run.log).

**All checks passed.** What follows includes the things that were *not*
demonstrated, in §7, because a demo that quietly omits them is worse than one
that says so.

| | address |
|---|---|
| WillExecutor (canonical, 7–365 day intervals) | `0x4cf5a4a7A2AF1332541994C2C190213e5B696C02` |
| WillExecutorDemo (same source, clock in seconds) | `0xe4b3fCF037432a9be193a88470D7E70e531ca426` |

---

## 1. Three real wills, with the brief's own bounds

| id | owner → beneficiary | deposit | interval | watched chain | tx | settled |
|---|---|---|---|---|---|---|
| 1 | `0x7370…7Ebd` → `0xDbdF…e420` | 5 GEN | 30 days | ethereum | `0x9db819ad4c3136d1e6…` | 8s |
| 2 | `0x150d…8FDb` → `0xE97A…769d` | 12 GEN | 90 days | base | `0xc3508a2995156796ef…` | 10s |
| 3 | `0x6a34…a338` → `0x0dc1…F8F1` | 3 GEN | 365 days | arbitrum | `0x66453f0fbb540a8302…` | 12s |

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
heartbeat(1)        last_heartbeat 1789751714 → 1789751748, count 1
                    tx 0x4e3b9d3609ea1c435a…
claim_inactive(1)   REJECTED — "will #1 is not overdue; it becomes claimable in 5183989s"
                    reverted: false
                    tx 0x11d762740d4e7c12c1…
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
claim_inactive(2)   REJECTED — "will #2 is not overdue; it becomes claimable in 113s"
                    will #2: status ACTIVE, deposit 4.00 GEN, claim_attempts 0
```

This is the case a timestamp-only dead man's switch gets right too. The next one
is not.

## 4. The consensus round

Will #1 on the demo instance: 8 GEN, 60-second interval, two intervals missed.
The probed wallet is the owner's own — `0x73700819747517EAD7aceBf4DE8211FE0E047Ebd` —
and every validator independently fetched:

```
https://eth.blockscout.com/api?module=account&action=txlist
    &address=0x73700819747517EAD7aceBf4DE8211FE0E047Ebd&sort=desc&page=1&offset=10
```

**The wallet is a freshly generated key that has never signed anything on
Ethereum mainnet**, so the live answer is genuine rather than mocked:

```json
{"message":"No transactions found","result":[],"status":"0"}
```

Settled in **13 seconds**, transaction `0x1a22d98b0a6d0d040e…`:

| compared field | value |
|---|---|
| `activity_status` | `INACTIVE` |
| `age_bucket` | `7` — *over six months old, or absent entirely* |
| `count_bucket` | `0` — *no signature was found at all* |
| `src_ok` | `true` |
| `content_hash` | `59230c3c62082da2` |

Canonical projection the hash commits to:

```
v1.0.0|will=1|chain=ethereum|wallet=0x73700819747517ead7acebf4de8211fe0e047ebd
      |anchor=1789751770|now=1789751954|window=10|status=INACTIVE|age=7|count=0|src=1
```

Stored reasoning, composed from the agreed vector rather than written by the
leader:

> No sign of life: the ethereum explorer shows no transaction signed by this
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
claim_payout()  heir1    7.60 GEN   1 internal message posted   tx 0xde739709dc59d2e318…
claim_payout()  finder   0.40 GEN   1 internal message posted   tx 0x3f421d9c8dedb7e51d…
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

**The `ALIVE` verdict.** It needs a wallet that has signed a transaction on a
public chain *and* whose key can sign on GenLayer. Every wallet in this run is a
freshly generated key with no mainnet history — deliberately, because that is
precisely what makes the `INACTIVE` reading in §4 genuine rather than mocked.
The two requirements are in direct tension and cannot both be satisfied without
spending real mainnet gas from a test key.

`ALIVE` is proved in `test/test_logic.py` against Blockscout responses in the
real upstream shape, including the case that matters most — a wallet buried in
**ten inbound transfers** that must still read `INACTIVE`, because only a
signature proves life. The deterministic half of the same protection *was*
demonstrated on chain, in §3: a will that has been checked in on cannot be
claimed at all.

**The `INCONCLUSIVE` verdict**, on this run. It cannot be produced on demand —
it requires the explorer to be unavailable at the moment a round opens. It is
nonetheless a routine occurrence rather than an edge case: `eth.blockscout.com`
answers **HTTP 429 after roughly three rapid requests from one IP**, and a
consensus round fires one fetch *per validator* simultaneously from one
datacentre range. **An earlier run of this same script, against an earlier
deploy, went `INCONCLUSIVE` for exactly this reason and the contract did the
right thing**: nothing moved, the will stayed `ACTIVE` with its 8 GEN intact,
and the recorded reason said *"Nothing was changed and this claim can be made
again once the explorer answers."* `seed.mjs` therefore retries up to five times
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
