/**
 * The live demonstration. Creates three real wills on the canonical contract,
 * then drives the whole lifecycle to a real release on the demo instance.
 *
 *   node seed.mjs
 *
 * WHAT IT PROVES, in order:
 *
 *   1. Three real wills exist on the canonical contract, with the brief's own
 *      bounds — 30, 90 and 365 days — and real GEN locked behind them.
 *   2. A heartbeat resets the timer, on chain.
 *   3. A claim against a will that is not overdue is REFUSED, not reverted:
 *      the caller gets {"status":"REJECTED"} and keeps anything they sent.
 *   4. On the demo instance, once the threshold really has passed, validators
 *      independently fetch the owner's wallet history from Blockscout and agree
 *      on a feature vector. The wallet is genuinely unused, so the honest live
 *      answer is `{"status":"0","message":"No transactions found","result":[]}`
 *      and the verdict is INACTIVE.
 *   5. The estate is released: 95% to the beneficiary, 5% to whoever pulled the
 *      switch, summing to the deposit exactly.
 *   6. `verify_claim` re-derives the stored content hash, the stored reasoning
 *      and the split from storage alone, with no network and no model.
 *   7. `claim_payout` posts a well-formed internal transfer to the right
 *      address for the right amount.
 *   8. A HEARTBEAT SAVES AN ESTATE: a second will is taken past its threshold,
 *      checked in on, and the claim that follows is refused.
 *
 * Everything is written to docs/evidence.json as it happens, so a failure
 * halfway through still leaves the evidence for what did work.
 *
 * A NOTE ON WHAT IS NOT DEMONSTRATED HERE, because a demo that quietly omits
 * things is worse than one that says so. The ALIVE verdict needs a wallet that
 * has signed a transaction on a public chain, and every wallet in this run is a
 * freshly generated key with no mainnet history — by design, since that is what
 * makes the INACTIVE reading genuine rather than mocked. ALIVE and INCONCLUSIVE
 * are proved in test/test_logic.py against real captured Blockscout responses.
 * docs/EVIDENCE.md states which is which.
 */
import { writeFileSync, readFileSync, mkdirSync } from "node:fs";
import { createClient, createAccount } from "genlayer-js";
import {
  CHAINS, argOf, accounts, connect, fundOnStudio, gen, sleep, waitFinalized,
  returnedJson, retry,
} from "./harness.mjs";

const networkName = argOf("network", "studiodev");
const chain = CHAINS[networkName];
const deployments = JSON.parse(
  readFileSync(new URL("../deployments.json", import.meta.url), "utf8"),
).deployments[networkName];

const MAIN = deployments.WillExecutor.address;
const DEMO = deployments.WillExecutorDemo.address;
const acc = accounts();
const read = createClient({ chain });

const GEN = 10n ** 18n;
const evidence = { network: networkName, started_at: new Date().toISOString(), main: MAIN, demo: DEMO, steps: [] };
let failures = 0;

function note(step) {
  evidence.steps.push(step);
  try {
    mkdirSync(new URL("../docs/", import.meta.url), { recursive: true });
  } catch { /* already there */ }
  writeFileSync(new URL("../docs/evidence.json", import.meta.url), JSON.stringify(evidence, null, 2) + "\n");
}

function ok(what, detail = "") { console.log(`  \x1b[32m✔\x1b[0m ${what}${detail ? "  " + detail : ""}`); }
function skip(what, why) { console.log(`  \x1b[33m-\x1b[0m ${what}  (skipped: ${why})`); }
function bad(what, detail = "") { failures++; console.log(`  \x1b[31m✗\x1b[0m ${what}${detail ? "  " + detail : ""}`); }
function head(t) { console.log(`\n\x1b[1m${t}\x1b[0m`); }

/**
 * Every balance read goes through `retry`.
 *
 * Studio intermittently answers an RPC with an HTML error page instead of JSON,
 * which surfaces as `Unexpected token '<'`. A bare `read.getBalance` therefore
 * throws and takes the whole run down — which is what happened on the run
 * before this one, in step 0, after the deploy had already succeeded. The
 * harness has a retry helper for exactly this; the balance reads were simply
 * not using it.
 */
const balanceOf = (address) =>
  retry(() => read.getBalance({ address }), { label: `balance ${address.slice(0, 10)}` });

const clients = {};
function at(address, role) {
  const key = `${address}:${role}`;
  if (!clients[key]) clients[key] = connect({ networkName, address, role });
  return clients[key];
}

async function send(address, role, fn, args = [], value = 0n) {
  const out = await at(address, role).send(fn, args, value);
  return { ...out, json: returnedJson(out) };
}
const view = async (address, fn, args = []) => at(address, "client").viewJson(fn, args);

/**
 * A write's return value is not always readable — that is a property of the
 * transport, not of the contract — so every assertion that matters is made
 * against CONTRACT STATE as well. A run that could only read return values
 * would report a transport hiccup as a contract failure.
 */
async function statusOf(address, fn, args, value, role) {
  const out = await send(address, role, fn, args, value);
  const status = out.json?.status ?? (out.ok ? "UNREADABLE" : "FAILED");
  return { out, status, json: out.json };
}

console.log(`\n\x1b[1mWillExecutor — live demonstration on ${networkName}\x1b[0m`);
console.log(`  canonical  ${MAIN}`);
console.log(`  demo       ${DEMO}`);

// ---------------------------------------------------------------------------
head("0. Funding the wallets");
for (const role of Object.keys(acc)) {
  await fundOnStudio(chain, acc[role].address, 200n * GEN);
}
for (const role of ["owner1", "owner2", "owner3", "finder"]) {
  const bal = await balanceOf(acc[role].address);
  console.log(`  ${role.padEnd(8)} ${acc[role].address}  ${gen(bal)} GEN`);
}

// ---------------------------------------------------------------------------
head("1. Three real wills on the canonical contract (7–365 day bounds)");
const WILLS = [
  { owner: "owner1", heir: "heir1", days: 30, deposit: 5n * GEN, chainName: "ethereum" },
  { owner: "owner2", heir: "heir2", days: 90, deposit: 12n * GEN, chainName: "base" },
  { owner: "owner3", heir: "heir3", days: 365, deposit: 3n * GEN, chainName: "arbitrum" },
];

for (const w of WILLS) {
  const { status, json, out } = await statusOf(
    MAIN, "create_will", [acc[w.heir].address, w.days, w.chainName], w.deposit, w.owner,
  );
  if (status === "OK" || status === "UNREADABLE") {
    w.id = json?.will_id ?? null;
    ok(`${w.owner} → ${w.heir}: ${gen(w.deposit)} GEN, check in every ${w.days} days, watching ${w.chainName}`,
       `${out.seconds.toFixed(0)}s`);
  } else {
    bad(`create_will for ${w.owner}`, json?.reason ?? out.revertReason);
  }
  note({ step: "create_will", owner: acc[w.owner].address, beneficiary: acc[w.heir].address,
         days: w.days, chain: w.chainName, deposit_wei: String(w.deposit),
         status, will_id: w.id, tx: out.hash, seconds: out.seconds });
}

const listed = await view(MAIN, "get_wills", [0, 10]);
if (listed.total === 3) ok(`the register holds 3 wills`); else bad(`register holds ${listed.total}, expected 3`);
for (const card of listed.wills) {
  console.log(`      #${card.will_id}  ${card.deposit_gen} GEN  ${card.chain}  claimable in ${card.seconds_until_claimable}s`);
}

const stats1 = await view(MAIN, "get_stats");
if (stats1.ledger.identity_holds) ok("ledger identity holds: balance == locked + payable");
else bad("LEDGER IDENTITY BROKEN", JSON.stringify(stats1.ledger));
if (stats1.protocol_revenue_wei === "0") ok("the contract keeps nothing (no protocol revenue, no withdraw method)");
note({ step: "stats_after_creation", stats: stats1 });

// ---------------------------------------------------------------------------
head("2. A heartbeat, on chain");
const beforeHb = (await view(MAIN, "get_will", [1])).will;
const hb = await statusOf(MAIN, "heartbeat", [1], 0n, "owner1");
const afterHb = (await view(MAIN, "get_will", [1])).will;
if (afterHb.last_heartbeat > beforeHb.last_heartbeat) {
  ok(`will #1 checked in`, `${beforeHb.last_heartbeat} → ${afterHb.last_heartbeat}, count ${afterHb.heartbeat_count}`);
} else bad("heartbeat did not move last_heartbeat");
note({ step: "heartbeat", will_id: 1, before: beforeHb.last_heartbeat, after: afterHb.last_heartbeat,
       heartbeat_count: afterHb.heartbeat_count, tx: hb.out.hash });

// ---------------------------------------------------------------------------
head("3. A premature claim is REFUSED, not reverted (rule 2)");
/*
 * Compare a STABLE PROJECTION of the will, not the whole view. `get_will` also
 * reports `seconds_until_claimable` and a countdown inside `timeline`, both of
 * which are computed against the block time and therefore differ between two
 * honest reads seconds apart. Comparing those would fail on a contract that is
 * behaving perfectly — which is exactly the kind of false alarm that teaches
 * people to ignore a suite.
 */
const stable = (w) => JSON.stringify({
  status: w.status, deposit_wei: w.deposit_wei, beneficiary: w.beneficiary,
  owner: w.owner, chain: w.chain, last_heartbeat: w.last_heartbeat,
  claimable_at: w.claimable_at, heartbeat_count: w.heartbeat_count,
  evidence: w.evidence, settlement: w.settlement,
});

const willBefore = (await view(MAIN, "get_will", [1])).will;
const early = await statusOf(MAIN, "claim_inactive", [1], 0n, "finder");
const willAfter = (await view(MAIN, "get_will", [1])).will;

if (early.out.reverted) bad("claim REVERTED — rule 2 broken", early.out.revertReason);
else ok("the transaction did not revert (rule 2: a refusal returns, it does not raise)");

// The assertion that matters is made against STATE. A return value that cannot
// be read is a property of the transport, not of the contract, and a suite that
// could only read return values would report a transport hiccup as a bug.
if (stable(willAfter) === stable(willBefore)) {
  ok("the will is byte-identical afterwards — nothing was consumed by the attempt");
} else bad("the refused claim changed the will", "see docs/evidence.json");
if (willAfter.evidence.claim_attempts === 0) ok("no claim attempt was counted (rule 3: no counter moves before a refusal)");
else bad(`claim_attempts is ${willAfter.evidence.claim_attempts}, expected 0`);

if (early.status === "REJECTED") ok("and it said why", early.json.reason.slice(0, 70));
else console.log(`  \x1b[33m-\x1b[0m the return value was unreadable (${early.status}); the state assertions above stand regardless`);
note({ step: "premature_claim", will_id: 1, status: early.status, reason: early.json?.reason ?? null,
       reverted: early.out.reverted, tx: early.out.hash });

const claimable = await view(MAIN, "get_claimable_wills");
if (claimable.count === 0) ok("no will on the canonical contract is claimable yet");
else bad(`${claimable.count} wills already claimable — the threshold is not being applied`);

// ---------------------------------------------------------------------------
head("4. The demo instance: the same source with the clock in seconds");
const INTERVAL = 60;
const DEPOSIT = 8n * GEN;

/*
 * WATCHED ON POLYGON, NOT ETHEREUM, AND THE REASON IS THE WHOLE POINT OF
 * RULE 8.
 *
 * `eth.blockscout.com` rate-limits at roughly three rapid requests per IP, and
 * a consensus round fires one fetch PER VALIDATOR simultaneously from one
 * datacentre range. Repeated demo runs against the same host therefore walk
 * straight into a 429, and a 429 is — correctly — INCONCLUSIVE. Spreading the
 * demo onto a host this run has not been hammering is the operational answer;
 * the contract's answer, which is the one that matters, is that it refuses to
 * move an estate on evidence it could not read, however many times you ask.
 *
 * The wallet is dormant on every chain, so the verdict is unchanged.
 */
const WATCH = "polygon";
const d1 = await statusOf(DEMO, "create_will", [acc.heir1.address, INTERVAL, WATCH], DEPOSIT, "owner1");
if (d1.status === "OK" || d1.status === "UNREADABLE") ok(`will #1: ${gen(DEPOSIT)} GEN, ${INTERVAL}s interval, watching owner1 on ${WATCH}`);
else bad("demo create_will #1", d1.json?.reason);

const d2 = await statusOf(DEMO, "create_will", [acc.heir2.address, INTERVAL, WATCH], 4n * GEN, "owner2");
if (d2.status === "OK" || d2.status === "UNREADABLE") ok(`will #2: 4 GEN, ${INTERVAL}s interval — this one's owner will check in and be saved`);
else bad("demo create_will #2", d2.json?.reason);

note({ step: "demo_wills_created", will_1: d1.status, will_2: d2.status,
       tx_1: d1.out.hash, tx_2: d2.out.hash });

const wait = INTERVAL * 2 + 25;
console.log(`\n  waiting ${wait}s for both wills to pass their threshold (${INTERVAL}s × 2 missed check-ins)…`);
await sleep(wait * 1000);

const nowClaimable = await view(DEMO, "get_claimable_wills");
if (nowClaimable.count === 2) ok(`both demo wills are now claimable`, `finder fee ${nowClaimable.wills[0].finder_fee_gen} GEN on #1`);
else bad(`expected 2 claimable wills, got ${nowClaimable.count}`);
note({ step: "claimable", count: nowClaimable.count, wills: nowClaimable.wills });

// ---------------------------------------------------------------------------
head("5. A heartbeat saves an estate");
const saveHb = await statusOf(DEMO, "heartbeat", [2], 0n, "owner2");
if (saveHb.status === "OK" || saveHb.status === "UNREADABLE") ok("owner2 checked in just in time");
else bad("owner2 heartbeat", saveHb.json?.reason);

const savedClaim = await statusOf(DEMO, "claim_inactive", [2], 0n, "finder");
const will2 = (await view(DEMO, "get_will", [2])).will;

/*
 * THE ASSERTION IS "THE ESTATE SURVIVED", not "a particular gate fired".
 *
 * Two different protections can save this will and which one does is a race
 * against how fast Studio is settling transactions today. The interval here is
 * 60s with a threshold of 2, so the heartbeat buys 120 seconds; on a slow run
 * the claim can land AFTER that window has reopened, in which case a real
 * consensus round runs and declines to release instead. Both outcomes are
 * correct, and an earlier version of this check demanded the first one and
 * reported a healthy contract as broken.
 */
if (will2.status === "ACTIVE" && will2.deposit_gen === "4.00") {
  ok("will #2 survived: still ACTIVE, still holding all 4 GEN");
} else bad("will #2 lost its estate", JSON.stringify(will2).slice(0, 200));

if (savedClaim.status === "REJECTED") {
  ok("the deadline gate refused it outright — the heartbeat reset the timer",
     savedClaim.json.reason.slice(0, 60));
} else if (will2.evidence.activity_status && will2.evidence.activity_status !== "INACTIVE") {
  ok(`the window had reopened, so a real round ran and declined to release`,
     `verdict ${will2.evidence.activity_status}`);
} else if (savedClaim.status === "UNREADABLE") {
  console.log(`  \x1b[33m-\x1b[0m return value unreadable; the state assertion above stands`);
} else {
  bad(`claim against a checked-in will returned ${savedClaim.status}`);
}
note({ step: "heartbeat_saves", will_id: 2, claim_status: savedClaim.status,
       reason: savedClaim.json?.reason ?? null, will_status: will2.status, deposit: will2.deposit_gen });

// ---------------------------------------------------------------------------
head("6. The consensus round: validators read Blockscout for themselves");
const heir1Before = await balanceOf(acc.heir1.address);
const finderBefore = await balanceOf(acc.finder.address);
const contractBefore = await balanceOf(MAIN);

console.log(`  the probed wallet is ${acc.owner1.address}`);
console.log(`  every validator independently fetches the ${WATCH} explorer for ${acc.owner1.address}…`);

/*
 * RETRY ON INCONCLUSIVE — the path the contract is built around, exercised.
 *
 * MEASURED: eth.blockscout.com answers HTTP 429 after roughly three requests in
 * quick succession from one IP, and a consensus round fires one fetch PER
 * VALIDATOR, all at once, from one datacentre range. So a round landing on a
 * rate limit is not an edge case here — it is the common case, and it is
 * precisely why INCONCLUSIVE exists and why `claim_inactive` is permissionless
 * and repeatable.
 *
 * An INCONCLUSIVE round is NOT a failure. It is the contract declining to move
 * an estate on evidence it could not read. Every attempt is recorded, because
 * watching the conservative path fire for real and then succeed is better
 * evidence than a single lucky round.
 */
let claim = null;
let will1 = null;
let ev = null;
const attempts = [];

for (let attempt = 1; attempt <= 6; attempt++) {
  claim = await statusOf(DEMO, "claim_inactive", [1], 0n, "finder");
  will1 = (await view(DEMO, "get_will", [1])).will;
  ev = will1.evidence;
  const outcome = claim.json?.outcome ?? ev.activity_status;
  attempts.push({ attempt, outcome, seconds: claim.out.seconds, tx: claim.out.hash,
                  src_ok: ev.src_ok, content_hash: ev.content_hash });
  console.log(`  attempt ${attempt}: ${outcome} in ${claim.out.seconds.toFixed(0)}s  (src_ok=${ev.src_ok})`);

  if (outcome === "INCONCLUSIVE") {
    // The explorer did not answer. Nothing moved — assert that, then wait for
    // the rate-limit window to roll over rather than hammering it.
    if (will1.status === "ACTIVE" && will1.deposit_gen === "8.00") {
      ok("INCONCLUSIVE moved nothing — the estate is exactly where it was (rule 8)");
    } else bad("an INCONCLUSIVE round changed state", JSON.stringify(will1).slice(0, 140));
    if (attempt < 6) {
      // Back off further each time: a shared-IP rate limit needs the window to
      // roll over, and hammering it is what caused the limit in the first place.
      const wait = 60_000 + attempt * 30_000;
      console.log(`      waiting ${wait / 1000}s for the explorer's rate-limit window to roll over…`);
      await sleep(wait);
    }
    continue;
  }
  break;
}

const finalOutcome = claim.json?.outcome ?? ev.activity_status;
const released = will1.status === "EXECUTED";
note({ step: "claim_attempts", attempts });

if (finalOutcome === "INACTIVE" && released) {
  ok(`verdict INACTIVE — the wallet has signed nothing since the last check-in`);
} else if (finalOutcome === "INCONCLUSIVE") {
  bad(`the explorer never answered across ${attempts.length} attempts`,
      "this is upstream rate limiting, not a contract fault — the will is untouched and still claimable");
} else {
  bad(`verdict was ${finalOutcome}`, claim.json?.reason ?? "");
}
console.log(`      activity_status ${ev.activity_status}`);
console.log(`      age_bucket      ${ev.age_bucket}  (${ev.age_meaning})`);
console.log(`      count_bucket    ${ev.count_bucket}  (${ev.count_meaning})`);
console.log(`      src_ok          ${ev.src_ok}`);
console.log(`      content_hash    ${ev.content_hash}`);
console.log(`      reason          ${will1.evidence.reason?.slice(0, 100) ?? ""}`);

if (ev.content_hash && ev.content_hash.length === 16) ok("a content hash was stored");
else bad("no content hash", String(ev.content_hash));
if (ev.src_ok === true) ok("src_ok is true — the explorer answered, so the verdict is evidence-backed");
else bad("src_ok is false but a verdict was recorded");

note({ step: "consensus_round", will_id: 1, outcome: claim.json?.outcome ?? null,
       seconds: claim.out.seconds, tx: claim.out.hash, evidence: ev,
       reason: will1.evidence.reason, settlement: will1.settlement });

// ---------------------------------------------------------------------------
head("7. The release, and that it conserves exactly");
if (!released) {
  skip("the split, the payouts and the freeze checks", "no release happened — see above");
} else {
  const settle = will1.settlement;
  const toHeir = BigInt(settle.paid_beneficiary_wei || "0");
  const toFinder = BigInt(settle.paid_finder_wei || "0");
  if (toHeir + toFinder === DEPOSIT) ok(`the split conserves exactly`, `${gen(toHeir)} + ${gen(toFinder)} = ${gen(DEPOSIT)} GEN`);
  else bad(`split does NOT conserve: ${toHeir} + ${toFinder} != ${DEPOSIT}`);
  if (toFinder === DEPOSIT * 500n / 10000n) ok("the finder fee is exactly 5%");
  else bad(`finder fee is ${toFinder}, expected ${DEPOSIT * 500n / 10000n}`);
  if (settle.finder?.toLowerCase() === acc.finder.address.toLowerCase()) ok("the finder is the wallet that pulled the switch");
  else bad("finder mismatch", `${settle.finder} vs ${acc.finder.address}`);

  const owedHeir = await view(DEMO, "payout_of", [acc.heir1.address]);
  const owedFinder = await view(DEMO, "payout_of", [acc.finder.address]);
  ok(`beneficiary is owed ${owedHeir.owed_gen} GEN, finder ${owedFinder.owed_gen} GEN`);
}

// ---------------------------------------------------------------------------
head("8. verify_claim re-derives everything from storage alone");
const verified = await view(DEMO, "verify_claim", [1]);
if (verified.verified) ok(`all ${verified.checks.length} checks pass — no network, no model, just the stored vector`);
else bad("verify_claim FAILED", JSON.stringify(verified.checks));
for (const c of verified.checks) console.log(`      ${c.ok ? "✔" : "✗"} ${c.check}`);
console.log(`      projection: ${verified.canonical_projection}`);
note({ step: "verify_claim", will_id: 1, verified: verified.verified, checks: verified.checks,
       canonical_projection: verified.canonical_projection });

// ---------------------------------------------------------------------------
head("9. Withdrawal — one message, one recipient");
const payHeir = released ? await statusOf(DEMO, "claim_payout", [], 0n, "heir1") : null;
const payFinder = released ? await statusOf(DEMO, "claim_payout", [], 0n, "finder") : null;

for (const [who, res] of (released ? [["heir1", payHeir], ["finder", payFinder]] : [])) {
  const pending = res.out.pending ?? 0;
  if (res.status === "OK" || res.status === "UNREADABLE") {
    ok(`${who} withdrew ${res.json?.amount_gen ?? "?"} GEN`, `${pending} internal message${pending === 1 ? "" : "s"} posted`);
  } else bad(`${who} claim_payout`, res.json?.reason ?? res.out.revertReason);
  if (pending === 1) ok(`${who}'s payout posted exactly one internal transfer`);
  else bad(`${who}'s payout posted ${pending} internal messages, expected 1`);
}

if (released) {
  await waitFinalized(read, payHeir.out.hash, { label: "heir payout" });
  await waitFinalized(read, payFinder.out.hash, { label: "finder payout" });
} else skip("the withdrawal path", "no release happened");

const heir1After = await balanceOf(acc.heir1.address);
const finderAfter = await balanceOf(acc.finder.address);
const delivered = heir1After > heir1Before;

/*
 * Studio Dev QUEUES an `on="finalized"` value transfer and never executes it.
 * Measured independently by two previous projects, three ways each. It is a
 * property of the network, not of this contract, and it is REPORTED rather than
 * hidden — get_stats publishes the gap as `undelivered_wei`. What this contract
 * is responsible for is posting a well-formed transfer to the right address for
 * the right amount, which the `pending` count above asserts.
 */
if (delivered) ok(`balances moved — heir1 +${gen(heir1After - heir1Before)} GEN`);
else if (released) console.log(`  \x1b[33m-\x1b[0m balances unchanged: Studio Dev queues on="finalized" transfers and does not execute them (known, measured; see NOTES.md §3c)`);

const stats2 = await view(DEMO, "get_stats");
if (stats2.ledger.identity_holds) ok("ledger identity still holds after the release and both withdrawals");
else bad("LEDGER IDENTITY BROKEN", JSON.stringify(stats2.ledger));
console.log(`      locked ${stats2.ledger.locked_gen} GEN   payable ${stats2.ledger.payable_gen} GEN   undelivered ${stats2.undelivered_wei}`);
note({ step: "payouts", released, heir_tx: payHeir?.out.hash ?? null, finder_tx: payFinder?.out.hash ?? null,
       heir_pending: payHeir?.out.pending ?? null, finder_pending: payFinder?.out.pending ?? null,
       balances_moved: delivered, stats: stats2 });

// ---------------------------------------------------------------------------
head("10. The executed will is frozen");
if (!released) skip("the frozen-will checks", "will #1 was never executed");
const frozenBefore = released ? (await view(DEMO, "get_will", [1])).will : null;
const again = released ? await statusOf(DEMO, "claim_inactive", [1], 0n, "outsider") : null;
const cancelDead = released ? await statusOf(DEMO, "cancel_will", [1], 0n, "owner1") : null;
const frozenAfter = released ? (await view(DEMO, "get_will", [1])).will : null;

if (released) {
  if (stable(frozenAfter) === stable(frozenBefore)) {
    ok("a second claim AND a cancel both bounced off — the will is byte-identical (rule 5)");
  } else bad("an executed will changed", "see docs/evidence.json");
  if (frozenAfter.status === "EXECUTED") ok("it is still EXECUTED, and the settlement is unchanged");
  else bad(`status drifted to ${frozenAfter.status}`);
}
for (const [what, res] of (released ? [["second claim", again], ["cancel", cancelDead]] : [])) {
  if (res.status === "REJECTED") ok(`${what} refused`, res.json.reason.slice(0, 55));
  else console.log(`  \x1b[33m-\x1b[0m ${what}: return value unreadable (${res.status}); the state assertion above stands`);
}
note({ step: "frozen", released, second_claim: again?.status ?? null, cancel: cancelDead?.status ?? null });

// ---------------------------------------------------------------------------
head("11. Cancellation returns the deposit");
const cancel = await statusOf(DEMO, "cancel_will", [2], 0n, "owner2");
if (cancel.status === "OK" || cancel.status === "UNREADABLE") {
  const owed = await view(DEMO, "payout_of", [acc.owner2.address]);
  if (owed.owed_gen === "4.00") ok(`owner2 cancelled and is owed the full ${owed.owed_gen} GEN back`);
  else bad(`owner2 is owed ${owed.owed_gen}, expected 4.00`);
} else bad("cancel_will", cancel.json?.reason);
const stats3 = await view(DEMO, "get_stats");
if (stats3.ledger.identity_holds) ok("ledger identity holds after cancellation");
else bad("LEDGER IDENTITY BROKEN");
note({ step: "cancel", status: cancel.status, stats: stats3 });

// ---------------------------------------------------------------------------
evidence.finished_at = new Date().toISOString();
evidence.failures = failures;
evidence.canonical_stats = await view(MAIN, "get_stats");
evidence.demo_stats = stats3;
note({ step: "done", failures });

console.log(`\n\x1b[1m${failures === 0 ? "\x1b[32mall checks passed" : `\x1b[31m${failures} check(s) failed`}\x1b[0m`);
console.log(`evidence written to docs/evidence.json`);
process.exit(failures === 0 ? 0 : 1);
