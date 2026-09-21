/**
 * The ALIVE path, demonstrated on chain with real validators.
 *
 *   node alive.mjs
 *
 * THE PROBLEM THIS SOLVES. `docs/EVIDENCE.md` §7 recorded ALIVE as *not*
 * demonstrated on chain, for a real reason: the wallet the validators probe is
 * the will's OWNER (rule: wallet = identity, so nobody can point the probe at a
 * wallet they do not control), and `create_will` has to be signed by that
 * owner. Proving ALIVE therefore needs one wallet that BOTH holds a signable
 * key AND has genuine outbound history on an allowlisted chain — and every
 * wallet in the main demo is a freshly generated key with no history anywhere,
 * deliberately, because that is what makes the INACTIVE reading genuine.
 *
 * THE RESOLUTION. `base-sepolia` is in the allowlist, and a testnet wallet can
 * be made to satisfy both conditions at once. The sequence is:
 *
 *   1. create a will on the demo instance, owner = a wallet we hold the key to,
 *      watching base-sepolia. This fixes the anchor at `last_heartbeat`.
 *   2. send a REAL transaction from that wallet on Base Sepolia — so it is
 *      signed by the owner, and stamped AFTER the anchor.
 *   3. wait for Blockscout to index it, polling the exact URL the contract
 *      builds, so we never claim before the evidence the validators will read
 *      actually exists.
 *   4. let the check-in threshold expire and call claim_inactive.
 *
 * The validators then independently fetch that wallet's history, see a
 * signature newer than the anchor, and must agree on ALIVE. The estate stays
 * locked even though the owner missed every check-in — which is the entire
 * thesis of the contract, and the half a timestamp-only dead man's switch
 * cannot do.
 *
 * ON THE KEY. It is read at run time out of an existing dev `.env` and never
 * copied, printed or committed. It is a testnet-only key: zero balance on every
 * mainnet, funded only on Sepolia and Base Sepolia. The Base Sepolia
 * transaction it sends costs testnet gas and nothing else. Nothing in this
 * script touches a mainnet.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { createClient, createAccount } from "genlayer-js";
import { createWalletClient, createPublicClient, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { sepolia } from "viem/chains";
import {
  CHAINS, argOf, accounts, connect, fundOnStudio, gen, sleep, returnedJson, retry,
} from "./harness.mjs";

const networkName = argOf("network", "studiodev");
const chain = CHAINS[networkName];
const deployments = JSON.parse(
  readFileSync(new URL("../deployments.json", import.meta.url), "utf8"),
).deployments[networkName];

const DEMO = deployments.WillExecutorDemo.address;

/*
 * SEPOLIA RATHER THAN BASE SEPOLIA, and the reason belongs in the record.
 *
 * The first attempt at this ran on base-sepolia and worked — but the polling
 * loop that waits for Blockscout to index the transaction asked every six
 * seconds, tripped that explorer's rate limit, and then sat reading 429s. Both
 * chains are in the contract's allowlist and the wallet is funded on both, so
 * the demonstration simply moved to the explorer that had not been hammered.
 *
 * It is worth being precise about what that does and does not say. It is an
 * operational fact about a public explorer's quota, not a property of the
 * contract: a 429 carries `result: null`, which `_parse` refuses, so the round
 * returns INCONCLUSIVE and NOTHING MOVES. Being unable to read the evidence
 * never becomes a verdict about the owner.
 */
const WATCH = "sepolia";
const EXPLORER = "eth-sepolia.blockscout.com";
const INTERVAL = 90;                 // seconds, on the demo instance
const DEPOSIT = 6n * 10n ** 18n;
const L1_CHAIN = sepolia;
const L1_RPC = "https://ethereum-sepolia-rpc.publicnode.com";

const acc = accounts();
const read = createClient({ chain });

let failures = 0;
const ok = (w, d = "") => console.log(`  \x1b[32m✔\x1b[0m ${w}${d ? "  " + d : ""}`);
const bad = (w, d = "") => { failures++; console.log(`  \x1b[31m✗\x1b[0m ${w}${d ? "  " + d : ""}`); };
const head = (t) => console.log(`\n\x1b[1m${t}\x1b[0m`);

const evidence = { network: networkName, demo: DEMO, watch_chain: WATCH,
                   started_at: new Date().toISOString(), steps: [] };
function note(step) {
  evidence.steps.push(step);
  writeFileSync(new URL("../docs/alive-evidence.json", import.meta.url),
                JSON.stringify(evidence, null, 2) + "\n");
}

/**
 * The owner key, read from an existing dev .env at run time.
 *
 * Located by DERIVING each 64-hex candidate and matching the address, so the
 * file's layout does not matter and no key is ever compared or logged. Refuses
 * to proceed if the wallet has a balance on any mainnet — this script is only
 * ever allowed to drive a testnet-only key.
 */
const TARGET = "0xBe9eE23694b69d287FbE096AB3E37f61Cff7B802";
const ENV_FILE = "/Users/kenilvakariya/Desktop/base-game/snake-arena/.env";

function ownerKey() {
  const txt = readFileSync(ENV_FILE, "utf8");
  for (const m of txt.matchAll(/(0x)?([0-9a-fA-F]{64})/g)) {
    const key = `0x${m[2]}`;
    try {
      if (privateKeyToAccount(key).address.toLowerCase() === TARGET.toLowerCase()) return key;
    } catch { /* not a key */ }
  }
  throw new Error(`no key deriving ${TARGET} found in ${ENV_FILE}`);
}

async function assertTestnetOnly(address) {
  for (const host of ["eth.blockscout.com", "base.blockscout.com",
                      "arbitrum.blockscout.com", "polygon.blockscout.com"]) {
    const res = await fetch(
      `https://${host}/api?module=account&action=balance&address=${address}`,
    ).then((r) => r.json()).catch(() => null);
    const bal = BigInt(res?.result ?? "0");
    if (bal > 0n) throw new Error(`${address} holds ${bal} wei on ${host} — refusing to use a mainnet-funded key`);
  }
}

const OWNER_KEY = ownerKey();
const glAccount = createAccount(OWNER_KEY);
const OWNER = glAccount.address;

console.log(`\n\x1b[1mWillExecutor — the ALIVE path, on chain\x1b[0m`);
console.log(`  demo contract  ${DEMO}`);
console.log(`  will owner     ${OWNER}`);
console.log(`  watched chain  ${WATCH} (${EXPLORER})`);

// ---------------------------------------------------------------------------
head("0. Safety: the key must be testnet-only");
try {
  await assertTestnetOnly(OWNER);
  ok("zero balance on ethereum, base, arbitrum and polygon mainnets");
} catch (e) {
  bad(String(e.message));
  process.exit(1);
}

const baseRead = createPublicClient({ chain: L1_CHAIN, transport: http(L1_RPC) });
const baseWallet = createWalletClient({
  account: privateKeyToAccount(OWNER_KEY), chain: L1_CHAIN, transport: http(L1_RPC),
});
const baseBalance = await baseRead.getBalance({ address: OWNER });
console.log(`  ${WATCH} balance ${gen(baseBalance)} ETH (testnet)`);
if (baseBalance < 10n ** 14n) { bad("not enough testnet gas to send a transaction"); process.exit(1); }

/**
 * The exact URL the contract builds — so we poll what the validators will read.
 *
 * `?filter=from` is the fix this run exists to demonstrate: the page is
 * OUTBOUND ONLY, server-side, so the owner's signature cannot be pushed out of
 * view by inbound traffic they do not control. Polling the old `txlist` URL
 * here would be polling a different question from the one the round asks.
 */
const probeUrl = (addr) =>
  `https://${EXPLORER}/api/v2/addresses/${addr.toLowerCase()}/transactions?filter=from`;

/** Newest transaction SIGNED BY this wallet, as the contract counts them. */
async function newestSigned(addr) {
  const res = await fetch(probeUrl(addr)).catch(() => null);
  if (!res || res.status !== 200) return { readable: false, newest: 0, signed: 0, foreign: 0 };
  const doc = await res.json().catch(() => null);
  const items = doc?.items;
  // `items` is a LIST when the explorer answered (even an empty one) and
  // absent when it refused — the same distinction the contract makes in
  // `_parse_v2`, and the reason a 429 or a 422 can never read as "this wallet
  // is dormant".
  if (!Array.isArray(items)) return { readable: false, newest: 0, signed: 0, foreign: 0 };
  const me = addr.toLowerCase();
  const mine = items.filter((t) => String(t.from?.hash ?? "").toLowerCase() === me);
  const newest = mine.reduce(
    (m, t) => Math.max(m, Math.floor(Date.parse(t.timestamp ?? 0) / 1000) || 0), 0);
  // The contract verifies the filter rather than trusting it, and so does this.
  // A non-zero count here would mean the explorer ignored `filter=from`, and
  // the round would fall back to proving coverage instead of taking the
  // outbound-only shortcut.
  return { readable: true, newest, signed: mine.length, foreign: items.length - mine.length };
}

// ---------------------------------------------------------------------------
head("1. Where the wallet stands before the will exists");
const before = await newestSigned(OWNER);
console.log(`  explorer readable ${before.readable}, ${before.signed} signed tx in the window`);
console.log(`  filter=from honoured: ${before.foreign === 0} (${before.foreign} non-outbound items returned)`);
console.log(`  newest signature  ${before.newest} (${new Date(before.newest * 1000).toISOString()})`);
// Deliberately NOT asserted here: on a resumed run the post-anchor signature
// already exists, and claiming otherwise would be describing a state this
// script had itself already changed. The anchor comparison that matters is
// made in step 3, against the will's real `last_heartbeat`.
console.log(`  (whether that counts as ALIVE depends entirely on the anchor, which step 2 fixes)`);
note({ step: "before", newest_signed: before.newest, signed_count: before.signed, foreign_items: before.foreign, probe_url: probeUrl(OWNER) });

// ---------------------------------------------------------------------------
head("2. Create the will (this fixes the anchor)");
await fundOnStudio(chain, OWNER, 200n * 10n ** 18n);
const glWallet = createClient({ chain, account: glAccount });
const c = connect({ networkName, address: DEMO, role: "client" });

/*
 * RESUMABLE. One wallet may hold only one ACTIVE will, so a re-run has to pick
 * up the one it already made rather than be refused by its own earlier
 * success. This also means the expensive half — a real Base Sepolia
 * transaction and the wait for Blockscout to index it — is never repeated
 * needlessly.
 */
const existing = JSON.parse(await c.view("get_wills_by_owner", [OWNER]))
  .wills.find((w) => w.status === "ACTIVE" && w.chain === WATCH);

const { estimateWriteFees, outcomeOf } = await import("./harness.mjs");
let createHash = null;

if (existing) {
  console.log(`  reusing the will this script already created`);
  ok(`will #${existing.will_id} is ACTIVE, watching ${WATCH}`);
} else {
  // The will must be signed BY the owner, so this call uses its own client.
  const args = [acc.heir3.address, INTERVAL, WATCH];
  const fees = await estimateWriteFees(glWallet, {
    address: DEMO, functionName: "create_will", args, value: DEPOSIT,
  });
  createHash = await retry(() => glWallet.writeContract({
    address: DEMO, functionName: "create_will", args, value: DEPOSIT,
    ...(fees ? { fees } : {}),
  }), { label: "create_will" });
  console.log(`  tx ${createHash}`);

  let createTx = null;
  for (let i = 0; i < 120; i++) {
    await sleep(2500);
    try {
      createTx = await read.getTransaction({ hash: createHash });
      if (outcomeOf(createTx).settled) break;
    } catch { /* blind poll */ }
  }
  const createOut = outcomeOf(createTx);
  if (!createOut.ok) { bad("create_will failed", createOut.revertReason); process.exit(1); }
}

const wills = JSON.parse(await c.view("get_wills_by_owner", [OWNER]));
const WILL_ID = wills.wills.find((w) => w.status === "ACTIVE" && w.chain === WATCH)?.will_id;
if (!WILL_ID) { bad("no active will for this owner after create"); process.exit(1); }
const willAfterCreate = JSON.parse(await c.view("get_will", [WILL_ID])).will;
const ANCHOR = willAfterCreate.last_heartbeat;
ok(`will #${WILL_ID}: ${willAfterCreate.deposit_gen} GEN, ${INTERVAL}s interval, watching ${WATCH}`);
console.log(`  anchor (last_heartbeat) ${ANCHOR}`);
console.log(`  claimable at            ${willAfterCreate.claimable_at} (in ${willAfterCreate.seconds_until_claimable}s)`);
/*
 * On a FRESH run this is the check that makes the result mean anything: if the
 * wallet already had a signature newer than the anchor, ALIVE would prove
 * nothing about the validators — the answer would have been baked in before
 * the will existed.
 *
 * On a RESUMED run the post-anchor signature is there precisely because this
 * script put it there, so asserting its absence would be asserting that an
 * earlier step had not happened.
 */
if (existing) {
  console.log(`  (resumed run — the post-anchor signature is one this script already sent)`);
} else if (before.newest < ANCHOR) {
  ok("every existing signature is OLDER than the anchor — nothing here yet says ALIVE");
} else {
  bad("a pre-existing signature is already newer than the anchor; the test would prove nothing");
}
note({ step: "create_will", will_id: WILL_ID, tx: createHash, reused: Boolean(existing),
       anchor: ANCHOR, claimable_at: willAfterCreate.claimable_at,
       deposit_wei: willAfterCreate.deposit_wei });

// ---------------------------------------------------------------------------
head(`3. The owner signs a real transaction on ${WATCH}`);
/*
 * The timestamp is read from BLOCKSCOUT, not from the RPC.
 *
  * `eth_getBlock` against a public testnet endpoint is load balanced, and
 * a node that has not yet caught up answers BlockNotFoundError for a block it
 * just gave us a receipt from — which killed an earlier run after the
 * transaction had already been mined. Blockscout is also the source the
 * validators actually read, so taking the timestamp from there is the more
 * faithful measurement as well as the more robust one.
 */
const already = await newestSigned(OWNER);
let baseTx = null;
let txTime = already.newest;

if (already.readable && already.newest > ANCHOR) {
  ok(`a signature newer than the anchor is already on chain and indexed`,
     `${already.newest} > ${ANCHOR}`);
  console.log(`  no new transaction needed`);
} else {
  // A 1-wei self-send: the cheapest thing that is unambiguously a signature by
  // this wallet, which is the only kind of evidence the contract accepts.
  baseTx = await baseWallet.sendTransaction({ to: OWNER, value: 1n });
  console.log(`  ${WATCH} tx ${baseTx}`);
  const receipt = await baseRead.waitForTransactionReceipt({ hash: baseTx, timeout: 180_000 });
  ok(`mined in block ${receipt.blockNumber}`, `status ${receipt.status}`);
  txTime = 0;  // resolved from Blockscout in step 4
}
note({ step: "l1_tx", chain: WATCH, hash: baseTx, timestamp: txTime,
       reused_existing: baseTx === null });

// ---------------------------------------------------------------------------
head("4. Wait for Blockscout to index it");
// Claiming before the explorer can see the transaction would produce an honest
// INACTIVE and prove nothing. Poll the exact URL the contract builds.
/*
 * DO NOT POLL. SPEND THE REQUEST BUDGET ONCE.
 *
 * Measured the hard way, twice: this explorer answers roughly three requests
 * per window per IP, so a polling loop — even at 25-second intervals — spends
 * its whole allowance on 429s and then cannot tell "not indexed yet" from
 * "not allowed to ask". The wait for the will's check-in threshold is dead
 * time anyway, so the right move is to wait it out in silence and then ask
 * once.
 *
 * This matters beyond tidiness: the requests the VALIDATORS make during the
 * round come from their own address range, but a demo that exhausts a quota
 * before the round even starts is a demo that measures its own impatience
 * instead of the contract.
 */
let unreadable = 0;
let indexed = null;
if (already.readable && already.newest > ANCHOR) {
  // Step 3 already confirmed it with the one request it spent. Asking again
  // would only burn quota the round itself may need.
  indexed = already;
  console.log(`  already confirmed in step 3 — no further requests needed`);
} else {
  const SETTLE_S = 150;
  console.log(`  waiting ${SETTLE_S}s for the explorer to index it — deliberately making no requests meanwhile`);
  await sleep(SETTLE_S * 1000);
}

for (let i = 1; indexed === null && i <= 5; i++) {
  const now = await newestSigned(OWNER);
  if (now.readable && now.newest > ANCHOR) { indexed = now; break; }
  if (!now.readable) {
    unreadable++;
    console.log(`      the explorer did not answer (check ${i}) — rate limited, which is NOT "no transaction"`);
  } else {
    console.log(`      indexed up to ${now.newest}, need > ${ANCHOR} (check ${i})`);
  }
  if (i < 5) await sleep(75_000);
}
if (indexed) {
  txTime = indexed.newest;
  ok(`the explorer reports a signature newer than the anchor`,
     `${indexed.newest} > ${ANCHOR}, ${indexed.signed} signed tx in the window`);
  console.log(`      that is ${indexed.newest - ANCHOR}s after the will was created`);
} else if (unreadable > 0) {
  /*
   * THIS IS A WARNING, NOT A FAILURE, AND THE DISTINCTION IS THE WHOLE POINT.
   *
   * What is rate limited here is THIS MACHINE's view of the explorer. The
   * validators fetch from their own address range during the round, so the
   * fact that a laptop cannot currently read Blockscout says nothing about
   * whether they can. Aborting here would be letting the observer's quota
   * decide the experiment.
   *
   * Proceeding is safe because the claim cannot silently do the wrong thing:
   * if the validators also cannot read the explorer they return INCONCLUSIVE
   * and nothing moves, and if the transaction genuinely is not indexed yet
   * they return INACTIVE — which this script asserts against and would report
   * as a failure. Either way the verdict is theirs, not this script's.
   */
  console.log(`  \x1b[33m-\x1b[0m could not confirm locally (${unreadable} rate-limited checks).`);
  console.log(`      The transaction IS mined; proceeding, because the validators read from`);
  console.log(`      their own address range and this machine's quota is not theirs.`);
  note({ step: "indexing", indexed: false, unreadable_checks: unreadable,
         proceeded_anyway: true });
} else {
  bad(`the explorer answered but shows no post-anchor signature`);
  note({ step: "indexing", indexed: false, unreadable_checks: 0 });
  process.exit(1);
}
note({ step: "indexing", indexed: true, newest_signed: indexed.newest,
       signed_count: indexed.signed, seconds_after_anchor: indexed.newest - ANCHOR });

// ---------------------------------------------------------------------------
head("5. Let the check-in threshold expire");
const pending = JSON.parse(await c.view("get_will", [WILL_ID])).will;
if (pending.seconds_until_claimable > 0) {
  const wait = pending.seconds_until_claimable + 10;
  console.log(`  waiting ${wait}s — the owner is deliberately NOT checking in`);
  await sleep(wait * 1000);
}
const ready = JSON.parse(await c.view("get_will", [WILL_ID])).will;
if (ready.is_claimable) ok("the will is now past its threshold and anyone may claim it");
else bad(`the will is still not claimable (${ready.seconds_until_claimable}s to go)`);

// ---------------------------------------------------------------------------
head("6. claim_inactive — with real validators");
const lockedBefore = JSON.parse(await c.view("get_stats")).ledger.locked_wei;
const finder = connect({ networkName, address: DEMO, role: "finder" });

let claim = null, will = null, outcome = null;
const attempts = [];
for (let attempt = 1; attempt <= 6; attempt++) {
  const out = await finder.send("claim_inactive", [WILL_ID], 0n);
  const json = returnedJson(out);
  will = JSON.parse(await c.view("get_will", [WILL_ID])).will;
  outcome = json?.outcome ?? will.evidence.activity_status;
  attempts.push({ attempt, outcome, seconds: out.seconds, tx: out.hash,
                  src_ok: will.evidence.src_ok, cov_ok: will.evidence.cov_ok });
  console.log(`  attempt ${attempt}: ${outcome} in ${out.seconds.toFixed(0)}s  (src_ok=${will.evidence.src_ok})`);
  claim = out;
  if (outcome !== "INCONCLUSIVE") break;
  // The explorer rate-limits a burst of validator fetches; wait it out.
  if (attempt < 6) {
    const w = 60_000 + attempt * 30_000;
    console.log(`      explorer unreadable — waiting ${w / 1000}s and asking again`);
    await sleep(w);
  }
}
note({ step: "claim_attempts", attempts });

// ---------------------------------------------------------------------------
head("7. The verdict, and that nothing moved");
const ev = will.evidence;
console.log(`      activity_status ${ev.activity_status}`);
console.log(`      age_bucket      ${ev.age_bucket}  (${ev.age_meaning})`);
console.log(`      count_bucket    ${ev.count_bucket}  (${ev.count_meaning})`);
console.log(`      src_ok          ${ev.src_ok}`);
console.log(`      cov_ok          ${ev.cov_ok}`);
console.log(`      content_hash    ${ev.content_hash}`);
console.log(`      reason          ${ev.reason}`);

if (outcome === "ALIVE") ok("VERDICT: ALIVE — the validators found a signature newer than the anchor");
else bad(`verdict was ${outcome}, expected ALIVE`);

if (ev.count_bucket > 0) ok(`count_bucket is ${ev.count_bucket} — signatures were actually counted, not assumed`);
else bad("count_bucket is 0, so no signature was seen");
if (ev.age_bucket === 0) ok("age_bucket is 0 — the newest signature is less than a day old");
else console.log(`  \x1b[33m-\x1b[0m age_bucket is ${ev.age_bucket}`);
if (ev.src_ok === true) ok("src_ok is true — the verdict is evidence-backed");
else bad("src_ok is false");
if (ev.cov_ok === true) ok("cov_ok is true — the evidence reached the question it was asked");
else bad("cov_ok is false");

// The point of the whole exercise: the money did not move.
const lockedAfter = JSON.parse(await c.view("get_stats")).ledger.locked_wei;
const owedHeir = JSON.parse(await c.view("payout_of", [acc.heir3.address])).owed_wei;
const owedFinder = JSON.parse(await c.view("payout_of", [acc.finder.address])).owed_wei;

if (will.status === "ACTIVE") ok("the will is still ACTIVE — it was not executed");
else bad(`the will status is ${will.status}`);
if (will.deposit_wei === String(DEPOSIT)) ok(`the deposit is untouched: ${gen(DEPOSIT)} GEN still locked`);
else bad(`deposit is now ${will.deposit_wei}, was ${DEPOSIT}`);
if (will.settlement.paid_beneficiary_wei === "0" && will.settlement.paid_finder_wei === "0") {
  ok("nothing was paid to the beneficiary and nothing to the finder");
} else bad("a settlement was recorded", JSON.stringify(will.settlement));
if (lockedAfter === lockedBefore) ok(`the contract's locked total is unchanged (${gen(BigInt(lockedBefore))} GEN)`);
else bad(`locked went ${lockedBefore} → ${lockedAfter}`);
if (owedHeir === "0") ok("the named beneficiary is owed nothing");
else bad(`beneficiary is owed ${owedHeir}`);

const stats = JSON.parse(await c.view("get_stats"));
if (stats.ledger.identity_holds) ok("ledger identity still holds");
else bad("LEDGER IDENTITY BROKEN");
console.log(`      verdict counters: alive=${stats.verdicts.alive} inactive=${stats.verdicts.inactive} inconclusive=${stats.verdicts.inconclusive}`);

const verified = JSON.parse(await c.view("verify_claim", [WILL_ID]));
if (verified.verified) ok(`verify_claim re-derives all ${verified.checks.length} checks from storage alone`);
else bad("verify_claim failed", JSON.stringify(verified.checks));
console.log(`      projection: ${verified.canonical_projection}`);

note({ step: "verdict", cov_ok: ev.cov_ok, outcome, will_id: WILL_ID, claim_tx: claim?.hash,
       evidence: ev, settlement: will.settlement, status: will.status,
       deposit_wei: will.deposit_wei, locked_before: lockedBefore,
       locked_after: lockedAfter, owed_beneficiary: owedHeir, owed_finder: owedFinder,
       verified: verified.verified, canonical_projection: verified.canonical_projection,
       stats });

evidence.finished_at = new Date().toISOString();
evidence.failures = failures;
note({ step: "done", failures });

console.log(`\n\x1b[1m${failures === 0 ? "\x1b[32mALIVE demonstrated on chain — the estate stayed locked" : `\x1b[31m${failures} check(s) failed`}\x1b[0m`);
console.log(`evidence written to docs/alive-evidence.json`);
process.exit(failures === 0 ? 0 : 1);
