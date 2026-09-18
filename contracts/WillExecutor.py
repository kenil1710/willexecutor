# v0.3.0
# { "Depends": "py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng" }
import genlayer as gl
from genlayer import *
from dataclasses import dataclass
import json
import typing

# WillExecutor - a dead man's switch that checks whether you are actually dead.
#
# An owner deposits GEN, names a beneficiary and a check-in interval, and calls
# heartbeat() to say they are still here. Miss enough check-ins and ANYONE may
# call claim_inactive(). That does not release the money. It opens a consensus
# round in which every validator independently fetches the owner's wallet
# history from Blockscout and votes on a feature vector. If the wallet has
# SIGNED anything since the last heartbeat, the owner is alive, the claim fails
# and nothing moves - however many check-ins they missed.
#
# Design notes and hazards: contracts/NOTES.md.
#
# The two header lines above are the whole of what GenVM reads before the code:
# the version line and the runner pin, in that order. NOTHING else may sit
# between line 1 and the imports - GenVM parses the contiguous leading `#` block
# as the runner header, and a stray comment there makes the contract
# undeployable, reporting nothing but `invalid_contract`. Lint does not catch
# it. It has cost three previous projects a deploy each.
#
# TEN RULES govern everything below. Every one of them is a past rejection
# written down so that it cannot happen again.
#
#   1. CONSENSUS BINDS EVERY STORED VALUE. Not the verdict - every field. A
#      field the validators did not compare is a field the leader can forge,
#      and a forged INACTIVE verdict empties a living person's estate. So the
#      compared axis is the whole FEATURE VECTOR: activity status, age bucket,
#      count bucket, source-availability flag and the content hash over all of
#      them. The corollary is enforced in the other direction too: this
#      contract STORES NOTHING IT DID NOT COMPARE. There is deliberately no
#      raw-body digest in storage - see rule 9 and NOTES.md §2.
#
#   2. NO PUBLIC WRITE EVER RAISES. There is not one `raise` statement in this
#      file. A revert rolls back storage but NOT the value that came with the
#      call, which then sits in the contract unaccounted for and unreachable.
#      Every refusal books the incoming value to a pull ledger and RETURNS
#      {"status": "REJECTED", "reason": ...}. Generalising this from "payable
#      methods" to "all of them" costs nothing and removes the version nobody
#      looks for: value arriving at a method that was never meant to receive
#      any. See `_refuse`.
#
#   3. NO COUNTER MOVES BEFORE A PATH THAT CAN STILL REFUSE. Every increment
#      happens after the last possible refusal. `total_rejected` is the single
#      exception, because it is a statistic ABOUT refusals.
#
#   4. THE DEPOSIT AND THE FINDER FEE ARE SNAPSHOTTED. `deposit_wei` is what
#      this will holds; `finder_fee_bps` is fixed at deploy with no setter. An
#      owner who could raise the finder fee tomorrow could restate the price of
#      a claim already in flight, and an owner who could lower it could starve
#      the bounty that makes the switch work at all.
#
#   5. A WILL IS FROZEN THE MOMENT IT REACHES A TERMINAL STATUS. Nothing - not
#      the owner, not a pause, not a second claim_inactive() - mutates an
#      EXECUTED or CANCELLED will. `_live_will` is the single gate and every
#      mutating method goes through it.
#
#   6. THE OWNER CANNOT FREEZE USER MONEY. `claim_payout`, `settle_stalled`,
#      `claim_inactive`, `heartbeat` and `cancel_will` are ALL ungated on
#      `paused`. Pause stops NEW wills and does nothing else. An owner who
#      could strand a deposit could extort a beneficiary, which is worse than
#      forging a verdict because it needs no validators at all. In particular
#      `settle_stalled` works while paused, by design and by test.
#
#   7. VALUE THE CONTRACT ACCEPTS IS VALUE SOMEBODY CAN GET BACK OUT. The
#      ledger identity, asserted after every single operation offline and
#      published by `get_stats` on chain:
#
#          balance_wei == locked_wei + payable_wei
#
#      Everything held is either locked in a live will (and every terminal
#      status converts it into payouts) or already somebody's to claim. THERE
#      IS NO THIRD BUCKET AND NO PROTOCOL REVENUE: the finder fee is paid out
#      of the deposit to the CALLER, never to the owner, so the owner has no
#      withdraw method at all - not a gated one, none.
#
#   8. CONSERVATIVE WHEN THE DATA IS NOT THERE. An explorer that does not
#      answer produces INCONCLUSIVE, which changes nothing and can be retried.
#      It is on the compared axis PRECISELY BECAUSE it is not a verdict:
#      validators must AGREE the source was unavailable, or one node's bad
#      minute silently becomes everybody's payout. Every ambiguous read - a
#      non-200, an unparseable body, a `result` that is not a list - lands
#      here. The failure direction is always "the money stays where it is".
#
#   9. ONLY A SIGNATURE PROVES LIFE. Activity means transactions the wallet
#      SENT, never ones it received. A dead wallet still receives - airdrops,
#      dust, refunds - and if an inbound transfer counted as a heartbeat then
#      any stranger could keep any will locked for ever for the price of one
#      wei. Only the key holder can sign. `_probe` filters on `from` and the
#      offline suite proves a wallet buried in inbound traffic still reads
#      INACTIVE. The brief did not ask for this; the contract would have been
#      griefable without it.
#
#  10. THE EXPLORER URL IS DERIVED FROM AN ALLOWLIST, NEVER SUPPLIED BY A
#      CALLER. A caller who could name the URL could point every validator at
#      a server they control and manufacture any verdict they liked. There is
#      no code path in this file that accepts a host, a URL or a path from
#      calldata. `chain` selects a key in `CHAIN_HOSTS` and nothing else.
#
# str.replace() is rejected by the runner; slice around find() instead.

RUBRIC_VERSION = "1.0.0"

# --- the explorer allowlist (rule 10).
#
# MEASURED 2026-09-18 against the live hosts, not assumed. All six answer the
# legacy `/api?module=account&action=txlist` endpoint with HTTP 200 and the same
# schema.
#
# `optimism.blockscout.com`, `gnosis.blockscout.com` and
# `eth-holesky.blockscout.com` are NOT in the list, and the reason is worth
# writing down: the first two answer 301 to a redirect the fetcher does not
# follow, and the third answers 404. A host added here without being measured
# would be a chain whose every probe came back INCONCLUSIVE - so no will on it
# could ever execute, silently and for ever, in a way that reads from the
# outside exactly like "this owner is still alive".
CHAIN_HOSTS = {
    "ethereum": "eth.blockscout.com",
    "base": "base.blockscout.com",
    "arbitrum": "arbitrum.blockscout.com",
    "polygon": "polygon.blockscout.com",
    # The two testnets are here on purpose, not as filler. A dead man's switch
    # is a thing you want to have REHEARSED before you put an estate behind it,
    # and rehearsing it on a chain where a transaction costs nothing is the
    # only way anybody sensibly will.
    "sepolia": "eth-sepolia.blockscout.com",
    "base-sepolia": "base-sepolia.blockscout.com",
}
CHAINS = ("ethereum", "base", "arbitrum", "polygon", "sepolia",
          "base-sepolia")
DEFAULT_CHAIN = "ethereum"

# How many transactions a probe asks for.
#
# The v2 endpoint `/api/v2/addresses/{a}/transactions` returns a fixed page of
# 50 and measured 530 KB on a busy wallet. The legacy endpoint takes an
# `offset` and returned the same facts in 4.5 KB. Every validator pays this
# cost on every probe, so the smaller one is not a micro-optimisation: it is
# the difference between a round that settles and a round that times out.
#
# Ten is enough to answer the only question asked - "did this wallet sign
# anything after the anchor" - because the list is sorted newest-first, so if
# the tenth-newest is already older than the anchor, the eleventh cannot help.
TX_WINDOW = 10

# --- statuses. ACTIVE is live; the other two are terminal and freeze the will
# for ever (rule 5).
W_ACTIVE = "ACTIVE"
W_EXECUTED = "EXECUTED"
W_CANCELLED = "CANCELLED"
LIVE_STATUSES = (W_ACTIVE,)
TERMINAL_STATUSES = (W_EXECUTED, W_CANCELLED)
ALL_STATUSES = (W_ACTIVE, W_EXECUTED, W_CANCELLED)

# --- the activity verdict. Three values, all three on the consensus axis.
A_NONE = ""
A_ALIVE = "ALIVE"
A_INACTIVE = "INACTIVE"
A_INCONCLUSIVE = "INCONCLUSIVE"
VERDICTS = (A_ALIVE, A_INACTIVE, A_INCONCLUSIVE)

# --- intervals. The brief's bounds: at least a week, at most a year.
#
# They are CONSTRUCTOR ARGUMENTS rather than module constants for exactly one
# reason, and it is the same reason CourtRoom's deadlines were: a claim path
# gated on a 14-day minimum cannot be DEMONSTRATED on chain, only asserted
# offline - and a payment path nobody has watched execute is a payment path
# nobody has tested. A second instance with second-scale bounds makes the whole
# heartbeat -> miss -> claim -> release lifecycle observable in one run.
#
# The thing worth guarding against was never the value. It was an owner who
# could RETUNE it, shortening an interval under a living owner to time a claim
# onto them. Fixed before any will exists, unchangeable for ever, and published
# by `get_config` to anyone who asks, it cannot do that. There is no setter and
# the offline suite walks the AST to prove there is no setter.
MIN_INTERVAL_S = 7 * 86400
MAX_INTERVAL_S = 365 * 86400

# What ONE UNIT of `check_in_days` is worth in seconds.
#
# 86400 on the canonical deploy, which makes `create_will(heir, 30)` mean
# thirty days exactly as the brief specifies. It is a constructor argument for
# the same reason the bounds are: a claim gated on a fourteen-day wait cannot
# be DEMONSTRATED on chain, only asserted offline, and a release path nobody
# has watched execute is a release path nobody has tested. A second instance
# deployed from this same source with the unit set to one second makes the
# whole lifecycle - create, check in, miss, claim, release, withdraw -
# observable in a couple of minutes.
#
# It is published by `get_config` as `interval_unit_s` alongside a plain-words
# `interval_unit`, so nobody has to infer which clock a given instance is on.
DEFAULT_INTERVAL_UNIT_S = 86400
MIN_INTERVAL_UNIT_S = 1
FLOOR_INTERVAL_S = 30          # the lowest a deploy may set the minimum to
CEIL_INTERVAL_S = 3650 * 86400

# How many whole intervals must elapse with no heartbeat before a claim opens.
# Two by default: one missed check-in is a holiday, two is a pattern.
DEFAULT_MISSED_THRESHOLD = 2
MIN_MISSED_THRESHOLD = 1
MAX_MISSED_THRESHOLD = 10

# --- the finder fee. Paid to whoever calls claim_inactive() on a will that
# really is dormant, OUT OF THE DEPOSIT. It is the entire economic engine: a
# dead man's switch nobody is paid to pull is a dead man's switch that never
# gets pulled. The protocol keeps nothing (rule 7).
BPS = 10000
DEFAULT_FINDER_FEE_BPS = 500   # 5%, the brief's figure
MAX_FINDER_FEE_BPS = 2000

# --- money. u256 throughout. A previous project capped value fields at u64 and
# discovered at 18.44 GEN that it had put a ceiling on an inheritance.
MIN_DEPOSIT_WEI = 10 ** 15     # 0.001 GEN
MAX_DEPOSIT_WEI = 10 ** 24

# --- a consensus round that never came back.
STALL_TTL = 48 * 3600
MIN_TTL = 60
MAX_TTL = 30 * 86400

# --- capacity
MAX_WILLS = 5000
SCAN_CAP = 400                 # wills a list view will walk
PAGE_CAP = 60                  # cards a list view will return
MAX_PAGE = 50
MAX_REASON_CHARS = 600

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# --- the feature-vector ladders.
#
# Seven bounds each, so `_rank` answers 0..7 - the 0-7 scale the brief asks
# for, expressed as a closed vocabulary rather than as a rounding step applied
# after the fact.
#
# THE QUANTISATION IS WHAT MAKES THE VECTOR COMPARABLE AT ALL. A previous
# project measured validators disagreeing about one round in four on a content
# digest of a document whose every decision-relevant field was identical,
# because Blockscout is a load-balanced cluster whose replicas index at
# slightly different rates. Raw values cannot go on a consensus axis. Day-scale
# buckets can: a replica half a block behind cannot move a wallet from the
# "under a week" rung to the "over a month" rung.
#
# Where a bucket CAN still disagree - a replica that has not indexed the newest
# transaction at all - the round simply does not settle, nothing changes, and
# anyone may call again. That is the safe direction, and it is the direction
# this whole file is tilted in.
AGE_LADDER = (1, 3, 7, 14, 30, 90, 180)       # days since the newest signature
COUNT_LADDER = (1, 2, 4, 8, 16, 32, 64)       # signed transactions in the window
TOP_BUCKET = 7

# An age bucket for "this wallet has never signed anything we can see". It is
# the top rung, which is the most-dormant reading - correct, and also the
# reading that releases money, so it is reached only through `src_ok`.
AGE_NEVER = TOP_BUCKET


# --- pure helpers ----------------------------------------------------------


def _flat(s: typing.Any) -> str:
    """Collapse whitespace. A stored string with a newline in it breaks every
    CSV and every log line downstream."""
    return " ".join(str(s).split())


def _clean(s: typing.Any, n: int) -> str:
    """Flattened, control-stripped, length-capped. Everything that reaches
    storage goes through here, once, at the boundary."""
    out = []
    for ch in _flat(s):
        o = ord(ch)
        if o < 32 or o == 127:
            continue
        out.append(ch)
        if len(out) >= n:
            break
    return "".join(out)


def _as_int(v: typing.Any, default: int = 0) -> int:
    """An int from whatever arrived on calldata.

    `bool` is excluded ON PURPOSE. Python makes `True` an int of value 1, so an
    argument that arrived as a boolean would silently read as 1 rather than as
    junk, and `isinstance(v, int)` alone cannot tell the two apart."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        t = v.strip()
        neg = t.startswith("-")
        if neg:
            t = t[1:]
        if t == "" or not t.isdigit():
            return default
        return -int(t) if neg else int(t)
    return default


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else (hi if v > hi else v)


def _rank(n: int, ladder: tuple) -> int:
    """How many of the ladder's lower bounds `n` has reached, 0..len(ladder).
    The one place a bucket edge is interpreted in this file."""
    r = 0
    for bound in ladder:
        if n >= bound:
            r += 1
    return r


def _is_addr(text: typing.Any) -> bool:
    """A 0x-prefixed 20-byte hex string, checked character by character.

    Used on calldata BEFORE `Address()` is constructed from it, because
    `Address("nonsense")` raises and rule 2 says nothing in this file may."""
    t = str(text).strip()
    if len(t) != 42 or not t.startswith("0x"):
        return False
    for ch in t[2:]:
        if ch not in "0123456789abcdefABCDEF":
            return False
    return True


def _lower(text: typing.Any) -> str:
    return str(text).strip().lower()


def _days_from_civil(y: int, m: int, d: int) -> int:
    """Days from 1970-01-01 to a civil date. Howard Hinnant's algorithm.

    Written out rather than imported because the block time arrives as an ISO
    string, and a date routine on the consensus axis should be one anyone can
    read and check."""
    y -= 1 if m <= 2 else 0
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _epoch_from_iso(value: typing.Any) -> int:
    """Seconds since the epoch from an ISO-8601 instant, by hand.

    The source is `gl.message.raw["datetime"]` - the block time, which is part
    of the transaction and therefore IDENTICAL on every validator. THERE IS NO
    block.timestamp ON THIS CHAIN, and a wall-clock read per node would put the
    difference between two nodes' clocks straight onto the consensus axis: a
    check-in interval would expire at a different instant for each of them, and
    an age bucket computed against a different "now" would differ by a rung
    whenever a probe landed near a boundary."""
    if not isinstance(value, str) or len(value) < 19:
        return 0
    try:
        year = int(value[0:4])
        month = int(value[5:7])
        day = int(value[8:10])
        hour = int(value[11:13])
        minute = int(value[14:16])
        second = int(value[17:19])
    except Exception:
        return 0
    if month < 1 or month > 12 or day < 1 or day > 31:
        return 0
    if hour > 23 or minute > 59 or second > 60:
        return 0
    return (_days_from_civil(year, month, day) * 86400
            + hour * 3600 + minute * 60 + second)


def _fnv(s: str) -> str:
    """FNV-1a, 64-bit, hex. The content hash (rule 1).

    Written out rather than imported because it must produce the same digest on
    every validator and years later inside `verify_claim`. A hash library whose
    implementation could differ across runner builds would put the commitment
    itself on the disagreement axis."""
    h = 0xCBF29CE484222325
    for ch in s:
        h ^= ord(ch) & 0xFF
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return format(h, "016x")


def _gen(wei: typing.Any) -> str:
    """Wei as a decimal GEN string, by integer arithmetic only.

    No float anywhere near money. A float in a nondet return is not calldata
    encodable, and a float in a settlement puts a platform's rounding mode on
    the consensus axis."""
    n = _as_int(wei, 0)
    sign = "-" if n < 0 else ""
    n = -n if n < 0 else n
    whole = n // 10 ** 18
    frac = n % 10 ** 18
    text = str(frac)
    while len(text) < 18:
        text = "0" + text
    while len(text) > 2 and text[-1] == "0":
        text = text[:-1]
    return sign + str(whole) + "." + text


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


# --- the explorer ----------------------------------------------------------


def _url(host: str, wallet: str, window: int) -> str:
    """The probe URL. Built ONLY from the allowlist host and the wallet address
    that is already in storage (rule 10).

    The legacy `txlist` endpoint rather than `/api/v2/...` for two measured
    reasons. It accepts `offset`, so the body is 4.5 KB instead of 530 KB - a
    cost every validator pays on every probe. And its `timeStamp` is a Unix
    integer rather than an ISO string, so the one field the verdict turns on
    needs no date parsing and carries no timezone to disagree about."""
    return ("https://" + host + "/api?module=account&action=txlist&address="
            + wallet + "&sort=desc&page=1&offset=" + str(int(window)))


def _http(url: str) -> tuple:
    """(status, body) for a plain GET. NEVER RAISES; a dead host is (0, "").

    Both spellings of the web API are tried because prior projects split
    between them and a settlement path must not die on which one a runner build
    happens to expose."""
    try:
        try:
            res = gl.nondet.web.request(url, method="GET")
        except AttributeError:
            res = gl.nondet.web.get(url)
    except Exception:
        return (0, "")
    status = getattr(res, "status_code", None)
    if status is None:
        status = getattr(res, "status", None)
    body = getattr(res, "body", None)
    if body is None:
        body = getattr(res, "text", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="ignore")
    try:
        code = int(status) if status is not None else 0
    except Exception:
        code = 0
    return (code, str(body) if body is not None else "")


def _parse(body: str) -> tuple:
    """(readable, items) from a txlist response. Never raises.

    THE MOST IMPORTANT FOUR LINES IN THIS FILE, and the reason they are written
    out rather than folded into a truthiness check:

        {"status":"1","message":"OK","result":[ ... ]}
            the wallet has transactions

        {"status":"0","message":"No transactions found","result":[]}
            the wallet has NONE - a real answer, and the one that releases
            money

        {"status":"0","message":"Invalid address format","result":null}
            the explorer REFUSED the query and told us nothing

    All three arrive as HTTP 200 with `status: "0"` on two of them, so `status`
    cannot distinguish them. `result` can: a LIST is an answer, `null` is a
    refusal. MEASURED against the live endpoint 2026-09-18.

    Reading the third as the second - which any `result or []` would do, and
    which is the obvious way to write this - would treat "we could not ask" as
    "this wallet is dormant" and pay out an inheritance on a typo. It fails the
    other way here: not a list means INCONCLUSIVE, which changes nothing."""
    if not body:
        return (False, [])
    try:
        doc = json.loads(body)
    except Exception:
        return (False, [])
    if not isinstance(doc, dict):
        return (False, [])
    items = doc.get("result")
    if not isinstance(items, list):
        return (False, [])
    return (True, items)


def _tx_time(item: typing.Any) -> int:
    """The Unix second a transaction was mined, or 0."""
    if not isinstance(item, dict):
        return 0
    return _as_int(item.get("timeStamp"), 0)


def _tx_from(item: typing.Any) -> str:
    """The SIGNER of a transaction, lowercased (rule 9)."""
    if not isinstance(item, dict):
        return ""
    return _lower(item.get("from") or "")


def _vector(status: str, age_bucket: int, count_bucket: int,
            src_ok: bool) -> dict:
    """The compared axis, in one shape, built in one place.

    Five fields, and `content_hash` is added by `_seal` once the will's
    identity is known. Nothing else the probe learns survives into storage -
    see rule 1 and NOTES.md §2."""
    return {
        "activity_status": str(status),
        "age_bucket": _clamp(_as_int(age_bucket, TOP_BUCKET), 0, TOP_BUCKET),
        "count_bucket": _clamp(_as_int(count_bucket, 0), 0, TOP_BUCKET),
        "src_ok": bool(src_ok),
    }


def _canon(will_id: int, chain: str, wallet: str, anchor_ts: int,
           now_ts: int, window: int, vector: dict) -> str:
    """The canonical projection: the exact string the content hash commits to.

    It binds the QUESTION as well as the ANSWER - will id, chain, wallet and
    the anchor the probe measured against - so a vector honestly computed for
    one will cannot be replayed onto another. Every input is either on-chain
    storage or the block time, so it is identical on every validator by
    construction.

    The field order is fixed and the separators cannot occur in any field, so
    two different projections cannot canonicalise to one string."""
    return "|".join([
        "v" + RUBRIC_VERSION,
        "will=" + str(int(will_id)),
        "chain=" + str(chain),
        "wallet=" + _lower(wallet),
        "anchor=" + str(int(anchor_ts)),
        "now=" + str(int(now_ts)),
        "window=" + str(int(window)),
        "status=" + str(vector.get("activity_status", "")),
        "age=" + str(_as_int(vector.get("age_bucket"), -1)),
        "count=" + str(_as_int(vector.get("count_bucket"), -1)),
        "src=" + ("1" if bool(vector.get("src_ok")) else "0"),
    ])


def _seal(will_id: int, chain: str, wallet: str, anchor_ts: int, now_ts: int,
          window: int, vector: dict) -> dict:
    """Add the content hash to a vector. The only place one is computed."""
    out = dict(vector)
    out["content_hash"] = _fnv(_canon(will_id, chain, wallet, anchor_ts,
                                      now_ts, window, vector))
    return out


def _probe(will_id: int, chain: str, wallet: str, anchor_ts: int, now_ts: int,
           window: int) -> dict:
    """Fetch the wallet's history and project it onto the feature vector.

    THE ONLY NON-DETERMINISTIC STEP IN THIS CONTRACT, and the whole reason it
    is on GenLayer. A conventional contract can compare `last_heartbeat` to a
    deadline and nothing else; it cannot know whether the person is still
    signing transactions somewhere. Every validator runs this function for
    itself, against the live explorer, and votes on what it got back.

    THERE IS NO LANGUAGE MODEL HERE, deliberately. The question - did this
    wallet sign anything after this timestamp - is a matter of fact, not of
    judgement, and a model on the consensus axis would add a disagreement
    source to a question that has a right answer. The intelligence GenLayer
    supplies here is INDEPENDENT VERIFICATION OF AN EXTERNAL FACT by every
    validator, which is exactly what no single-node chain can do. See
    NOTES.md §3 for why that is the right call and not a shortcut.

    Every failure lands on INCONCLUSIVE (rule 8)."""
    host = CHAIN_HOSTS.get(str(chain))
    if not host:
        # Unreachable through any public path - `create_will` validates the
        # chain against the same table - but a probe that cannot name its host
        # must still return a vector rather than fall off the end.
        return _vector(A_INCONCLUSIVE, AGE_NEVER, 0, False)

    status, body = _http(_url(host, _lower(wallet), window))
    if status != 200:
        return _vector(A_INCONCLUSIVE, AGE_NEVER, 0, False)

    readable, items = _parse(body)
    if not readable:
        return _vector(A_INCONCLUSIVE, AGE_NEVER, 0, False)

    # RULE 9. Only transactions this wallet SIGNED count. `from` is the signer;
    # an inbound transfer says something about the sender, not about whether
    # the owner is alive to press a button.
    me = _lower(wallet)
    signed = []
    for item in items:
        if _tx_from(item) == me:
            when = _tx_time(item)
            if when > 0:
                signed.append(when)

    newest = 0
    for when in signed:
        if when > newest:
            newest = when

    since_anchor = 0
    for when in signed:
        if when > anchor_ts:
            since_anchor += 1

    if newest <= 0:
        age_bucket = AGE_NEVER
    else:
        # A transaction stamped in the future - a badly-configured indexer, or
        # a chain whose clock runs ahead - reads as age zero rather than as a
        # negative rung. It is also, correctly, the most-alive reading.
        gap = now_ts - newest
        age_days = 0 if gap < 0 else gap // 86400
        age_bucket = _rank(age_days, AGE_LADDER)

    count_bucket = _rank(len(signed), COUNT_LADDER)

    # THE VERDICT IS ANCHORED TO AN ON-CHAIN VALUE, not to a relative window.
    # "Has this wallet signed anything since the owner last checked in" is a
    # question about two fixed timestamps, so it gives the same answer on every
    # validator and the same answer tomorrow. "Anything in the last N days"
    # would move under the probe every time it ran.
    verdict = A_ALIVE if since_anchor > 0 else A_INACTIVE
    return _vector(verdict, age_bucket, count_bucket, True)


def _reason(vector: dict, chain: str, window: int) -> str:
    """The stored explanation, COMPOSED from the agreed vector - never written.

    Two nodes asked to describe what they saw produce two different sentences,
    and a stored sentence the validators never compared is a stored value the
    leader forged (rule 1). So this is a pure function of fields that are
    already on the consensus axis: every validator composes the same string,
    and `verify_claim` re-derives it character for character years later."""
    status = str(vector.get("activity_status", ""))
    age = _as_int(vector.get("age_bucket"), TOP_BUCKET)
    count = _as_int(vector.get("count_bucket"), 0)
    where = "the " + str(chain) + " explorer"

    if status == A_INCONCLUSIVE:
        return ("No verdict: " + where + " did not return a readable "
                "transaction list for this wallet. Nothing was changed and "
                "this claim can be made again once the explorer answers.")

    if status == A_ALIVE:
        return ("Still alive: this wallet signed at least one transaction "
                "AFTER the owner's last heartbeat, according to " + where
                + ". The newest signature is " + AGE_WORDS[age]
                + ", and " + COUNT_WORDS[count] + " in the last "
                + str(int(window)) + " " + _plural(int(window),
                                                   "transaction", "transactions")
                + " examined. Missing a check-in is not the same as being "
                "gone, so the deposit stays where it is.")

    return ("No sign of life: " + where + " shows no transaction signed by "
            "this wallet since the owner's last heartbeat. The newest "
            "signature is " + AGE_WORDS[age] + ", and " + COUNT_WORDS[count]
            + " in the last " + str(int(window)) + " "
            + _plural(int(window), "transaction", "transactions")
            + " examined. Inbound transfers were ignored: only a signature "
            "proves the key holder is present.")


# The bucket vocabularies the reasoning reads from. Indexed 0..7 to match the
# ladders exactly, so a rung can never be described as a rung it is not.
AGE_WORDS = (
    "less than a day old",
    "one to three days old",
    "three to seven days old",
    "one to two weeks old",
    "two to four weeks old",
    "one to three months old",
    "three to six months old",
    "over six months old, or absent entirely",
)
COUNT_WORDS = (
    "no signature was found at all",
    "one signature was found",
    "two or three signatures were found",
    "four to seven signatures were found",
    "eight to fifteen signatures were found",
    "sixteen to thirty-one signatures were found",
    "thirty-two to sixty-three signatures were found",
    "sixty-four or more signatures were found",
)


def _coherent(payload: typing.Any) -> bool:
    """Is the leader's payload internally well formed?

    A PURE GATE ON THE LEADER'S OWN CALLDATA - it reads nothing but the payload
    itself, so it gives the same answer on every validator and lets a node
    refuse an incoherent leader WITHOUT itself becoming a source of
    disagreement. It runs before the validator spends a network fetch.

    Every field is checked for type and range, because `_agrees` compares
    values and two fields that are both nonsense can still be equal."""
    if not isinstance(payload, dict):
        return False

    status = payload.get("activity_status")
    if not isinstance(status, str) or status not in VERDICTS:
        return False

    src = payload.get("src_ok")
    if not isinstance(src, bool):
        return False

    # `bool` is an `int` in Python, so a bucket that arrived as `True` would
    # otherwise pass as bucket 1.
    for key in ("age_bucket", "count_bucket"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if value < 0 or value > TOP_BUCKET:
            return False

    digest = payload.get("content_hash")
    if not isinstance(digest, str) or len(digest) != 16:
        return False
    for ch in digest:
        if ch not in "0123456789abcdef":
            return False

    # THE ONE CROSS-FIELD RULE, and it is the rule that stops the only forgery
    # worth attempting. INCONCLUSIVE means the source did not answer, so it is
    # the only verdict compatible with src_ok being false - and a verdict that
    # RELEASES MONEY is not. A leader claiming INACTIVE while admitting it
    # could not read the explorer is refused here, by every validator, without
    # any of them needing to fetch anything.
    if bool(src) != (status != A_INCONCLUSIVE):
        return False

    # An unreadable source cannot have counted anything. Pinning the buckets
    # removes the free variables from the one verdict that is not evidence-
    # backed, so two INCONCLUSIVE payloads are always byte-identical.
    if status == A_INCONCLUSIVE:
        if _as_int(payload.get("age_bucket"), -1) != AGE_NEVER:
            return False
        if _as_int(payload.get("count_bucket"), -1) != 0:
            return False

    # A wallet that has signed nothing at all cannot be alive on the evidence.
    if status == A_ALIVE and _as_int(payload.get("count_bucket"), -1) <= 0:
        return False

    return True


def _agrees(theirs: typing.Any, mine: dict) -> bool:
    """Does the leader's vector match this validator's, field for field?

    RULE 1, discharged. Five fields, and the comparison is EXACT - there is no
    tolerance here, and the tolerance that does exist lives in the ladders
    where a bucket is wide enough to absorb a replica being half a block
    behind. A tolerance in the comparison instead would mean two accepted
    verdicts for one claim could differ, and then which one released the
    money?"""
    if not isinstance(theirs, dict) or not isinstance(mine, dict):
        return False
    for key in ("activity_status", "age_bucket", "count_bucket", "src_ok",
                "content_hash"):
        if key not in theirs or key not in mine:
            return False
    if str(theirs["activity_status"]) != str(mine["activity_status"]):
        return False
    if bool(theirs["src_ok"]) != bool(mine["src_ok"]):
        return False
    # Buckets are compared as INTEGERS OF THE RIGHT TYPE, not as whatever
    # `_as_int` can coerce. Coercing here would make the string "7" equal to
    # the integer 7, so a leader could ship a differently-typed payload that
    # compared equal on every node and then stored as something else. That is
    # defence in depth - `_coherent` already refuses a non-int bucket - but the
    # comparison is the last line before storage, and the last line should not
    # be the lenient one.
    for key in ("age_bucket", "count_bucket"):
        left = theirs[key]
        right = mine[key]
        if isinstance(left, bool) or not isinstance(left, int):
            return False
        if isinstance(right, bool) or not isinstance(right, int):
            return False
        if left != right:
            return False
    if str(theirs["content_hash"]) != str(mine["content_hash"]):
        return False
    return True


def _split(deposit: int, fee_bps: int) -> tuple:
    """(to_beneficiary, to_finder) for a deposit that is being released.

    EXACT, WITH NO REMAINDER: the two always sum to the deposit. Integer
    division floors the finder's cut, so the leftover wei goes to the
    BENEFICIARY - the party the contract exists to serve, rather than the
    bounty hunter. Proved over the whole cross product of deposit and fee
    offline."""
    d = _as_int(deposit, 0)
    if d <= 0:
        return (0, 0)
    bps = _clamp(_as_int(fee_bps, 0), 0, MAX_FINDER_FEE_BPS)
    finder = (d * bps) // BPS
    if finder > d:
        finder = d
    return (d - finder, finder)


def _pay(who: Address, amount: int) -> None:
    """Send native value to an address. THE ONLY WAY MONEY LEAVES THIS
    CONTRACT.

    Written out here rather than inlined because getting it wrong is SILENT.
    The obvious-looking spelling, inherited from an earlier project:

        _Payee(who).emit(value=u256(amount))

    posts NO MESSAGE AT ALL on this runner. `Proxy.emit()` returns a method
    GETTER - a namespace you are then supposed to call a method on - so an
    `emit()` with nothing after it constructs an object and drops it. Every
    payout appeared to succeed: the transaction settled ACCEPTED, the ledger
    zeroed, the call returned OK, and not one wei moved. It was caught by
    comparing the CONTRACT'S ON-CHAIN BALANCE before and after a claim, which
    is the only check that could have caught it.

    `emit_transfer` is the spelling that posts a bare value transfer, and
    `gl.chain.Account` is the wrapper documented for ANY on-chain account,
    contract or EOA.

    `on="finalized"` is the default and is kept deliberately. A payout applied
    at ACCEPTED would already have happened if the transaction that authorised
    it were later appealed and rolled back - the contract would have executed a
    will it no longer owed. Slow is the direction to be wrong in when the
    mistake is irreversible.

    STUDIO DEV QUEUES THIS MESSAGE AND DOES NOT EXECUTE IT. Measured
    independently by two previous projects, three ways each: the message is
    posted with the right recipient and the right value, the parent transaction
    reaches FINALIZED, and no balance moves. It is a property of the network,
    not of this contract, and it is REPORTED rather than hidden - `get_stats`
    publishes the contract's real chain balance beside its own books and names
    the gap `undelivered_wei`. On a network that delivers, that number is
    zero."""
    if amount <= 0:
        return
    gl.chain.Account(who).emit_transfer(u256(int(amount)))


# --- storage ---------------------------------------------------------------


@gl.storage.allow
@dataclass
class Will:
    """One dead man's switch.

    EVERY FIELD BELOW THE LINE MARKED `--- claim evidence` IS WRITTEN ONLY FROM
    AN AGREED CONSENSUS VECTOR (rule 1). There is no field in this struct that
    a leader can set to a value the validators did not compare, and the offline
    suite enumerates the struct and fails if one appears."""
    will_id: u32
    owner: Address
    beneficiary: Address
    chain: str

    deposit_wei: u256
    check_in_interval_s: u64
    last_heartbeat: u64
    created_at: u64
    status: str

    heartbeat_count: u32
    top_up_count: u32
    beneficiary_changes: u32

    # --- claim evidence. Every one of these is a field of the agreed vector,
    # or a pure function of it and of values that were on chain before the
    # round opened.
    claim_attempts: u32
    last_claim_at: u64
    last_verdict: str
    last_age_bucket: u32
    last_count_bucket: u32
    last_src_ok: bool
    last_content_hash: str
    last_anchor_ts: u64
    last_now_ts: u64
    last_window: u32
    last_reason: str

    # --- settlement, written once, only on an INACTIVE verdict.
    executed_at: u64
    finder: Address
    finder_fee_bps: u32
    paid_beneficiary_wei: u256
    paid_finder_wei: u256
    refunded_wei: u256


class WillExecutor(gl.contract.Contract):
    # --- ownership. The owner can pause NEW wills and nothing else. There is
    # no method by which an owner touches a will, a deposit or a verdict, and
    # there is no revenue to withdraw because the contract keeps nothing at all
    # (rules 6 and 7).
    owner: Address
    paused: bool

    # --- written once, in the constructor, and never again. The offline suite
    # walks the AST to prove no method assigns any of them.
    min_interval_s: u64
    max_interval_s: u64
    interval_unit_s: u64
    missed_threshold: u32
    finder_fee_bps: u32
    stall_ttl_s: u64

    # --- the ledger. `balance_wei == locked_wei + payable_wei` holds after
    # every operation, and the offline suite asserts it after every one.
    balance_wei: u256
    locked_wei: u256
    payable_wei: u256
    payout_wei: gl.storage.TreeMap[Address, u256]
    claimed_total_wei: u256

    # --- the register
    wills: gl.storage.DynArray[Will]
    by_owner: gl.storage.TreeMap[Address, gl.storage.DynArray[u32]]
    by_beneficiary: gl.storage.TreeMap[Address, gl.storage.DynArray[u32]]
    executed_ids: gl.storage.DynArray[u32]
    status_counts: gl.storage.TreeMap[str, u32]
    verdict_counts: gl.storage.TreeMap[str, u32]

    # --- one ACTIVE will per wallet, and an in-flight claim marker.
    active_will_of: gl.storage.TreeMap[Address, u32]
    claiming: gl.storage.TreeMap[str, u64]

    # --- counters
    next_id: u32
    total_wills: u256
    total_executed: u256
    total_cancelled: u256
    total_rejected: u256
    total_claims_attempted: u256
    total_deposited_wei: u256
    total_released_wei: u256
    total_finder_fees_wei: u256
    total_claimed_wei: u256

    def __init__(self, min_interval_s: int = MIN_INTERVAL_S,
                 max_interval_s: int = MAX_INTERVAL_S,
                 interval_unit_s: int = DEFAULT_INTERVAL_UNIT_S,
                 missed_threshold: int = DEFAULT_MISSED_THRESHOLD,
                 finder_fee_bps: int = DEFAULT_FINDER_FEE_BPS,
                 stall_ttl_s: int = STALL_TTL):
        self.owner = gl.message.sender_address
        self.paused = False

        # Clamped rather than rejected: a deploy that fails on a mistyped
        # constructor argument wastes a whole deploy, and the ceiling is the
        # real rule either way.
        lo = _clamp(_as_int(min_interval_s, MIN_INTERVAL_S),
                    FLOOR_INTERVAL_S, CEIL_INTERVAL_S)
        hi = _clamp(_as_int(max_interval_s, MAX_INTERVAL_S),
                    FLOOR_INTERVAL_S, CEIL_INTERVAL_S)
        if hi < lo:
            hi = lo
        self.min_interval_s = u64(lo)
        self.max_interval_s = u64(hi)
        self.interval_unit_s = u64(_clamp(
            _as_int(interval_unit_s, DEFAULT_INTERVAL_UNIT_S),
            MIN_INTERVAL_UNIT_S, DEFAULT_INTERVAL_UNIT_S))
        self.missed_threshold = u32(_clamp(
            _as_int(missed_threshold, DEFAULT_MISSED_THRESHOLD),
            MIN_MISSED_THRESHOLD, MAX_MISSED_THRESHOLD))
        self.finder_fee_bps = u32(_clamp(
            _as_int(finder_fee_bps, DEFAULT_FINDER_FEE_BPS), 0,
            MAX_FINDER_FEE_BPS))
        self.stall_ttl_s = u64(_clamp(_as_int(stall_ttl_s, STALL_TTL),
                                      MIN_TTL, MAX_TTL))

        self.balance_wei = u256(0)
        self.locked_wei = u256(0)
        self.payable_wei = u256(0)
        self.claimed_total_wei = u256(0)
        self.next_id = u32(1)
        self.total_wills = u256(0)
        self.total_executed = u256(0)
        self.total_cancelled = u256(0)
        self.total_rejected = u256(0)
        self.total_claims_attempted = u256(0)
        self.total_deposited_wei = u256(0)
        self.total_released_wei = u256(0)
        self.total_finder_fees_wei = u256(0)
        self.total_claimed_wei = u256(0)

    # --- internals ---------------------------------------------------------

    def _now(self) -> int:
        """Block time, from the message. Identical on every validator, which is
        what lets a check-in deadline sit on the consensus axis at all."""
        return _epoch_from_iso(gl.message.raw.get("datetime", ""))

    def _bank(self) -> int:
        """Book incoming value AND MAKE IT THE SENDER'S, immediately.

        Everything that arrives belongs to whoever sent it until this contract
        has a reason to hold it, and `_take` is the only thing that gives it
        one. Written this way after the obvious alternative - bank here, refund
        again in the refusal path - DOUBLE-CREDITED any value attached to a
        method that refused. A stranger sending 1 GEN to a call that refuses
        them came away owed 2. The bug was in the SHAPE of the accounting, not
        in any one path, which is why no individual method looked wrong.

        The fix is not another check. It is that there is now exactly one place
        value becomes the sender's (here) and exactly one place it stops being
        theirs (`_take`), so a double credit is not expressible.

        Called as the FIRST STATEMENT of every write, payable or not. A
        non-payable method should never see value; if the runner ever let one
        through, that value still has an owner and a way out rather than
        becoming an unaccounted balance (rule 7)."""
        value = int(gl.message.value)
        if value > 0:
            self.balance_wei = u256(int(self.balance_wei) + value)
            self._credit(gl.message.sender_address, value)
        return value

    def _take(self, who: Address, amount: int) -> bool:
        """Move value out of a sender's claimable balance and lock it into a
        will. THE ONLY WAY VALUE STOPS BEING THE SENDER'S.

        Returns False rather than raising if the balance is short, so a caller
        can refuse cleanly - though it cannot happen through any public path,
        because every one of them checks the amount sent before it gets
        here."""
        if amount <= 0:
            return True
        have = int(self.payout_wei.get(who) or 0)
        if have < amount:
            return False
        self.payout_wei[who] = u256(have - amount)
        self.payable_wei = u256(int(self.payable_wei) - amount)
        self.locked_wei = u256(int(self.locked_wei) + amount)
        return True

    def _credit(self, who: Address, amount: int) -> None:
        """Move value into somebody's claimable balance. The only way value
        leaves a will, and the only way a deposit comes back."""
        if amount <= 0:
            return
        self.payout_wei[who] = u256(int(self.payout_wei.get(who) or 0) + amount)
        self.payable_wei = u256(int(self.payable_wei) + amount)

    def _release(self, amount: int) -> None:
        """Unlock value from the will register. Always paired with `_credit`."""
        if amount <= 0:
            return
        held = int(self.locked_wei)
        self.locked_wei = u256(held - amount if held >= amount else 0)

    def _refuse(self, reason: str, extra: typing.Any = None) -> dict:
        """RULE 2. EVERY refusal in this contract comes through here.

        A revert would roll back the storage write that recorded the deposit
        while leaving the value itself in the contract - unaccounted for, and
        unreachable by anybody. So nothing raises: a REJECTED object is
        returned, and the caller reads `status` rather than guessing from a
        revert reason that arrives empty half the time.

        IT DOES NOT CREDIT ANYTHING. `_bank` already made the deposit the
        sender's on the first line of the method, and refusing simply means
        never calling `_take`. A refund here as well would pay it twice."""
        value = int(gl.message.value)
        self.total_rejected = u256(int(self.total_rejected) + 1)
        out = {"status": "REJECTED", "reason": str(reason),
               "refunded_wei": str(value),
               "claim_with": "claim_payout()"}
        if isinstance(extra, dict):
            for key in extra:
                out[key] = extra[key]
        return out

    def _is_owner(self) -> bool:
        return gl.message.sender_address == self.owner

    def _will(self, will_id: typing.Any) -> typing.Any:
        """The will with this id, or None. Ids are 1-based and dense, so the
        index is the id minus one - but the bound is CHECKED rather than
        assumed, because an out-of-range index on a DynArray is a revert, and a
        revert is rule 2 broken."""
        wid = _as_int(will_id, 0)
        if wid < 1 or wid > len(self.wills):
            return None
        return self.wills[wid - 1]

    def _live_will(self, will_id: typing.Any) -> tuple:
        """RULE 5, in one place. Returns (will, error_or_empty).

        A will at a terminal status is FROZEN. Every mutating method calls this
        and refuses on a non-empty error, so there is no path - not a second
        claim, not a late heartbeat, not an owner, not a pause - by which an
        executed or cancelled will changes. Returning the error rather than
        raising is what lets every caller obey rule 2 through one gate."""
        will = self._will(will_id)
        if will is None:
            return (None, "no will with id " + str(_as_int(will_id, 0)))
        status = str(will.status)
        if status in TERMINAL_STATUSES:
            return (None, "will " + str(int(will.will_id)) + " is "
                    + status.lower() + " and can no longer change")
        return (will, "")

    def _bump_status(self, old: str, new: str) -> None:
        if old:
            have = int(self.status_counts.get(old) or 0)
            if have > 0:
                self.status_counts[old] = u32(have - 1)
        self.status_counts[new] = u32(int(self.status_counts.get(new) or 0) + 1)

    def _unit_word(self, n: int) -> str:
        """"days" or "seconds", whichever clock this instance is on. One place,
        so no message can describe an interval in a unit the contract is not
        actually using."""
        if int(self.interval_unit_s) == DEFAULT_INTERVAL_UNIT_S:
            return _plural(n, "day", "days")
        return _plural(n, "second", "seconds")

    def _units(self, seconds: int) -> int:
        """An interval expressed back in this instance's own units."""
        unit = int(self.interval_unit_s)
        return int(seconds) // (unit if unit > 0 else 1)

    def _deadline(self, will: Will) -> int:
        """The instant after which this will may be claimed.

        `last_heartbeat + interval * missed_threshold`, computed from the
        will's OWN snapshotted interval and the deploy-time threshold. Both are
        immutable, so this number cannot move under an owner who is still
        alive."""
        return (int(will.last_heartbeat)
                + int(will.check_in_interval_s) * int(self.missed_threshold))

    def _overdue(self, will: Will, now: int) -> bool:
        return str(will.status) == W_ACTIVE and now > self._deadline(will)

    def _claim_open(self, will_id: int) -> int:
        """When the in-flight claim on this will started, or 0."""
        return int(self.claiming.get(str(int(will_id))) or 0)

    def _facts(self, will: Will) -> dict:
        """The will as PLAIN PYTHON VALUES, copied out of storage.

        Every field is forced through `str()` or `int()` here. A storage
        reference carried into a nondet closure kills the leader mid-round with
        no usable error, and it took a day to find the first time. Doing the
        copy in one named place means no future caller can forget."""
        return {
            "will_id": int(will.will_id),
            "owner": will.owner.as_hex,
            "beneficiary": will.beneficiary.as_hex,
            "chain": str(will.chain),
            "deposit_wei": int(will.deposit_wei),
            "interval_s": int(will.check_in_interval_s),
            "last_heartbeat": int(will.last_heartbeat),
        }

    def _record(self, will: Will, vector: dict, facts: dict, now: int,
                window: int) -> None:
        """Write the agreed vector into the will. THE ONLY PLACE CLAIM EVIDENCE
        IS STORED, and it stores nothing that is not in `vector` or was not on
        chain before the round opened (rule 1).

        `last_reason` is RECOMPUTED here from the agreed vector rather than
        copied out of the leader's payload, so even a leader that shipped a
        different sentence cannot get it into storage."""
        will.claim_attempts = u32(int(will.claim_attempts) + 1)
        will.last_claim_at = u64(now)
        will.last_verdict = str(vector.get("activity_status", ""))
        will.last_age_bucket = u32(_clamp(
            _as_int(vector.get("age_bucket"), TOP_BUCKET), 0, TOP_BUCKET))
        will.last_count_bucket = u32(_clamp(
            _as_int(vector.get("count_bucket"), 0), 0, TOP_BUCKET))
        will.last_src_ok = bool(vector.get("src_ok"))
        will.last_content_hash = str(vector.get("content_hash", ""))
        will.last_anchor_ts = u64(int(facts["last_heartbeat"]))
        will.last_now_ts = u64(now)
        will.last_window = u32(int(window))
        will.last_reason = _clean(
            _reason(vector, str(facts["chain"]), window), MAX_REASON_CHARS)
        key = str(vector.get("activity_status", ""))
        if key:
            self.verdict_counts[key] = u32(
                int(self.verdict_counts.get(key) or 0) + 1)

    def _execute(self, will: Will, finder: Address, now: int) -> dict:
        """Release a will to its beneficiary. THE ONLY PLACE A WILL PAYS OUT.

        Conserves exactly: `to_beneficiary + to_finder == deposit`, with the
        remainder wei going to the beneficiary. After this returns the will is
        terminal and `_live_will` refuses every further mutation of it."""
        deposit = int(will.deposit_wei)
        fee_bps = int(self.finder_fee_bps)
        to_beneficiary, to_finder = _split(deposit, fee_bps)

        self._release(deposit)
        self._credit(will.beneficiary, to_beneficiary)
        self._credit(finder, to_finder)

        old = str(will.status)
        will.status = W_EXECUTED
        will.executed_at = u64(now)
        will.finder = finder
        will.finder_fee_bps = u32(fee_bps)
        will.paid_beneficiary_wei = u256(to_beneficiary)
        will.paid_finder_wei = u256(to_finder)
        self._bump_status(old, W_EXECUTED)

        wid = int(will.will_id)
        self.executed_ids.append(u32(wid))
        self.active_will_of[will.owner] = u32(0)
        self.total_executed = u256(int(self.total_executed) + 1)
        self.total_released_wei = u256(
            int(self.total_released_wei) + to_beneficiary)
        self.total_finder_fees_wei = u256(
            int(self.total_finder_fees_wei) + to_finder)

        return {
            "to_beneficiary_wei": str(to_beneficiary),
            "to_beneficiary_gen": _gen(to_beneficiary),
            "to_finder_wei": str(to_finder),
            "to_finder_gen": _gen(to_finder),
            "beneficiary": will.beneficiary.as_hex,
            "finder": finder.as_hex,
            "finder_fee_bps": fee_bps,
        }

    def _timeline(self, will: Will, now: int) -> list:
        """What has happened to this will, and what happens next."""
        out = []
        out.append({"at": int(will.created_at), "event": "CREATED",
                    "detail": "deposit " + _gen(int(will.deposit_wei))
                              + " GEN, check in every "
                              + str(self._units(int(will.check_in_interval_s)))
                              + " " + self._unit_word(
                                  self._units(int(will.check_in_interval_s)))})
        if int(will.heartbeat_count) > 0:
            out.append({"at": int(will.last_heartbeat), "event": "HEARTBEAT",
                        "detail": str(int(will.heartbeat_count)) + " "
                                  + _plural(int(will.heartbeat_count),
                                            "check-in", "check-ins")
                                  + " so far"})
        if int(will.claim_attempts) > 0:
            out.append({"at": int(will.last_claim_at), "event": "CLAIM_CHECKED",
                        "detail": str(will.last_verdict) + " after "
                                  + str(int(will.claim_attempts)) + " "
                                  + _plural(int(will.claim_attempts),
                                            "attempt", "attempts")})
        if str(will.status) == W_EXECUTED:
            out.append({"at": int(will.executed_at), "event": "EXECUTED",
                        "detail": _gen(int(will.paid_beneficiary_wei))
                                  + " GEN to the beneficiary"})
        elif str(will.status) == W_CANCELLED:
            out.append({"at": int(will.executed_at), "event": "CANCELLED",
                        "detail": _gen(int(will.refunded_wei))
                                  + " GEN returned to the owner"})
        else:
            out.append({"at": self._deadline(will), "event": "CLAIMABLE_AT",
                        "detail": "claimable in "
                                  + str(max(0, self._deadline(will) - now))
                                  + "s"})
        return out

    def _view(self, will: Will, now: int) -> dict:
        """The full public shape of one will."""
        deposit = int(will.deposit_wei)
        to_beneficiary, to_finder = _split(deposit, int(self.finder_fee_bps))
        deadline = self._deadline(will)
        claim_started = self._claim_open(int(will.will_id))
        return {
            "will_id": int(will.will_id),
            "owner": will.owner.as_hex,
            "beneficiary": will.beneficiary.as_hex,
            "chain": str(will.chain),
            "status": str(will.status),
            "deposit_wei": str(deposit),
            "deposit_gen": _gen(deposit),
            "check_in_interval_s": int(will.check_in_interval_s),
            "check_in_interval_units": self._units(
                int(will.check_in_interval_s)),
            "interval_unit_s": int(self.interval_unit_s),
            "last_heartbeat": int(will.last_heartbeat),
            "created_at": int(will.created_at),
            "heartbeat_count": int(will.heartbeat_count),
            "top_up_count": int(will.top_up_count),
            "beneficiary_changes": int(will.beneficiary_changes),
            "missed_threshold": int(self.missed_threshold),
            "claimable_at": deadline,
            "seconds_until_claimable": max(0, deadline - now),
            "is_claimable": bool(self._overdue(will, now)),
            "claim_in_flight": bool(claim_started > 0),
            "claim_started_at": claim_started,
            "claim_stalls_at": (claim_started + int(self.stall_ttl_s)
                                if claim_started > 0 else 0),
            "if_executed": {
                "to_beneficiary_wei": str(to_beneficiary),
                "to_beneficiary_gen": _gen(to_beneficiary),
                "to_finder_wei": str(to_finder),
                "to_finder_gen": _gen(to_finder),
                "finder_fee_bps": int(self.finder_fee_bps),
            },
            # Built from `_evidence_of` so that `get_will` and the receipt a
            # claim returns describe the same reading in the same words. It
            # carries `age_meaning` and `count_meaning` because a reader should
            # not have to know that bucket 7 means "over six months old, or
            # absent entirely" in order to understand why their money moved.
            "evidence": self._evidence_of(will),
            "settlement": {
                "executed_at": int(will.executed_at),
                "finder": will.finder.as_hex,
                "finder_fee_bps": int(will.finder_fee_bps),
                "paid_beneficiary_wei": str(will.paid_beneficiary_wei),
                "paid_finder_wei": str(will.paid_finder_wei),
                "refunded_wei": str(will.refunded_wei),
            },
            "timeline": self._timeline(will, now),
        }

    def _card(self, will: Will, now: int) -> dict:
        """The short shape a list view returns."""
        deadline = self._deadline(will)
        return {
            "will_id": int(will.will_id),
            "owner": will.owner.as_hex,
            "beneficiary": will.beneficiary.as_hex,
            "chain": str(will.chain),
            "status": str(will.status),
            "deposit_wei": str(int(will.deposit_wei)),
            "deposit_gen": _gen(int(will.deposit_wei)),
            "check_in_interval_units": self._units(
                int(will.check_in_interval_s)),
            "last_heartbeat": int(will.last_heartbeat),
            "claimable_at": deadline,
            "seconds_until_claimable": max(0, deadline - now),
            "is_claimable": bool(self._overdue(will, now)),
            "claim_in_flight": bool(self._claim_open(int(will.will_id)) > 0),
            "activity_status": str(will.last_verdict),
        }

    # --- writes ------------------------------------------------------------

    @gl.public.write.payable
    def create_will(self, beneficiary: str, check_in_days: typing.Any,
                    chain: str = DEFAULT_CHAIN) -> typing.Any:
        """Open a dead man's switch. The GEN sent with the call is the estate.

        `chain` names which explorer the validators will consult about this
        owner's wallet, and it is validated against `CHAIN_HOSTS` here - the
        only place a caller influences the probe URL at all, and it influences
        it by picking a key, never by supplying text (rule 10)."""
        value = self._bank()
        now = self._now()
        sender = gl.message.sender_address

        if self.paused:
            return self._refuse("new wills are paused")
        if now <= 0:
            return self._refuse("the block time was unreadable; nothing was "
                                "changed and this call can be retried")
        if len(self.wills) >= MAX_WILLS:
            return self._refuse("this register is full at "
                                + str(MAX_WILLS) + " wills")

        if not _is_addr(beneficiary):
            return self._refuse("beneficiary must be a 0x-prefixed 20-byte "
                                "address")
        beneficiary_addr = Address(str(beneficiary).strip())
        if beneficiary_addr.as_hex == ZERO_ADDRESS:
            return self._refuse("the zero address cannot inherit")
        if beneficiary_addr == sender:
            return self._refuse("the beneficiary cannot be the owner; a will "
                                "that pays you back is a locked wallet, not "
                                "an inheritance")

        chain_key = _lower(chain) if chain else DEFAULT_CHAIN
        if chain_key not in CHAIN_HOSTS:
            return self._refuse("chain must be one of " + ", ".join(CHAINS),
                                {"supported_chains": list(CHAINS)})

        days = _as_int(check_in_days, 0)
        if days <= 0:
            return self._refuse("check_in_days must be a positive whole "
                                "number of days")
        interval = days * int(self.interval_unit_s)
        lo = int(self.min_interval_s)
        hi = int(self.max_interval_s)
        if interval < lo or interval > hi:
            return self._refuse(
                "check-in interval must be between " + str(lo) + "s and "
                + str(hi) + "s",
                {"min_interval_s": lo, "max_interval_s": hi})

        if value < MIN_DEPOSIT_WEI:
            return self._refuse("a will must be funded with at least "
                                + _gen(MIN_DEPOSIT_WEI) + " GEN",
                                {"sent_wei": str(value)})
        if value > MAX_DEPOSIT_WEI:
            return self._refuse("a single will may hold at most "
                                + _gen(MAX_DEPOSIT_WEI) + " GEN")

        # RULE: one ACTIVE will per wallet. A wallet whose will has been
        # executed or cancelled is free to open another - the limit is on
        # concurrent wills, not on lifetime ones, because the alternative
        # would permanently retire a wallet the moment its first will closed.
        open_id = int(self.active_will_of.get(sender) or 0)
        if open_id > 0:
            return self._refuse(
                "this wallet already has an active will (#" + str(open_id)
                + "); cancel it before opening another",
                {"active_will_id": open_id})

        if not self._take(sender, value):
            # Unreachable: `_bank` credited exactly `value` to `sender` on the
            # first line. Checked anyway, because locking value that was not
            # banked would break the ledger identity silently.
            return self._refuse("the deposit could not be locked")

        # --- past the last refusal. Counters move only from here (rule 3).
        wid = int(self.next_id)
        will = self.wills.append_new_get()
        will.will_id = u32(wid)
        will.owner = sender
        will.beneficiary = beneficiary_addr
        will.chain = chain_key
        will.deposit_wei = u256(value)
        will.check_in_interval_s = u64(interval)
        will.last_heartbeat = u64(now)
        will.created_at = u64(now)
        will.status = W_ACTIVE
        will.heartbeat_count = u32(0)
        will.top_up_count = u32(0)
        will.beneficiary_changes = u32(0)
        will.claim_attempts = u32(0)
        will.last_claim_at = u64(0)
        will.last_verdict = A_NONE
        will.last_age_bucket = u32(0)
        will.last_count_bucket = u32(0)
        will.last_src_ok = False
        will.last_content_hash = ""
        will.last_anchor_ts = u64(0)
        will.last_now_ts = u64(0)
        will.last_window = u32(0)
        will.last_reason = ""
        will.executed_at = u64(0)
        will.finder = Address(ZERO_ADDRESS)
        will.finder_fee_bps = u32(0)
        will.paid_beneficiary_wei = u256(0)
        will.paid_finder_wei = u256(0)
        will.refunded_wei = u256(0)

        # `get_or_insert_default`, NEVER `self.by_owner[sender]`. Indexing a
        # TreeMap with a key it does not hold RAISES KeyError on the runner —
        # which would be a revert, on the one path that has just banked a
        # deposit, which is rule 2 broken in the most expensive place there is.
        # The offline stub used to auto-create the entry and hid this
        # completely; it now raises exactly as the runner does.
        self.by_owner.get_or_insert_default(sender).append(u32(wid))
        self.by_beneficiary.get_or_insert_default(
            beneficiary_addr).append(u32(wid))
        self.active_will_of[sender] = u32(wid)
        self._bump_status("", W_ACTIVE)
        self.next_id = u32(wid + 1)
        self.total_wills = u256(int(self.total_wills) + 1)
        self.total_deposited_wei = u256(int(self.total_deposited_wei) + value)

        return {
            "status": "OK",
            "will_id": wid,
            "owner": sender.as_hex,
            "beneficiary": beneficiary_addr.as_hex,
            "chain": chain_key,
            "deposit_wei": str(value),
            "deposit_gen": _gen(value),
            "check_in_interval_s": interval,
            "claimable_at": now + interval * int(self.missed_threshold),
            "note": ("call heartbeat(" + str(wid) + ") at least every "
                     + str(days) + " " + self._unit_word(days)
                     + "; after " + str(int(self.missed_threshold))
                     + " missed intervals anyone may call claim_inactive("
                     + str(wid) + ")"),
        }

    @gl.public.write
    def heartbeat(self, will_id: typing.Any) -> typing.Any:
        """I am still here. Resets the timer. Free, and never gated on
        `paused` (rule 6) - an owner who could stop you checking in could time
        a claim onto you."""
        self._bank()
        now = self._now()
        sender = gl.message.sender_address

        will, error = self._live_will(will_id)
        if error:
            return self._refuse(error)
        if now <= 0:
            return self._refuse("the block time was unreadable; nothing was "
                                "changed and this call can be retried")
        if will.owner != sender:
            return self._refuse("only the owner of will #"
                                + str(int(will.will_id))
                                + " can check in on it")

        # A heartbeat DOES NOT clear an in-flight claim marker, and that is
        # deliberate. The marker means validators are mid-round on this will;
        # letting the owner clear it would let them race a claim that has
        # already fetched its evidence. The heartbeat still counts - it moves
        # the anchor - and the round in flight will read the OLD anchor it
        # opened with, which is the honest reading of the question that was
        # asked. See NOTES.md §5.
        will.last_heartbeat = u64(now)
        will.heartbeat_count = u32(int(will.heartbeat_count) + 1)

        return {
            "status": "OK",
            "will_id": int(will.will_id),
            "last_heartbeat": now,
            "heartbeat_count": int(will.heartbeat_count),
            "claimable_at": self._deadline(will),
            "next_check_in_by": now + int(will.check_in_interval_s),
        }

    @gl.public.write.payable
    def top_up(self, will_id: typing.Any) -> typing.Any:
        """Add GEN to an existing will. Owner only."""
        value = self._bank()
        now = self._now()
        sender = gl.message.sender_address

        will, error = self._live_will(will_id)
        if error:
            return self._refuse(error)
        if self.paused:
            return self._refuse("top-ups are paused")
        if will.owner != sender:
            return self._refuse("only the owner of will #"
                                + str(int(will.will_id)) + " can top it up")

        # Refused while a claim is in flight, for the same reason `cancel_will`
        # and `change_beneficiary` are.
        #
        # A top-up does two things a live round must not have moving underneath
        # it: it changes `deposit_wei`, which is the amount a settlement pays
        # out, and it RESETS `last_heartbeat`, which is the anchor the
        # validators measured against. Leaving this ungated made `top_up` the
        # one owner method that could move the anchor during the window a stuck
        # marker holds open — so an owner could escape a stalled claim for the
        # price of one wei, while the two sibling methods that touch the same
        # will were refused. `heartbeat` deliberately does not clear a marker
        # either (see NOTES.md §8); this closes the same door on the payable
        # path.
        wid = int(will.will_id)
        started = self._claim_open(wid)
        if started > 0 and now - started < int(self.stall_ttl_s):
            return self._refuse(
                "a claim on will #" + str(wid) + " is in flight; it must "
                "resolve, or be cleared with settle_stalled(" + str(wid)
                + "), before this will can be topped up",
                {"claim_started_at": started,
                 "stalls_at": started + int(self.stall_ttl_s)})

        if value <= 0:
            return self._refuse("send some GEN to top up with")
        new_total = int(will.deposit_wei) + value
        if new_total > MAX_DEPOSIT_WEI:
            return self._refuse("a single will may hold at most "
                                + _gen(MAX_DEPOSIT_WEI) + " GEN",
                                {"current_wei": str(int(will.deposit_wei))})
        if not self._take(sender, value):
            return self._refuse("the top-up could not be locked")

        will.deposit_wei = u256(new_total)
        will.top_up_count = u32(int(will.top_up_count) + 1)
        self.total_deposited_wei = u256(int(self.total_deposited_wei) + value)

        # A top-up is a message from the owner's own key, so it is also proof
        # of life. Not counting it would mean an owner who funded their will
        # and nothing else could be claimed against while visibly present.
        will.last_heartbeat = u64(now if now > 0 else int(will.last_heartbeat))

        return {
            "status": "OK",
            "will_id": int(will.will_id),
            "added_wei": str(value),
            "added_gen": _gen(value),
            "deposit_wei": str(new_total),
            "deposit_gen": _gen(new_total),
            "top_up_count": int(will.top_up_count),
            "claimable_at": self._deadline(will),
            "note": "a top-up counts as a check-in; the timer has been reset",
        }

    @gl.public.write
    def claim_inactive(self, will_id: typing.Any) -> typing.Any:
        """Ask the validators whether this owner has gone quiet.

        PERMISSIONLESS - anyone may call, and whoever calls earns the finder
        fee if the wallet really is dormant. That is the whole economic engine:
        a dead man's switch nobody is paid to pull never gets pulled.

        Not gated on `paused` (rule 6). An owner who could suspend claims could
        strand a beneficiary indefinitely.

        A caller who is wrong loses nothing but gas, by design. If the owner is
        alive the claim simply fails, and the evidence of that is written into
        the will for anyone to read."""
        self._bank()
        now = self._now()
        sender = gl.message.sender_address

        will, error = self._live_will(will_id)
        if error:
            return self._refuse(error)
        if now <= 0:
            return self._refuse("the block time was unreadable; nothing was "
                                "changed and this call can be retried")

        wid = int(will.will_id)
        deadline = self._deadline(will)
        if now <= deadline:
            return self._refuse(
                "will #" + str(wid) + " is not overdue; it becomes claimable "
                "in " + str(deadline - now) + "s",
                {"claimable_at": deadline,
                 "seconds_remaining": deadline - now})

        # In-flight guard. SELF-HEALING: a round that never settles applies no
        # state at all, so a failed claim leaves no marker behind to brick the
        # will. `settle_stalled` exists for the case the network leaves one.
        started = self._claim_open(wid)
        if started > 0 and now - started < int(self.stall_ttl_s):
            return self._refuse(
                "a claim on will #" + str(wid) + " is already in flight",
                {"claim_started_at": started,
                 "stalls_at": started + int(self.stall_ttl_s)})

        facts = self._facts(will)
        self.claiming[str(wid)] = u64(now)
        self.total_claims_attempted = u256(
            int(self.total_claims_attempted) + 1)

        # Every value the closure reads is a PLAIN str/int copied out of
        # storage by `_facts`. A nondet closure that captures `self` or a
        # storage object pickles storage and kills the leader at run time 0s.
        f_id = int(facts["will_id"])
        f_chain = str(facts["chain"])
        f_wallet = str(facts["owner"])
        f_anchor = int(facts["last_heartbeat"])
        f_now = int(now)
        f_window = int(TX_WINDOW)

        def leader_fn() -> dict:
            return _seal(f_id, f_chain, f_wallet, f_anchor, f_now, f_window,
                         _probe(f_id, f_chain, f_wallet, f_anchor, f_now,
                                f_window))

        def validator_fn(leader_result: gl.vm.Result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                # A leader ERROR must be re-run, never voted False. Answering
                # False turns a transient fetch failure into a genuine
                # disagreement and burns a round for nothing.
                leader_fn()
                return False
            theirs = leader_result.calldata
            # A pure gate on the leader's OWN calldata: identical for every
            # validator, so an incoherent leader is refused without this node
            # ever becoming a source of UNDETERMINED - and without spending a
            # network fetch on a payload that cannot be right.
            if not _coherent(theirs):
                return False
            mine = _seal(f_id, f_chain, f_wallet, f_anchor, f_now, f_window,
                         _probe(f_id, f_chain, f_wallet, f_anchor, f_now,
                                f_window))
            return _agrees(theirs, mine)

        out = gl.vm.run_nondet(leader_fn, validator_fn)

        # Re-gate the agreed payload before a single field of it is stored.
        # `run_nondet` returns what the validators accepted, and accepting is
        # not the same as being well formed - a payload that somehow arrived
        # malformed must not become storage (rule 1).
        if not _coherent(out):
            self.claiming[str(wid)] = u64(0)
            return self._refuse(
                "the validators did not return a usable reading; nothing was "
                "changed and this claim can be made again")

        vector = {
            "activity_status": str(out.get("activity_status", "")),
            "age_bucket": _as_int(out.get("age_bucket"), TOP_BUCKET),
            "count_bucket": _as_int(out.get("count_bucket"), 0),
            "src_ok": bool(out.get("src_ok")),
            "content_hash": str(out.get("content_hash", "")),
        }

        # THE CONTENT HASH IS RECOMPUTED, NOT TRUSTED. The leader supplies a
        # digest; this recomputes it from the fields the validators actually
        # compared and from the will's own identity, and refuses if they
        # differ. It is the difference between a commitment and a decoration.
        expected = _fnv(_canon(f_id, f_chain, f_wallet, f_anchor, f_now,
                               f_window, vector))
        if expected != vector["content_hash"]:
            self.claiming[str(wid)] = u64(0)
            return self._refuse(
                "the reading did not hash to its own contents; nothing was "
                "changed and this claim can be made again")

        self._record(will, vector, facts, now, f_window)
        self.claiming[str(wid)] = u64(0)
        verdict = vector["activity_status"]

        if verdict == A_ALIVE:
            return {
                "status": "OK",
                "outcome": A_ALIVE,
                "will_id": wid,
                "released": False,
                "reason": str(will.last_reason),
                "evidence": self._evidence_of(will),
                "note": ("the owner's wallet has signed a transaction since "
                         "their last check-in, so the deposit stays where it "
                         "is"),
            }

        if verdict == A_INCONCLUSIVE:
            return {
                "status": "OK",
                "outcome": A_INCONCLUSIVE,
                "will_id": wid,
                "released": False,
                "reason": str(will.last_reason),
                "evidence": self._evidence_of(will),
                "note": ("the explorer did not answer; nothing changed and "
                         "this claim can be made again shortly"),
            }

        settlement = self._execute(will, sender, now)
        return {
            "status": "OK",
            "outcome": A_INACTIVE,
            "will_id": wid,
            "released": True,
            "reason": str(will.last_reason),
            "evidence": self._evidence_of(will),
            "settlement": settlement,
            "claim_with": "claim_payout()",
        }

    @gl.public.write
    def cancel_will(self, will_id: typing.Any) -> typing.Any:
        """Close a will and take the deposit back. Owner only.

        NOT gated on `paused` (rule 6): an owner who could stop you withdrawing
        could hold your estate hostage. Refused while a claim is in flight,
        because a will whose evidence is already being fetched must not be
        able to evaporate underneath the validators reading it."""
        self._bank()
        now = self._now()
        sender = gl.message.sender_address

        will, error = self._live_will(will_id)
        if error:
            return self._refuse(error)
        if will.owner != sender:
            return self._refuse("only the owner of will #"
                                + str(int(will.will_id)) + " can cancel it")

        wid = int(will.will_id)
        started = self._claim_open(wid)
        if started > 0 and now - started < int(self.stall_ttl_s):
            return self._refuse(
                "a claim on will #" + str(wid) + " is in flight; it must "
                "resolve, or be cleared with settle_stalled(" + str(wid)
                + "), before this will can be cancelled",
                {"claim_started_at": started,
                 "stalls_at": started + int(self.stall_ttl_s)})

        deposit = int(will.deposit_wei)
        self._release(deposit)
        self._credit(will.owner, deposit)

        old = str(will.status)
        will.status = W_CANCELLED
        will.executed_at = u64(now)
        will.refunded_wei = u256(deposit)
        self._bump_status(old, W_CANCELLED)
        self.active_will_of[sender] = u32(0)
        self.total_cancelled = u256(int(self.total_cancelled) + 1)

        return {
            "status": "OK",
            "will_id": wid,
            "refunded_wei": str(deposit),
            "refunded_gen": _gen(deposit),
            "claim_with": "claim_payout()",
        }

    @gl.public.write
    def change_beneficiary(self, will_id: typing.Any,
                           new_beneficiary: str) -> typing.Any:
        """Name a different heir. Owner only, and not while a claim is in
        flight - a beneficiary swapped mid-round would redirect a payout the
        validators were in the middle of authorising."""
        self._bank()
        now = self._now()
        sender = gl.message.sender_address

        will, error = self._live_will(will_id)
        if error:
            return self._refuse(error)
        if will.owner != sender:
            return self._refuse("only the owner of will #"
                                + str(int(will.will_id))
                                + " can change its beneficiary")

        wid = int(will.will_id)
        started = self._claim_open(wid)
        if started > 0 and now - started < int(self.stall_ttl_s):
            return self._refuse(
                "a claim on will #" + str(wid) + " is in flight; the "
                "beneficiary cannot change until it resolves",
                {"claim_started_at": started})

        if not _is_addr(new_beneficiary):
            return self._refuse("beneficiary must be a 0x-prefixed 20-byte "
                                "address")
        new_addr = Address(str(new_beneficiary).strip())
        if new_addr.as_hex == ZERO_ADDRESS:
            return self._refuse("the zero address cannot inherit")
        if new_addr == sender:
            return self._refuse("the beneficiary cannot be the owner")
        if new_addr == will.beneficiary:
            return self._refuse("that is already the beneficiary of will #"
                                + str(wid))

        old_addr = will.beneficiary
        will.beneficiary = new_addr
        will.beneficiary_changes = u32(int(will.beneficiary_changes) + 1)
        self.by_beneficiary.get_or_insert_default(new_addr).append(u32(wid))

        # A change of heir is a message from the owner's key, so it is proof of
        # life for the same reason a top-up is.
        if now > 0:
            will.last_heartbeat = u64(now)

        return {
            "status": "OK",
            "will_id": wid,
            "previous_beneficiary": old_addr.as_hex,
            "beneficiary": new_addr.as_hex,
            "beneficiary_changes": int(will.beneficiary_changes),
            "claimable_at": self._deadline(will),
            "note": "changing the beneficiary counts as a check-in",
        }

    @gl.public.write
    def settle_stalled(self, will_id: typing.Any) -> typing.Any:
        """Clear a claim marker that outlived its round.

        PERMISSIONLESS AND WORKS WHILE PAUSED (rule 6). An owner who could keep
        a will frozen by declining to unstick it would be an owner who can
        strand a beneficiary, which needs no validators at all and is therefore
        worse than forging a verdict.

        It returns the will to its pre-claim state: the will stays ACTIVE, the
        deposit stays locked, and nobody is paid. No money moves because no
        money moved when the claim opened - the deposit is never staged into an
        intermediate bucket, which is what makes this recovery path a one-line
        deletion rather than an unwind."""
        self._bank()
        now = self._now()

        will, error = self._live_will(will_id)
        if error:
            return self._refuse(error)
        wid = int(will.will_id)

        started = self._claim_open(wid)
        if started <= 0:
            return self._refuse("no claim is in flight on will #" + str(wid))
        age = now - started
        ttl = int(self.stall_ttl_s)
        if age < ttl:
            return self._refuse(
                "the claim on will #" + str(wid) + " has been in flight for "
                + str(age) + "s; it can be cleared after " + str(ttl) + "s",
                {"claim_started_at": started, "stalls_at": started + ttl})

        self.claiming[str(wid)] = u64(0)
        return {
            "status": "OK",
            "will_id": wid,
            "cleared_claim_started_at": started,
            "stalled_for_s": age,
            "will_status": str(will.status),
            "note": ("the will is untouched and still active; anyone may call "
                     "claim_inactive(" + str(wid) + ") again"),
        }

    @gl.public.write
    def claim_payout(self) -> typing.Any:
        """Withdraw everything this contract owes you.

        THE ONLY EXIT, and it is ungated on `paused`, permissionless, and
        available to anyone with a balance - owner, beneficiary, finder or a
        stranger whose refused deposit was booked back to them (rules 6 and 7).

        Payouts are PULLED rather than pushed. A settlement that paid two
        parties inside the transaction that had just run a consensus round
        would post two internal messages, and a fee allocation that does not
        name the right recipient fails INSIDE the transaction with
        `fee no_matching_allocation` - which reads like a contract fault and is
        not one. One message, one recipient, is the shape that has been proved
        to work."""
        self._bank()
        who = gl.message.sender_address
        owed = int(self.payout_wei.get(who) or 0)
        if owed <= 0:
            return self._refuse("nothing is owed to " + who.as_hex)

        self.payout_wei[who] = u256(0)
        self.payable_wei = u256(int(self.payable_wei) - owed)
        self.balance_wei = u256(int(self.balance_wei) - owed)
        self.claimed_total_wei = u256(int(self.claimed_total_wei) + owed)
        self.total_claimed_wei = u256(int(self.total_claimed_wei) + owed)
        _pay(who, owed)

        return {
            "status": "OK",
            "to": who.as_hex,
            "amount_wei": str(owed),
            "amount_gen": _gen(owed),
            "note": ("the transfer applies on finalisation, so the balance "
                     "moves a little after this receipt"),
        }

    @gl.public.write
    def set_paused(self, paused: typing.Any) -> typing.Any:
        """Stop NEW wills and top-ups. Owner only.

        This is the entire list of what pause does. Heartbeats, claims,
        cancellations, stall recovery and payouts all keep working while paused
        (rule 6), and the offline suite fires every one of them at a paused
        contract to prove it."""
        self._bank()
        if not self._is_owner():
            return self._refuse("only the owner can pause this contract")
        want = bool(paused) if isinstance(paused, bool) else bool(
            _as_int(paused, 0))
        self.paused = want
        return {"status": "OK", "paused": want,
                "note": ("heartbeat, claim_inactive, cancel_will, "
                         "settle_stalled and claim_payout are unaffected")}

    @gl.public.write
    def transfer_ownership(self, new_owner: str) -> typing.Any:
        """Hand the pause switch to somebody else. Owner only."""
        self._bank()
        if not self._is_owner():
            return self._refuse("only the owner can transfer ownership")
        if not _is_addr(new_owner):
            return self._refuse("new_owner must be a 0x-prefixed 20-byte "
                                "address")
        addr = Address(str(new_owner).strip())
        if addr.as_hex == ZERO_ADDRESS:
            return self._refuse("ownership cannot be transferred to the zero "
                                "address")
        previous = self.owner.as_hex
        self.owner = addr
        return {"status": "OK", "previous_owner": previous,
                "owner": addr.as_hex}

    # --- views -------------------------------------------------------------

    def _evidence_of(self, will: Will) -> dict:
        """The stored reading, in one shape, with the buckets spelled out.

        One place, so a claim receipt and `get_will` can never describe the
        same reading differently."""
        return {
            "claim_attempts": int(will.claim_attempts),
            "last_claim_at": int(will.last_claim_at),
            "reason": str(will.last_reason),
            "activity_status": str(will.last_verdict),
            "age_bucket": int(will.last_age_bucket),
            "age_meaning": (AGE_WORDS[int(will.last_age_bucket)]
                            if str(will.last_verdict) else ""),
            "count_bucket": int(will.last_count_bucket),
            "count_meaning": (COUNT_WORDS[int(will.last_count_bucket)]
                              if str(will.last_verdict) else ""),
            "src_ok": bool(will.last_src_ok),
            "content_hash": str(will.last_content_hash),
            "anchor_ts": int(will.last_anchor_ts),
            "checked_at": int(will.last_now_ts),
            "window": int(will.last_window),
            "chain": str(will.chain),
        }

    @gl.public.view
    def get_will(self, will_id: typing.Any) -> typing.Any:
        will = self._will(will_id)
        if will is None:
            return json.dumps({"found": False,
                               "will_id": _as_int(will_id, 0)})
        return json.dumps({"found": True,
                           "will": self._view(will, self._now())})

    @gl.public.view
    def get_wills_by_owner(self, address: str) -> typing.Any:
        return self._by_party(address, True)

    @gl.public.view
    def get_wills_by_beneficiary(self, address: str) -> typing.Any:
        return self._by_party(address, False)

    def _by_party(self, address: str, as_owner: bool) -> typing.Any:
        if not _is_addr(address):
            return json.dumps({"address": str(address), "count": 0,
                               "wills": [],
                               "error": "not a 0x-prefixed 20-byte address"})
        who = Address(str(address).strip())
        index = self.by_owner.get(who) if as_owner else \
            self.by_beneficiary.get(who)
        now = self._now()
        out = []
        seen = []
        if index is not None:
            for raw in index:
                wid = int(raw)
                # The beneficiary index appends on every change, so one will
                # can appear twice for the same address. De-duplicated here
                # rather than by rewriting the index, because an index that is
                # append-only is an index no method can corrupt.
                if wid in seen:
                    continue
                seen.append(wid)
                will = self._will(wid)
                if will is None:
                    continue
                if not as_owner and will.beneficiary != who:
                    # A former beneficiary stays in the index but is not a
                    # beneficiary any more.
                    continue
                out.append(self._card(will, now))
                if len(out) >= PAGE_CAP:
                    break
        return json.dumps({
            "address": who.as_hex,
            "role": "owner" if as_owner else "beneficiary",
            "count": len(out),
            "wills": out,
        })

    @gl.public.view
    def get_claimable_wills(self) -> typing.Any:
        """Every active will already past its threshold, soonest first.

        This is the finder's worklist: each entry names what calling
        `claim_inactive` on it would pay if the wallet really is dormant."""
        now = self._now()
        out = []
        total = len(self.wills)
        start = 0 if total <= SCAN_CAP else total - SCAN_CAP
        for i in range(start, total):
            will = self.wills[i]
            if not self._overdue(will, now):
                continue
            card = self._card(will, now)
            _, to_finder = _split(int(will.deposit_wei),
                                  int(self.finder_fee_bps))
            card["finder_fee_wei"] = str(to_finder)
            card["finder_fee_gen"] = _gen(to_finder)
            card["overdue_by_s"] = now - self._deadline(will)
            out.append(card)
            if len(out) >= PAGE_CAP:
                break
        return json.dumps({
            "count": len(out),
            "scanned": total - start,
            "total_wills": total,
            "finder_fee_bps": int(self.finder_fee_bps),
            "wills": out,
        })

    @gl.public.view
    def get_wills(self, offset: typing.Any, count: typing.Any) -> typing.Any:
        total = len(self.wills)
        start = _clamp(_as_int(offset, 0), 0, total)
        want = _clamp(_as_int(count, 20), 1, MAX_PAGE)
        now = self._now()
        out = []
        for i in range(start, min(total, start + want)):
            out.append(self._card(self.wills[i], now))
        return json.dumps({"offset": start, "count": len(out),
                           "total": total, "wills": out})

    @gl.public.view
    def get_stats(self) -> typing.Any:
        """The books, and the chain's own opinion of them.

        `undelivered_wei` is the gap between what this contract has actually
        got and what its ledger says it should have. It exists because Studio
        Dev QUEUES an `on="finalized"` value transfer and never executes it, so
        a claimed payout leaves the real balance high. Publishing the number
        beside the books is the honest way to report that: on a network that
        delivers, it stays at zero. See `_pay`."""
        ledger = int(self.locked_wei) + int(self.payable_wei)
        # `self.balance` is the contract's REAL balance on chain, and it is
        # the spelling the runner exposes — not `gl.message.contract_balance`,
        # which does not exist and would make this whole view raise.
        try:
            on_chain = int(self.balance)
        except Exception:
            on_chain = -1
        return json.dumps({
            "total_wills": int(self.total_wills),
            "active": int(self.status_counts.get(W_ACTIVE) or 0),
            "executed": int(self.total_executed),
            "cancelled": int(self.total_cancelled),
            "claims_attempted": int(self.total_claims_attempted),
            "verdicts": {
                "alive": int(self.verdict_counts.get(A_ALIVE) or 0),
                "inactive": int(self.verdict_counts.get(A_INACTIVE) or 0),
                "inconclusive": int(
                    self.verdict_counts.get(A_INCONCLUSIVE) or 0),
            },
            "rejected_calls": int(self.total_rejected),
            "ledger": {
                "balance_wei": str(int(self.balance_wei)),
                "balance_gen": _gen(int(self.balance_wei)),
                "locked_wei": str(int(self.locked_wei)),
                "locked_gen": _gen(int(self.locked_wei)),
                "payable_wei": str(int(self.payable_wei)),
                "payable_gen": _gen(int(self.payable_wei)),
                "identity_holds": bool(int(self.balance_wei) == ledger),
                "identity": "balance_wei == locked_wei + payable_wei",
            },
            "on_chain_balance_wei": str(on_chain),
            "undelivered_wei": (str(on_chain - int(self.balance_wei))
                                if on_chain >= 0 else "unknown"),
            "totals": {
                "deposited_wei": str(int(self.total_deposited_wei)),
                "deposited_gen": _gen(int(self.total_deposited_wei)),
                "released_to_beneficiaries_wei": str(
                    int(self.total_released_wei)),
                "released_to_beneficiaries_gen": _gen(
                    int(self.total_released_wei)),
                "finder_fees_wei": str(int(self.total_finder_fees_wei)),
                "finder_fees_gen": _gen(int(self.total_finder_fees_wei)),
                "claimed_wei": str(int(self.total_claimed_wei)),
                "claimed_gen": _gen(int(self.total_claimed_wei)),
            },
            "protocol_revenue_wei": "0",
            "protocol_revenue_note": (
                "there is none, and there is no owner withdraw method; the "
                "finder fee is paid out of the deposit to the caller"),
        })

    @gl.public.view
    def get_config(self) -> typing.Any:
        return json.dumps({
            "rubric_version": RUBRIC_VERSION,
            "owner": self.owner.as_hex,
            "paused": bool(self.paused),
            "paused_affects": ["create_will", "top_up"],
            "unaffected_by_pause": ["heartbeat", "claim_inactive",
                                    "cancel_will", "change_beneficiary",
                                    "settle_stalled", "claim_payout"],
            "min_interval_s": int(self.min_interval_s),
            "max_interval_s": int(self.max_interval_s),
            "interval_unit_s": int(self.interval_unit_s),
            "interval_unit": ("days" if int(self.interval_unit_s)
                              == DEFAULT_INTERVAL_UNIT_S else "seconds"),
            "min_interval_units": self._units(int(self.min_interval_s)),
            "max_interval_units": self._units(int(self.max_interval_s)),
            "missed_threshold": int(self.missed_threshold),
            "finder_fee_bps": int(self.finder_fee_bps),
            "stall_ttl_s": int(self.stall_ttl_s),
            "min_deposit_wei": str(MIN_DEPOSIT_WEI),
            "max_deposit_wei": str(MAX_DEPOSIT_WEI),
            "max_wills": MAX_WILLS,
            "wills_per_wallet": 1,
            "wills_per_wallet_note": "one ACTIVE will; closed wills do not count",
            "supported_chains": list(CHAINS),
            "explorer_hosts": dict(CHAIN_HOSTS),
            "tx_window": TX_WINDOW,
            "immutable": ["min_interval_s", "max_interval_s",
                          "interval_unit_s", "missed_threshold",
                          "finder_fee_bps", "stall_ttl_s"],
            "consensus": {
                "compared_fields": ["activity_status", "age_bucket",
                                    "count_bucket", "src_ok", "content_hash"],
                "verdicts": list(VERDICTS),
                "age_ladder_days": list(AGE_LADDER),
                "count_ladder": list(COUNT_LADDER),
                "age_words": list(AGE_WORDS),
                "count_words": list(COUNT_WORDS),
                "signed_only": True,
                "signed_only_note": (
                    "only transactions the wallet SENT count as activity; "
                    "inbound transfers are ignored so that a stranger cannot "
                    "keep a will locked with one wei"),
                "anchor": "the will's last_heartbeat",
                "uses_language_model": False,
                "uses_language_model_note": (
                    "the question is a matter of fact, not judgement; the "
                    "non-determinism is the explorer fetch, which every "
                    "validator performs independently"),
            },
        })

    @gl.public.view
    def verify_claim(self, will_id: typing.Any) -> typing.Any:
        """Re-derive everything stored about a claim, from the claim itself.

        Takes no network and no model: it recomputes the content hash from the
        stored feature vector, recomposes the stored reasoning from the same
        vector, and re-splits the stored deposit at the stored fee. Any
        mismatch is a field that does not follow from the evidence, which is
        the shape a forged value would have.

        This is what makes the content hash a commitment rather than a
        decoration - it can be checked by anybody, for ever, without trusting
        the node that answered."""
        will = self._will(will_id)
        if will is None:
            return json.dumps({"found": False,
                               "will_id": _as_int(will_id, 0)})
        if int(will.claim_attempts) <= 0:
            return json.dumps({
                "found": True, "will_id": int(will.will_id),
                "checked": False,
                "note": "no claim has ever been made on this will",
            })

        vector = {
            "activity_status": str(will.last_verdict),
            "age_bucket": int(will.last_age_bucket),
            "count_bucket": int(will.last_count_bucket),
            "src_ok": bool(will.last_src_ok),
        }
        canon = _canon(int(will.will_id), str(will.chain), will.owner.as_hex,
                       int(will.last_anchor_ts), int(will.last_now_ts),
                       int(will.last_window), vector)
        recomputed = _fnv(canon)
        stored_hash = str(will.last_content_hash)

        recomposed = _clean(_reason(vector, str(will.chain),
                                    int(will.last_window)), MAX_REASON_CHARS)
        stored_reason = str(will.last_reason)

        checks = []
        checks.append({
            "check": "content_hash re-derives from the stored vector",
            "ok": bool(recomputed == stored_hash),
            "stored": stored_hash, "recomputed": recomputed,
        })
        checks.append({
            "check": "the stored reasoning recomposes from the stored vector",
            "ok": bool(recomposed == stored_reason),
        })
        sealed = _seal(int(will.will_id), str(will.chain), will.owner.as_hex,
                       int(will.last_anchor_ts), int(will.last_now_ts),
                       int(will.last_window), vector)
        checks.append({
            "check": "the stored vector is internally coherent",
            "ok": bool(_coherent(sealed)),
        })

        released = str(will.status) == W_EXECUTED
        if released:
            deposit = (int(will.paid_beneficiary_wei)
                       + int(will.paid_finder_wei))
            expect_b, expect_f = _split(deposit, int(will.finder_fee_bps))
            checks.append({
                "check": "the split re-derives and conserves exactly",
                "ok": bool(expect_b == int(will.paid_beneficiary_wei)
                           and expect_f == int(will.paid_finder_wei)
                           and expect_b + expect_f == deposit),
                "deposit_wei": str(deposit),
                "to_beneficiary_wei": str(int(will.paid_beneficiary_wei)),
                "to_finder_wei": str(int(will.paid_finder_wei)),
            })
            checks.append({
                "check": "a release was authorised by an INACTIVE reading "
                         "from a source that answered",
                "ok": bool(str(will.last_verdict) == A_INACTIVE
                           and bool(will.last_src_ok)),
            })

        all_ok = True
        for item in checks:
            if not item["ok"]:
                all_ok = False

        return json.dumps({
            "found": True,
            "will_id": int(will.will_id),
            "checked": True,
            "verified": all_ok,
            "status": str(will.status),
            "released": released,
            "canonical_projection": canon,
            "vector": vector,
            "content_hash": stored_hash,
            "reason": stored_reason,
            "checks": checks,
            "rubric_version": RUBRIC_VERSION,
        })

    @gl.public.view
    def payout_of(self, address: str) -> typing.Any:
        if not _is_addr(address):
            return json.dumps({"address": str(address), "owed_wei": "0",
                               "error": "not a 0x-prefixed 20-byte address"})
        who = Address(str(address).strip())
        owed = int(self.payout_wei.get(who) or 0)
        return json.dumps({"address": who.as_hex, "owed_wei": str(owed),
                           "owed_gen": _gen(owed),
                           "claim_with": "claim_payout()"})

    @gl.public.view
    def preview_will(self, check_in_days: typing.Any,
                     deposit_wei: typing.Any) -> typing.Any:
        """What a will with these parameters would look like, before making
        one. Pure, free, and it answers the two questions people actually ask:
        when does it become claimable, and what does the finder take."""
        days = _as_int(check_in_days, 0)
        deposit = _as_int(deposit_wei, 0)
        interval = days * int(self.interval_unit_s)
        lo = int(self.min_interval_s)
        hi = int(self.max_interval_s)
        ok_interval = interval >= lo and interval <= hi and days > 0
        ok_deposit = deposit >= MIN_DEPOSIT_WEI and deposit <= MAX_DEPOSIT_WEI
        to_beneficiary, to_finder = _split(deposit, int(self.finder_fee_bps))
        now = self._now()
        return json.dumps({
            "acceptable": bool(ok_interval and ok_deposit),
            "interval_ok": bool(ok_interval),
            "deposit_ok": bool(ok_deposit),
            "check_in_interval_s": interval,
            "missed_threshold": int(self.missed_threshold),
            "claimable_after_s": interval * int(self.missed_threshold),
            "claimable_at_if_created_now": (
                now + interval * int(self.missed_threshold) if now > 0 else 0),
            "deposit_wei": str(deposit),
            "to_beneficiary_wei": str(to_beneficiary),
            "to_beneficiary_gen": _gen(to_beneficiary),
            "to_finder_wei": str(to_finder),
            "to_finder_gen": _gen(to_finder),
            "finder_fee_bps": int(self.finder_fee_bps),
            "min_interval_s": lo,
            "max_interval_s": hi,
            "min_deposit_wei": str(MIN_DEPOSIT_WEI),
            "max_deposit_wei": str(MAX_DEPOSIT_WEI),
        })
