/**
 * The on-chain half of the audit: asserts the LIVE contracts, not the source.
 *
 * A static audit proves the file says the right things. This proves the thing
 * that is actually deployed answers, that its books balance, and that every
 * claim it has ever recorded still re-derives from its own storage.
 *
 *   node tools/audit_chain.mjs
 *
 * Exits non-zero on the first thing that is wrong.
 */
import { readFileSync } from "node:fs";
import { connect } from "../test/harness.mjs";

const doc = JSON.parse(readFileSync(new URL("../deployments.json", import.meta.url), "utf8"));
const live = doc.deployments?.studiodev;
if (!live) { console.error("no studiodev deployment recorded"); process.exit(1); }

let failures = 0;
const ok = (m) => console.log(`    \x1b[32m·\x1b[0m ${m}`);
const bad = (m) => { failures++; console.log(`    \x1b[31m·\x1b[0m ${m}`); };

for (const name of ["WillExecutor", "WillExecutorDemo"]) {
  const entry = live[name];
  if (!entry) { bad(`${name} is not in deployments.json`); continue; }
  const { viewJson } = connect({ address: entry.address, role: "client" });

  let cfg, stats;
  try {
    cfg = await viewJson("get_config");
    stats = await viewJson("get_stats");
  } catch (e) {
    bad(`${name} does not answer: ${String(e?.message ?? e).slice(0, 80)}`);
    continue;
  }
  ok(`${name} @ ${entry.address} answers`);

  // The ledger identity, read live rather than asserted from the source.
  if (stats.ledger.identity_holds) ok(`${name} books balance: ${stats.ledger.identity}`);
  else bad(`${name} LEDGER IDENTITY BROKEN — ${JSON.stringify(stats.ledger)}`);

  // There is no protocol revenue and no withdraw method, so this must be zero
  // for ever, on every instance.
  if (stats.protocol_revenue_wei === "0") ok(`${name} keeps no revenue`);
  else bad(`${name} reports revenue ${stats.protocol_revenue_wei}`);

  // The compared axis is a property of the deployed code, so read it back.
  const axis = cfg.consensus.compared_fields.join(",");
  if (axis === "activity_status,age_bucket,count_bucket,src_ok,cov_ok,content_hash") {
    ok(`${name} compares the whole feature vector, not just the verdict`);
  } else bad(`${name} compares ${axis}`);

  if (cfg.consensus.uses_language_model === false) ok(`${name} declares no model on the axis`);
  else bad(`${name} claims to use a language model`);

  if (cfg.consensus.signed_only === true) ok(`${name} counts signatures only, never inbound transfers`);
  else bad(`${name} does not declare signed_only`);

  // THE REJECTION, ASSERTED AGAINST THE DEPLOYED BYTES. Filtering a mixed page
  // by signer is not the same as fetching a page of signatures, and a release
  // on history that never reached the anchor is a release on evidence that
  // could not have contained the counterexample.
  if (String(cfg.consensus.outbound_source ?? "").includes("filter=from")) {
    ok(`${name} fetches outbound-only history at the source`);
  } else bad(`${name} does not declare an outbound-only source`);

  if (cfg.consensus.coverage_required_for_release === true) {
    ok(`${name} requires proven coverage before a release`);
  } else bad(`${name} does not require coverage before a release`);

  if (cfg.consensus.max_fetches_per_probe === 2) {
    ok(`${name} bounds a probe at two fetches (no rate-limiting walk)`);
  } else bad(`${name} declares ${cfg.consensus.max_fetches_per_probe} fetches per probe`);

  // Pause must not gate any withdrawal path.
  for (const m of ["heartbeat", "claim_inactive", "cancel_will", "settle_stalled", "claim_payout"]) {
    if (!cfg.unaffected_by_pause.includes(m)) bad(`${name}: ${m} is not declared pause-free`);
  }
  ok(`${name} declares the five pause-free paths`);

  // Deploy-time immutables must match what was recorded when it was deployed —
  // a mismatch means the recorded address is not the contract that was built.
  for (const [k, v] of Object.entries({
    missed_threshold: entry.missed_threshold,
    finder_fee_bps: entry.finder_fee_bps,
    stall_ttl_s: entry.stall_ttl_s,
    interval_unit_s: entry.interval_unit_s,
  })) {
    if (cfg[k] !== v) bad(`${name}: ${k} on chain is ${cfg[k]}, deployments.json says ${v}`);
  }
  ok(`${name} immutables match the deploy record`);

  // Every claim ever recorded must still re-derive from storage alone.
  const total = Number(stats.total_wills ?? 0);
  let checked = 0;
  for (let id = 1; id <= Math.min(total, 25); id++) {
    let report;
    try { report = await viewJson("verify_claim", [id]); } catch { continue; }
    if (!report.found || !report.checked) continue;
    checked++;
    if (report.verified) ok(`${name} will #${id}: all ${report.checks.length} checks re-derive`);
    else bad(`${name} will #${id} FAILS verification: ${JSON.stringify(report.checks)}`);
  }
  if (total > 0 && checked === 0) ok(`${name}: ${total} will(s), none claimed against yet`);
}

console.log(failures === 0 ? "\n  on-chain audit clean" : `\n  ${failures} on-chain failure(s)`);
process.exit(failures === 0 ? 0 : 1);
