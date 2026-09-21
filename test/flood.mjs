/**
 * THE REJECTED BUG, STAGED ON A PUBLIC CHAIN, AGAINST THE DEPLOYED CONTRACT.
 *
 *   node flood.mjs
 *
 * WHAT THE REVIEWER FOUND. The probe used to fetch the newest ten transactions
 * of an ACCOUNT and filter them by signer. Inbound transfers the owner does not
 * control share that page, so ten of them arriving after the owner's own last
 * signature push it off the end. The filter then finds nothing, the verdict is
 * INACTIVE, and a living person's estate is released.
 *
 * `test_logic.py` proves the fix offline in about forty tests. This script
 * proves it where it counts: real transactions, a real explorer, real
 * validators, and real GEN locked in a deployed contract.
 *
 * THE SEQUENCE.
 *
 *   1. create a will on the demo instance, owner = a wallet we hold the key to,
 *      watching sepolia. This fixes the anchor at `last_heartbeat`.
 *   2. the owner SIGNS one transaction on sepolia, after the anchor. They are,
 *      by the contract's own definition, alive.
 *   3. a burner wallet then sends TWELVE transfers back to the owner — the
 *      flood. Every one is newer than the owner's signature, and not one of
 *      them is signed by the owner.
 *   4. ask BOTH endpoints the same question at the same instant, and show that
 *      they disagree: the legacy page says the owner has signed nothing, the
 *      outbound-only page says they signed after the anchor. One of those is
 *      the bug and the other is the fix.
 *   5. let the check-in threshold expire and call claim_inactive for real.
 *
 * The deployed contract must answer ALIVE and the deposit must not move. Under
 * the rejected code, step 4's first answer is the one it would have acted on.
 *
 * ON THE KEY. Read at run time out of an existing dev `.env`, never copied,
 * printed or committed, and asserted to hold zero balance on every mainnet
 * before anything is signed. The burner is generated fresh in memory each run
 * and its key never leaves this process. Nothing here touches a mainnet.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { createClient, createAccount } from "genlayer-js";
import { createWalletClient, createPublicClient, http } from "viem";
import { privateKeyToAccount, generatePrivateKey } from "viem/accounts";
import { sepolia } from "viem/chains";
import {
  CHAINS, argOf, accounts, connect, fundOnStudio, gen, sleep, returnedJson,
  retry, estimateWriteFees, outcomeOf,
} from "./harness.mjs";

const networkName = argOf("network", "studiodev");
const chain = CHAINS[networkName];
const deployments = JSON.parse(
  readFileSync(new URL("../deployments.json", import.meta.url), "utf8"),
).deployments[networkName];

const DEMO = deployments.WillExecutorDemo.address;
const WATCH = "sepolia";
const EXPLORER = "eth-sepolia.blockscout.com";
/*
 * FIVE MINUTES, NOT TWO, AND THE REASON IS WRITTEN DOWN BECAUSE IT COST A RUN.
 *
 * The threshold must not expire before Blockscout has indexed the transactions
 * this script just sent. The first attempt used 120s, claimed on a page whose
 * newest entry was five minutes stale, and got a perfectly honest INACTIVE — a
 * correct verdict on the evidence the explorer had, and a useless demonstration
 * of a fix about evidence that reaches further back. The gate in step 5 is the
 * real protection; this is the headroom that lets the gate do its job.
 */
const INTERVAL = 300;                // seconds, on the demo instance
const DEPOSIT = 5n * 10n ** 18n;
/*
 * TWENTY, NOT TWELVE. The legacy page holds ten, so twelve is only a margin of
 * two — and the wallet's own history already contains outbound transactions
 * from previous runs, any of which can sit inside that window. A run with
 * twelve produced a legacy page of "8 inbound, 2 signed", which does not bury
 * anything and so demonstrates nothing. The margin has to be comfortable.
 */
const FLOOD_N = 20;                  // must comfortably exceed the legacy page of ten
const L1_CHAIN = sepolia;
const L1_RPC = "https://ethereum-sepolia-rpc.publicnode.com";

const acc = accounts();
const read = createClient({ chain });

let failures = 0;
const ok = (w, d = "") => console.log(`  \x1b[32m✔\x1b[0m ${w}${d ? "  " + d : ""}`);
const bad = (w, d = "") => { failures++; console.log(`  \x1b[31m✗\x1b[0m ${w}${d ? "  " + d : ""}`); };
const head = (t) => console.log(`\n\x1b[1m${t}\x1b[0m`);

const evidence = { network: networkName, demo: DEMO, watch_chain: WATCH,
                   flood_size: FLOOD_N, started_at: new Date().toISOString(),
                   steps: [] };
function note(step) {
  evidence.steps.push(step);
  writeFileSync(new URL("../docs/flood-evidence.json", import.meta.url),
                JSON.stringify(evidence, null, 2) + "\n");
}

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

/* ------------------------------------------------------------------------ *
 * The two questions, asked of the two endpoints.
 *
 * `oldProbe` is the REJECTED algorithm, reimplemented here in full and run
 * against the live explorer. It is not a strawman and it is not a mock: it is
 * the URL the contract used to build and the filter it used to apply. Keeping
 * it in the script is what makes the comparison in step 4 mean anything — the
 * two answers come from the same wallet at the same instant, and differ only
 * in which page was fetched.
 * ------------------------------------------------------------------------ */
const legacyUrl = (addr) =>
  `https://${EXPLORER}/api?module=account&action=txlist&address=${addr.toLowerCase()}&sort=desc&page=1&offset=10`;
const v2Url = (addr) =>
  `https://${EXPLORER}/api/v2/addresses/${addr.toLowerCase()}/transactions?filter=from`;

async function oldProbe(addr, anchor) {
  const doc = await fetch(legacyUrl(addr)).then((r) => r.json()).catch(() => null);
  const items = doc?.result;
  if (!Array.isArray(items)) return { readable: false };
  const me = addr.toLowerCase();
  const mine = items.filter((t) => String(t.from ?? "").toLowerCase() === me);
  const since = mine.filter((t) => Number(t.timeStamp) > anchor);
  return {
    readable: true, page: items.length, signed: mine.length,
    inbound: items.length - mine.length, since_anchor: since.length,
    verdict: since.length > 0 ? "ALIVE" : "INACTIVE",
  };
}

async function newProbe(addr, anchor) {
  const res = await fetch(v2Url(addr)).catch(() => null);
  if (!res || res.status !== 200) return { readable: false };
  const doc = await res.json().catch(() => null);
  const items = doc?.items;
  if (!Array.isArray(items)) return { readable: false };
  const me = addr.toLowerCase();
  const mine = items.filter((t) => String(t.from?.hash ?? "").toLowerCase() === me);
  const ts = (t) => Math.floor(Date.parse(t.timestamp ?? 0) / 1000) || 0;
  const since = mine.filter((t) => ts(t) > anchor);
  return {
    readable: true, page: items.length, signed: mine.length,
    foreign: items.length - mine.length, since_anchor: since.length,
    newest: mine.reduce((m, t) => Math.max(m, ts(t)), 0),
    verdict: since.length > 0 ? "ALIVE" : "INACTIVE",
  };
}

console.log(`\n\x1b[1mWillExecutor — the inbound flood, on chain\x1b[0m`);
console.log(`  demo contract  ${DEMO}`);
console.log(`  will owner     ${OWNER}`);
console.log(`  watched chain  ${WATCH} (${EXPLORER})`);
console.log(`  flood size     ${FLOOD_N} inbound transfers (the legacy page holds 10)`);

// ---------------------------------------------------------------------------
head("0. Safety: the key must be testnet-only");
try {
  await assertTestnetOnly(OWNER);
  ok("zero balance on ethereum, base, arbitrum and polygon mainnets");
} catch (e) { bad(String(e.message)); process.exit(1); }

const l1Read = createPublicClient({ chain: L1_CHAIN, transport: http(L1_RPC) });
const l1Wallet = createWalletClient({
  account: privateKeyToAccount(OWNER_KEY), chain: L1_CHAIN, transport: http(L1_RPC),
});
const l1Balance = await l1Read.getBalance({ address: OWNER });
console.log(`  ${WATCH} balance ${gen(l1Balance)} ETH (testnet)`);
if (l1Balance < 3n * 10n ** 16n) { bad("not enough testnet gas for a flood"); process.exit(1); }

// ---------------------------------------------------------------------------
head("1. Clear the way, then create the will (this fixes the anchor)");
await fundOnStudio(chain, OWNER, 200n * 10n ** 18n);
const glWallet = createClient({ chain, account: glAccount });
const c = connect({ networkName, address: DEMO, role: "client" });

/*
 * One wallet may hold only one ACTIVE will, and this script needs a FRESH
 * anchor — an anchor inherited from an earlier run would already have the
 * owner's signature after it, and the flood would then be arriving after a
 * verdict rather than before one.
 */
const prior = JSON.parse(await c.view("get_wills_by_owner", [OWNER]))
  .wills.filter((w) => w.status === "ACTIVE");
for (const w of prior) {
  const fees = await estimateWriteFees(glWallet, {
    address: DEMO, functionName: "cancel_will", args: [w.will_id],
  });
  const h = await retry(() => glWallet.writeContract({
    address: DEMO, functionName: "cancel_will", args: [w.will_id],
    ...(fees ? { fees } : {}),
  }), { label: "cancel_will" });
  for (let i = 0; i < 120; i++) {
    await sleep(2500);
    try { if (outcomeOf(await read.getTransaction({ hash: h })).settled) break; } catch { /* poll */ }
  }
  console.log(`  cancelled prior will #${w.will_id} so this run starts from a clean anchor`);
}

const createArgs = [acc.heir3.address, INTERVAL, WATCH];
const createFees = await estimateWriteFees(glWallet, {
  address: DEMO, functionName: "create_will", args: createArgs, value: DEPOSIT,
});
const createHash = await retry(() => glWallet.writeContract({
  address: DEMO, functionName: "create_will", args: createArgs, value: DEPOSIT,
  ...(createFees ? { fees: createFees } : {}),
}), { label: "create_will" });
console.log(`  tx ${createHash}`);
for (let i = 0; i < 120; i++) {
  await sleep(2500);
  try { if (outcomeOf(await read.getTransaction({ hash: createHash })).settled) break; } catch { /* poll */ }
}

const mine = JSON.parse(await c.view("get_wills_by_owner", [OWNER]))
  .wills.filter((w) => w.status === "ACTIVE" && w.chain === WATCH);
const WILL_ID = mine[mine.length - 1]?.will_id;
if (!WILL_ID) { bad("no active will after create"); process.exit(1); }
const created = JSON.parse(await c.view("get_will", [WILL_ID])).will;
const ANCHOR = created.last_heartbeat;
ok(`will #${WILL_ID}: ${created.deposit_gen} GEN, ${INTERVAL}s interval, watching ${WATCH}`);
console.log(`  anchor (last_heartbeat) ${ANCHOR}`);
console.log(`  claimable at            ${created.claimable_at} (in ${created.seconds_until_claimable}s)`);
note({ step: "create_will", will_id: WILL_ID, tx: createHash, anchor: ANCHOR,
       claimable_at: created.claimable_at, deposit_wei: created.deposit_wei });

// ---------------------------------------------------------------------------
head("2. The owner signs — they are alive");
/*
 * This transaction FUNDS THE BURNER, which is what lets the burner send the
 * flood back. One transaction doing both jobs is not a shortcut: it is the
 * cleanest possible staging, because the owner's only post-anchor signature is
 * then unambiguously the oldest of the thirteen transactions that follow.
 */
const burnerKey = generatePrivateKey();
const burner = privateKeyToAccount(burnerKey);
const burnerWallet = createWalletClient({ account: burner, chain: L1_CHAIN, transport: http(L1_RPC) });
console.log(`  burner ${burner.address} (generated in memory, key never leaves this process)`);

const fundHash = await l1Wallet.sendTransaction({ to: burner.address, value: 2n * 10n ** 16n });
console.log(`  ${WATCH} tx ${fundHash}  (owner → burner)`);
const fundReceipt = await l1Read.waitForTransactionReceipt({ hash: fundHash, timeout: 240_000 });
const fundBlock = await l1Read.getBlock({ blockNumber: fundReceipt.blockNumber });
const SIGNED_AT = Number(fundBlock.timestamp);
ok(`mined in block ${fundReceipt.blockNumber}`, `status ${fundReceipt.status}`);
if (SIGNED_AT > ANCHOR) ok(`the owner's signature is AFTER the anchor`, `${SIGNED_AT} > ${ANCHOR}`);
else { bad(`the signature is not after the anchor (${SIGNED_AT} <= ${ANCHOR})`); process.exit(1); }
note({ step: "owner_signature", hash: fundHash, timestamp: SIGNED_AT,
       seconds_after_anchor: SIGNED_AT - ANCHOR });

// ---------------------------------------------------------------------------
head(`3. The flood: ${FLOOD_N} inbound transfers, all newer than that signature`);
/*
 * Sent with explicit nonces and without awaiting each receipt, so they land in
 * as few blocks as possible. Every one is signed by the BURNER and sent TO the
 * owner: inbound, and therefore no evidence whatsoever about whether the owner
 * still holds their key. That is the entire point — the contract must not
 * treat them as evidence in either direction.
 */
const startNonce = await l1Read.getTransactionCount({ address: burner.address });
const floodHashes = [];
for (let i = 0; i < FLOOD_N; i++) {
  const h = await burnerWallet.sendTransaction({
    to: OWNER, value: 10n ** 12n, nonce: startNonce + i,
  });
  floodHashes.push(h);
  process.stdout.write(`\r  sent ${i + 1}/${FLOOD_N}`);
}
console.log("");
const lastReceipt = await l1Read.waitForTransactionReceipt({
  hash: floodHashes[floodHashes.length - 1], timeout: 300_000,
});
const lastBlock = await l1Read.getBlock({ blockNumber: lastReceipt.blockNumber });
ok(`all ${FLOOD_N} mined, last in block ${lastReceipt.blockNumber}`,
   `${Number(lastBlock.timestamp) - SIGNED_AT}s after the owner's signature`);
note({ step: "flood", count: FLOOD_N, hashes: floodHashes,
       last_block: Number(lastReceipt.blockNumber),
       last_timestamp: Number(lastBlock.timestamp) });

// ---------------------------------------------------------------------------
head("4. Wait for the explorer to index the flood");
/*
 * DO NOT POLL TIGHTLY. This explorer answers roughly three requests per window
 * per IP, and a fast loop spends its whole allowance on 429s and then cannot
 * tell "not indexed yet" from "not allowed to ask".
 *
 * BUT DO GATE. A fixed wait is not enough and the first run of this script
 * proved it: 150 seconds elapsed, the explorer was still five minutes behind,
 * and the claim landed on a page that did not yet contain the owner's
 * signature. The contract answered INACTIVE, which was the correct reading of
 * the evidence available to it and told us nothing at all about the fix.
 *
 * So: wait once, then ask at a spacing the quota can absorb, and DO NOT CLAIM
 * until the owner's post-anchor signature is actually visible. If it never
 * becomes visible this script aborts without claiming — leaving the will
 * ACTIVE and claimable — because a demonstration that cannot see its own
 * premise has nothing to demonstrate.
 */
const SETTLE_S = 150;
console.log(`  waiting ${SETTLE_S}s before the first look — making no requests meanwhile`);
await sleep(SETTLE_S * 1000);

/*
 * TWO CONDITIONS, NOT ONE, AND THE SECOND ONE IS THE EXPERIMENT.
 *
 *   1. the owner's signature must be indexed — otherwise the premise is
 *      missing and a claim would get an honest INACTIVE that proves nothing;
 *   2. the flood must ALSO be indexed, far enough that the legacy page no
 *      longer contains that signature — otherwise there is no burial to show,
 *      and the comparison in step 5 has nothing to compare.
 *
 * An earlier run waited only for (1) and asked at once. Eight of the twelve
 * transfers had landed, the legacy page still showed the owner's signature,
 * and the rejected probe would have answered ALIVE too. A demonstration that
 * happens to catch the explorer mid-index demonstrates the explorer, not the
 * contract.
 */
let newAnswer = null;
let oldAnswer = null;
for (let i = 1; i <= 7; i++) {
  const look = newAnswer ?? await newProbe(OWNER, ANCHOR);
  if (look.readable && look.since_anchor > 0) {
    if (!newAnswer) ok(`the owner's signature is indexed`, `newest ${look.newest} > anchor ${ANCHOR}`);
    newAnswer = look;
    await sleep(8000);               // one breath between requests, for the quota
    const buried = await oldProbe(OWNER, ANCHOR);
    if (buried.readable && buried.signed === 0) {
      oldAnswer = buried;
      ok(`the flood has buried it`, `the legacy page is ${buried.inbound} inbound, 0 signed`);
      break;
    }
    if (buried.readable) {
      oldAnswer = buried;            // keep the most recent reading either way
      console.log(`      check ${i}: legacy page still shows ${buried.signed} signature(s) — flood not fully indexed`);
    } else {
      console.log(`      check ${i}: the legacy endpoint did not answer (rate limited)`);
    }
  } else if (!look.readable) {
    console.log(`      check ${i}: the explorer did not answer (rate limited — NOT "no transaction")`);
  } else {
    console.log(`      check ${i}: indexed up to ${look.newest}, need > ${ANCHOR}`);
  }
  if (i < 7) await sleep(90_000);
}
if (!newAnswer) {
  bad("the explorer never indexed the owner's post-anchor signature");
  console.log(`      Aborting WITHOUT claiming. The will is still ACTIVE and still claimable,`);
  console.log(`      and nothing has been proved either way — which is the honest outcome when`);
  console.log(`      the premise of the experiment cannot be observed.`);
  note({ step: "indexing", indexed: false, claimed: false });
  process.exit(1);
}
note({ step: "indexing", indexed: true, newest_signed: newAnswer.newest,
       seconds_after_anchor: newAnswer.newest - ANCHOR,
       buried: oldAnswer?.signed === 0 });

// ---------------------------------------------------------------------------
head("5. THE SAME QUESTION, THE TWO ENDPOINTS, THE SAME INSTANT");
if (!oldAnswer) oldAnswer = await oldProbe(OWNER, ANCHOR);

console.log(`\n  the REJECTED probe — GET /api?…&action=txlist&…&offset=10`);
if (oldAnswer.readable) {
  console.log(`      page of ${oldAnswer.page}: ${oldAnswer.inbound} inbound, ${oldAnswer.signed} signed by the owner`);
  console.log(`      signatures after the anchor: ${oldAnswer.since_anchor}`);
  console.log(`      \x1b[31mwould have concluded ${oldAnswer.verdict}\x1b[0m`);
} else console.log(`      the explorer did not answer`);

console.log(`\n  the DEPLOYED probe — GET /api/v2/addresses/{owner}/transactions?filter=from`);
if (newAnswer.readable) {
  console.log(`      page of ${newAnswer.page}: ${newAnswer.foreign} not outbound, ${newAnswer.signed} signed by the owner`);
  console.log(`      signatures after the anchor: ${newAnswer.since_anchor}  (newest ${newAnswer.newest})`);
  console.log(`      \x1b[32mconcludes ${newAnswer.verdict}\x1b[0m`);
} else console.log(`      the explorer did not answer`);
console.log("");

if (oldAnswer.readable && oldAnswer.signed === 0 && oldAnswer.verdict === "INACTIVE") {
  ok("the rejected probe is blind here — the owner's signature is off its page");
} else if (oldAnswer.readable) {
  bad(`the legacy page still shows ${oldAnswer.signed} signature(s) — the flood did not bury it, so this run does not demonstrate the bug`);
} else {
  bad("the legacy endpoint was rate limited; the comparison is unavailable this run");
}
if (newAnswer.readable && newAnswer.foreign === 0) {
  ok("filter=from was honoured — every returned item is outbound");
} else if (newAnswer.readable) {
  // Reported, not failed. The contract does not depend on the filter having
  // worked: a mixed page simply has to reach back past the anchor before it is
  // allowed to answer, and if it cannot the round returns INCONCLUSIVE.
  console.log(`  \x1b[33m-\x1b[0m the explorer returned ${newAnswer.foreign} non-outbound items — the round will fall back to proving coverage`);
}
if (newAnswer.readable && newAnswer.since_anchor > 0) {
  ok("the deployed probe sees the signature the rejected one missed");
} else if (newAnswer.readable) {
  bad("the outbound page shows no signature after the anchor — the flood test cannot prove anything");
}
note({ step: "endpoint_comparison", anchor: ANCHOR,
       rejected_probe: { url: legacyUrl(OWNER), ...oldAnswer },
       deployed_probe: { url: v2Url(OWNER), ...newAnswer } });

// ---------------------------------------------------------------------------
head("6. Let the check-in threshold expire");
const pending = JSON.parse(await c.view("get_will", [WILL_ID])).will;
if (pending.seconds_until_claimable > 0) {
  const wait = pending.seconds_until_claimable + 10;
  console.log(`  waiting ${wait}s — the owner is deliberately NOT checking in`);
  await sleep(wait * 1000);
}
const ready = JSON.parse(await c.view("get_will", [WILL_ID])).will;
if (ready.is_claimable) ok("the will is past its threshold and anyone may claim it");
else bad(`still not claimable (${ready.seconds_until_claimable}s to go)`);

// ---------------------------------------------------------------------------
head("7. claim_inactive — with real validators, through the flood");
const lockedBefore = JSON.parse(await c.view("get_stats")).ledger.locked_wei;
/*
 * SNAPSHOT, DON'T ASSUME ZERO.
 *
 * Payouts on this contract are PULLED, so the beneficiary's and finder's
 * ledger balances accumulate across runs and are not zero on a demo instance
 * that has settled anything before. An earlier run of this very script
 * released a will (correctly, on an explorer that was five minutes behind),
 * and asserting an absolute zero here reported that as a failure of THIS
 * round. What this round has to prove is that it moved nothing — so the
 * comparison is before against after.
 */
const owedHeirBefore = JSON.parse(await c.view("payout_of", [acc.heir3.address])).owed_wei;
const owedFinderBefore = JSON.parse(await c.view("payout_of", [acc.finder.address])).owed_wei;
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
  console.log(`  attempt ${attempt}: ${outcome} in ${out.seconds.toFixed(0)}s  (src_ok=${will.evidence.src_ok}, cov_ok=${will.evidence.cov_ok})`);
  claim = out;
  if (outcome !== "INCONCLUSIVE") break;
  if (attempt < 6) {
    const w = 60_000 + attempt * 30_000;
    console.log(`      explorer unreadable — waiting ${w / 1000}s and asking again`);
    await sleep(w);
  }
}
note({ step: "claim_attempts", attempts });

// ---------------------------------------------------------------------------
head("8. The verdict, and that nothing moved");
const ev = will.evidence;
console.log(`      activity_status ${ev.activity_status}`);
console.log(`      age_bucket      ${ev.age_bucket}  (${ev.age_meaning})`);
console.log(`      count_bucket    ${ev.count_bucket}  (${ev.count_meaning})`);
console.log(`      src_ok          ${ev.src_ok}`);
console.log(`      cov_ok          ${ev.cov_ok}`);
console.log(`      content_hash    ${ev.content_hash}`);
console.log(`      reason          ${ev.reason}`);

if (outcome === "ALIVE") ok("VERDICT: ALIVE — the flood did not hide the owner's signature");
else bad(`verdict was ${outcome}, expected ALIVE — THE BUG IS NOT FIXED`);
if (ev.cov_ok === true) ok("cov_ok is true — the evidence reached the question it was asked");
else bad("cov_ok is false");
if (ev.src_ok === true) ok("src_ok is true — the verdict is evidence-backed");
else bad("src_ok is false");
if (ev.count_bucket > 0) ok(`count_bucket is ${ev.count_bucket} — signatures were counted, not assumed`);
else bad("count_bucket is 0, so no signature was seen");

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
if (owedHeir === owedHeirBefore && owedFinder === owedFinderBefore) {
  ok("this round credited nobody — the beneficiary's and finder's ledgers are unchanged",
     `heir ${gen(BigInt(owedHeir))}, finder ${gen(BigInt(owedFinder))} GEN, both carried in from earlier runs`);
} else {
  bad(`a payout was credited: heir ${owedHeirBefore} → ${owedHeir}, finder ${owedFinderBefore} → ${owedFinder}`);
}

const stats = JSON.parse(await c.view("get_stats"));
if (stats.ledger.identity_holds) ok("ledger identity still holds");
else bad("LEDGER IDENTITY BROKEN", JSON.stringify(stats.ledger));

const verify = JSON.parse(await c.view("verify_claim", [WILL_ID]));
const passed = (verify.checks ?? []).filter((x) => x.ok).length;
if (passed === (verify.checks ?? []).length && passed > 0) {
  ok(`verify_claim re-derives all ${passed} checks from storage alone`);
} else bad(`verify_claim: ${passed}/${(verify.checks ?? []).length} checks passed`);
console.log(`      projection: ${verify.canonical_projection ?? ""}`);

note({ step: "verdict", outcome, cov_ok: ev.cov_ok, src_ok: ev.src_ok,
       will_id: WILL_ID, claim_tx: claim?.hash, evidence: ev,
       locked_before: lockedBefore, locked_after: lockedAfter,
       owed_heir_before: owedHeirBefore, owed_heir_after: owedHeir,
       owed_finder_before: owedFinderBefore, owed_finder_after: owedFinder,
       verify_checks_passed: passed, released: will.status !== "ACTIVE" });

evidence.finished_at = new Date().toISOString();
evidence.failures = failures;
note({ step: "done", failures });

if (failures === 0) {
  console.log(`\n\x1b[1m\x1b[32mThe inbound flood was survived on chain — the estate stayed locked\x1b[0m`);
} else {
  console.log(`\n\x1b[1m\x1b[31m${failures} check(s) failed\x1b[0m`);
}
console.log(`evidence written to docs/flood-evidence.json`);
process.exit(failures === 0 ? 0 : 1);
