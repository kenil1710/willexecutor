/**
 * Deploys WillExecutor to Studio Dev.
 *
 *   node deploy.mjs              # the canonical instance: 7-365 DAYS
 *   node deploy.mjs --demo       # a second instance with the clock in SECONDS
 *   node deploy.mjs --both       # both, in one run
 *
 * WHY TWO INSTANCES. The canonical contract enforces the brief exactly: a
 * check-in interval of at least seven days and at most a year, and a claim that
 * opens only after two whole intervals have passed. That is the right rule and
 * it is completely un-watchable — the earliest a release could be demonstrated
 * on it is fourteen days from now.
 *
 * A release path nobody has watched execute is a release path nobody has
 * tested, so a SECOND INSTANCE OF THE SAME SOURCE is deployed with
 * `interval_unit_s = 1`. Every rule, every gate and every line of consensus
 * logic is identical; only the clock is faster. That is what `seed.mjs` drives
 * end to end, and what `docs/EVIDENCE.md` records.
 *
 * Every deploy estimates its fee first. Studio Dev prices transactions and
 * REFUSES one whose attached feeValue is below the floor; estimating per-call
 * rather than hardcoding a number is the difference between a script that keeps
 * working when the fee policy moves and one that starts failing everywhere for
 * a reason that looks like a contract bug.
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { createClient, createAccount } from "genlayer-js";
import { CHAINS, argOf, accounts, fundOnStudio, deploy, gen } from "./harness.mjs";

const networkName = argOf("network", "studiodev");
const chain = CHAINS[networkName];
if (!chain) throw new Error(`unknown network ${networkName}`);

const both = process.argv.includes("--both");
const demoOnly = process.argv.includes("--demo");

/** `RUBRIC_VERSION` as the contract itself declares it. */
function rubricVersion(source) {
  const m = String(source).match(/^RUBRIC_VERSION\s*=\s*"([^"]+)"/m);
  if (!m) throw new Error("no RUBRIC_VERSION in the contract source");
  return m[1];
}

const acc = accounts();
const account = createAccount(acc.client.key);
const wallet = createClient({ chain, account });
const read = createClient({ chain });

console.log(`\nWillExecutor deploy → ${networkName}`);
console.log(`  signer     ${account.address} (client)`);

await fundOnStudio(chain, account.address, 500n * 10n ** 18n);
console.log(`  balance    ${gen(await read.getBalance({ address: account.address }))} GEN`);

const path = new URL("../deployments.json", import.meta.url);
const doc = existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : {};
doc.deployments = doc.deployments || {};
const record = doc.deployments[networkName] || { network: networkName, chain_id: chain.id };

/**
 * Persist AFTER EACH CONTRACT, not once at the end.
 *
 * A previous project's deploy script put one contract on chain, then exited on
 * the second one's failure before writing anything — so a live contract existed
 * nowhere on disk and the next run happily deployed a duplicate. A deploy record
 * that only survives a fully clean run is a deploy record that loses exactly the
 * addresses you most need after a partial failure.
 */
function persist() {
  record.explorer = "https://explorer-studio-dev.genlayer.com/";
  doc.deployments[networkName] = record;
  writeFileSync(path, JSON.stringify(doc, null, 2) + "\n");
}

const code = readFileSync(new URL("../contracts/WillExecutor.py", import.meta.url));

// (min_interval_s, max_interval_s, interval_unit_s, missed_threshold,
//  finder_fee_bps, stall_ttl_s) — all six immutable after deploy.
const VARIANTS = {
  WillExecutor: {
    label: "canonical (the brief: 7–365 days)",
    args: [7 * 86400, 365 * 86400, 86400, 2, 500, 48 * 3600],
  },
  WillExecutorDemo: {
    label: "demo (same source, clock in seconds)",
    args: [60, 365 * 86400, 1, 2, 500, 300],
  },
};

const wanted = both
  ? ["WillExecutor", "WillExecutorDemo"]
  : demoOnly
    ? ["WillExecutorDemo"]
    : ["WillExecutor"];

for (const name of wanted) {
  const { label, args } = VARIANTS[name];
  console.log(`\n  ${name}  ${label}`);
  console.log(`  source     contracts/WillExecutor.py (${code.length.toLocaleString()} bytes)`);
  console.log(`  interval   ${args[0]}s – ${args[1]}s, unit ${args[2]}s, threshold ×${args[3]}`);
  console.log(`  finder fee ${args[4] / 100}%   stall ttl ${args[5]}s`);

  const res = await deploy({ chain, wallet, read, code, args, label: `${name} deploy` });
  if (!res.ok) {
    console.error(`\n${name} deploy FAILED: ${res.out?.status} ${res.reason ?? ""} ${res.out?.revertReason ?? ""}`);
    console.error((res.out?.stderr ?? "").split("\n").slice(-25).join("\n"));
    persist();
    process.exit(1);
  }
  console.log(`  address    ${res.address}`);

  record[name] = {
    address: res.address,
    deploy_tx: res.hash,
    source_bytes: code.length,
    owner: account.address,
    // READ OUT OF THE SOURCE, never retyped here. This was a hardcoded
    // "1.0.0" and it silently recorded the wrong rubric the first time the
    // contract's consensus projection changed — a deployments file that
    // disagrees with the bytes it describes is worse than one that omits the
    // field, because it is believed.
    rubric_version: rubricVersion(code),
    min_interval_s: args[0],
    max_interval_s: args[1],
    interval_unit_s: args[2],
    missed_threshold: args[3],
    finder_fee_bps: args[4],
    stall_ttl_s: args[5],
    deployed_at: new Date().toISOString(),
  };
  persist();
}

console.log(`\nwrote deployments.json`);
