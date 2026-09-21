# WillExecutor — resubmission

**The reviewer was right, the bug was real, and it is fixed at the source rather
than papered over.**

---

## What was wrong

> *"The current probe fetches only the newest ten account transactions and then
> filters by sender, so newer inbound transfers can hide an owner-signed
> transaction made after the heartbeat and incorrectly release the estate. Use
> authoritative outbound-only history or paginate until the heartbeat boundary
> is covered; if bounded evidence cannot prove that coverage, return
> INCONCLUSIVE rather than releasing funds."*

The probe fetched the newest ten transactions of an **account** and filtered
them by signer. There was an unstated assumption underneath that, and being
unstated is why it survived both the original submission and a self-audit that
was looking for exactly this class of problem: *a page with no signature on it
is a wallet with no signature after the anchor.* That holds only if the page
reaches back to the anchor.

1. the owner signs a transaction — they are **alive**
2. ten inbound transfers arrive afterwards
3. the validator fetches the ten newest transactions of the account
4. all ten are inbound, so the filter yields nothing
5. verdict `INACTIVE` — **and the estate is released**

Ten dust transfers stage it deliberately. **Ordinary chain noise stages it by
accident:** measured 2026-09-21, the ten newest transactions of `vitalik.eth`
are all inbound, while its newest outbound transaction is a month old.

---

## What replaced it

The fix is not a bigger window — fifty inbound transfers hide a signature
exactly as well as ten. It is two things: **ask the right endpoint**, and
**refuse to answer from evidence that cannot reach the question.** Only the
second is what makes a wrong release impossible; the first is what keeps the
contract useful rather than permanently undecided. Keeping those two jobs
separate is most of what §2 and §3 are about.

### 1. Ask for outbound history at the source

`/api/v2/addresses/{owner}/transactions?filter=from` is **server-side and
authoritative**. The page is outbound-only, so its newest entry *is* the newest
signature that exists, and no volume of inbound traffic can displace it.
Measured against all six allowlisted hosts on 2026-09-21: HTTP 200 with an
`items` list on every one, every returned item outbound.

### 2. Never depend on the filter having worked

The reviewer's suggested first step was to add `?filter=from`. That is right for
v2 — and it is exactly the kind of thing that must not be *assumed* to have
worked, because **the legacy endpoint accepts `filterby=from` and silently
ignores it**: byte-identical response, HTTP 200, no warning. (`starttimestamp`
is dropped the same way. Both measured 2026-09-21.)

The first draft of this fix handled that by counting how many returned items
were *not* outbound and falling back when any were. That was removed, because
the check turned out to be **redundant with something stronger**: a page of
outbound transactions with nothing after the anchor has every one of its entries
at or before the anchor, so the coverage test below already reaches the same
conclusion without it — and in one case (a page whose timestamps are all
unreadable) the shortcut said *"covered"* where coverage says *"ask again"*.

So there is no shortcut, and the resulting claim is the stronger one:
**coverage is proved from the page itself, the same way whichever endpoint
returned it and whether or not the filter was honoured.** `filter=from` is what
makes the answer *useful* — it is why a dormant wallet buried in inbound traffic
settles instead of going `INCONCLUSIVE` for ever. It is not what makes the
answer *safe*.

That separation is visible in the mutation tests: deleting `?filter=from` from
the URL breaks 9 tests about *usefulness*, while the audit check *"the inbound
flood cannot produce INACTIVE on any page shape"* keeps passing.

### 3. Refuse to prove a negative from evidence that cannot contain it

`ALIVE` and `INACTIVE` are not symmetric and are no longer treated as such.

- **`ALIVE` is positive evidence.** One signature after the anchor proves it,
  and how far back the page reaches is irrelevant — anything found is found.
- **`INACTIVE` is a negative**, provable only by evidence that could have
  contained the counterexample. `_covers` allows it in exactly two cases: the
  page is **shorter** than what was asked for (so it is the whole history), or
  its **oldest entry is at or before the anchor** (so the page spans the
  boundary).

Anything else is `INCONCLUSIVE` — which changes nothing and can be retried by
anyone — rather than a release that cannot be recalled.

This is carried on the consensus axis as a new compared field, **`cov_ok`**, so
no single leader can assert coverage alone. `_coherent` refuses any `INACTIVE`
or `ALIVE` payload with `cov_ok` false, on the payload alone, before a validator
spends a fetch.

### On pagination, and why there is a bound instead

The review offered pagination as the alternative. On this network it is
self-defeating, and the reason is measured rather than assumed:
`eth.blockscout.com` answers **429 at roughly the third rapid request from one
address**, and a consensus round fires one probe **per validator**
simultaneously from one datacentre range. A five-page walk would rate-limit
every round into a permanent `INCONCLUSIVE` — turning a rare failure into a
total one.

So the bound is **two fetches**: the outbound-only page, and the legacy page
only if the first returned nothing readable. Where two fetches cannot prove the
negative, the contract says `INCONCLUSIVE` **by design rather than by
exhaustion** — which is the behaviour the review asked for, reached by a route
that survives contact with the explorer's quota.

---

## Proof

### On chain, against the deployed bytes — `docs/EVIDENCE.md` §10

The attack was staged on a public chain: the owner signs, twelve inbound
transfers bury it, and **both endpoints are asked the same question at the same
instant.**

| | the **rejected** probe | the **deployed** probe |
|---|---|---|
| page returned | 10 transactions | 7 transactions |
| signed by the owner | **0** | **7** |
| not signed by the owner | 10 | 0 |
| signatures after the anchor | **0** | **1** |
| verdict | **`INACTIVE`** — releases the estate | **`ALIVE`** |

`claim_inactive` then settled **`ALIVE` on the first attempt**, with `src_ok` and
`cov_ok` both true, and the 5 GEN deposit did not move. `verify_claim`
re-derives every stored field from storage alone.

The `ALIVE` path was also re-demonstrated end to end on the redeployed contract
(`docs/EVIDENCE.md` §9).

### Offline

- **494 tests** (was 445), including `TestInboundFlood`, `TestCovers`,
  `TestWindowMatchesThePage` and `TestV2Shape`.
- `test_the_reviewers_exact_scenario_at_the_old_window` stages the reported
  numbers verbatim — ten inbound after one outbound, on a page of ten — and
  requires `INCONCLUSIVE`, never `INACTIVE`.
- `test_the_old_algorithm_would_have_released_the_estate` is a **regression
  oracle**: it reimplements the rejected probe in four lines and asserts that it
  gets the wrong answer on this input. If anyone reverts to client-side
  filtering, that test keeps passing while every other test in the class fails —
  which is the signature to look for.
- **Mutation-tested.** Disabling the coverage check fails 4 targeted tests;
  reverting the primary source to the mixed page fails 8. Neither half is
  decorative.

### Static and on-chain audit

**51 static checks** (was 40), including a new section 9 dedicated to this
rejection. Two of them are worth naming:

- *"the inbound flood cannot produce INACTIVE on any page shape"* — runs the
  staged attack through `_probe` across both explorer behaviours and four flood
  depths, and fails if `INACTIVE` comes back from any of them.
- *"TX_WINDOW equals the v2 page size"* — `TX_WINDOW` is 50 **because v2's page
  is 50**. A larger value would make a full page satisfy `len(items) <
  requested`, be read as a complete history, and hand the flood its old result
  back through the coverage check. It is pinned so nobody "optimises" it later.

The deployed contract now **declares the fix in `get_config`**
(`outbound_source`, `coverage_required_for_release`, `max_fetches_per_probe`),
and `tools/audit.sh --chain` asserts those against the live bytes on both
instances.

---

## What changed, in files

| file | change |
|---|---|
| `contracts/WillExecutor.py` | `_url_v2`, `_parse_v2`, `_covers`; `_tx_from`/`_tx_time` read both endpoint spellings; `_probe` fetches outbound-only and proves coverage from the page; `cov_ok` added to the vector, canon, `_coherent`, `_agrees`, storage, `_record`, `verify_claim`; `TX_WINDOW` 10 → 50; rubric `1.0.0` → `1.1.0` |
| `test/test_logic.py` | two-lane web stub; v2 fixtures; `TestInboundFlood`, `TestCovers`, `TestWindowMatchesThePage`, `TestV2Shape` |
| `test/flood.mjs` | **new** — stages the attack on chain |
| `tools/audit.sh` | new section 9 (11 checks) |
| `tools/audit_chain.mjs` | asserts the fix against the deployed bytes |
| `contracts/NOTES.md` | §3 rewritten (it previously argued *for* the endpoint that caused this); §4 gains the distinction that was missed |

---

## The note I would most like read

`contracts/NOTES.md` §3 used to argue, at length and with measurements, that the
legacy endpoint was the right choice — 4.5 KB against 530 KB, integer timestamps,
no timezone on the consensus axis. **Every word of that was true and none of it
mattered**, because the cheap endpoint cannot answer the question being asked.
That section now opens by saying so.

The general form of the mistake was optimising a measurement I had taken
(payload size) while never measuring the thing the verdict actually rested on
(whether the page could contain the answer). The fix for that is not a bigger
page. It is asking the right endpoint, checking that it did what it said, and
declining to answer when the evidence in hand cannot reach the question.
