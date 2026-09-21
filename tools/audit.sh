#!/usr/bin/env bash
#
# WillExecutor — the executable audit.
#
# Every check below is a PAST REJECTION from an earlier project in this series,
# or a hazard measured during this one, turned into something that fails loudly
# rather than into a paragraph somebody has to remember to read. It exits with
# the number of failures, so CI and a human get the same answer.
#
#   tools/audit.sh              # static + offline, no network
#   tools/audit.sh --chain      # also asserts the live deploy on Studio Dev
#
# The AST checks are written as Python rather than as grep ON PURPOSE. The
# contract's own header documents `str.replace()` and the silent `.emit(value=)`
# spelling in order to warn about them, and on a previous project a grep-based
# audit matched that prose and reported three failures against a clean file. An
# audit that fires on its own documentation is an audit people learn to ignore,
# and the next real finding goes with it.
#
set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0
FAIL=0
SKIP=0

ok()   { PASS=$((PASS+1)); printf '  \033[32m✔\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31m✗\033[0m %s\n' "$1"; }
skp()  { SKIP=$((SKIP+1)); printf '  \033[33m-\033[0m %s (skipped: %s)\n' "$1" "$2"; }
sec()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

# check <description> <command...> — passes when the command exits 0
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }

WE=contracts/WillExecutor.py
WITH_CHAIN=0
[ "${1:-}" = "--chain" ] && WITH_CHAIN=1

printf '\033[1mWillExecutor audit\033[0m — every check is a past rejection or a measured hazard\n'

# ---------------------------------------------------------------------------
sec "1. The runner header (an undeployable contract with no error message)"
# GenVM parses the contiguous leading `#` block as the runner header. A stray
# comment between line 1 and the imports makes the contract undeployable and
# reports only `invalid_contract`. Lint does not catch it; this does. Three
# projects in this series lost a deploy to it.
[ "$(sed -n '1p' "$WE")" = "# v0.3.0" ] && ok "line 1 is the version line" || bad "line 1 is not '# v0.3.0'"
sed -n '2p' "$WE" | grep -q '^# { "Depends": "py-genlayer:' && ok "line 2 is the runner pin" || bad "line 2 is not the runner pin"
sed -n '3p' "$WE" | grep -q '^#' && bad "line 3 is a comment (this breaks the deploy)" || ok "line 3 is not a comment"
sed -n '2p' "$WE" | grep -q 'latest' && bad "the runner is pinned to 'latest'" || ok "a concrete runner hash is pinned, never 'latest'"
sed -n '3p' "$WE" | grep -q '^import genlayer as gl' && ok "the imports start on line 3" || bad "line 3 is not the first import"

# ---------------------------------------------------------------------------
sec "2. Runner restrictions and determinism"
check "str.replace() is never called (the runner rejects it)" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
bad = [n.lineno for n in ast.walk(t)
       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
       and n.func.attr == "replace"]
sys.exit(1 if bad else 0)
PY

check "no float literal anywhere (a float near money is a consensus hazard)" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
bad = [n.lineno for n in ast.walk(t)
       if isinstance(n, ast.Constant) and isinstance(n.value, float)]
sys.exit(1 if bad else 0)
PY

check "no wall-clock read (the only clock is gl.message.raw['datetime'])" python3 - <<'PY'
import sys
s = open("contracts/WillExecutor.py").read()
sys.exit(1 if any(x in s for x in ("time.time", "datetime.now", "datetime.utcnow",
                                   "time.monotonic")) else 0)
PY

check "no randomness" python3 - <<'PY'
import sys
s = open("contracts/WillExecutor.py").read()
sys.exit(1 if any(x in s for x in ("random.", "uuid", "os.urandom", "secrets.")) else 0)
PY

check "no forbidden import" python3 - <<'PY'
import ast, sys
BANNED = {"os", "sys", "subprocess", "random", "time", "datetime", "socket",
          "requests", "urllib", "secrets", "hashlib"}
t = ast.parse(open("contracts/WillExecutor.py").read())
for n in ast.walk(t):
    if isinstance(n, ast.Import):
        for a in n.names:
            if a.name.split(".")[0] in BANNED:
                sys.exit(1)
    if isinstance(n, ast.ImportFrom) and n.module:
        if n.module.split(".")[0] in BANNED:
            sys.exit(1)
sys.exit(0)
PY

# ---------------------------------------------------------------------------
sec "3. Rule 2 — no public write ever raises"
# A revert rolls back storage but NOT the value that came with the call, which
# then sits in the contract unaccounted for and unreachable by anybody.
check "there is not one raise statement in the file" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
bad = [n.lineno for n in ast.walk(t) if isinstance(n, ast.Raise)]
if bad:
    print("raise at lines", bad)
sys.exit(1 if bad else 0)
PY

check "every public write banks incoming value on its first statement" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
bad = []
for n in ast.walk(t):
    if not isinstance(n, ast.FunctionDef):
        continue
    if not any(ast.unparse(d).startswith("gl.public.write") for d in n.decorator_list):
        continue
    first = n.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        first = n.body[1]
    if "self._bank()" not in ast.unparse(first):
        bad.append(n.name)
if bad:
    print("do not bank first:", bad)
sys.exit(1 if bad else 0)
PY

check "every address from calldata is shape-checked before Address() is built" python3 - <<'PY'
import sys
s = open("contracts/WillExecutor.py").read()
# `Address("nonsense")` raises, which would break rule 2 on a typo.
sys.exit(0 if s.count("_is_addr(") >= 5 else 1)
PY

# ---------------------------------------------------------------------------
sec "4. Rule 6 — the owner cannot freeze user money"
check "pause is read only by create_will, top_up, set_paused and the views" python3 - <<'PY'
import ast, sys
ALLOWED = {"create_will", "top_up", "set_paused", "get_config", "get_stats",
           "__init__"}
t = ast.parse(open("contracts/WillExecutor.py").read())
bad = []
for n in ast.walk(t):
    if not isinstance(n, ast.FunctionDef) or n.name in ALLOWED:
        continue
    for sub in ast.walk(n):
        if (isinstance(sub, ast.Attribute) and sub.attr == "paused"
                and isinstance(sub.value, ast.Name) and sub.value.id == "self"):
            bad.append((n.name, sub.lineno))
if bad:
    print("gated on pause but must not be:", bad)
sys.exit(1 if bad else 0)
PY

check "there is no owner withdraw / sweep / drain method" python3 - <<'PY'
import ast, sys
BANNED = {"withdraw", "sweep", "collect_fees", "rescue", "emergency_withdraw",
          "drain", "claim_fees"}
t = ast.parse(open("contracts/WillExecutor.py").read())
names = {n.name for n in ast.walk(t) if isinstance(n, ast.FunctionDef)}
sys.exit(1 if names & BANNED else 0)
PY

check "settle_stalled and claim_payout are permissionless (no owner gate)" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
for n in ast.walk(t):
    if isinstance(n, ast.FunctionDef) and n.name in ("settle_stalled", "claim_payout"):
        if "_is_owner" in ast.unparse(n):
            sys.exit(1)
sys.exit(0)
PY

# ---------------------------------------------------------------------------
sec "5. Rule 4 — the immutables really are immutable"
check "no method outside __init__ assigns an immutable field" python3 - <<'PY'
import ast, sys
IMMUTABLE = {"min_interval_s", "max_interval_s", "interval_unit_s",
             "missed_threshold", "finder_fee_bps", "stall_ttl_s"}
t = ast.parse(open("contracts/WillExecutor.py").read())
bad = []
for n in ast.walk(t):
    if not isinstance(n, ast.FunctionDef) or n.name == "__init__":
        continue
    for sub in ast.walk(n):
        if not isinstance(sub, (ast.Assign, ast.AugAssign)):
            continue
        targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
        for tg in targets:
            if (isinstance(tg, ast.Attribute) and isinstance(tg.value, ast.Name)
                    and tg.value.id == "self" and tg.attr in IMMUTABLE):
                bad.append((n.name, tg.attr, sub.lineno))
if bad:
    print("immutable reassigned:", bad)
sys.exit(1 if bad else 0)
PY

# ---------------------------------------------------------------------------
sec "6. Money leaves by exactly one door"
check "emit_transfer appears exactly once" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
calls = [n for n in ast.walk(t) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute) and n.func.attr == "emit_transfer"]
sys.exit(0 if len(calls) == 1 else 1)
PY

# `Proxy.emit()` returns a method GETTER, so `x.emit(value=…)` with nothing
# after it constructs an object and drops it, posting NO MESSAGE AT ALL. On an
# earlier project every payout looked perfect and not one wei moved.
check "there is no bare .emit( call (the spelling that silently pays nobody)" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
bad = [n.lineno for n in ast.walk(t) if isinstance(n, ast.Call)
       and isinstance(n.func, ast.Attribute) and n.func.attr == "emit"]
sys.exit(1 if bad else 0)
PY

check "only claim_payout reaches _pay" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
callers = set()
for n in ast.walk(t):
    if not isinstance(n, ast.FunctionDef):
        continue
    for sub in ast.walk(n):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "_pay":
            callers.add(n.name)
sys.exit(0 if callers == {"claim_payout"} else 1)
PY

# ---------------------------------------------------------------------------
sec "7. TreeMap semantics (this one shipped to chain)"
# `self.by_owner[sender].append(...)` raises KeyError on the runner when the key
# is absent — a revert inside create_will, on the one path that has already
# banked a deposit. 431 offline tests passed because the stub auto-created the
# entry. `get_or_insert_default` is the spelling that inserts.
check "no array-valued map is ever subscripted" python3 - <<'PY'
import ast, sys
MAPS = {"by_owner", "by_beneficiary"}
t = ast.parse(open("contracts/WillExecutor.py").read())
bad = []
for n in ast.walk(t):
    if not isinstance(n, ast.Subscript):
        continue
    v = n.value
    if (isinstance(v, ast.Attribute) and v.attr in MAPS
            and isinstance(v.value, ast.Name) and v.value.id == "self"):
        bad.append((v.attr, n.lineno))
if bad:
    print("subscripted instead of get_or_insert_default:", bad)
sys.exit(1 if bad else 0)
PY

check "the offline stub raises on a missing key, like the runner does" python3 - <<'PY'
import sys, re
s = open("test/test_logic.py").read()
# A stub more forgiving than the runner certifies bugs.
sys.exit(0 if "return dict.__getitem__(self, self._k(key))" in s else 1)
PY

# ---------------------------------------------------------------------------
sec "8. Rule 10 — the explorer URL is never caller-supplied"
check "_url takes only (host, wallet, window)" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
for n in ast.walk(t):
    if isinstance(n, ast.FunctionDef) and n.name == "_url":
        sys.exit(0 if {a.arg for a in n.args.args} == {"host", "wallet", "window"} else 1)
sys.exit(1)
PY

check "_url_v2 takes only (host, wallet)" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
for n in ast.walk(t):
    if isinstance(n, ast.FunctionDef) and n.name == "_url_v2":
        sys.exit(0 if {a.arg for a in n.args.args} == {"host", "wallet"} else 1)
sys.exit(1)
PY

check "the only https:// literal in the file is the scheme itself" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
for n in ast.walk(t):
    if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("https://"):
        if n.value != "https://":
            print("hardcoded URL:", n.value[:60])
            sys.exit(1)
sys.exit(0)
PY

check "every allowlisted chain resolves to a blockscout host" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
for c in T.C.CHAINS:
    if c not in T.C.CHAIN_HOSTS or not T.C.CHAIN_HOSTS[c].endswith("blockscout.com"):
        sys.exit(1)
sys.exit(0)
PY

# ---------------------------------------------------------------------------
sec "9. Rule 9 — outbound-only evidence, and coverage before release"
# THE REJECTION THIS SECTION EXISTS FOR. The probe used to fetch the newest ten
# transactions of an account and filter them by signer; ten inbound transfers
# arriving after the owner's last signature pushed it off the page, the filter
# found nothing, and a living owner's estate was released. Every check below is
# one half of the fix, wired so that removing it fails here as well as in the
# suite.
check "the primary probe asks the explorer for outbound history only" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
url = T.C._url_v2("eth.blockscout.com", "0x" + "b" * 40)
if "filter=from" not in url or "/api/v2/addresses/" not in url:
    print("primary URL is not outbound-filtered:", url)
    sys.exit(1)
sys.exit(0)
PY

check "the probe never TRUSTS the filter — coverage is proved from the page" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
fn = next(n for n in ast.walk(t)
          if isinstance(n, ast.FunctionDef) and n.name == "_probe")
# There must be exactly ONE way for a non-ALIVE verdict to become covered, and
# it must be `_covers` reading the page. A second path — "every item came back
# outbound, so trust it" — is how an assumption about the endpoint gets back
# into the money path, which is the shape of the original bug.
covers = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
          and ast.unparse(n.func) == "_covers"]
if len(covers) != 1:
    print("expected exactly one _covers call, found", len(covers))
    sys.exit(1)
assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
           and any(getattr(t_, "id", "") == "covered" for t_ in n.targets)]
# One `covered = True` for the ALIVE case, one `covered = _covers(...)`.
if len(assigns) != 2:
    print("expected exactly two assignments to `covered`, found", len(assigns))
    sys.exit(1)
sys.exit(0)
PY

check "TX_WINDOW equals the v2 page size (a larger one re-opens the bug)" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
# v2 returns a FIXED page of 50 and takes no size parameter. If TX_WINDOW were
# larger, a full 50-item page would satisfy `len(items) < requested` and be
# read as a COMPLETE history — handing the inbound flood its old result back
# through the coverage check. Measured against the live endpoint 2026-09-21.
sys.exit(0 if T.C.TX_WINDOW == 50 else 1)
PY

check "an INACTIVE verdict is incoherent without proven coverage" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
C = T.C
for status in (C.A_INACTIVE, C.A_ALIVE):
    v = {"activity_status": status, "age_bucket": 3, "count_bucket": 2,
         "src_ok": True, "cov_ok": False}
    if C._coherent(C._seal(1, "ethereum", "0x" + "b" * 40, 0, 0, 50, v)):
        print("a verdict passed _coherent with cov_ok false:", status)
        sys.exit(1)
sys.exit(0)
PY

check "cov_ok is on the compared axis, not merely stored" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
C = T.C
base = {"activity_status": C.A_INACTIVE, "age_bucket": 7, "count_bucket": 0,
        "src_ok": True, "cov_ok": True}
other = dict(base); other["cov_ok"] = False
mine = C._seal(1, "ethereum", "0x" + "b" * 40, 0, 0, 50, base)
theirs = C._seal(1, "ethereum", "0x" + "b" * 40, 0, 0, 50, other)
if C._agrees(theirs, mine):
    print("_agrees accepted a differing cov_ok")
    sys.exit(1)
if "cov=" not in C._canon(1, "ethereum", "0x" + "b" * 40, 0, 0, 50, base):
    print("the canonical projection does not commit to coverage")
    sys.exit(1)
sys.exit(0)
PY

check "the inbound flood cannot produce INACTIVE on any page shape" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
C, WEB = T.C, T.WEB
ALICE, STRANGER = T.ALICE, T.STRANGER
NOW, DAY = T.NOW, T.DAY
anchor = NOW - 10 * DAY
# The owner signs once after the anchor, then is buried under inbound traffic.
# Whatever the explorer does with the filter, and however deep the flood, the
# one verdict that must never come back is INACTIVE.
for honoured in (True, False):
    for n in (5, 10, 50, 120):
        WEB.reset()
        items = [T.tx(NOW - i * 60, STRANGER, to=ALICE) for i in range(n)]
        items.append(T.tx(anchor + DAY, ALICE))
        items.sort(key=lambda i: -int(i["timeStamp"]))
        (T.serve_txs if honoured else T.serve_unfiltered)(items)
        got = C._probe(1, "ethereum", str(ALICE), anchor, NOW,
                       C.TX_WINDOW)["activity_status"]
        if got == C.A_INACTIVE:
            print("released a living owner:", honoured, n, got)
            sys.exit(1)
sys.exit(0)
PY

check "no pagination loop (a round would rate-limit itself into silence)" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
fn = next(n for n in ast.walk(t)
          if isinstance(n, ast.FunctionDef) and n.name == "_probe")
# `eth.blockscout.com` answers 429 at roughly the third rapid request from one
# address, and a consensus round fires one probe per validator at once. The
# fetches must be a fixed, small number of straight-line calls — never a loop.
fetches = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
           and ast.unparse(c.func).endswith("_http")]
if len(fetches) != 2:
    print("expected exactly two _http calls, found", len(fetches))
    sys.exit(1)
for f in fetches:
    for loop in ast.walk(fn):
        if isinstance(loop, (ast.For, ast.While)) and f in ast.walk(loop):
            print("a fetch sits inside a loop")
            sys.exit(1)
sys.exit(0)
PY

check "at most two fetches per probe, measured rather than parsed" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
C, WEB = T.C, T.WEB
for setup in (T.serve_empty, T.serve_refused, T.serve_down,
              lambda: T.serve_txs([T.tx(T.NOW - T.HOUR, T.ALICE)])):
    WEB.reset()
    setup()
    C._probe(1, "ethereum", str(T.ALICE), T.NOW - T.DAY, T.NOW, C.TX_WINDOW)
    if len(WEB.log) > 2:
        print("probe made", len(WEB.log), "fetches")
        sys.exit(1)
sys.exit(0)
PY

check "both transaction spellings are read (v2 nests the signer)" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
C = T.C
legacy = T.tx(T.NOW, T.ALICE)
v2 = T.v2_tx(T.NOW, T.ALICE)
me = str(T.ALICE).lower()
# Reading only the flat spelling would make every v2 item look unsigned by
# anybody — zero signatures, which reads as INACTIVE.
if C._tx_from(legacy) != me or C._tx_from(v2) != me:
    print("a signer spelling is not read")
    sys.exit(1)
if C._tx_time(legacy) != T.NOW or C._tx_time(v2) != T.NOW:
    print("a timestamp spelling is not read")
    sys.exit(1)
sys.exit(0)
PY

check "a v2 error body is never mistaken for an empty wallet" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
C = T.C
# The v2 spelling of the `result: null` trap. HTTP 422 with an `errors` key and
# no `items` key at all — measured 2026-09-21. Reading it as "no transactions"
# would pay out an inheritance on a typo.
if C._parse_v2(T.V2_REFUSED_BODY)[0]:
    print("an error body parsed as an answer")
    sys.exit(1)
if not C._parse_v2(T.V2_EMPTY_BODY)[0]:
    print("a genuinely empty wallet was read as unreadable")
    sys.exit(1)
sys.exit(0)
PY

# ---------------------------------------------------------------------------
sec "10. Rule 1 — consensus binds every stored value"
check "exactly one run_nondet call (one round, one axis)" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
calls = [n for n in ast.walk(t) if isinstance(n, ast.Call)
         and ast.unparse(n.func).endswith("run_nondet")]
sys.exit(0 if len(calls) == 1 else 1)
PY

check "no language model is ever called" python3 - <<'PY'
import sys
sys.exit(1 if "exec_prompt" in open("contracts/WillExecutor.py").read() else 0)
PY

check "every claim-evidence field is written only by _record" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
will = next(n for n in ast.walk(t) if isinstance(n, ast.ClassDef) and n.name == "Will")
fields = [n.target.id for n in will.body if isinstance(n, ast.AnnAssign)]
evidence = [f for f in fields if f.startswith("last_") and f != "last_heartbeat"]
writers = {}
for n in ast.walk(t):
    if not isinstance(n, ast.FunctionDef):
        continue
    for sub in ast.walk(n):
        if not isinstance(sub, ast.Assign):
            continue
        for tg in sub.targets:
            if isinstance(tg, ast.Attribute) and tg.attr in evidence:
                writers.setdefault(tg.attr, set()).add(n.name)
bad = {f: sorted(w - {"create_will", "_record"}) for f, w in writers.items()
       if w - {"create_will", "_record"}}
if bad:
    print("evidence written outside _record:", bad)
sys.exit(1 if bad else 0)
PY

check "the content hash is recomputed after consensus, never trusted" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
for n in ast.walk(t):
    if isinstance(n, ast.FunctionDef) and n.name == "claim_inactive":
        src = ast.unparse(n)
        sys.exit(0 if "_fnv(_canon(" in src and "expected" in src else 1)
sys.exit(1)
PY

# ---------------------------------------------------------------------------
sec "11. The offline suite"
if python3 test/test_logic.py >/tmp/we_tests.log 2>&1; then
  N=$(grep -oE 'Ran [0-9]+ tests' /tmp/we_tests.log | grep -oE '[0-9]+')
  if [ "${N:-0}" -ge 200 ]; then ok "$N offline tests pass (the brief asks for 200+)"
  else bad "only ${N:-0} offline tests — the brief asks for 200+"; fi
else
  bad "the offline suite FAILS"
  tail -20 /tmp/we_tests.log
fi

check "the suite has a static undefined-name check" python3 - <<'PY'
import sys
sys.exit(0 if "def undefined_names" in open("test/test_logic.py").read() else 1)
PY

check "no undefined name anywhere in the contract, class bodies included" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
p = T.undefined_names(T.SOURCE)
if p:
    print(p)
sys.exit(1 if p else 0)
PY

# ---------------------------------------------------------------------------
sec "12. The brief's surface"
check "every method the brief names exists" python3 - <<'PY'
import ast, sys
REQUIRED = ["create_will", "heartbeat", "top_up", "claim_inactive",
            "cancel_will", "change_beneficiary", "settle_stalled",
            "get_will", "get_wills_by_owner", "get_wills_by_beneficiary",
            "get_claimable_wills", "get_stats", "get_config", "verify_claim"]
t = ast.parse(open("contracts/WillExecutor.py").read())
names = {n.name for n in ast.walk(t) if isinstance(n, ast.FunctionDef)}
missing = [r for r in REQUIRED if r not in names]
if missing:
    print("missing:", missing)
sys.exit(1 if missing else 0)
PY

check "claim_payout exists (rule 7: accepted value must have an exit)" python3 - <<'PY'
import sys
sys.exit(0 if "def claim_payout" in open("contracts/WillExecutor.py").read() else 1)
PY

check "exactly two payable methods: create_will and top_up" python3 - <<'PY'
import ast, sys
t = ast.parse(open("contracts/WillExecutor.py").read())
payable = {n.name for n in ast.walk(t) if isinstance(n, ast.FunctionDef)
           and any(ast.unparse(d) == "gl.public.write.payable" for d in n.decorator_list)}
sys.exit(0 if payable == {"create_will", "top_up"} else 1)
PY

check "the finder fee defaults to 5% and is capped" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
sys.exit(0 if T.C.DEFAULT_FINDER_FEE_BPS == 500 and T.C.MAX_FINDER_FEE_BPS <= 2000 else 1)
PY

check "the brief's interval bounds are the canonical defaults (7–365 days)" python3 - <<'PY'
import sys
sys.path.insert(0, "test")
import test_logic as T
sys.exit(0 if T.C.MIN_INTERVAL_S == 7 * 86400 and T.C.MAX_INTERVAL_S == 365 * 86400 else 1)
PY

# ---------------------------------------------------------------------------
sec "13. Contract size"
BYTES=$(wc -c < "$WE" | tr -d ' ')
if [ "$BYTES" -lt 200000 ]; then ok "contract is ${BYTES} bytes (Studio Dev has taken 121 KB; measured)"
else bad "contract is ${BYTES} bytes — past anything measured"; fi

# ---------------------------------------------------------------------------
sec "14. The docs describe the contract that is actually deployed"
# A README naming a dead address documents a contract nobody can open, and it
# is the single easiest thing to get wrong after a redeploy.
check "README and EVIDENCE.md carry the live addresses from deployments.json" python3 - <<'PY'
import json, sys
d = json.load(open("deployments.json"))["deployments"]["studiodev"]
readme = open("README.md").read()
ev = open("docs/EVIDENCE.md").read()
missing = []
for name in ("WillExecutor", "WillExecutorDemo"):
    addr = d[name]["address"]
    if addr not in readme:
        missing.append(f"README missing {name} {addr}")
    if addr not in ev:
        missing.append(f"EVIDENCE.md missing {name} {addr}")
for m in missing:
    print(m)
sys.exit(1 if missing else 0)
PY

check "the source on disk is the source that was deployed" python3 - <<'PY'
import hashlib, json, os, sys
# Written by tools/verify_artifact.mjs; absent means it has not been checked
# since the last deploy, which is a fail rather than a skip.
if not os.path.exists("docs/artifact-check.json"):
    print("docs/artifact-check.json missing — run: node tools/verify_artifact.mjs")
    sys.exit(1)
rec = json.load(open("docs/artifact-check.json"))
local = hashlib.sha256(open("contracts/WillExecutor.py", "rb").read().rstrip(b"\n")).hexdigest()
if rec.get("source_sha256") != local:
    print(f"source has changed since the last artifact check:\n  recorded {rec.get('source_sha256')}\n  on disk  {local}")
    sys.exit(1)
d = json.load(open("deployments.json"))["deployments"]["studiodev"]
for name in ("WillExecutor", "WillExecutorDemo"):
    got = rec.get("contracts", {}).get(name, {})
    if got.get("address") != d[name]["address"]:
        print(f"{name}: checked {got.get('address')} but deployments.json says {d[name]['address']}")
        sys.exit(1)
    if not got.get("match"):
        print(f"{name}: deployed bytes do NOT match the local source")
        sys.exit(1)
sys.exit(0)
PY

# ---------------------------------------------------------------------------
sec "15. The live deploy"
if [ "$WITH_CHAIN" -eq 0 ]; then
  skp "on-chain assertions" "pass --chain to run them"
elif [ ! -f deployments.json ]; then
  bad "deployments.json is missing"
else
  if node tools/audit_chain.mjs; then ok "the live contracts answer and their books balance"
  else bad "the on-chain audit failed"; fi
fi

# ---------------------------------------------------------------------------
printf '\n\033[1m%d passed, %d failed, %d skipped\033[0m\n' "$PASS" "$FAIL" "$SKIP"
exit "$FAIL"
