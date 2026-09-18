/**
 * Resets the ALIVE demonstration so `alive.mjs` can run fresh.
 *
 *   node alive-reset.mjs
 *
 * `alive.mjs` is resumable, which is right for recovering a half-finished run
 * but wrong for producing EVIDENCE: a resumed run reuses a will whose
 * post-anchor signature it had already sent, so the ordering that makes the
 * result meaningful — anchor first, signature second — is not visible in that
 * run's own log.
 *
 * This cancels the owner's active will and withdraws the refund, leaving the
 * wallet free to create a new one. The next `alive.mjs` then does every step in
 * order, in one log, against a brand-new anchor.
 *
 * The key is read at run time from an existing dev .env and never copied,
 * printed or committed; see the note at the top of alive.mjs.
 */
import { readFileSync } from "node:fs";
import { privateKeyToAccount } from "viem/accounts";
import { createClient, createAccount } from "genlayer-js";
import { CHAINS, argOf, connect, estimateWriteFees, retry, outcomeOf, sleep } from "./harness.mjs";

const networkName = argOf("network", "studiodev");
const chain = CHAINS[networkName];
const deployments = JSON.parse(
  readFileSync(new URL("../deployments.json", import.meta.url), "utf8"),
).deployments[networkName];
const ADDRESS = deployments.WillExecutorDemo.address;

const TARGET = "0xbe9ee23694b69d287fbe096ab3e37f61cff7b802";
const ENV_FILE = "/Users/kenilvakariya/Desktop/base-game/snake-arena/.env";

let key = null;
for (const m of readFileSync(ENV_FILE, "utf8").matchAll(/(0x)?([0-9a-fA-F]{64})/g)) {
  const k = `0x${m[2]}`;
  try { if (privateKeyToAccount(k).address.toLowerCase() === TARGET) { key = k; break; } } catch { /* not a key */ }
}
if (!key) { console.error(`no key deriving ${TARGET} in ${ENV_FILE}`); process.exit(1); }

const wallet = createClient({ chain, account: createAccount(key) });
const read = createClient({ chain });
const c = connect({ networkName, address: ADDRESS, role: "client" });

async function send(functionName, args) {
  const fees = await estimateWriteFees(wallet, { address: ADDRESS, functionName, args, value: 0n });
  const hash = await retry(
    () => wallet.writeContract({ address: ADDRESS, functionName, args, value: 0n, ...(fees ? { fees } : {}) }),
    { label: functionName },
  );
  let out = null;
  for (let i = 0; i < 80; i++) {
    await sleep(2500);
    try { out = outcomeOf(await read.getTransaction({ hash })); if (out.settled) break; } catch { /* blind poll */ }
  }
  console.log(`  ${functionName.padEnd(14)} ${hash}  → ${out?.status} ok=${out?.ok}`);
  return out;
}

const owned = JSON.parse(await c.view("get_wills_by_owner", [TARGET])).wills;
const active = owned.find((w) => w.status === "ACTIVE");
if (!active) {
  console.log("  no active will for this owner — already reset");
} else {
  console.log(`  cancelling will #${active.will_id} (${active.deposit_gen} GEN)`);
  await send("cancel_will", [active.will_id]);
  const owed = JSON.parse(await c.view("payout_of", [TARGET])).owed_gen;
  if (owed !== "0.00") await send("claim_payout", []);
}

const after = JSON.parse(await c.view("get_wills_by_owner", [TARGET])).wills;
const stats = JSON.parse(await c.view("get_stats"));
console.log(`  wills now: ${after.map((w) => `#${w.will_id} ${w.status}`).join(", ") || "none"}`);
console.log(`  locked ${stats.ledger.locked_gen} GEN   payable ${stats.ledger.payable_gen} GEN   identity ${stats.ledger.identity_holds}`);
console.log(after.some((w) => w.status === "ACTIVE") ? "\n  still has an active will" : "\n  ready for a fresh alive.mjs run");
