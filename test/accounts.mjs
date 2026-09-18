/**
 * Creates test/.accounts.json — a stable, reusable pool of signing keys.
 *
 * A POOL rather than one key because WillExecutor's rules are RELATIONAL. "The
 * beneficiary cannot be the owner" cannot even be STATED with a single address;
 * neither can "only the owner may check in", nor "anyone may pull the switch",
 * nor the one-active-will-per-wallet limit. Proving any of them needs at least
 * two wallets, and the three demo wills need one owner each because a wallet
 * may hold only one active will at a time.
 *
 * Keys are written by hand rather than read off `createAccount()`, because that
 * helper does NOT expose a `privateKey` field — it returns a viem account whose
 * key stays private to the closure. Persisting `account.privateKey` therefore
 * writes `undefined`, JSON.stringify drops the field entirely, and every later
 * `createAccount(undefined)` silently mints a brand-new random account. On a
 * faucet-funded network that failure is INVISIBLE: every run works, just from a
 * different address each time. It surfaces later, as access-control tests that
 * can never trigger and an owner nobody holds the key to.
 *
 * THE OWNER WALLETS ARE ALSO THE SUBJECT OF THE CONSENSUS ROUND. Each one is a
 * freshly generated key that has never signed anything on Ethereum mainnet, so
 * Blockscout answers `{"status":"0","message":"No transactions found",
 * "result":[]}` for it — a real, readable "this wallet is dormant". That is not
 * a mock: it is the genuine live answer for a genuinely unused wallet, which is
 * exactly the situation the contract exists to detect.
 *
 * Existing roles are PRESERVED across runs unless --force is passed, so a
 * funded address is never silently replaced.
 *
 * Usage: node accounts.mjs [--force]
 */
import { createAccount } from "genlayer-js";
import { randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";

const target = new URL("./.accounts.json", import.meta.url);
const force = process.argv.includes("--force");

// `client` deploys and owns the contract. Its only power is pausing NEW wills.
// `owner1..3` each hold one will — one apiece, because one wallet cannot hold
// two active wills and the run would otherwise spend its time being refused,
// with the refusals reading exactly like a contract fault in a log.
// `heir1..3` are the beneficiaries. `finder` pulls the switch, which proves the
// path really is permissionless, and earns the 5% for doing it. `outsider` only
// ever probes access control and must never be granted a privilege by any test.
const ROLES = [
  "client",
  "owner1", "heir1",
  "owner2", "heir2",
  "owner3", "heir3",
  "finder", "outsider",
];

const existing = existsSync(target) && !force ? JSON.parse(readFileSync(target, "utf8")) : {};
const out = {};
let created = 0;

for (const role of ROLES) {
  if (existing[role]?.key) {
    out[role] = existing[role];
    continue;
  }
  const key = `0x${randomBytes(32).toString("hex")}`;
  const account = createAccount(key);
  // Round-trip assertion: the stored address must be the one this key actually
  // derives. Without it a mismatch just sits in the file looking plausible.
  if (createAccount(key).address !== account.address) {
    throw new Error(`key for ${role} does not derive a stable address`);
  }
  out[role] = { key, address: account.address };
  created++;
}

writeFileSync(target, JSON.stringify(out, null, 2) + "\n");
console.log(`wrote .accounts.json — ${created} new, ${ROLES.length - created} preserved`);
for (const role of ROLES) console.log(`  ${role.padEnd(10)} ${out[role].address}`);
