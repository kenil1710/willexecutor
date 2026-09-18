/**
 * Shared helpers for the WillExecutor integration scripts.
 *
 * Two of these exist because a GenLayer network does not report success in one
 * place, and reading the wrong place turns every failure into a silent pass.
 *
 *   `outcomeOf`  — a revert is recorded in THREE different spellings:
 *       tx.txExecutionResultName === "FINISHED_WITH_ERROR",
 *       tx.consensus_data.leader_receipt[0].execution_result === "ERROR",
 *       tx.consensus_data.leader_receipt[0].result.status === "rollback".
 *     Studio Dev leaves `txExecutionResultName` UNDEFINED and uses the second
 *     and third. A check written against the first alone reads
 *     `undefined !== "FINISHED_WITH_ERROR"` and reports success for a
 *     transaction that reverted and rolled back.
 *
 *   `contractAddressOf` — the created address lives under `tx.data.contract_address`
 *     on Studio and `tx.txDataDecoded.contractAddress` elsewhere. Reading only
 *     one spelling yields `undefined` for a deploy that fully succeeded, and an
 *     undefined address fed into the next constructor becomes `Address("None")`
 *     there — a second deploy that reverts pointing at the wrong contract.
 */
import { createClient, createAccount } from "genlayer-js";
import { studioDevnet } from "genlayer-js/chains";
import { transactionsStatusNumberToName } from "genlayer-js/types";
import { readFileSync } from "node:fs";

export const CHAINS = { studiodev: studioDevnet };

/** States that genuinely END a transaction. Not DECIDED_STATES — an
 *  UNDETERMINED transaction is finished as far as a caller is concerned even
 *  though consensus never decided it, and a poll loop that waits for a decision
 *  waits forever on one. */
export const TERMINAL_STATES = ["ACCEPTED", "FINALIZED", "UNDETERMINED", "CANCELED"];

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export const argOf = (name, fallback = null) => {
  const found = process.argv.find((a) => a.startsWith(`--${name}=`));
  return found ? found.slice(name.length + 3) : fallback;
};

/** The single source of truth for "did this transaction actually work". */
export function outcomeOf(tx) {
  const status = transactionsStatusNumberToName[tx?.status];
  const receipt = tx?.consensus_data?.leader_receipt?.[0];
  const exec = receipt?.execution_result ?? null;
  const named = tx?.txExecutionResultName ?? null;

  const returnedOk = named === "FINISHED_WITH_RETURN";
  const settled = Boolean(status && TERMINAL_STATES.includes(status));
  const rolledBack = receipt?.result?.status === "rollback";
  const reverted = exec === "ERROR" || named === "FINISHED_WITH_ERROR" || rolledBack;
  const accepted = status === "ACCEPTED" || status === "FINALIZED";

  return {
    settled,
    status,
    exec,
    named,
    ok: settled && accepted && !reverted && (exec === "SUCCESS" || returnedOk || exec === null),
    /**
     * Whether the return VALUE could be read at all. False wherever
     * `consensus_data` is not populated, since that is where the value lives.
     * Callers must not treat an unreadable return as a rejection — that is a
     * property of the transport, not of the contract — and should fall back to
     * observing contract state instead.
     */
    returnReadable: receipt !== null && receipt !== undefined,
    reverted,
    stderr: String(receipt?.genvm_result?.stderr ?? receipt?.stderr ?? ""),
    stdout: String(receipt?.genvm_result?.stdout ?? receipt?.stdout ?? ""),
    /**
     * The UserError text. NOT in stderr — stderr and stdout both come back empty
     * for a revert, so a suite that asserts on them can only ever check THAT a
     * call reverted, never that it reverted for the RIGHT reason, and every
     * wrong-reason revert passes silently.
     */
    revertReason: revertReasonOf(receipt),
    returned: returnValueOf(receipt),
    pending: (receipt?.pending_transactions ?? []).length,
    raw: receipt ?? null,
  };
}

/**
 * What a successful call returned, decoded.
 *
 * The receipt spells the two outcomes differently: on a revert `result.payload`
 * is the reason as a bare string; on a success it is an object carrying the
 * value both as raw calldata bytes and as `readable`, a JSON-encoded form.
 * Reading `payload` without checking which shape it is gets you "[object Object]".
 */
export function returnValueOf(receipt) {
  const payload = receipt?.result?.payload;
  if (payload === null || payload === undefined) return null;
  if (typeof payload === "string") return payload;
  if (typeof payload.readable === "string") {
    try {
      return JSON.parse(payload.readable);
    } catch {
      return payload.readable;
    }
  }
  return null;
}

/**
 * Repair the SDK's `readable` payload, which is NOT valid JSON.
 *
 * MEASURED against genlayer-js 2.0.0-rc.1 on Studio Dev: the encoder omits the
 * comma between map entries, so a contract returning a four-key object gets
 * back
 *
 *     {"claim_with":"claim_payout()""claimable_at":1794929934"status":"REJECTED"}
 *
 * which `JSON.parse` rejects. The `raw` calldata beside it is correct — the
 * bug is purely in the human-readable rendering — and `abi.calldata.decode`
 * answers `{}` for the same bytes, so neither of the SDK's own paths reads it.
 *
 * This inserts the missing separators, tracking string literals so that a
 * quote INSIDE a value is never mistaken for the start of the next key. It is
 * a workaround for a client-library bug and is labelled as one; the contract's
 * return value is correct on the wire.
 *
 * Nothing in this suite DEPENDS on it. Every assertion that matters is also
 * made against contract state, because a return value that cannot be read is a
 * property of the transport and must never be reported as a contract failure.
 */
export function repairReadable(text) {
  let out = "";
  let inString = false;
  let escaped = false;
  for (const ch of text) {
    if (inString) {
      out += ch;
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') {
      const prev = out.replace(/\s+$/, "").slice(-1);
      // A string starting straight after a completed value means the
      // separator was dropped.
      if (prev && !"{[,:".includes(prev)) out += ",";
      out += ch;
      inString = true;
      continue;
    }
    out += ch;
  }
  return out;
}

/** A returned JSON string parsed into an object, or null if it was not one. */
export function returnedJson(out) {
  const value = typeof out === "string" ? out : out?.returned;
  if (value && typeof value === "object") return value;
  if (typeof value !== "string") return null;
  for (const candidate of [value, repairReadable(value)]) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === "object") return parsed;
    } catch { /* try the repaired form next */ }
  }
  return null;
}

/** The revert message a `gl.vm.UserError` produced, or "" if it did not revert. */
export function revertReasonOf(receipt) {
  const result = receipt?.result;
  if (!result) return "";
  if (typeof result.payload === "string" && result.payload) return result.payload;
  if (typeof result.raw === "string" && result.raw) {
    try {
      // The leading byte is a status tag, not text; strip anything unprintable.
      return Buffer.from(result.raw, "base64").toString("utf8").replace(/^[\x00-\x1f]+/, "");
    } catch {
      return "";
    }
  }
  return "";
}

/** The last frame of a GenVM traceback — the line that actually failed. */
export function failureLine(stderr) {
  const lines = String(stderr).split("\n").filter((l) => l.trim());
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].includes("/contract.py")) return lines[i].trim();
  }
  return lines[lines.length - 1]?.trim() ?? "";
}

/** Studio faucet. No-op elsewhere. */
export async function fundOnStudio(chain, address, wei) {
  if (!chain.isStudio) return false;
  const res = await fetch(chain.rpcUrls.default.http[0], {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "sim_fundAccount",
      params: [address, Number(wei)],
    }),
  });
  const json = await res.json();
  return Boolean(json?.result);
}

/**
 * Retry a flaky RPC call.
 *
 * Studio intermittently answers with an HTML error page instead of JSON, which
 * surfaces as `Unexpected token '<'`. That is infrastructure noise, not a
 * contract fault, and without a retry it aborts a run mid-assertion and reads
 * like a failure of whatever call happened to be in flight.
 */
export async function retry(fn, { attempts = 6, baseMs = 4000, label = "rpc" } = {}) {
  let last;
  for (let i = 1; i <= attempts; i++) {
    try {
      return await fn();
    } catch (e) {
      last = e;
      const message = String(e?.message ?? e);
      const transient =
        /Unexpected token '<'|not valid JSON|fetch failed|ECONNRESET|ETIMEDOUT|502|503|504/i.test(message) ||
        // Studio meters requests per minute. A read loop over every assessment
        // trips it, and the answer is to wait rather than to fail a suite that
        // was measuring something else entirely.
        /rate limit exceeded|-32029/i.test(message) ||
        // A node holds ONE transaction slot per recipient contract. A write that
        // arrives while the previous one is still settling is rejected at the
        // consensus contract, which surfaces as an EVM revert rather than as
        // congestion. Backing off and resubmitting is the correct response;
        // treating it as a contract fault is not.
        /to consensus contract .* was reverted/i.test(message);
      if (!transient || i === attempts) throw e;
      const rateLimited = /rate limit exceeded|-32029/i.test(message);
      // A per-minute bucket needs the minute to roll over; ordinary noise does
      // not. Backing off 4s against a 60s window just burns the retry budget.
      const wait = rateLimited ? 12_000 : baseMs * i;
      console.log(`  … ${label} attempt ${i} ${rateLimited ? "hit the RPC rate limit" : "hit transient RPC noise"}, waiting ${(wait / 1000).toFixed(0)}s`);
      await sleep(wait);
    }
  }
  throw last;
}

/** The address of a contract created by a deploy transaction. Both spellings. */
export function contractAddressOf(tx) {
  return (
    tx?.data?.contract_address ??
    tx?.txDataDecoded?.contractAddress ??
    tx?.contract_address ??
    tx?.consensus_data?.leader_receipt?.[0]?.contract_address ??
    null
  );
}

/**
 * Fee estimation, with an explicit fallback.
 *
 * Studio Dev prices every transaction and REFUSES one whose attached feeValue
 * is below the floor. Estimating per-call rather than hardcoding one number is
 * the difference between a suite that keeps working when the fee policy moves
 * and one that starts failing everywhere for a reason that looks like a
 * contract bug.
 *
 * A failed estimate is NOT fatal: it returns `null`, and the caller submits
 * without an explicit fee so the node applies its own default. Making a flaky
 * pricing endpoint able to block a deploy would be trading one failure mode for
 * a worse one.
 */
export async function estimateFees(client, label = "fees") {
  try {
    const est = await retry(() => client.estimateTransactionFees(), { attempts: 3, baseMs: 2000, label });
    if (!est?.distribution) return null;
    return {
      distribution: est.distribution,
      ...(est.messageAllocations ? { messageAllocations: est.messageAllocations } : {}),
      feeValue: est.feeValue,
    };
  } catch (e) {
    console.log(`  … fee estimate unavailable (${String(e?.message ?? e).slice(0, 80)}), using node default`);
    return null;
  }
}

/**
 * Fee estimation for a WRITE, by simulating it first.
 *
 * This is not interchangeable with `estimateFees`. A method that posts an
 * INTERNAL MESSAGE — which on this runner means any method that moves value,
 * because `emit_transfer` posts one — needs a `messageAllocations` entry
 * budgeting that message, naming its recipient and its fee params. A generic
 * estimate produces `totalMessageFees: 0` and no allocations, and the
 * transaction is then accepted by the node and fails inside it with:
 *
 *     ACCEPTED   fee no_matching_allocation # internal
 *
 * which reads like a contract fault and is not one: `claim_refund` ran, cleared
 * nothing, and the wei stayed put. `estimateTransactionFeesForWrite` simulates
 * the call with the real signer and the real arguments, so the allocation it
 * returns names the real recipient.
 *
 * Falls back to the generic estimate, then to the node default, because a
 * pricing endpoint having a bad minute must not be able to block a write.
 */
export async function estimateWriteFees(client, { address, functionName, args = [], value = 0n }) {
  try {
    const est = await retry(
      () => client.estimateTransactionFeesForWrite({ address, functionName, args, value }),
      { attempts: 3, baseMs: 2000, label: `${functionName} fee` },
    );
    if (est?.distribution) {
      return {
        distribution: est.distribution,
        ...(est.messageAllocations ? { messageAllocations: est.messageAllocations } : {}),
        feeValue: est.feeValue,
      };
    }
  } catch (e) {
    console.log(`  … write fee simulation failed for ${functionName} (${String(e?.message ?? e).slice(0, 100)})`);
  }
  return estimateFees(client, `${functionName} fee`);
}

/** Human-readable GEN from wei. */
export const gen = (wei) => (Number(wei ?? 0n) / 1e18).toFixed(6);

/** Every role in .accounts.json, keyed by name. */
export function accounts() {
  return JSON.parse(readFileSync(new URL("./.accounts.json", import.meta.url), "utf8"));
}

/** Builds the read/wallet client pair plus a settle-aware, fee-estimating `send`. */
export function connect({ networkName = argOf("network", "studiodev"), address, role = "client" } = {}) {
  const chain = CHAINS[networkName];
  if (!chain) throw new Error(`unknown network ${networkName}`);
  const acc = accounts();
  if (!acc[role]?.key) throw new Error(`no key for role ${role} — run: node accounts.mjs`);
  const account = createAccount(acc[role].key);
  const wallet = createClient({ chain, account });
  const read = createClient({ chain });
  const deadline = chain.isStudio ? 300_000 : 600_000;
  const pollMs = chain.isStudio ? 1_500 : 5_000;

  /**
   * Submit a write and wait for it to reach a terminal state.
   *
   * NEVER THROWS. Every caller already branches on `out.ok`, so a give-up is
   * returned as an unsettled outcome and costs one red check. Letting it escape
   * as an exception instead can take down a whole run mid-suite with a dozen
   * tests still unreported — one dropped transaction must not be able to do that
   * to tests it never touched.
   */
  async function send(functionName, args = [], value = 0n, opts = {}) {
    const started = Date.now();
    const giveUp = (reason, hash = null) => ({
      ...outcomeOf(null),
      status: "UNSETTLED",
      hash,
      tx: null,
      seconds: (Date.now() - started) / 1000,
      failure: reason,
      revertReason: reason,
    });

    const fees = opts.fees === undefined
      ? await estimateWriteFees(wallet, { address, functionName, args, value })
      : opts.fees;

    let hash;
    try {
      hash = await retry(
        () => wallet.writeContract({
          address, functionName, args, value,
          ...(fees ? { fees } : {}),
        }),
        { label: functionName },
      );
    } catch (e) {
      return giveUp(`submit failed — ${String(e?.message ?? e)}`);
    }

    let blindSince = null;
    for (;;) {
      /*
       * A failing RPC and a genuinely absent transaction must NOT both arrive
       * here as `null`. `.catch(() => null)` makes a burst of `fetch failed`
       * indistinguishable from the endpoint answering "no such transaction", so
       * the loop goes blind and then sits out its entire deadline in silence.
       * A poll the RPC could not answer is recorded as BLIND rather than read as
       * an outcome: absence of evidence is not evidence of absence.
       */
      let answered = true;
      let tx = null;
      try {
        tx = await retry(() => read.getTransaction({ hash }), {
          attempts: 4, baseMs: 2_000, label: `${functionName} poll`,
        });
      } catch {
        answered = false;
      }

      if (answered) {
        blindSince = null;
        const out = outcomeOf(tx);
        if (out.settled) return { ...out, hash, tx, seconds: (Date.now() - started) / 1000 };
      } else if (blindSince === null) {
        blindSince = Date.now();
      }

      if (Date.now() - started > deadline) {
        const secs = (deadline / 1000).toFixed(0);
        // Say WHICH of the two it was. "never settled" alone cannot distinguish
        // a stuck transaction from an endpoint that stopped answering, and the
        // two call for opposite responses.
        return giveUp(
          blindSince
            ? `never settled in ${secs}s — RPC unreadable for the last ${((Date.now() - blindSince) / 1000).toFixed(0)}s, tx may still be in flight`
            : `never settled in ${secs}s — tx stayed non-terminal`,
          hash,
        );
      }
      await sleep(pollMs);
    }
  }

  const view = async (functionName, args = []) =>
    retry(() => read.readContract({ address, functionName, args }), { label: functionName });
  const viewJson = async (functionName, args = []) => JSON.parse(await view(functionName, args));

  return { chain, account, wallet, read, send, view, viewJson };
}

/**
 * Deploy a contract and wait for its address. Returns { ok, address, hash, out }.
 * Never throws for a contract-level failure; only for a missing artifact.
 */
export async function deploy({ chain, wallet, read, code, args = [], label = "deploy" }) {
  const fees = await estimateFees(wallet, label);
  if (fees) console.log(`  fee        ${gen(fees.feeValue)} GEN (estimated)`);
  const hash = await retry(
    () => wallet.deployContract({ code, args, leaderOnly: false, ...(fees ? { fees } : {}) }),
    { label },
  );
  console.log(`  tx         ${hash}`);
  const started = Date.now();
  for (;;) {
    await sleep(chain.isStudio ? 2500 : 5000);
    let tx = null;
    try {
      tx = await retry(() => read.getTransaction({ hash }), { attempts: 4, baseMs: 2000, label: `${label} poll` });
    } catch { /* blind poll — keep waiting */ }
    const out = outcomeOf(tx);
    if (out.settled) {
      if (!out.ok) return { ok: false, address: null, hash, out };
      const address = contractAddressOf(tx);
      if (!address) return { ok: false, address: null, hash, out, reason: "no contract address in receipt" };
      return { ok: true, address, hash, out };
    }
    if (Date.now() - started > 900_000) {
      return { ok: false, address: null, hash, out, reason: "never settled in 900s" };
    }
  }
}

/**
 * Wait for a transaction to FINALIZE, not merely to be accepted.
 *
 * This matters for exactly one thing and it is the most important thing in the
 * suite. WillExecutor's `_pay` posts its internal transfer with `on="finalized"`, deliberately:
 * a payout applied at ACCEPTED would already have happened if the transaction
 * that authorised it were later appealed and rolled back. So the recipient's
 * balance does NOT move when `claim_payout` is accepted — it moves when that
 * transaction finalises, which is later.
 *
 * A balance check written against acceptance therefore reads "no wei moved" on
 * a payout that is working perfectly, and would send you looking for a bug in
 * the contract that is not there. It reads exactly like the silent-`emit()` bug
 * that WAS there once, which is what makes it worth spelling out.
 *
 * Never throws: finalisation is the network's business, and a claim that has
 * not finalised yet is reported as pending rather than as a failure.
 */
export async function waitFinalized(client, hash, { timeoutMs = 240_000, label = "finalize" } = {}) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const tx = await client.getTransaction({ hash });
      const status = transactionsStatusNumberToName[tx?.status];
      if (status === "FINALIZED") return { finalized: true, seconds: (Date.now() - started) / 1000 };
      if (status === "CANCELED" || status === "UNDETERMINED") {
        return { finalized: false, seconds: (Date.now() - started) / 1000, status };
      }
    } catch {
      /* blind poll — the RPC having a bad minute is not an outcome */
    }
    await sleep(5000);
  }
  return { finalized: false, seconds: (Date.now() - started) / 1000, status: "TIMEOUT" };
}
