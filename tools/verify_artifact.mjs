/**
 * Proves the source on disk is the source that is actually deployed.
 *
 *   node tools/verify_artifact.mjs
 *
 * A contract is only as auditable as the guarantee that the file you are
 * reading is the file running on chain. `genlayer code <address>` returns the
 * deployed source; this hashes it against the local file and records the result
 * in docs/artifact-check.json so `tools/audit.sh` can assert it without needing
 * the network.
 *
 * The CLI frames its output with a leading "\nResult:\n" and may add a trailing
 * newline; both are stripped before hashing, and the framing that was stripped
 * is recorded so nobody has to take that on trust.
 */
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";

const RPC = "https://studio-dev.genlayer.com/api";
const root = new URL("..", import.meta.url);
const localBytes = readFileSync(new URL("contracts/WillExecutor.py", root));
const sha = (b) => createHash("sha256").update(b).digest("hex");
const trimEnd = (b) => { let e = b.length; while (e > 0 && b[e - 1] === 0x0a) e--; return b.subarray(0, e); };

const localHash = sha(trimEnd(localBytes));
const deployments = JSON.parse(readFileSync(new URL("deployments.json", root), "utf8"))
  .deployments.studiodev;

const record = {
  checked_at: new Date().toISOString(),
  source_sha256: localHash,
  source_bytes: localBytes.length,
  contracts: {},
};
let failures = 0;

for (const name of ["WillExecutor", "WillExecutorDemo"]) {
  const address = deployments[name]?.address;
  if (!address) { console.log(`  ✗ ${name} is not in deployments.json`); failures++; continue; }

  let raw;
  try {
    raw = execFileSync("genlayer", ["code", address, "--rpc", RPC], { maxBuffer: 64 * 1024 * 1024 });
  } catch (e) {
    console.log(`  ✗ ${name}: could not fetch code — ${String(e?.message ?? e).slice(0, 90)}`);
    failures++;
    continue;
  }

  const start = raw.indexOf(Buffer.from("# v0.3.0"));
  if (start < 0) { console.log(`  ✗ ${name}: fetched code has no runner header`); failures++; continue; }
  const framing = raw.subarray(0, start).toString();
  const body = trimEnd(raw.subarray(start));
  const chainHash = sha(body);
  const match = chainHash === localHash;

  record.contracts[name] = { address, chain_sha256: chainHash, match, stripped_framing: framing };
  console.log(`  ${match ? "✔" : "✗"} ${name} @ ${address}`);
  console.log(`      chain ${chainHash}`);
  console.log(`      local ${localHash}`);
  if (!match) failures++;
}

writeFileSync(new URL("docs/artifact-check.json", root), JSON.stringify(record, null, 2) + "\n");
console.log(failures === 0
  ? `\n  the deployed artifact matches contracts/WillExecutor.py`
  : `\n  ${failures} artifact mismatch(es)`);
process.exit(failures === 0 ? 0 : 1);
