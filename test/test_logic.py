#!/usr/bin/env python3
"""Offline tests for WillExecutor. No chain, no network, no model, no genlayer
install - stdlib only:

    python3 test/test_logic.py

Nine things are under test, not one.

1. **The pure projection** - the ladders, the buckets, the URL builder, the
   response parser and the vector. This is the half every validator computes
   for itself. If two validators disagree here, no claim ever settles.

2. **The `result: null` trap.** The explorer answers "this wallet has no
   transactions" and "I refuse to answer" with the same HTTP 200 and the same
   `status: "0"`. Only `result` distinguishes them - a list is an answer, null
   is a refusal - and conflating them pays out an inheritance on a typo.

3. **Rule 9: only a signature proves life.** A wallet buried in inbound
   transfers must still read INACTIVE, or any stranger could keep any will
   locked for ever for the price of one wei.

4. **The consensus gates.** `_coherent` and `_agrees` are what stop a leader
   forging a stored value, so they are tested by BUILDING FORGERIES - one per
   field - and requiring each one refused.

5. **The money rules.** That no public write raises, that every refusal
   refunds, that no counter moves before a refusal, that the ledger identity
   `balance == locked + payable` holds after EVERY operation, and that value
   the contract accepted can always be got back out.

6. **The state machine**, including every transition that exists only to be
   refused.

7. **A static undefined-name check** over the WHOLE file, class bodies
   included. The pure region can be exec'd, but a name error inside a
   `@gl.public.view` only fires when that view is called on chain. A parser
   catches it in a millisecond; a deploy catches it in ten minutes.

8. **The AST invariants** - zero raises, no `str.replace()`, a two-line header,
   immutable fields with no setter, and pause gating nothing it must not gate.
   All walked as syntax, never grepped: this file's own documentation mentions
   `str.replace()` in order to warn about it, and a grep-based audit cries wolf
   on its own comments.

9. **The stateful contract**, driven through a storage stub rich enough to run
   create -> heartbeat -> miss -> claim -> release -> withdraw end to end with
   consensus wired up.
"""

import ast
import builtins
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "contracts" / "WillExecutor.py"

GEN = 10 ** 18
MINUTE = 60
HOUR = 3600
DAY = 86400

_UNSET = object()


# ---------------------------------------------------------------------------
# runtime stub
#
# Ported from the proven CourtRoom/Sentinel harness and kept on the v0.6 runner
# namespace: `gl.contract.Contract`, `gl.storage.TreeMap`, `gl.storage.DynArray`,
# `gl.storage.allow`, `gl.message.raw`, `gl.chain.Account`. A stub still shaped
# like an older namespace would let every test pass against a contract the
# current runner cannot even load.
#
# The TreeMap missing-key semantics in particular are load-bearing: on chain a
# map with a SCALAR value type answers a missing key with that type's ZERO, not
# with None, so a presence check written as `is not None` matches everything. A
# stub that returned None could never reproduce that bug.
# ---------------------------------------------------------------------------


class _UserError(Exception):
    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class _Return:
    """gl.vm.Return - a leader result carrying its calldata."""

    def __init__(self, calldata):
        self.calldata = calldata


class _Rollback:
    def __init__(self, message=""):
        self.message = message


class _Addr:
    """Address. Compared and keyed by its lowercase text, like the real one, and
    carrying `.as_hex`, which is the ONLY spelling the runner guarantees. A stub
    whose `str()` happened to produce the hex would hide every place the
    contract forgot `.as_hex`."""

    def __init__(self, value=""):
        v = str(value)
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError("not an address: " + v[:60])
        for ch in v[2:]:
            if ch not in "0123456789abcdefABCDEF":
                raise ValueError("not an address: " + v[:60])
        self._v = v.lower()

    @property
    def as_hex(self):
        return self._v

    def __str__(self):
        return self._v

    def __repr__(self):
        return "Address(" + self._v + ")"

    def __eq__(self, other):
        return isinstance(other, _Addr) and self._v == other._v

    def __hash__(self):
        return hash(self._v)


class _TreeMap(dict):
    """Models the runtime's TreeMap, INCLUDING what it returns for a key that is
    not there."""

    _value_type = None

    @classmethod
    def __class_getitem__(cls, item):
        vt = item[1] if isinstance(item, tuple) and len(item) > 1 else None
        return type("_TreeMapOf", (cls,), {"_value_type": vt})

    def _k(self, key):
        return str(key) if isinstance(key, _Addr) else key

    def _missing(self):
        vt = type(self)._value_type
        if vt is None:
            return None
        name = getattr(vt, "__name__", str(vt))
        if name.startswith("_TreeMap") or name.startswith("_DynArray"):
            return _zero_for(vt)
        if vt is int or vt is str or vt is bool:
            return _zero_for(vt)
        if hasattr(vt, "__annotations__") and getattr(vt, "__annotations__"):
            return None
        return _zero_for(vt)

    def get(self, key, default=_UNSET):
        k = self._k(key)
        if k in self:
            return dict.__getitem__(self, k)
        if default is not _UNSET:
            return default
        return self._missing()

    def __contains__(self, key):
        return dict.__contains__(self, self._k(key))

    def __setitem__(self, key, value):
        dict.__setitem__(self, self._k(key), value)

    def __getitem__(self, key):
        """Indexing a key the map does not hold RAISES KeyError, exactly as the
        runner does.

        This stub used to auto-create the entry instead, and that single line
        of convenience hid a real revert: `self.by_owner[sender].append(...)`
        passed 431 offline tests and then died on chain inside `create_will`,
        on the one path that had already banked a deposit. `get_or_insert_default`
        is the spelling that inserts. A stub that is more forgiving than the
        runner is a stub that certifies bugs."""
        return dict.__getitem__(self, self._k(key))

    def __delitem__(self, key):
        dict.__delitem__(self, self._k(key))

    def get_or_insert_default(self, key):
        k = self._k(key)
        if k not in self:
            dict.__setitem__(self, k, self._factory())
        return dict.__getitem__(self, k)

    def _factory(self):
        vt = type(self)._value_type
        if vt is None:
            return _DynArray()
        if hasattr(vt, "__annotations__") and getattr(vt, "__annotations__"):
            return _make_struct(vt)
        return _zero_for(vt)


class _DynArray(list):
    """Models DynArray, INCLUDING `append_new_get()`.

    On chain a DynArray of structs cannot be appended to with a constructed
    value, so the runtime allocates a zeroed element in place and hands back a
    REFERENCE to it. Reproducing that matters for more than API coverage: the
    returned object must be the SAME object the array holds, or a later
    mutation through the reference would be invisible in the array, and every
    test would pass while every will written on chain stayed zero."""

    _elem_type = None

    @classmethod
    def __class_getitem__(cls, item):
        return type("_DynArrayOf", (cls,), {"_elem_type": item})

    def append_new_get(self):
        elem = type(self)._elem_type
        value = _make_struct(elem) if elem is not None and \
            hasattr(elem, "__annotations__") else _zero_for(elem)
        list.append(self, value)
        return value


def _zero_for(annotation):
    """The value the runtime auto-initialises a storage field to."""
    name = getattr(annotation, "__name__", str(annotation))
    if annotation is bool or name == "bool":
        return False
    if annotation is str or name == "str":
        return ""
    if name == "_Addr" or name == "Address":
        return _Addr("0x" + "0" * 40)
    if name.startswith("_TreeMap") or name == "TreeMap":
        return annotation() if isinstance(annotation, type) else _TreeMap()
    if name.startswith("_DynArray") or name == "DynArray":
        return annotation() if isinstance(annotation, type) else _DynArray()
    if name.startswith("u") or name.startswith("i"):
        return 0
    if hasattr(annotation, "__annotations__"):
        return _make_struct(annotation)
    return 0


def _make_struct(cls):
    obj = cls.__new__(cls)
    for field, ann in getattr(cls, "__annotations__", {}).items():
        setattr(obj, field, _zero_for(ann))
    return obj


class _Contract:
    """gl.contract.Contract. Storage fields are declared as class annotations and
    never assigned before use, exactly as on chain, so they are created on
    demand."""

    balance = 0

    def __getattr__(self, name):
        anns = {}
        for klass in reversed(type(self).__mro__):
            anns.update(getattr(klass, "__annotations__", {}))
        if name in anns:
            value = _zero_for(anns[name])
            object.__setattr__(self, name, value)
            return value
        raise AttributeError(name)


TRANSFERS = []
BALANCES = {}


class _Proxy:
    """gl.contract.Proxy. `.emit()` is a METHOD GETTER, exactly like the
    runner's, and it records NOTHING. That is the whole point: on chain,
    `emit()` with no method call after it constructs a namespace and drops it,
    posting no message. A stub that treated a bare `emit(value=...)` as a
    transfer would make this suite agree with a contract that silently never
    pays - which is precisely the bug that shipped once and had to be caught on
    chain by comparing real balances."""

    def __init__(self, address):
        self.address = address

    def view(self, **_k):
        return None

    def emit(self, **_k):
        return None

    def emit_transfer(self, value, **_k):
        if int(value) <= 0:
            raise ValueError("value must be greater than 0 for emit_transfer")
        key = str(self.address)
        TRANSFERS.append((key, int(value)))
        BALANCES[key] = BALANCES.get(key, 0) + int(value)


class _Account:
    """gl.chain.Account - the wrapper the SDK documents for ANY on-chain
    account, contract or EOA.

    Its `emit_transfer` DELIVERS here. That is a deliberate difference from the
    network the contract is deployed on: Studio Dev queues an `on="finalized"`
    value transfer and never executes it, which is a property of that network
    and not of this contract. This suite models the INTENDED semantics so the
    money invariants can be proved end to end; `test/seed.mjs` asserts the other
    half on chain - that the call posts a well-formed queued transfer to the
    right address for the right amount. Neither check is sufficient alone."""

    def __init__(self, address):
        self.address = address

    @property
    def balance(self):
        return BALANCES.get(str(self.address), 0)

    def emit_transfer(self, value, **_k):
        if int(value) <= 0:
            raise ValueError("value must be greater than 0 for emit_transfer")
        key = str(self.address)
        TRANSFERS.append((key, int(value)))
        BALANCES[key] = BALANCES.get(key, 0) + int(value)


def _proxy_for(address):
    return _Proxy(address)


def _contract_interface(cls):
    return _proxy_for


def _evm_contract_interface(cls):
    class _Handle:
        def __init__(self, to):
            self.to = to
    return _Handle


MESSAGE = types.SimpleNamespace(sender_address=_Addr("0x" + "a" * 40), value=0,
                                raw={"datetime": "2026-09-18T12:00:00Z"})


# ---------------------------------------------------------------------------
# the web stub
#
# `_probe` is called TWICE per consensus round offline - once by the leader and
# once by the validator - so the default mode is STICKY: one queued response
# serves every fetch until it is replaced. `script()` exists for the opposite
# case, where the leader and the validator must be made to see different things
# in order to prove that disagreement settles nothing.
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body


class _Web:
    def __init__(self):
        self.reset()

    def reset(self):
        self.sticky = None
        self.queue = []
        self.log = []
        self.raise_next = 0
        self.calls = 0

    def serve(self, status, body):
        """Every fetch from now on answers this."""
        self.sticky = (status, body)
        self.queue = []

    def script(self, *pairs):
        """The next fetches answer these, in order; then the sticky value."""
        self.queue = list(pairs)

    def fail(self, times=1):
        self.raise_next = times

    def _next(self, url):
        self.calls += 1
        self.log.append(url)
        if self.raise_next > 0:
            self.raise_next -= 1
            raise RuntimeError("connection reset")
        if self.queue:
            return self.queue.pop(0)
        if self.sticky is None:
            raise AssertionError("web fetch with no queued response: " + url)
        return self.sticky


WEB = _Web()


def _web_request(url, method="GET", **_k):
    status, body = WEB._next(url)
    return _Response(status, body)


def _web_get(url, **_k):
    status, body = WEB._next(url)
    return _Response(status, body)


def _web_render(url, **_k):
    raise AssertionError("WillExecutor must not use web.render")


def _exec_prompt(prompt, **_k):
    raise AssertionError(
        "WillExecutor must not call a language model: the question is a matter "
        "of fact, and a model on the consensus axis would add a disagreement "
        "source to a question that has a right answer")


LAST_CONSENSUS = {}

# Set by a test to make the leader misbehave. Kept OUT of LAST_CONSENSUS
# because that dict is cleared at the top of every round - a forgery stored
# there would be wiped before it could be used, and the test would silently
# assert nothing.
FORGE = {"payload": None, "leader_dies": False}


def _run_nondet(leader_fn, validator_fn):
    """Runs the real consensus shape offline: the leader produces a result, a
    validator is handed it as gl.vm.Return and must agree, and disagreement is
    surfaced the way the chain surfaces it - as a round that returns nothing.

    The validator runs the SAME closure the contract gave it, so a validator
    that re-fetches really does re-fetch here too."""
    LAST_CONSENSUS.clear()
    if FORGE["leader_dies"]:
        # A round that never settled. On chain the transaction goes
        # UNDETERMINED and NO state is applied at all; here the call simply
        # answers nothing, which is what the contract must survive.
        LAST_CONSENSUS["agreed"] = False
        return None
    try:
        result = leader_fn()
    except Exception as e:
        LAST_CONSENSUS["agreed"] = False
        LAST_CONSENSUS["leader_error"] = str(e)
        return None
    LAST_CONSENSUS["leader"] = result
    if FORGE["payload"] is not None:
        result = FORGE["payload"]
    agreed = validator_fn(_Return(result))
    LAST_CONSENSUS["agreed"] = bool(agreed)
    if not agreed:
        return None
    return result


def _install_stub():
    if "genlayer" in sys.modules:
        return
    mod = types.ModuleType("genlayer")
    vm = types.SimpleNamespace(UserError=_UserError, Return=_Return,
                               Result=object, Rollback=_Rollback,
                               run_nondet=_run_nondet,
                               run_nondet_unsafe=_run_nondet)
    web = types.SimpleNamespace(request=_web_request, render=_web_render,
                                get=_web_get)
    nondet = types.SimpleNamespace(web=web, exec_prompt=_exec_prompt)
    public = types.SimpleNamespace()
    public.view = lambda fn: fn
    write = lambda fn: fn
    write.payable = lambda fn: fn
    public.write = write
    evm = types.SimpleNamespace(contract_interface=_evm_contract_interface)
    storage = types.SimpleNamespace(TreeMap=_TreeMap, DynArray=_DynArray,
                                    allow=lambda cls: cls)
    contract_ns = types.SimpleNamespace(Contract=_Contract,
                                        get_at=lambda a: _proxy_for(a),
                                        interface=_contract_interface)
    chain_ns = types.SimpleNamespace(Account=_Account, id=61997)
    mod.gl = types.SimpleNamespace(vm=vm, nondet=nondet, public=public, evm=evm,
                                   storage=storage, message=MESSAGE,
                                   contract=contract_ns, chain=chain_ns)
    mod.Address = _Addr
    mod.TreeMap = _TreeMap
    mod.DynArray = _DynArray
    for name in ("u8", "u16", "u32", "u64", "u128", "u256", "i8", "i16", "i32",
                 "i64", "bigint"):
        mod.__dict__[name] = int
    sys.modules["genlayer"] = mod
    sys.modules["genlayer.gl"] = mod.gl


def load_pure(path: Path, name: str) -> types.ModuleType:
    """Exec only the pure region - every top-level statement before the first
    class definition. That region never touches storage."""
    tree = ast.parse(path.read_text(encoding="utf8"))
    cut = len(tree.body)
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.ClassDef):
            cut = i
            break
    tree.body = tree.body[:cut]
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


def load_full(path: Path, name: str) -> types.ModuleType:
    """Exec the WHOLE file so the contract class itself can be driven."""
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf8"), str(path), "exec"),
         module.__dict__)
    return module


# ---------------------------------------------------------------------------
# static undefined-name check
# ---------------------------------------------------------------------------

def _own_nodes(scope):
    out = []

    def rec(node):
        for sub in ast.iter_child_nodes(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda)):
                continue
            out.append(sub)
            rec(sub)
    rec(scope)
    return out


def _child_scopes(scope):
    out = []

    def rec(node):
        for sub in ast.iter_child_nodes(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda)):
                out.append(sub)
            else:
                rec(sub)
    rec(scope)
    return out


def _bound_names(scope) -> set:
    out = set()
    args = getattr(scope, "args", None)
    if args is not None:
        for group in (args.posonlyargs, args.args, args.kwonlyargs):
            for a in group:
                out.add(a.arg)
        if args.vararg:
            out.add(args.vararg.arg)
        if args.kwarg:
            out.add(args.kwarg.arg)
    for sub in _own_nodes(scope):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            out.add(sub.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            out.add(sub.name)
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            out.update(sub.names)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for al in sub.names:
                out.add((al.asname or al.name).split(".")[0])
        elif isinstance(sub, ast.comprehension):
            for nm in ast.walk(sub.target):
                if isinstance(nm, ast.Name):
                    out.add(nm.id)
    for sub in _child_scopes(scope):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(sub.name)
    for sub in _own_nodes(scope):
        if isinstance(sub, ast.ClassDef):
            out.add(sub.name)
    return out


def undefined_names(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf8"))
    module_names = _bound_names(tree) | {
        "gl", "u8", "u16", "u32", "u64", "u128", "u256", "i8", "i16", "i32",
        "i64", "Address", "TreeMap", "DynArray", "bigint", "Array", "self"}
    builtin_names = set(dir(builtins))
    problems = []

    def visit(scope, enclosing, label):
        scope_names = enclosing | _bound_names(scope)
        for sub in _own_nodes(scope):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in scope_names and sub.id not in builtin_names:
                    problems.append((label, sub.id, sub.lineno))
        for child in _child_scopes(scope):
            visit(child, scope_names,
                  label + "." + getattr(child, "name", "<lambda>"))

    for child in _child_scopes(tree):
        visit(child, module_names, getattr(child, "name", "<lambda>"))
    for node in _own_nodes(tree):
        if isinstance(node, ast.ClassDef):
            for child in _child_scopes(node):
                visit(child, module_names | _bound_names(node),
                      node.name + "." + getattr(child, "name", "<lambda>"))
    return problems


# ---------------------------------------------------------------------------
# module loading and shared fixtures
# ---------------------------------------------------------------------------

_install_stub()

C = load_pure(SOURCE, "willexecutor_pure")
MOD = load_full(SOURCE, "willexecutor_full")
TREE = ast.parse(SOURCE.read_text(encoding="utf8"))
SRC_TEXT = SOURCE.read_text(encoding="utf8")

NOW_ISO = "2026-09-18T12:00:00Z"
NOW = C._epoch_from_iso(NOW_ISO)

OWNER = _Addr("0x" + "a" * 40)
ALICE = _Addr("0x" + "b" * 40)
BOB = _Addr("0x" + "c" * 40)
CAROL = _Addr("0x" + "d" * 40)
DAVE = _Addr("0x" + "e" * 40)
FINDER = _Addr("0x" + "f" * 40)
STRANGER = _Addr("0x" + "1" * 40)
# Never sends value and is never credited, so `claim_payout` from here is
# always a refusal. STRANGER cannot serve: its own refused deposits leave it
# with a balance, and a claim that succeeds is not a test of a refusal.
NOBODY = _Addr("0x" + "2" * 40)


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def set_now(ts: int) -> None:
    MESSAGE.raw["datetime"] = iso(ts)


def tx(ts: int, sender, to=None, h=None) -> dict:
    """One transaction as the legacy txlist endpoint spells it. Field names and
    types copied from a live 2026-09-18 response, not invented: `timeStamp` is
    a STRING of Unix seconds and `from` is the signer."""
    return {
        "blockNumber": str(20000000 + (ts % 100000)),
        "timeStamp": str(int(ts)),
        "hash": h or ("0x" + format(abs(hash((ts, str(sender)))) %
                                   (16 ** 64), "064x")),
        "from": str(sender),
        "to": str(to) if to is not None else str(OWNER),
        "value": "1000000000000000",
        "isError": "0",
        "txreceipt_status": "1",
    }


def txlist(items, status="1", message="OK") -> str:
    return json.dumps({"status": status, "message": message,
                       "result": [x for x in items]})


NO_TX_BODY = json.dumps({"status": "0", "message": "No transactions found",
                         "result": []})
REFUSED_BODY = json.dumps({"status": "0", "message": "Invalid address format",
                           "result": None})


def serve_txs(items):
    WEB.serve(200, txlist(items))


def serve_empty():
    WEB.serve(200, NO_TX_BODY)


def serve_refused():
    WEB.serve(200, REFUSED_BODY)


def serve_down(status=500):
    WEB.serve(status, "")


class Base(unittest.TestCase):
    """Every test starts from the same clean world, and every test that touches
    money asserts the ledger identity before it finishes."""

    def setUp(self):
        WEB.reset()
        TRANSFERS.clear()
        BALANCES.clear()
        LAST_CONSENSUS.clear()
        FORGE["payload"] = None
        FORGE["leader_dies"] = False
        set_now(NOW)
        MESSAGE.sender_address = OWNER
        MESSAGE.value = 0

    # --- driving the contract

    def make(self, **kw):
        """A contract with second-scale bounds unless told otherwise, so the
        whole lifecycle fits in a test."""
        kw.setdefault("min_interval_s", 60)
        kw.setdefault("max_interval_s", 365 * DAY)
        MESSAGE.sender_address = OWNER
        MESSAGE.value = 0
        return MOD.WillExecutor(**kw)

    def real(self, **kw):
        """A contract with the brief's real bounds: 7 to 365 days."""
        MESSAGE.sender_address = OWNER
        MESSAGE.value = 0
        return MOD.WillExecutor(**kw)

    def call(self, contract, who, fn, *args, value=0):
        MESSAGE.sender_address = who
        MESSAGE.value = int(value)
        try:
            return getattr(contract, fn)(*args)
        finally:
            MESSAGE.value = 0

    def view(self, contract, fn, *args):
        MESSAGE.sender_address = STRANGER
        MESSAGE.value = 0
        return json.loads(getattr(contract, fn)(*args))

    # --- invariants

    def ledger(self, contract):
        """RULE 7. Asserted after every operation in every money test."""
        self.assertEqual(
            int(contract.balance_wei),
            int(contract.locked_wei) + int(contract.payable_wei),
            "balance_wei == locked_wei + payable_wei")

    def owed(self, contract, who):
        return int(contract.payout_wei.get(who) or 0)

    def counters(self, contract):
        return {
            "total_wills": int(contract.total_wills),
            "total_executed": int(contract.total_executed),
            "total_cancelled": int(contract.total_cancelled),
            "total_claims_attempted": int(contract.total_claims_attempted),
            "total_deposited_wei": int(contract.total_deposited_wei),
            "total_released_wei": int(contract.total_released_wei),
            "total_finder_fees_wei": int(contract.total_finder_fees_wei),
            "next_id": int(contract.next_id),
            "wills": len(contract.wills),
        }

    def make_will(self, contract, owner=ALICE, beneficiary=BOB, days=1,
                  deposit=10 * GEN, chain="ethereum"):
        out = self.call(contract, owner, "create_will", str(beneficiary),
                        days, chain, value=deposit)
        self.assertEqual(out.get("status"), "OK", out)
        return int(out["will_id"])


# ===========================================================================
# 1. pure helpers
# ===========================================================================


class TestAsInt(Base):
    def test_plain_int(self):
        self.assertEqual(C._as_int(7), 7)

    def test_zero(self):
        self.assertEqual(C._as_int(0), 0)

    def test_negative_int(self):
        self.assertEqual(C._as_int(-5), -5)

    def test_true_is_not_one(self):
        """`bool` is an `int` in Python. Without the explicit exclusion, an
        argument that arrived as `True` would silently read as 1."""
        self.assertEqual(C._as_int(True, 99), 99)

    def test_false_is_not_zero(self):
        self.assertEqual(C._as_int(False, 99), 99)

    def test_digit_string(self):
        self.assertEqual(C._as_int("42"), 42)

    def test_negative_string(self):
        self.assertEqual(C._as_int("-42"), -42)

    def test_padded_string(self):
        self.assertEqual(C._as_int("  42  "), 42)

    def test_empty_string_is_default(self):
        self.assertEqual(C._as_int("", 3), 3)

    def test_junk_string_is_default(self):
        self.assertEqual(C._as_int("12abc", 3), 3)

    def test_none_is_default(self):
        self.assertEqual(C._as_int(None, 3), 3)

    def test_list_is_default(self):
        self.assertEqual(C._as_int([1], 3), 3)

    def test_dict_is_default(self):
        self.assertEqual(C._as_int({"a": 1}, 3), 3)

    def test_float_truncates(self):
        self.assertEqual(C._as_int(4.9), 4)

    def test_lone_minus_is_default(self):
        self.assertEqual(C._as_int("-", 3), 3)


class TestClampAndRank(Base):
    def test_clamp_low(self):
        self.assertEqual(C._clamp(-1, 0, 7), 0)

    def test_clamp_high(self):
        self.assertEqual(C._clamp(99, 0, 7), 7)

    def test_clamp_inside(self):
        self.assertEqual(C._clamp(3, 0, 7), 3)

    def test_clamp_at_bounds(self):
        self.assertEqual(C._clamp(0, 0, 7), 0)
        self.assertEqual(C._clamp(7, 0, 7), 7)

    def test_rank_zero(self):
        self.assertEqual(C._rank(0, C.AGE_LADDER), 0)

    def test_rank_top(self):
        self.assertEqual(C._rank(10 ** 9, C.AGE_LADDER), 7)

    def test_age_ladder_every_rung(self):
        """Each ladder bound is the FIRST value at its rung. An off-by-one here
        would move a wallet a whole bucket and break consensus at the edge."""
        expected = [(0, 0), (1, 1), (2, 1), (3, 2), (6, 2), (7, 3), (13, 3),
                    (14, 4), (29, 4), (30, 5), (89, 5), (90, 6), (179, 6),
                    (180, 7), (5000, 7)]
        for days, rung in expected:
            self.assertEqual(C._rank(days, C.AGE_LADDER), rung,
                             "age " + str(days))

    def test_count_ladder_every_rung(self):
        expected = [(0, 0), (1, 1), (2, 2), (3, 2), (4, 3), (7, 3), (8, 4),
                    (15, 4), (16, 5), (31, 5), (32, 6), (63, 6), (64, 7),
                    (999, 7)]
        for count, rung in expected:
            self.assertEqual(C._rank(count, C.COUNT_LADDER), rung,
                             "count " + str(count))

    def test_ladders_are_seven_long(self):
        """Seven bounds is what makes `_rank` answer 0..7 - the scale the brief
        asks for. An eighth bound would silently produce a bucket 8 that every
        range check rejects."""
        self.assertEqual(len(C.AGE_LADDER), 7)
        self.assertEqual(len(C.COUNT_LADDER), 7)

    def test_ladders_strictly_increasing(self):
        for ladder in (C.AGE_LADDER, C.COUNT_LADDER):
            for i in range(1, len(ladder)):
                self.assertGreater(ladder[i], ladder[i - 1])

    def test_rank_never_exceeds_top_bucket(self):
        for n in (-10, 0, 1, 7, 100, 10 ** 12):
            self.assertLessEqual(C._rank(n, C.AGE_LADDER), C.TOP_BUCKET)
            self.assertGreaterEqual(C._rank(n, C.AGE_LADDER), 0)

    def test_negative_ranks_zero(self):
        self.assertEqual(C._rank(-5, C.AGE_LADDER), 0)


class TestAddressHelpers(Base):
    def test_valid_address(self):
        self.assertTrue(C._is_addr("0x" + "a" * 40))

    def test_uppercase_hex_is_valid(self):
        self.assertTrue(C._is_addr("0x" + "A" * 40))

    def test_mixed_case_is_valid(self):
        self.assertTrue(C._is_addr("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"))

    def test_too_short(self):
        self.assertFalse(C._is_addr("0x" + "a" * 39))

    def test_too_long(self):
        self.assertFalse(C._is_addr("0x" + "a" * 41))

    def test_no_prefix(self):
        self.assertFalse(C._is_addr("a" * 42))

    def test_non_hex_char(self):
        self.assertFalse(C._is_addr("0x" + "g" * 40))

    def test_empty(self):
        self.assertFalse(C._is_addr(""))

    def test_none(self):
        self.assertFalse(C._is_addr(None))

    def test_whitespace_tolerated(self):
        self.assertTrue(C._is_addr("  0x" + "a" * 40 + "  "))

    def test_lower_normalises(self):
        self.assertEqual(C._lower("0xAABB"), "0xaabb")

    def test_lower_strips(self):
        self.assertEqual(C._lower("  0xAABB "), "0xaabb")


class TestTime(Base):
    def test_epoch_round_trip(self):
        self.assertEqual(C._epoch_from_iso("1970-01-01T00:00:00Z"), 0)

    def test_known_instant(self):
        self.assertEqual(C._epoch_from_iso("2026-09-18T12:00:00Z"),
                         int(datetime(2026, 9, 18, 12, 0, 0,
                                      tzinfo=timezone.utc).timestamp()))

    def test_microseconds_ignored(self):
        """The explorer stamps ISO with microseconds; only the first 19
        characters are read, so the two must agree."""
        self.assertEqual(C._epoch_from_iso("2026-09-18T12:00:00.123456Z"),
                         C._epoch_from_iso("2026-09-18T12:00:00Z"))

    def test_short_string_is_zero(self):
        self.assertEqual(C._epoch_from_iso("2026-09-18"), 0)

    def test_junk_is_zero(self):
        self.assertEqual(C._epoch_from_iso("not a date at all!!"), 0)

    def test_none_is_zero(self):
        self.assertEqual(C._epoch_from_iso(None), 0)

    def test_bad_month_is_zero(self):
        self.assertEqual(C._epoch_from_iso("2026-13-18T12:00:00Z"), 0)

    def test_bad_day_is_zero(self):
        self.assertEqual(C._epoch_from_iso("2026-09-32T12:00:00Z"), 0)

    def test_bad_hour_is_zero(self):
        self.assertEqual(C._epoch_from_iso("2026-09-18T25:00:00Z"), 0)

    def test_non_numeric_is_zero(self):
        self.assertEqual(C._epoch_from_iso("20xx-09-18T12:00:00Z"), 0)

    def test_leap_day(self):
        self.assertEqual(C._epoch_from_iso("2024-02-29T00:00:00Z"),
                         int(datetime(2024, 2, 29, tzinfo=timezone.utc)
                             .timestamp()))

    def test_leap_second_tolerated(self):
        self.assertGreater(C._epoch_from_iso("2016-12-31T23:59:60Z"), 0)

    def test_civil_epoch(self):
        self.assertEqual(C._days_from_civil(1970, 1, 1), 0)

    def test_civil_known(self):
        self.assertEqual(C._days_from_civil(2000, 3, 1), 11017)

    def test_monotonic_over_a_year(self):
        last = -1
        for month in range(1, 13):
            value = C._days_from_civil(2026, month, 1)
            self.assertGreater(value, last)
            last = value


class TestHashAndFormat(Base):
    def test_fnv_is_16_hex(self):
        h = C._fnv("anything")
        self.assertEqual(len(h), 16)
        for ch in h:
            self.assertIn(ch, "0123456789abcdef")

    def test_fnv_is_deterministic(self):
        self.assertEqual(C._fnv("abc"), C._fnv("abc"))

    def test_fnv_is_sensitive(self):
        self.assertNotEqual(C._fnv("abc"), C._fnv("abd"))

    def test_fnv_sensitive_to_order(self):
        self.assertNotEqual(C._fnv("ab"), C._fnv("ba"))

    def test_fnv_empty(self):
        self.assertEqual(len(C._fnv("")), 16)

    def test_gen_whole(self):
        self.assertEqual(C._gen(GEN), "1.00")

    def test_gen_zero(self):
        self.assertEqual(C._gen(0), "0.00")

    def test_gen_fraction(self):
        self.assertEqual(C._gen(GEN // 2), "0.50")

    def test_gen_small(self):
        self.assertEqual(C._gen(10 ** 15), "0.001")

    def test_gen_one_wei(self):
        self.assertEqual(C._gen(1), "0.000000000000000001")

    def test_gen_large(self):
        self.assertEqual(C._gen(1234 * GEN), "1234.00")

    def test_gen_negative(self):
        self.assertTrue(C._gen(-GEN).startswith("-1."))

    def test_gen_has_no_float(self):
        """A float anywhere near money puts a platform's rounding on the
        consensus axis. Large values must not go exponential."""
        text = C._gen(10 ** 24)
        self.assertNotIn("e", text)
        self.assertTrue(text.startswith("1000000."))

    def test_flat_collapses(self):
        self.assertEqual(C._flat("a \n b\t c"), "a b c")

    def test_clean_caps(self):
        self.assertEqual(len(C._clean("x" * 500, 10)), 10)

    def test_clean_strips_control(self):
        self.assertNotIn("\x01", C._clean("a\x01b", 50))

    def test_clean_has_no_newline(self):
        self.assertNotIn("\n", C._clean("a\nb", 50))

    def test_plural(self):
        self.assertEqual(C._plural(1, "day", "days"), "day")
        self.assertEqual(C._plural(2, "day", "days"), "days")
        self.assertEqual(C._plural(0, "day", "days"), "days")


# ===========================================================================
# 2. the explorer: URL, parsing, and the result-null trap
# ===========================================================================


class TestUrl(Base):
    def test_url_uses_allowlist_host(self):
        url = C._url(C.CHAIN_HOSTS["ethereum"], str(ALICE), 10)
        self.assertTrue(url.startswith("https://eth.blockscout.com/api?"))

    def test_url_carries_address(self):
        self.assertIn(str(ALICE), C._url("h", str(ALICE), 10))

    def test_url_carries_window(self):
        self.assertIn("offset=10", C._url("h", str(ALICE), 10))

    def test_url_sorts_desc(self):
        self.assertIn("sort=desc", C._url("h", str(ALICE), 10))

    def test_url_is_txlist(self):
        self.assertIn("action=txlist", C._url("h", str(ALICE), 10))

    def test_every_chain_has_a_host(self):
        for chain in C.CHAINS:
            self.assertIn(chain, C.CHAIN_HOSTS)
            self.assertTrue(C.CHAIN_HOSTS[chain].endswith("blockscout.com"))

    def test_default_chain_is_supported(self):
        self.assertIn(C.DEFAULT_CHAIN, C.CHAIN_HOSTS)

    def test_no_chain_host_is_caller_supplied(self):
        """RULE 10, as syntax. Nothing in the file may build a probe URL out of
        anything but the allowlist table."""
        for node in ast.walk(TREE):
            if isinstance(node, ast.FunctionDef) and node.name == "_url":
                names = {a.arg for a in node.args.args}
                self.assertEqual(names, {"host", "wallet", "window"})


class TestParse(Base):
    def test_ok_list(self):
        readable, items = C._parse(txlist([tx(NOW, ALICE)]))
        self.assertTrue(readable)
        self.assertEqual(len(items), 1)

    def test_empty_list_is_readable(self):
        """`result: []` is a REAL ANSWER - this wallet has no transactions -
        and it is the answer that releases money."""
        readable, items = C._parse(NO_TX_BODY)
        self.assertTrue(readable)
        self.assertEqual(items, [])

    def test_null_result_is_not_readable(self):
        """THE TRAP. `result: null` means the explorer refused the query, and it
        arrives with the same HTTP 200 and the same `status: "0"` as the empty
        answer above. Any `result or []` would read it as 'no transactions' and
        pay out an inheritance on a typo."""
        readable, items = C._parse(REFUSED_BODY)
        self.assertFalse(readable)
        self.assertEqual(items, [])

    def test_empty_and_refused_differ(self):
        self.assertNotEqual(C._parse(NO_TX_BODY)[0], C._parse(REFUSED_BODY)[0])

    def test_bad_json(self):
        self.assertFalse(C._parse("<html>502 Bad Gateway</html>")[0])

    def test_empty_body(self):
        self.assertFalse(C._parse("")[0])

    def test_non_dict_json(self):
        self.assertFalse(C._parse("[1,2,3]")[0])

    def test_json_null(self):
        self.assertFalse(C._parse("null")[0])

    def test_missing_result_key(self):
        self.assertFalse(C._parse('{"status":"1"}')[0])

    def test_result_is_a_string(self):
        self.assertFalse(C._parse('{"result":"nope"}')[0])

    def test_result_is_a_dict(self):
        self.assertFalse(C._parse('{"result":{"a":1}}')[0])

    def test_tx_time_reads_string_seconds(self):
        self.assertEqual(C._tx_time(tx(1789470311, ALICE)), 1789470311)

    def test_tx_time_of_junk(self):
        self.assertEqual(C._tx_time({"timeStamp": "soon"}), 0)

    def test_tx_time_of_non_dict(self):
        self.assertEqual(C._tx_time("nope"), 0)

    def test_tx_time_missing(self):
        self.assertEqual(C._tx_time({}), 0)

    def test_tx_from_lowercases(self):
        self.assertEqual(C._tx_from({"from": "0xAABB"}), "0xaabb")

    def test_tx_from_missing(self):
        self.assertEqual(C._tx_from({}), "")

    def test_tx_from_null(self):
        self.assertEqual(C._tx_from({"from": None}), "")

    def test_tx_from_non_dict(self):
        self.assertEqual(C._tx_from(12), "")


# ===========================================================================
# 3. the probe - what the validators actually vote on
# ===========================================================================


class TestProbe(Base):
    def probe(self, anchor=None, now=None, chain="ethereum", wallet=None):
        return C._probe(1, chain, str(wallet or ALICE),
                        NOW - 10 * DAY if anchor is None else anchor,
                        NOW if now is None else now, 10)

    def test_signed_after_anchor_is_alive(self):
        serve_txs([tx(NOW - DAY, ALICE)])
        self.assertEqual(self.probe()["activity_status"], C.A_ALIVE)

    def test_signed_before_anchor_is_inactive(self):
        serve_txs([tx(NOW - 40 * DAY, ALICE)])
        self.assertEqual(self.probe()["activity_status"], C.A_INACTIVE)

    def test_no_transactions_is_inactive(self):
        serve_empty()
        self.assertEqual(self.probe()["activity_status"], C.A_INACTIVE)

    def test_no_transactions_pins_buckets(self):
        serve_empty()
        vector = self.probe()
        self.assertEqual(vector["age_bucket"], C.AGE_NEVER)
        self.assertEqual(vector["count_bucket"], 0)
        self.assertTrue(vector["src_ok"])

    def test_inbound_only_is_inactive(self):
        """RULE 9, and the griefing vector it closes. A wallet receiving a
        transfer every day is not a wallet whose owner is alive - anyone can
        send to a dead address. If this read ALIVE, one wei a week would keep
        any will locked for ever."""
        serve_txs([tx(NOW - i * HOUR, STRANGER, to=ALICE) for i in range(10)])
        vector = self.probe()
        self.assertEqual(vector["activity_status"], C.A_INACTIVE)
        self.assertEqual(vector["count_bucket"], 0)

    def test_inbound_does_not_move_the_age_bucket(self):
        serve_txs([tx(NOW - 60, STRANGER, to=ALICE)])
        self.assertEqual(self.probe()["age_bucket"], C.AGE_NEVER)

    def test_one_signature_among_many_inbound_is_alive(self):
        items = [tx(NOW - i * HOUR, STRANGER, to=ALICE) for i in range(9)]
        items.append(tx(NOW - HOUR, ALICE))
        serve_txs(items)
        self.assertEqual(self.probe()["activity_status"], C.A_ALIVE)

    def test_signer_match_is_case_insensitive(self):
        serve_txs([tx(NOW - HOUR, str(ALICE).upper().replace("0X", "0x"))])
        self.assertEqual(self.probe()["activity_status"], C.A_ALIVE)

    def test_non_200_is_inconclusive(self):
        for status in (0, 301, 403, 404, 429, 500, 502, 503):
            WEB.reset()
            serve_down(status)
            vector = self.probe()
            self.assertEqual(vector["activity_status"], C.A_INCONCLUSIVE,
                             "status " + str(status))
            self.assertFalse(vector["src_ok"])

    def test_network_exception_is_inconclusive(self):
        WEB.serve(200, NO_TX_BODY)
        WEB.fail(1)
        self.assertEqual(self.probe()["activity_status"], C.A_INCONCLUSIVE)

    def test_unparseable_body_is_inconclusive(self):
        WEB.serve(200, "<html>oops</html>")
        self.assertEqual(self.probe()["activity_status"], C.A_INCONCLUSIVE)

    def test_refused_query_is_inconclusive_not_inactive(self):
        """THE MOST IMPORTANT TEST IN THIS CLASS. A refusal must never be read
        as dormancy."""
        serve_refused()
        vector = self.probe()
        self.assertEqual(vector["activity_status"], C.A_INCONCLUSIVE)
        self.assertNotEqual(vector["activity_status"], C.A_INACTIVE)
        self.assertFalse(vector["src_ok"])

    def test_unknown_chain_is_inconclusive(self):
        serve_txs([tx(NOW - HOUR, ALICE)])
        self.assertEqual(self.probe(chain="dogecoin")["activity_status"],
                         C.A_INCONCLUSIVE)

    def test_inconclusive_always_pins_its_buckets(self):
        for setup in (serve_refused, serve_down, lambda: WEB.serve(200, "x")):
            WEB.reset()
            setup()
            vector = self.probe()
            self.assertEqual(vector["age_bucket"], C.AGE_NEVER)
            self.assertEqual(vector["count_bucket"], 0)
            self.assertFalse(vector["src_ok"])

    def test_age_bucket_from_newest_signature(self):
        cases = [(0, 0), (2 * DAY, 1), (5 * DAY, 2), (10 * DAY, 3),
                 (20 * DAY, 4), (60 * DAY, 5), (120 * DAY, 6), (400 * DAY, 7)]
        for gap, rung in cases:
            WEB.reset()
            serve_txs([tx(NOW - gap, ALICE)])
            self.assertEqual(self.probe(anchor=NOW)["age_bucket"], rung,
                             "gap " + str(gap))

    def test_count_bucket_counts_only_signatures(self):
        for n, rung in [(0, 0), (1, 1), (2, 2), (4, 3), (8, 4)]:
            WEB.reset()
            items = [tx(NOW - (i + 1) * DAY, ALICE) for i in range(n)]
            items += [tx(NOW - HOUR, STRANGER, to=ALICE) for _ in range(3)]
            serve_txs(items)
            self.assertEqual(self.probe(anchor=NOW)["count_bucket"], rung,
                             "n " + str(n))

    def test_future_dated_transaction_reads_age_zero(self):
        serve_txs([tx(NOW + 10 * DAY, ALICE)])
        vector = self.probe(anchor=NOW)
        self.assertEqual(vector["age_bucket"], 0)
        self.assertEqual(vector["activity_status"], C.A_ALIVE)

    def test_zero_timestamp_is_ignored(self):
        serve_txs([{"timeStamp": "0", "from": str(ALICE), "hash": "0x0"}])
        vector = self.probe(anchor=NOW)
        self.assertEqual(vector["count_bucket"], 0)
        self.assertEqual(vector["age_bucket"], C.AGE_NEVER)

    def test_non_dict_items_are_ignored(self):
        WEB.serve(200, json.dumps({"status": "1", "result": ["nope", 7, None]}))
        vector = self.probe()
        self.assertEqual(vector["activity_status"], C.A_INACTIVE)
        self.assertTrue(vector["src_ok"])

    def test_anchor_is_exclusive(self):
        """A transaction stamped exactly AT the anchor is not after it."""
        serve_txs([tx(NOW - 10 * DAY, ALICE)])
        self.assertEqual(self.probe(anchor=NOW - 10 * DAY)["activity_status"],
                         C.A_INACTIVE)

    def test_one_second_after_the_anchor_is_alive(self):
        serve_txs([tx(NOW - 10 * DAY + 1, ALICE)])
        self.assertEqual(self.probe(anchor=NOW - 10 * DAY)["activity_status"],
                         C.A_ALIVE)

    def test_probe_is_deterministic(self):
        serve_txs([tx(NOW - 3 * DAY, ALICE), tx(NOW - 40 * DAY, ALICE)])
        self.assertEqual(self.probe(), self.probe())

    def test_probe_never_returns_out_of_range_buckets(self):
        for setup in (serve_empty, serve_refused,
                      lambda: serve_txs([tx(NOW - 900 * DAY, ALICE)])):
            WEB.reset()
            setup()
            vector = self.probe()
            self.assertGreaterEqual(vector["age_bucket"], 0)
            self.assertLessEqual(vector["age_bucket"], C.TOP_BUCKET)
            self.assertGreaterEqual(vector["count_bucket"], 0)
            self.assertLessEqual(vector["count_bucket"], C.TOP_BUCKET)

    def test_probe_returns_only_the_four_vector_fields(self):
        """RULE 1's corollary: the probe must not smuggle a field into storage
        that the validators never compared."""
        serve_empty()
        self.assertEqual(set(self.probe().keys()),
                         {"activity_status", "age_bucket", "count_bucket",
                          "src_ok"})

    def test_verdict_is_one_of_three(self):
        for setup in (serve_empty, serve_refused,
                      lambda: serve_txs([tx(NOW - HOUR, ALICE)])):
            WEB.reset()
            setup()
            self.assertIn(self.probe()["activity_status"], C.VERDICTS)

    def test_alive_requires_a_signature_in_the_count(self):
        serve_txs([tx(NOW - HOUR, ALICE)])
        vector = self.probe()
        self.assertEqual(vector["activity_status"], C.A_ALIVE)
        self.assertGreater(vector["count_bucket"], 0)

    def test_window_is_passed_to_the_url(self):
        serve_empty()
        C._probe(1, "ethereum", str(ALICE), NOW, NOW, 10)
        self.assertIn("offset=10", WEB.log[-1])

    def test_probe_does_not_call_a_model(self):
        """`_exec_prompt` in this harness raises on sight. A probe that reached
        for a model would fail here rather than on chain."""
        serve_empty()
        self.probe()


# ===========================================================================
# 4. the content hash and the canonical projection
# ===========================================================================


class TestCanon(Base):
    def vec(self, **kw):
        base = {"activity_status": C.A_INACTIVE, "age_bucket": 7,
                "count_bucket": 0, "src_ok": True}
        base.update(kw)
        return base

    def canon(self, **kw):
        args = {"will_id": 1, "chain": "ethereum", "wallet": str(ALICE),
                "anchor_ts": NOW - DAY, "now_ts": NOW, "window": 10}
        args.update(kw)
        vector = args.pop("vector", self.vec())
        return C._canon(args["will_id"], args["chain"], args["wallet"],
                        args["anchor_ts"], args["now_ts"], args["window"],
                        vector)

    def test_canon_is_deterministic(self):
        self.assertEqual(self.canon(), self.canon())

    def test_canon_binds_will_id(self):
        self.assertNotEqual(self.canon(), self.canon(will_id=2))

    def test_canon_binds_chain(self):
        self.assertNotEqual(self.canon(), self.canon(chain="base"))

    def test_canon_binds_wallet(self):
        self.assertNotEqual(self.canon(), self.canon(wallet=str(BOB)))

    def test_canon_binds_anchor(self):
        self.assertNotEqual(self.canon(), self.canon(anchor_ts=NOW - 2 * DAY))

    def test_canon_binds_now(self):
        self.assertNotEqual(self.canon(), self.canon(now_ts=NOW + 1))

    def test_canon_binds_window(self):
        self.assertNotEqual(self.canon(), self.canon(window=25))

    def test_canon_binds_status(self):
        self.assertNotEqual(
            self.canon(),
            self.canon(vector=self.vec(activity_status=C.A_ALIVE)))

    def test_canon_binds_age_bucket(self):
        self.assertNotEqual(self.canon(),
                            self.canon(vector=self.vec(age_bucket=3)))

    def test_canon_binds_count_bucket(self):
        self.assertNotEqual(self.canon(),
                            self.canon(vector=self.vec(count_bucket=3)))

    def test_canon_binds_src_ok(self):
        self.assertNotEqual(self.canon(),
                            self.canon(vector=self.vec(src_ok=False)))

    def test_canon_normalises_wallet_case(self):
        upper = str(ALICE).upper().replace("0X", "0x")
        self.assertEqual(self.canon(), self.canon(wallet=upper))

    def test_canon_carries_the_rubric_version(self):
        self.assertIn(C.RUBRIC_VERSION, self.canon())

    def test_seal_adds_a_hash(self):
        sealed = C._seal(1, "ethereum", str(ALICE), NOW - DAY, NOW, 10,
                         self.vec())
        self.assertEqual(len(sealed["content_hash"]), 16)

    def test_seal_does_not_mutate_its_input(self):
        vector = self.vec()
        C._seal(1, "ethereum", str(ALICE), NOW - DAY, NOW, 10, vector)
        self.assertNotIn("content_hash", vector)

    def test_seal_hash_matches_canon(self):
        sealed = C._seal(1, "ethereum", str(ALICE), NOW - DAY, NOW, 10,
                         self.vec())
        self.assertEqual(sealed["content_hash"], C._fnv(self.canon()))

    def test_a_vector_cannot_be_replayed_onto_another_will(self):
        """The hash binds the QUESTION as well as the answer, so an honest
        reading of will 1 cannot be presented as a reading of will 2."""
        one = C._seal(1, "ethereum", str(ALICE), NOW - DAY, NOW, 10, self.vec())
        two = C._seal(2, "ethereum", str(ALICE), NOW - DAY, NOW, 10, self.vec())
        self.assertNotEqual(one["content_hash"], two["content_hash"])

    def test_a_vector_cannot_be_replayed_onto_another_wallet(self):
        one = C._seal(1, "ethereum", str(ALICE), NOW - DAY, NOW, 10, self.vec())
        two = C._seal(1, "ethereum", str(BOB), NOW - DAY, NOW, 10, self.vec())
        self.assertNotEqual(one["content_hash"], two["content_hash"])

    def test_separator_cannot_appear_in_a_field(self):
        """Two different projections must not canonicalise to one string."""
        canon = self.canon()
        parts = canon.split("|")
        self.assertEqual(len(parts), 11)


# ===========================================================================
# 5. the consensus gates - one forgery per field
# ===========================================================================


class TestCoherent(Base):
    def good(self, **kw):
        vector = {"activity_status": C.A_INACTIVE, "age_bucket": 7,
                  "count_bucket": 0, "src_ok": True}
        vector.update(kw)
        return C._seal(1, "ethereum", str(ALICE), NOW - DAY, NOW, 10, vector)

    def test_a_good_payload_passes(self):
        self.assertTrue(C._coherent(self.good()))

    def test_alive_payload_passes(self):
        self.assertTrue(C._coherent(
            self.good(activity_status=C.A_ALIVE, age_bucket=0,
                      count_bucket=2)))

    def test_inconclusive_payload_passes(self):
        self.assertTrue(C._coherent(
            self.good(activity_status=C.A_INCONCLUSIVE, age_bucket=7,
                      count_bucket=0, src_ok=False)))

    def test_not_a_dict(self):
        for value in (None, "x", 7, [], True):
            self.assertFalse(C._coherent(value))

    def test_missing_status(self):
        payload = self.good()
        del payload["activity_status"]
        self.assertFalse(C._coherent(payload))

    def test_unknown_status(self):
        self.assertFalse(C._coherent(self.good(activity_status="DEAD")))

    def test_empty_status(self):
        self.assertFalse(C._coherent(self.good(activity_status="")))

    def test_status_wrong_type(self):
        self.assertFalse(C._coherent(self.good(activity_status=1)))

    def test_src_ok_wrong_type(self):
        """`src_ok` must be a real bool - `1` would pass a truthiness check and
        then compare unequal to `True` on another node."""
        self.assertFalse(C._coherent(self.good(src_ok=1)))

    def test_src_ok_missing(self):
        payload = self.good()
        del payload["src_ok"]
        self.assertFalse(C._coherent(payload))

    def test_age_bucket_as_bool(self):
        """`True` is an int of value 1 in Python. Without the explicit bool
        exclusion it would silently pass as bucket 1."""
        self.assertFalse(C._coherent(self.good(age_bucket=True)))

    def test_count_bucket_as_bool(self):
        self.assertFalse(C._coherent(self.good(count_bucket=True)))

    def test_age_bucket_as_string(self):
        self.assertFalse(C._coherent(self.good(age_bucket="7")))

    def test_count_bucket_as_string(self):
        self.assertFalse(C._coherent(self.good(count_bucket="0")))

    def test_age_bucket_negative(self):
        self.assertFalse(C._coherent(self.good(age_bucket=-1)))

    def test_age_bucket_too_high(self):
        self.assertFalse(C._coherent(self.good(age_bucket=8)))

    def test_count_bucket_too_high(self):
        self.assertFalse(C._coherent(
            self.good(activity_status=C.A_ALIVE, count_bucket=99)))

    def test_age_bucket_missing(self):
        payload = self.good()
        del payload["age_bucket"]
        self.assertFalse(C._coherent(payload))

    def test_count_bucket_missing(self):
        payload = self.good()
        del payload["count_bucket"]
        self.assertFalse(C._coherent(payload))

    def test_hash_missing(self):
        payload = self.good()
        del payload["content_hash"]
        self.assertFalse(C._coherent(payload))

    def test_hash_wrong_length(self):
        payload = self.good()
        payload["content_hash"] = "abc"
        self.assertFalse(C._coherent(payload))

    def test_hash_wrong_alphabet(self):
        payload = self.good()
        payload["content_hash"] = "zzzzzzzzzzzzzzzz"
        self.assertFalse(C._coherent(payload))

    def test_hash_uppercase_rejected(self):
        payload = self.good()
        payload["content_hash"] = payload["content_hash"].upper()
        self.assertFalse(C._coherent(payload))

    def test_hash_wrong_type(self):
        payload = self.good()
        payload["content_hash"] = 12345
        self.assertFalse(C._coherent(payload))

    def test_inactive_with_dead_source_is_refused(self):
        """THE FORGERY WORTH ATTEMPTING, and the one this gate exists for. A
        leader claiming the wallet is dormant while admitting it could not read
        the explorer is refused by every validator without any of them fetching
        anything."""
        self.assertFalse(C._coherent(
            self.good(activity_status=C.A_INACTIVE, src_ok=False)))

    def test_alive_with_dead_source_is_refused(self):
        self.assertFalse(C._coherent(
            self.good(activity_status=C.A_ALIVE, src_ok=False,
                      count_bucket=2)))

    def test_inconclusive_with_live_source_is_refused(self):
        self.assertFalse(C._coherent(
            self.good(activity_status=C.A_INCONCLUSIVE, src_ok=True)))

    def test_inconclusive_with_a_real_age_bucket_is_refused(self):
        self.assertFalse(C._coherent(
            self.good(activity_status=C.A_INCONCLUSIVE, src_ok=False,
                      age_bucket=2, count_bucket=0)))

    def test_inconclusive_with_a_real_count_bucket_is_refused(self):
        self.assertFalse(C._coherent(
            self.good(activity_status=C.A_INCONCLUSIVE, src_ok=False,
                      age_bucket=7, count_bucket=3)))

    def test_alive_with_no_signatures_is_refused(self):
        """A wallet that has signed nothing cannot be alive on the evidence."""
        self.assertFalse(C._coherent(
            self.good(activity_status=C.A_ALIVE, count_bucket=0)))

    def test_every_vector_field_has_a_forgery_test(self):
        """Enumerates the compared axis and fails if a field is not covered by
        a test in this class. A field nobody forged is a field nobody checked.

        The mapping from field to test-name fragment is spelled out rather than
        derived, because a derivation that happened to match nothing would make
        this test pass by accident - which is the one failure mode a coverage
        test must not have."""
        blob = " ".join(n for n in dir(self) if n.startswith("test_"))
        fragments = {
            "activity_status": ("status", "verdict"),
            "age_bucket": ("age_bucket",),
            "count_bucket": ("count_bucket",),
            "src_ok": ("src_ok", "dead_source"),
            "content_hash": ("hash",),
        }
        self.assertEqual(set(fragments),
                         {"activity_status", "age_bucket", "count_bucket",
                          "src_ok", "content_hash"})
        for field, tokens in fragments.items():
            self.assertTrue(any(t in blob for t in tokens),
                            "no forgery test covers " + field)


class TestAgrees(Base):
    def vec(self, **kw):
        base = {"activity_status": C.A_INACTIVE, "age_bucket": 7,
                "count_bucket": 0, "src_ok": True,
                "content_hash": "0123456789abcdef"}
        base.update(kw)
        return base

    def test_identical_agree(self):
        self.assertTrue(C._agrees(self.vec(), self.vec()))

    def test_different_status(self):
        self.assertFalse(C._agrees(self.vec(activity_status=C.A_ALIVE),
                                   self.vec()))

    def test_different_age_bucket(self):
        self.assertFalse(C._agrees(self.vec(age_bucket=6), self.vec()))

    def test_different_count_bucket(self):
        self.assertFalse(C._agrees(self.vec(count_bucket=1), self.vec()))

    def test_different_src_ok(self):
        self.assertFalse(C._agrees(self.vec(src_ok=False), self.vec()))

    def test_different_hash(self):
        self.assertFalse(C._agrees(self.vec(content_hash="f" * 16),
                                   self.vec()))

    def test_missing_key_on_either_side(self):
        short = self.vec()
        del short["age_bucket"]
        self.assertFalse(C._agrees(short, self.vec()))
        self.assertFalse(C._agrees(self.vec(), short))

    def test_non_dict_either_side(self):
        self.assertFalse(C._agrees(None, self.vec()))
        self.assertFalse(C._agrees(self.vec(), None))
        self.assertFalse(C._agrees("x", "x"))

    def test_extra_keys_are_ignored(self):
        """Only the compared axis matters; a leader adding a field cannot make
        two honest readings disagree."""
        rich = self.vec()
        rich["chatter"] = "hello"
        self.assertTrue(C._agrees(rich, self.vec()))

    def test_there_is_no_tolerance(self):
        """The tolerance lives in the ladders, never in the comparison. A
        comparison with slack would mean two accepted readings for one claim
        could differ - and then which one released the money?"""
        self.assertFalse(C._agrees(self.vec(age_bucket=7),
                                   self.vec(age_bucket=6)))

    def test_string_bucket_does_not_equal_int_bucket(self):
        self.assertFalse(C._agrees(self.vec(age_bucket="7"),
                                   self.vec(age_bucket=7)))


# ===========================================================================
# 6. the split - exact conservation
# ===========================================================================


class TestSplit(Base):
    def test_five_percent(self):
        b, f = C._split(100 * GEN, 500)
        self.assertEqual(f, 5 * GEN)
        self.assertEqual(b, 95 * GEN)

    def test_conserves_exactly(self):
        self.assertEqual(sum(C._split(100 * GEN, 500)), 100 * GEN)

    def test_conserves_over_the_cross_product(self):
        """Every awkward amount against every fee, with no wei leaking. A
        contract that leaks a wei per will has a revenue model it never
        declared."""
        amounts = [1, 2, 3, 7, 99, 1000, 10 ** 15, 10 ** 15 + 1, GEN,
                   GEN + 1, 3 * GEN + 7, 123456789, 10 ** 22, 10 ** 24,
                   10 ** 18 - 1, 7 * 10 ** 17 + 13]
        fees = [0, 1, 7, 100, 250, 499, 500, 501, 999, 1000, 1999, 2000]
        for amount in amounts:
            for fee in fees:
                b, f = C._split(amount, fee)
                self.assertEqual(b + f, amount,
                                 "amount " + str(amount) + " fee " + str(fee))
                self.assertGreaterEqual(b, 0)
                self.assertGreaterEqual(f, 0)
                self.assertLessEqual(f, amount)

    def test_remainder_goes_to_the_beneficiary(self):
        """Integer division floors the finder's cut, so the leftover wei goes
        to the party the contract exists to serve."""
        b, f = C._split(10 ** 15 + 7, 500)
        self.assertEqual(b + f, 10 ** 15 + 7)
        self.assertGreaterEqual(b * 100, f * 100)

    def test_zero_deposit(self):
        self.assertEqual(C._split(0, 500), (0, 0))

    def test_negative_deposit(self):
        self.assertEqual(C._split(-5, 500), (0, 0))

    def test_zero_fee_pays_the_beneficiary_everything(self):
        self.assertEqual(C._split(GEN, 0), (GEN, 0))

    def test_one_wei_deposit(self):
        b, f = C._split(1, 500)
        self.assertEqual((b, f), (1, 0))

    def test_fee_is_capped(self):
        """A fee above the ceiling cannot be expressed, so a mis-set constant
        cannot take the whole estate."""
        b, f = C._split(GEN, 100000)
        self.assertEqual(b + f, GEN)
        self.assertLessEqual(f, GEN * C.MAX_FINDER_FEE_BPS // C.BPS)

    def test_negative_fee_is_clamped(self):
        self.assertEqual(C._split(GEN, -500), (GEN, 0))

    def test_finder_never_takes_more_than_the_deposit(self):
        for amount in (1, 10, GEN):
            b, f = C._split(amount, C.MAX_FINDER_FEE_BPS)
            self.assertLessEqual(f, amount)
            self.assertEqual(b + f, amount)

    def test_default_fee_is_five_percent(self):
        self.assertEqual(C.DEFAULT_FINDER_FEE_BPS, 500)


# ===========================================================================
# 7. the composed reasoning
# ===========================================================================


class TestReason(Base):
    def vec(self, **kw):
        base = {"activity_status": C.A_INACTIVE, "age_bucket": 7,
                "count_bucket": 0, "src_ok": True}
        base.update(kw)
        return base

    def test_is_deterministic(self):
        """RULE 1. Two nodes asked to describe what they saw would write two
        paragraphs, and a stored paragraph the validators never compared is a
        stored value the leader forged. So it is composed, not written."""
        self.assertEqual(C._reason(self.vec(), "ethereum", 10),
                         C._reason(self.vec(), "ethereum", 10))

    def test_differs_by_verdict(self):
        a = C._reason(self.vec(activity_status=C.A_ALIVE, count_bucket=2),
                      "ethereum", 10)
        b = C._reason(self.vec(), "ethereum", 10)
        self.assertNotEqual(a, b)

    def test_differs_by_age_bucket(self):
        self.assertNotEqual(C._reason(self.vec(age_bucket=1), "ethereum", 10),
                            C._reason(self.vec(age_bucket=5), "ethereum", 10))

    def test_differs_by_count_bucket(self):
        self.assertNotEqual(C._reason(self.vec(count_bucket=1), "ethereum", 10),
                            C._reason(self.vec(count_bucket=5), "ethereum", 10))

    def test_names_the_chain(self):
        self.assertIn("base", C._reason(self.vec(), "base", 10))

    def test_inactive_mentions_signatures_only(self):
        text = C._reason(self.vec(), "ethereum", 10)
        self.assertIn("Inbound transfers were ignored", text)

    def test_alive_says_the_deposit_stays(self):
        text = C._reason(self.vec(activity_status=C.A_ALIVE, count_bucket=2),
                         "ethereum", 10)
        self.assertIn("stays where it is", text)

    def test_inconclusive_says_retry(self):
        text = C._reason(self.vec(activity_status=C.A_INCONCLUSIVE,
                                  src_ok=False), "ethereum", 10)
        self.assertIn("again", text)

    def test_fits_the_cap(self):
        for status in C.VERDICTS:
            for age in range(8):
                for count in range(8):
                    text = C._reason(
                        self.vec(activity_status=status, age_bucket=age,
                                 count_bucket=count), "arbitrum", 10)
                    self.assertLessEqual(len(text), C.MAX_REASON_CHARS,
                                         status + " " + str(age) + " "
                                         + str(count))

    def test_has_no_newline(self):
        self.assertNotIn("\n", C._reason(self.vec(), "ethereum", 10))

    def test_word_tables_cover_every_bucket(self):
        self.assertEqual(len(C.AGE_WORDS), 8)
        self.assertEqual(len(C.COUNT_WORDS), 8)

    def test_every_bucket_pair_composes(self):
        for age in range(8):
            for count in range(8):
                text = C._reason(self.vec(age_bucket=age, count_bucket=count),
                                 "ethereum", 10)
                self.assertIn(C.AGE_WORDS[age], text)
                self.assertIn(C.COUNT_WORDS[count], text)


# ===========================================================================
# 8. create_will
# ===========================================================================


class TestCreateWill(Base):
    def test_happy_path(self):
        c = self.make()
        out = self.call(c, ALICE, "create_will", str(BOB), 1, "ethereum",
                        value=10 * GEN)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["will_id"], 1)
        self.assertEqual(out["beneficiary"], str(BOB))
        self.ledger(c)

    def test_deposit_is_locked(self):
        c = self.make()
        self.make_will(c, deposit=10 * GEN)
        self.assertEqual(int(c.locked_wei), 10 * GEN)
        self.assertEqual(int(c.payable_wei), 0)
        self.ledger(c)

    def test_ids_are_dense_and_one_based(self):
        c = self.make()
        self.assertEqual(self.make_will(c, owner=ALICE), 1)
        self.assertEqual(self.make_will(c, owner=CAROL), 2)

    def test_owner_index(self):
        c = self.make()
        wid = self.make_will(c)
        out = self.view(c, "get_wills_by_owner", str(ALICE))
        self.assertEqual([w["will_id"] for w in out["wills"]], [wid])

    def test_beneficiary_index(self):
        c = self.make()
        wid = self.make_will(c)
        out = self.view(c, "get_wills_by_beneficiary", str(BOB))
        self.assertEqual([w["will_id"] for w in out["wills"]], [wid])

    def test_beneficiary_cannot_be_self(self):
        c = self.make()
        out = self.call(c, ALICE, "create_will", str(ALICE), 1, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "REJECTED")
        self.assertIn("cannot be the owner", out["reason"])

    def test_beneficiary_cannot_be_zero(self):
        c = self.make()
        out = self.call(c, ALICE, "create_will", C.ZERO_ADDRESS, 1,
                        "ethereum", value=GEN)
        self.assertEqual(out["status"], "REJECTED")

    def test_beneficiary_must_be_an_address(self):
        c = self.make()
        out = self.call(c, ALICE, "create_will", "not-an-address", 1,
                        "ethereum", value=GEN)
        self.assertEqual(out["status"], "REJECTED")

    def test_bad_address_does_not_raise(self):
        """`Address("nonsense")` raises. Rule 2 says nothing here may, so the
        shape is checked before the constructor is reached."""
        c = self.make()
        for bad in ("", "0x", "0xzz", "0x" + "a" * 41, None, 12):
            out = self.call(c, ALICE, "create_will", bad, 1, "ethereum",
                            value=GEN)
            self.assertEqual(out["status"], "REJECTED")

    def test_chain_must_be_supported(self):
        c = self.make()
        out = self.call(c, ALICE, "create_will", str(BOB), 1, "dogecoin",
                        value=GEN)
        self.assertEqual(out["status"], "REJECTED")
        self.assertIn("supported_chains", out)

    def test_every_supported_chain_is_accepted(self):
        for chain in C.CHAINS:
            c = self.make()
            out = self.call(c, ALICE, "create_will", str(BOB), 1, chain,
                            value=GEN)
            self.assertEqual(out["status"], "OK", chain)

    def test_chain_is_case_insensitive(self):
        c = self.make()
        out = self.call(c, ALICE, "create_will", str(BOB), 1, "ETHEREUM",
                        value=GEN)
        self.assertEqual(out["status"], "OK")

    def test_interval_below_the_minimum(self):
        c = self.real()
        out = self.call(c, ALICE, "create_will", str(BOB), 3, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "REJECTED")
        self.assertIn("min_interval_s", out)

    def test_interval_above_the_maximum(self):
        c = self.real()
        out = self.call(c, ALICE, "create_will", str(BOB), 400, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "REJECTED")

    def test_seven_days_is_accepted(self):
        c = self.real()
        out = self.call(c, ALICE, "create_will", str(BOB), 7, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "OK")

    def test_three_hundred_and_sixty_five_days_is_accepted(self):
        c = self.real()
        out = self.call(c, ALICE, "create_will", str(BOB), 365, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "OK")

    def test_zero_days(self):
        c = self.real()
        out = self.call(c, ALICE, "create_will", str(BOB), 0, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "REJECTED")

    def test_negative_days(self):
        c = self.real()
        out = self.call(c, ALICE, "create_will", str(BOB), -5, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "REJECTED")

    def test_days_as_junk(self):
        c = self.real()
        for bad in ("soon", None, [], True):
            out = self.call(c, ALICE, "create_will", str(BOB), bad,
                            "ethereum", value=GEN)
            self.assertEqual(out["status"], "REJECTED")

    def test_deposit_below_the_minimum(self):
        c = self.make()
        out = self.call(c, ALICE, "create_will", str(BOB), 1, "ethereum",
                        value=1)
        self.assertEqual(out["status"], "REJECTED")

    def test_no_deposit_at_all(self):
        c = self.make()
        out = self.call(c, ALICE, "create_will", str(BOB), 1, "ethereum",
                        value=0)
        self.assertEqual(out["status"], "REJECTED")

    def test_deposit_above_the_maximum(self):
        c = self.make()
        out = self.call(c, ALICE, "create_will", str(BOB), 1, "ethereum",
                        value=C.MAX_DEPOSIT_WEI + 1)
        self.assertEqual(out["status"], "REJECTED")

    def test_one_active_will_per_wallet(self):
        c = self.make()
        self.make_will(c, owner=ALICE)
        out = self.call(c, ALICE, "create_will", str(CAROL), 1, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "REJECTED")
        self.assertEqual(out["active_will_id"], 1)

    def test_a_cancelled_will_frees_the_wallet(self):
        """The limit is on CONCURRENT wills. A wallet whose will has closed is
        free to open another - the alternative would retire a wallet for ever
        the moment its first will ended."""
        c = self.make()
        self.make_will(c, owner=ALICE)
        self.call(c, ALICE, "cancel_will", 1)
        out = self.call(c, ALICE, "create_will", str(CAROL), 1, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "OK")
        self.ledger(c)

    def test_paused_blocks_creation(self):
        c = self.make()
        self.call(c, OWNER, "set_paused", True)
        out = self.call(c, ALICE, "create_will", str(BOB), 1, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "REJECTED")

    def test_two_owners_can_name_the_same_beneficiary(self):
        c = self.make()
        self.make_will(c, owner=ALICE, beneficiary=BOB)
        self.make_will(c, owner=CAROL, beneficiary=BOB)
        out = self.view(c, "get_wills_by_beneficiary", str(BOB))
        self.assertEqual(out["count"], 2)

    def test_unreadable_block_time_refuses(self):
        c = self.make()
        MESSAGE.raw["datetime"] = "nonsense"
        out = self.call(c, ALICE, "create_will", str(BOB), 1, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "REJECTED")
        set_now(NOW)

    def test_heartbeat_starts_at_creation(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(int(c.wills[0].last_heartbeat), NOW)

    def test_claimable_at_is_interval_times_threshold(self):
        c = self.make(missed_threshold=3)
        out = self.call(c, ALICE, "create_will", str(BOB), 2, "ethereum",
                        value=GEN)
        self.assertEqual(out["claimable_at"], NOW + 2 * DAY * 3)


# ===========================================================================
# 9. heartbeat, top_up, change_beneficiary, cancel
# ===========================================================================


class TestHeartbeat(Base):
    def test_resets_the_timer(self):
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 12 * HOUR)
        out = self.call(c, ALICE, "heartbeat", 1)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(int(c.wills[0].last_heartbeat), NOW + 12 * HOUR)

    def test_counts(self):
        c = self.make()
        self.make_will(c)
        for i in range(3):
            set_now(NOW + (i + 1) * HOUR)
            self.call(c, ALICE, "heartbeat", 1)
        self.assertEqual(int(c.wills[0].heartbeat_count), 3)

    def test_owner_only(self):
        c = self.make()
        self.make_will(c)
        out = self.call(c, STRANGER, "heartbeat", 1)
        self.assertEqual(out["status"], "REJECTED")

    def test_beneficiary_cannot_heartbeat(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(self.call(c, BOB, "heartbeat", 1)["status"],
                         "REJECTED")

    def test_unknown_will(self):
        c = self.make()
        self.assertEqual(self.call(c, ALICE, "heartbeat", 99)["status"],
                         "REJECTED")

    def test_works_while_paused(self):
        """RULE 6. An owner who could stop you checking in could time a claim
        onto you."""
        c = self.make()
        self.make_will(c)
        self.call(c, OWNER, "set_paused", True)
        set_now(NOW + HOUR)
        self.assertEqual(self.call(c, ALICE, "heartbeat", 1)["status"], "OK")

    def test_is_free(self):
        c = self.make()
        self.make_will(c)
        out = self.call(c, ALICE, "heartbeat", 1)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(int(c.locked_wei), 10 * GEN)
        self.ledger(c)

    def test_value_sent_is_refundable(self):
        """Nothing here is payable, but if the runner ever let value through it
        must still have an owner and a way out (rule 7)."""
        c = self.make()
        self.make_will(c)
        self.call(c, ALICE, "heartbeat", 1, value=GEN)
        self.assertEqual(self.owed(c, ALICE), GEN)
        self.ledger(c)

    def test_pushes_the_deadline_out(self):
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 12 * HOUR)
        out = self.call(c, ALICE, "heartbeat", 1)
        self.assertEqual(out["claimable_at"],
                         NOW + 12 * HOUR + 2 * DAY)


class TestTopUp(Base):
    def test_adds_to_the_deposit(self):
        c = self.make()
        self.make_will(c, deposit=10 * GEN)
        out = self.call(c, ALICE, "top_up", 1, value=5 * GEN)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(int(c.wills[0].deposit_wei), 15 * GEN)
        self.ledger(c)

    def test_owner_only(self):
        c = self.make()
        self.make_will(c)
        out = self.call(c, STRANGER, "top_up", 1, value=GEN)
        self.assertEqual(out["status"], "REJECTED")

    def test_refused_top_up_is_refundable(self):
        c = self.make()
        self.make_will(c)
        self.call(c, STRANGER, "top_up", 1, value=GEN)
        self.assertEqual(self.owed(c, STRANGER), GEN)
        self.ledger(c)

    def test_zero_value_refused(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(self.call(c, ALICE, "top_up", 1)["status"],
                         "REJECTED")

    def test_counts_as_a_check_in(self):
        """A top-up is a message from the owner's own key. Not counting it
        would mean an owner who funded their will and nothing else could be
        claimed against while visibly present."""
        c = self.make()
        self.make_will(c)
        set_now(NOW + 6 * HOUR)
        self.call(c, ALICE, "top_up", 1, value=GEN)
        self.assertEqual(int(c.wills[0].last_heartbeat), NOW + 6 * HOUR)

    def test_cap_is_enforced(self):
        c = self.make()
        self.make_will(c, deposit=C.MAX_DEPOSIT_WEI)
        out = self.call(c, ALICE, "top_up", 1, value=GEN)
        self.assertEqual(out["status"], "REJECTED")
        self.ledger(c)

    def test_refused_while_a_claim_is_in_flight(self):
        """THE BUG THIS TEST EXISTS FOR. `top_up` was the one owner method that
        could touch a will while a claim was in flight — and it both changes
        `deposit_wei` (the amount a settlement pays) and resets
        `last_heartbeat` (the anchor the validators measured against). Its two
        siblings were gated; it was not."""
        c = self.make(stall_ttl_s=600)
        self.make_will(c, deposit=10 * GEN)
        c.claiming["1"] = NOW
        out = self.call(c, ALICE, "top_up", 1, value=GEN)
        self.assertEqual(out["status"], "REJECTED")
        self.assertIn("in flight", out["reason"])
        self.ledger(c)

    def test_an_in_flight_top_up_cannot_move_the_anchor(self):
        """The griefing shape: escape a stalled claim for one wei by resetting
        the timer for another two intervals."""
        c = self.make(stall_ttl_s=600)
        self.make_will(c, days=1)
        anchor = int(c.wills[0].last_heartbeat)
        c.claiming["1"] = NOW
        # Inside the stall TTL: the claim is genuinely in flight. (Past the TTL
        # the gate SHOULD let a top-up through — that case is the test below.)
        set_now(NOW + 300)
        out = self.call(c, ALICE, "top_up", 1, value=GEN)
        self.assertEqual(out["status"], "REJECTED")
        self.assertEqual(int(c.wills[0].last_heartbeat), anchor)
        self.assertEqual(int(c.wills[0].deposit_wei), 10 * GEN)

    def test_a_refused_in_flight_top_up_is_refundable(self):
        c = self.make(stall_ttl_s=600)
        self.make_will(c)
        c.claiming["1"] = NOW
        self.call(c, ALICE, "top_up", 1, value=2 * GEN)
        self.assertEqual(self.owed(c, ALICE), 2 * GEN)
        self.ledger(c)

    def test_allowed_once_the_claim_has_stalled(self):
        c = self.make(stall_ttl_s=600)
        self.make_will(c, deposit=10 * GEN)
        c.claiming["1"] = NOW
        set_now(NOW + 601)
        self.assertEqual(self.call(c, ALICE, "top_up", 1, value=GEN)["status"],
                         "OK")
        self.assertEqual(int(c.wills[0].deposit_wei), 11 * GEN)

    def test_paused_blocks_top_up(self):
        c = self.make()
        self.make_will(c)
        self.call(c, OWNER, "set_paused", True)
        out = self.call(c, ALICE, "top_up", 1, value=GEN)
        self.assertEqual(out["status"], "REJECTED")
        self.assertEqual(self.owed(c, ALICE), GEN)


class TestChangeBeneficiary(Base):
    def test_changes(self):
        c = self.make()
        self.make_will(c, beneficiary=BOB)
        out = self.call(c, ALICE, "change_beneficiary", 1, str(CAROL))
        self.assertEqual(out["status"], "OK")
        self.assertEqual(c.wills[0].beneficiary, CAROL)

    def test_owner_only(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(
            self.call(c, STRANGER, "change_beneficiary", 1, str(CAROL))[
                "status"], "REJECTED")

    def test_cannot_be_self(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(
            self.call(c, ALICE, "change_beneficiary", 1, str(ALICE))[
                "status"], "REJECTED")

    def test_cannot_be_zero(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(
            self.call(c, ALICE, "change_beneficiary", 1, C.ZERO_ADDRESS)[
                "status"], "REJECTED")

    def test_cannot_be_the_same(self):
        c = self.make()
        self.make_will(c, beneficiary=BOB)
        self.assertEqual(
            self.call(c, ALICE, "change_beneficiary", 1, str(BOB))["status"],
            "REJECTED")

    def test_bad_address_refused(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(
            self.call(c, ALICE, "change_beneficiary", 1, "nope")["status"],
            "REJECTED")

    def test_counts_as_a_check_in(self):
        c = self.make()
        self.make_will(c)
        set_now(NOW + 6 * HOUR)
        self.call(c, ALICE, "change_beneficiary", 1, str(CAROL))
        self.assertEqual(int(c.wills[0].last_heartbeat), NOW + 6 * HOUR)

    def test_old_beneficiary_drops_out_of_the_index(self):
        c = self.make()
        self.make_will(c, beneficiary=BOB)
        self.call(c, ALICE, "change_beneficiary", 1, str(CAROL))
        self.assertEqual(self.view(c, "get_wills_by_beneficiary",
                                   str(BOB))["count"], 0)
        self.assertEqual(self.view(c, "get_wills_by_beneficiary",
                                   str(CAROL))["count"], 1)

    def test_index_does_not_duplicate_after_two_changes(self):
        c = self.make()
        self.make_will(c, beneficiary=BOB)
        self.call(c, ALICE, "change_beneficiary", 1, str(CAROL))
        self.call(c, ALICE, "change_beneficiary", 1, str(DAVE))
        self.call(c, ALICE, "change_beneficiary", 1, str(CAROL))
        self.assertEqual(self.view(c, "get_wills_by_beneficiary",
                                   str(CAROL))["count"], 1)

    def test_counts_changes(self):
        c = self.make()
        self.make_will(c)
        self.call(c, ALICE, "change_beneficiary", 1, str(CAROL))
        self.call(c, ALICE, "change_beneficiary", 1, str(DAVE))
        self.assertEqual(int(c.wills[0].beneficiary_changes), 2)


class TestCancel(Base):
    def test_refunds_the_deposit(self):
        c = self.make()
        self.make_will(c, deposit=10 * GEN)
        out = self.call(c, ALICE, "cancel_will", 1)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(self.owed(c, ALICE), 10 * GEN)
        self.assertEqual(int(c.locked_wei), 0)
        self.ledger(c)

    def test_refunds_after_a_top_up(self):
        c = self.make()
        self.make_will(c, deposit=10 * GEN)
        self.call(c, ALICE, "top_up", 1, value=5 * GEN)
        self.call(c, ALICE, "cancel_will", 1)
        self.assertEqual(self.owed(c, ALICE), 15 * GEN)
        self.ledger(c)

    def test_owner_only(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(self.call(c, STRANGER, "cancel_will", 1)["status"],
                         "REJECTED")

    def test_beneficiary_cannot_cancel(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(self.call(c, BOB, "cancel_will", 1)["status"],
                         "REJECTED")

    def test_status_becomes_terminal(self):
        c = self.make()
        self.make_will(c)
        self.call(c, ALICE, "cancel_will", 1)
        self.assertEqual(str(c.wills[0].status), C.W_CANCELLED)

    def test_cannot_cancel_twice(self):
        c = self.make()
        self.make_will(c)
        self.call(c, ALICE, "cancel_will", 1)
        self.assertEqual(self.call(c, ALICE, "cancel_will", 1)["status"],
                         "REJECTED")

    def test_works_while_paused(self):
        """RULE 6. An owner who could stop you withdrawing could hold your
        estate hostage."""
        c = self.make()
        self.make_will(c)
        self.call(c, OWNER, "set_paused", True)
        self.assertEqual(self.call(c, ALICE, "cancel_will", 1)["status"], "OK")
        self.assertEqual(self.owed(c, ALICE), 10 * GEN)

    def test_refused_while_a_claim_is_in_flight(self):
        c = self.make()
        self.make_will(c)
        c.claiming["1"] = NOW
        out = self.call(c, ALICE, "cancel_will", 1)
        self.assertEqual(out["status"], "REJECTED")
        self.assertIn("in flight", out["reason"])

    def test_allowed_once_the_claim_has_stalled(self):
        c = self.make(stall_ttl_s=600)
        self.make_will(c)
        c.claiming["1"] = NOW
        set_now(NOW + 601)
        self.assertEqual(self.call(c, ALICE, "cancel_will", 1)["status"], "OK")


# ===========================================================================
# 10. claim_inactive - the consensus round
# ===========================================================================


class ClaimBase(Base):
    def overdue_will(self, c=None, **kw):
        c = c or self.make(**kw)
        wid = self.make_will(c, days=1, deposit=10 * GEN)
        set_now(NOW + 3 * DAY)
        return c, wid


class TestClaimGates(ClaimBase):
    def test_refused_before_the_threshold(self):
        c = self.make()
        self.make_will(c, days=1)
        serve_empty()
        set_now(NOW + DAY)
        out = self.call(c, FINDER, "claim_inactive", 1)
        self.assertEqual(out["status"], "REJECTED")
        self.assertIn("not overdue", out["reason"])

    def test_exactly_at_the_deadline_is_not_overdue(self):
        c = self.make()
        self.make_will(c, days=1)
        serve_empty()
        set_now(NOW + 2 * DAY)
        self.assertEqual(self.call(c, FINDER, "claim_inactive", 1)["status"],
                         "REJECTED")

    def test_one_second_past_the_deadline_is_claimable(self):
        c = self.make()
        self.make_will(c, days=1)
        serve_empty()
        set_now(NOW + 2 * DAY + 1)
        self.assertEqual(self.call(c, FINDER, "claim_inactive", 1)["status"],
                         "OK")

    def test_unknown_will(self):
        c = self.make()
        self.assertEqual(self.call(c, FINDER, "claim_inactive", 99)["status"],
                         "REJECTED")

    def test_refused_while_another_claim_is_in_flight(self):
        c, wid = self.overdue_will()
        c.claiming["1"] = NOW + 3 * DAY
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["status"], "REJECTED")
        self.assertIn("already in flight", out["reason"])

    def test_not_gated_on_paused(self):
        """RULE 6. An owner who could suspend claims could strand a
        beneficiary indefinitely."""
        c, wid = self.overdue_will()
        self.call(c, OWNER, "set_paused", True)
        serve_empty()
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["outcome"], C.A_INACTIVE)

    def test_permissionless(self):
        c, wid = self.overdue_will()
        serve_empty()
        out = self.call(c, STRANGER, "claim_inactive", wid)
        self.assertEqual(out["status"], "OK")

    def test_a_heartbeat_resets_the_claim_window(self):
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 3 * DAY)
        self.call(c, ALICE, "heartbeat", 1)
        serve_empty()
        self.assertEqual(self.call(c, FINDER, "claim_inactive", 1)["status"],
                         "REJECTED")


class TestClaimOutcomes(ClaimBase):
    def test_inactive_releases_the_estate(self):
        c, wid = self.overdue_will()
        serve_empty()
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["outcome"], C.A_INACTIVE)
        self.assertTrue(out["released"])
        self.assertEqual(self.owed(c, BOB), 95 * GEN // 10)
        self.assertEqual(self.owed(c, FINDER), 5 * GEN // 10)
        self.ledger(c)

    def test_the_split_conserves(self):
        c, wid = self.overdue_will()
        serve_empty()
        self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(self.owed(c, BOB) + self.owed(c, FINDER), 10 * GEN)
        self.assertEqual(int(c.locked_wei), 0)
        self.ledger(c)

    def test_alive_changes_nothing(self):
        c, wid = self.overdue_will()
        serve_txs([tx(NOW + 2 * DAY, ALICE)])
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["outcome"], C.A_ALIVE)
        self.assertFalse(out["released"])
        self.assertEqual(int(c.locked_wei), 10 * GEN)
        self.assertEqual(self.owed(c, BOB), 0)
        self.assertEqual(self.owed(c, FINDER), 0)
        self.ledger(c)

    def test_alive_keeps_the_will_active(self):
        c, wid = self.overdue_will()
        serve_txs([tx(NOW + 2 * DAY, ALICE)])
        self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(str(c.wills[0].status), C.W_ACTIVE)

    def test_inconclusive_changes_nothing(self):
        c, wid = self.overdue_will()
        serve_down(503)
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["outcome"], C.A_INCONCLUSIVE)
        self.assertFalse(out["released"])
        self.assertEqual(int(c.locked_wei), 10 * GEN)
        self.ledger(c)

    def test_refused_query_does_not_pay_out(self):
        """The `result: null` trap, end to end. An explorer that refuses the
        query must not empty a living person's estate."""
        c, wid = self.overdue_will()
        serve_refused()
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["outcome"], C.A_INCONCLUSIVE)
        self.assertEqual(int(c.locked_wei), 10 * GEN)

    def test_inbound_only_still_releases(self):
        """RULE 9, end to end. Dust sent to a dead wallet is not a heartbeat."""
        c, wid = self.overdue_will()
        serve_txs([tx(NOW + 2 * DAY, STRANGER, to=ALICE) for _ in range(9)])
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["outcome"], C.A_INACTIVE)
        self.assertTrue(out["released"])

    def test_a_single_signature_saves_the_estate(self):
        c, wid = self.overdue_will()
        serve_txs([tx(NOW + 2 * DAY, ALICE)])
        self.assertEqual(self.call(c, FINDER, "claim_inactive", wid)[
            "outcome"], C.A_ALIVE)

    def test_an_old_signature_does_not_save_it(self):
        c, wid = self.overdue_will()
        serve_txs([tx(NOW - 30 * DAY, ALICE)])
        self.assertEqual(self.call(c, FINDER, "claim_inactive", wid)[
            "outcome"], C.A_INACTIVE)

    def test_evidence_is_stored(self):
        c, wid = self.overdue_will()
        serve_empty()
        self.call(c, FINDER, "claim_inactive", wid)
        will = c.wills[0]
        self.assertEqual(str(will.last_verdict), C.A_INACTIVE)
        self.assertEqual(len(str(will.last_content_hash)), 16)
        self.assertTrue(bool(will.last_src_ok))
        self.assertEqual(int(will.last_anchor_ts), NOW)
        self.assertEqual(int(will.last_window), C.TX_WINDOW)

    def test_claim_attempts_count(self):
        c, wid = self.overdue_will()
        serve_down(500)
        self.call(c, FINDER, "claim_inactive", wid)
        self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(int(c.wills[0].claim_attempts), 2)

    def test_the_marker_is_cleared_on_every_outcome(self):
        for setup in (serve_empty, serve_down, serve_refused,
                      lambda: serve_txs([tx(NOW + 2 * DAY, ALICE)])):
            WEB.reset()
            c, wid = self.overdue_will()
            setup()
            self.call(c, FINDER, "claim_inactive", wid)
            self.assertEqual(int(c.claiming.get("1") or 0), 0)

    def test_inconclusive_can_be_retried_into_a_release(self):
        c, wid = self.overdue_will()
        serve_down(500)
        self.assertEqual(self.call(c, FINDER, "claim_inactive", wid)[
            "outcome"], C.A_INCONCLUSIVE)
        WEB.reset()
        serve_empty()
        self.assertEqual(self.call(c, FINDER, "claim_inactive", wid)[
            "outcome"], C.A_INACTIVE)
        self.ledger(c)

    def test_caller_becomes_the_finder(self):
        c, wid = self.overdue_will()
        serve_empty()
        self.call(c, STRANGER, "claim_inactive", wid)
        self.assertEqual(c.wills[0].finder, STRANGER)

    def test_beneficiary_may_claim_and_takes_everything(self):
        """A beneficiary who does the work earns the finder fee too, which
        lands back with them. Both credits go to one address and the total is
        still exactly the deposit."""
        c, wid = self.overdue_will()
        serve_empty()
        self.call(c, BOB, "claim_inactive", wid)
        self.assertEqual(self.owed(c, BOB), 10 * GEN)
        self.ledger(c)

    def test_owner_may_claim_against_themselves(self):
        """Nothing stops it, and nothing should: the money still goes to the
        beneficiary. It is only possible at all if the wallet really is
        dormant, which an owner calling from it cannot arrange."""
        c, wid = self.overdue_will()
        serve_empty()
        out = self.call(c, ALICE, "claim_inactive", wid)
        self.assertEqual(out["outcome"], C.A_INACTIVE)
        self.assertEqual(self.owed(c, BOB), 95 * GEN // 10)

    def test_cannot_claim_an_executed_will(self):
        c, wid = self.overdue_will()
        serve_empty()
        self.call(c, FINDER, "claim_inactive", wid)
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["status"], "REJECTED")

    def test_verdict_counters(self):
        c, wid = self.overdue_will()
        serve_down(500)
        self.call(c, FINDER, "claim_inactive", wid)
        WEB.reset()
        serve_txs([tx(NOW + 2 * DAY, ALICE)])
        self.call(c, FINDER, "claim_inactive", wid)
        stats = self.view(c, "get_stats")
        self.assertEqual(stats["verdicts"]["inconclusive"], 1)
        self.assertEqual(stats["verdicts"]["alive"], 1)


class TestConsensusBinding(ClaimBase):
    def test_disagreement_settles_nothing(self):
        """The leader sees a dormant wallet, the validator sees a live one.
        Nothing may move."""
        c, wid = self.overdue_will()
        WEB.script((200, NO_TX_BODY),
                   (200, txlist([tx(NOW + 2 * DAY, ALICE)])))
        WEB.serve(200, NO_TX_BODY)
        WEB.script((200, NO_TX_BODY),
                   (200, txlist([tx(NOW + 2 * DAY, ALICE)])))
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["status"], "REJECTED")
        self.assertEqual(int(c.locked_wei), 10 * GEN)
        self.assertEqual(self.owed(c, BOB), 0)
        self.ledger(c)

    def test_a_forged_inactive_verdict_is_refused(self):
        """The leader reports a dormant wallet while the explorer is down. The
        validator refuses it on `_coherent` alone, without fetching."""
        c, wid = self.overdue_will()
        serve_down(500)
        FORGE["payload"] = {
            "activity_status": C.A_INACTIVE, "age_bucket": 7,
            "count_bucket": 0, "src_ok": False,
            "content_hash": "0" * 16}
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["status"], "REJECTED")
        self.assertEqual(int(c.locked_wei), 10 * GEN)

    def test_a_forged_bucket_is_refused(self):
        c, wid = self.overdue_will()
        serve_empty()
        good = C._seal(wid, "ethereum", str(ALICE), NOW, NOW + 3 * DAY,
                       C.TX_WINDOW,
                       {"activity_status": C.A_INACTIVE, "age_bucket": 7,
                        "count_bucket": 0, "src_ok": True})
        forged = dict(good)
        forged["age_bucket"] = 3
        FORGE["payload"] = forged
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["status"], "REJECTED")
        self.assertEqual(int(c.locked_wei), 10 * GEN)

    def test_a_forged_hash_is_refused(self):
        c, wid = self.overdue_will()
        serve_empty()
        forged = {"activity_status": C.A_INACTIVE, "age_bucket": 7,
                  "count_bucket": 0, "src_ok": True,
                  "content_hash": "deadbeefdeadbeef"}
        FORGE["payload"] = forged
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["status"], "REJECTED")

    def test_a_replayed_vector_from_another_will_is_refused(self):
        """An honest reading of will 2 presented as a reading of will 1. The
        hash binds the question, so it does not fit."""
        c = self.make()
        self.make_will(c, owner=ALICE, beneficiary=BOB, days=1)
        self.make_will(c, owner=CAROL, beneficiary=DAVE, days=1)
        set_now(NOW + 3 * DAY)
        serve_empty()
        stolen = C._seal(2, "ethereum", str(CAROL), NOW, NOW + 3 * DAY,
                         C.TX_WINDOW,
                         {"activity_status": C.A_INACTIVE, "age_bucket": 7,
                          "count_bucket": 0, "src_ok": True})
        FORGE["payload"] = stolen
        out = self.call(c, FINDER, "claim_inactive", 1)
        self.assertEqual(out["status"], "REJECTED")
        self.assertEqual(self.owed(c, BOB), 0)

    def test_an_alive_verdict_with_no_signatures_is_refused(self):
        c, wid = self.overdue_will()
        serve_empty()
        FORGE["payload"] = {
            "activity_status": C.A_ALIVE, "age_bucket": 7, "count_bucket": 0,
            "src_ok": True, "content_hash": "0" * 16}
        self.assertEqual(self.call(c, FINDER, "claim_inactive", wid)["status"],
                         "REJECTED")

    def test_a_round_that_never_settles_changes_nothing(self):
        """On chain a round the validators do not decide goes UNDETERMINED and
        applies no state at all. The contract must survive being handed
        nothing back, and must not store a verdict it never got."""
        c, wid = self.overdue_will()
        serve_empty()
        FORGE["leader_dies"] = True
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["status"], "REJECTED")
        self.assertEqual(int(c.locked_wei), 10 * GEN)
        self.assertEqual(self.owed(c, BOB), 0)
        self.assertEqual(str(c.wills[0].last_verdict), "")
        self.ledger(c)

    def test_a_dead_explorer_settles_nothing(self):
        """`_http` swallows every transport failure and answers (0, ""), so a
        fetch that dies becomes INCONCLUSIVE rather than an exception. That is
        rule 8, and this is the end-to-end proof of it."""
        c, wid = self.overdue_will()
        WEB.serve(200, NO_TX_BODY)
        WEB.fail(2)
        out = self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(out["outcome"], C.A_INCONCLUSIVE)
        self.assertEqual(int(c.locked_wei), 10 * GEN)
        self.ledger(c)

    def test_the_marker_is_cleared_after_an_unsettled_round(self):
        """Otherwise one failed round would brick the will until the stall TTL
        expired."""
        c, wid = self.overdue_will()
        serve_empty()
        FORGE["leader_dies"] = True
        self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(int(c.claiming.get("1") or 0), 0)

    def test_stored_hash_re_derives(self):
        c, wid = self.overdue_will()
        serve_empty()
        self.call(c, FINDER, "claim_inactive", wid)
        report = self.view(c, "verify_claim", wid)
        self.assertTrue(report["verified"], report)

    def test_the_validator_really_re_fetches(self):
        """Two fetches per round - the leader's and the validator's. A
        validator that trusted the leader would make only one."""
        c, wid = self.overdue_will()
        serve_empty()
        WEB.calls = 0
        self.call(c, FINDER, "claim_inactive", wid)
        self.assertEqual(WEB.calls, 2)

    def test_the_validator_agrees_on_an_honest_round(self):
        c, wid = self.overdue_will()
        serve_empty()
        self.call(c, FINDER, "claim_inactive", wid)
        self.assertTrue(LAST_CONSENSUS.get("agreed"))


# ===========================================================================
# 11. settle_stalled
# ===========================================================================


class TestSettleStalled(Base):
    def stuck(self, ttl=600):
        c = self.make(stall_ttl_s=ttl)
        self.make_will(c, days=1)
        c.claiming["1"] = NOW
        return c

    def test_refused_before_the_ttl(self):
        c = self.stuck()
        set_now(NOW + 100)
        out = self.call(c, STRANGER, "settle_stalled", 1)
        self.assertEqual(out["status"], "REJECTED")

    def test_clears_after_the_ttl(self):
        c = self.stuck()
        set_now(NOW + 601)
        out = self.call(c, STRANGER, "settle_stalled", 1)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(int(c.claiming.get("1") or 0), 0)

    def test_is_permissionless(self):
        c = self.stuck()
        set_now(NOW + 601)
        self.assertEqual(self.call(c, STRANGER, "settle_stalled", 1)["status"],
                         "OK")

    def test_works_while_paused(self):
        """RULE 6, and the most important instance of it. An owner who could
        keep a will frozen by declining to unstick it could strand a
        beneficiary - which needs no validators at all, and is therefore worse
        than forging a verdict."""
        c = self.stuck()
        self.call(c, OWNER, "set_paused", True)
        set_now(NOW + 601)
        self.assertEqual(self.call(c, STRANGER, "settle_stalled", 1)["status"],
                         "OK")

    def test_the_will_stays_active(self):
        c = self.stuck()
        set_now(NOW + 601)
        out = self.call(c, STRANGER, "settle_stalled", 1)
        self.assertEqual(out["will_status"], C.W_ACTIVE)
        self.assertEqual(str(c.wills[0].status), C.W_ACTIVE)

    def test_no_money_moves(self):
        """No money moved when the claim opened - the deposit is never staged
        into an intermediate bucket - so recovery is a deletion, not an
        unwind."""
        c = self.stuck()
        before = (int(c.locked_wei), int(c.payable_wei), int(c.balance_wei))
        set_now(NOW + 601)
        self.call(c, STRANGER, "settle_stalled", 1)
        self.assertEqual(
            (int(c.locked_wei), int(c.payable_wei), int(c.balance_wei)),
            before)
        self.ledger(c)

    def test_refused_with_no_claim_in_flight(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(self.call(c, STRANGER, "settle_stalled", 1)["status"],
                         "REJECTED")

    def test_unknown_will(self):
        c = self.make()
        self.assertEqual(
            self.call(c, STRANGER, "settle_stalled", 99)["status"], "REJECTED")

    def test_a_claim_can_follow_it(self):
        c = self.stuck()
        set_now(NOW + 601)
        self.call(c, STRANGER, "settle_stalled", 1)
        serve_empty()
        set_now(NOW + 3 * DAY)
        self.assertEqual(self.call(c, FINDER, "claim_inactive", 1)["outcome"],
                         C.A_INACTIVE)


# ===========================================================================
# 12. claim_payout and the deposit lifecycle
# ===========================================================================


class TestPayout(Base):
    def test_pays_what_is_owed(self):
        c = self.make()
        self.make_will(c, deposit=10 * GEN)
        self.call(c, ALICE, "cancel_will", 1)
        out = self.call(c, ALICE, "claim_payout")
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["amount_wei"], str(10 * GEN))
        self.assertEqual(BALANCES.get(str(ALICE)), 10 * GEN)
        self.ledger(c)

    def test_posts_exactly_one_transfer(self):
        c = self.make()
        self.make_will(c)
        self.call(c, ALICE, "cancel_will", 1)
        TRANSFERS.clear()
        self.call(c, ALICE, "claim_payout")
        self.assertEqual(len(TRANSFERS), 1)
        self.assertEqual(TRANSFERS[0], (str(ALICE), 10 * GEN))

    def test_nothing_owed(self):
        c = self.make()
        self.assertEqual(self.call(c, STRANGER, "claim_payout")["status"],
                         "REJECTED")

    def test_cannot_be_claimed_twice(self):
        c = self.make()
        self.make_will(c)
        self.call(c, ALICE, "cancel_will", 1)
        self.call(c, ALICE, "claim_payout")
        self.assertEqual(self.call(c, ALICE, "claim_payout")["status"],
                         "REJECTED")

    def test_works_while_paused(self):
        c = self.make()
        self.make_will(c)
        self.call(c, ALICE, "cancel_will", 1)
        self.call(c, OWNER, "set_paused", True)
        self.assertEqual(self.call(c, ALICE, "claim_payout")["status"], "OK")

    def test_balance_drops_by_the_amount(self):
        c = self.make()
        self.make_will(c, deposit=10 * GEN)
        self.call(c, ALICE, "cancel_will", 1)
        self.call(c, ALICE, "claim_payout")
        self.assertEqual(int(c.balance_wei), 0)
        self.ledger(c)

    def test_payout_of_view(self):
        c = self.make()
        self.make_will(c)
        self.call(c, ALICE, "cancel_will", 1)
        out = self.view(c, "payout_of", str(ALICE))
        self.assertEqual(out["owed_wei"], str(10 * GEN))

    def test_payout_of_bad_address(self):
        c = self.make()
        self.assertIn("error", self.view(c, "payout_of", "nope"))


class TestDepositLifecycle(Base):
    """RULE 7, exhaustively: every wei that enters must be able to leave."""

    def test_cancel_returns_everything(self):
        c = self.make()
        self.make_will(c, deposit=7 * GEN)
        self.call(c, ALICE, "top_up", 1, value=3 * GEN)
        self.call(c, ALICE, "cancel_will", 1)
        self.call(c, ALICE, "claim_payout")
        self.assertEqual(BALANCES.get(str(ALICE)), 10 * GEN)
        self.assertEqual(int(c.balance_wei), 0)

    def test_execution_returns_everything(self):
        c = self.make()
        self.make_will(c, deposit=10 * GEN, days=1)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        self.call(c, BOB, "claim_payout")
        self.call(c, FINDER, "claim_payout")
        self.assertEqual(BALANCES.get(str(BOB), 0)
                         + BALANCES.get(str(FINDER), 0), 10 * GEN)
        self.assertEqual(int(c.balance_wei), 0)

    def test_a_refused_deposit_is_returnable(self):
        c = self.make()
        self.call(c, ALICE, "create_will", str(ALICE), 1, "ethereum",
                  value=5 * GEN)
        self.call(c, ALICE, "claim_payout")
        self.assertEqual(BALANCES.get(str(ALICE)), 5 * GEN)
        self.assertEqual(int(c.balance_wei), 0)

    def test_value_sent_to_a_non_payable_method_is_returnable(self):
        c = self.make()
        self.call(c, STRANGER, "set_paused", True, value=2 * GEN)
        self.call(c, STRANGER, "claim_payout")
        self.assertEqual(BALANCES.get(str(STRANGER)), 2 * GEN)
        self.assertEqual(int(c.balance_wei), 0)

    def test_a_refusal_credits_exactly_once(self):
        """THE DOUBLE-CREDIT BUG, in the shape that caught it. A stranger
        sending 1 GEN to a call that refuses them must come away owed 1, not
        2."""
        c = self.make()
        self.call(c, STRANGER, "set_paused", True, value=GEN)
        self.assertEqual(self.owed(c, STRANGER), GEN)
        self.ledger(c)

    def test_many_refusals_credit_once_each(self):
        c = self.make()
        for _ in range(5):
            self.call(c, STRANGER, "set_paused", True, value=GEN)
        self.assertEqual(self.owed(c, STRANGER), 5 * GEN)
        self.ledger(c)

    def test_the_contract_never_keeps_anything(self):
        """There is no protocol revenue and no owner withdraw method. After
        everyone has claimed, the books are empty."""
        c = self.make()
        self.make_will(c, owner=ALICE, beneficiary=BOB, days=1,
                       deposit=10 * GEN)
        self.make_will(c, owner=CAROL, beneficiary=DAVE, days=1,
                       deposit=3 * GEN)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        self.call(c, CAROL, "cancel_will", 2)
        for who in (BOB, FINDER, CAROL):
            self.call(c, who, "claim_payout")
        self.assertEqual(int(c.balance_wei), 0)
        self.assertEqual(int(c.locked_wei), 0)
        self.assertEqual(int(c.payable_wei), 0)

    def test_stats_report_no_revenue(self):
        c = self.make()
        self.assertEqual(self.view(c, "get_stats")["protocol_revenue_wei"],
                         "0")


class TestLedgerIdentity(Base):
    """RULE 7's identity, asserted after EVERY operation in a long mixed run."""

    def test_identity_holds_through_a_full_lifecycle(self):
        c = self.make()
        steps = [
            (ALICE, "create_will", (str(BOB), 1, "ethereum"), 10 * GEN),
            (ALICE, "top_up", (1,), 2 * GEN),
            (ALICE, "heartbeat", (1,), 0),
            (STRANGER, "heartbeat", (1,), GEN),
            (ALICE, "change_beneficiary", (1, str(CAROL)), 0),
            (CAROL, "create_will", (str(DAVE), 1, "base"), 4 * GEN),
            (STRANGER, "set_paused", (True,), GEN),
            (OWNER, "set_paused", (False,), 0),
            (ALICE, "cancel_will", (1,), 0),
            (ALICE, "claim_payout", (), 0),
            (STRANGER, "claim_payout", (), 0),
            (CAROL, "cancel_will", (2,), 0),
            (CAROL, "claim_payout", (), 0),
        ]
        for who, fn, args, value in steps:
            self.call(c, who, fn, *args, value=value)
            self.ledger(c)
        self.assertEqual(int(c.balance_wei), 0)

    def test_identity_holds_through_a_claim(self):
        c = self.make()
        self.make_will(c, days=1, deposit=10 * GEN)
        self.ledger(c)
        set_now(NOW + 3 * DAY)
        for setup in (serve_down, serve_refused, serve_empty):
            WEB.reset()
            setup()
            self.call(c, FINDER, "claim_inactive", 1)
            self.ledger(c)
        self.call(c, BOB, "claim_payout")
        self.ledger(c)
        self.call(c, FINDER, "claim_payout")
        self.ledger(c)
        self.assertEqual(int(c.balance_wei), 0)

    def test_stats_publish_the_identity(self):
        c = self.make()
        self.make_will(c)
        self.assertTrue(self.view(c, "get_stats")["ledger"]["identity_holds"])


# ===========================================================================
# 13. rule 3 - no counter moves before a refusal
# ===========================================================================


class TestNoCounterMovesBeforeARefusal(Base):
    def refusals(self, c):
        return [
            (ALICE, "create_will", (str(ALICE), 1, "ethereum"), GEN),
            (ALICE, "create_will", (str(BOB), 0, "ethereum"), GEN),
            (ALICE, "create_will", (str(BOB), 1, "dogecoin"), GEN),
            (ALICE, "create_will", (str(BOB), 1, "ethereum"), 1),
            (ALICE, "create_will", ("bad", 1, "ethereum"), GEN),
            (STRANGER, "heartbeat", (1,), 0),
            (STRANGER, "top_up", (1,), GEN),
            (STRANGER, "cancel_will", (1,), 0),
            (STRANGER, "change_beneficiary", (1, str(DAVE)), 0),
            (FINDER, "claim_inactive", (1,), 0),
            (STRANGER, "settle_stalled", (1,), 0),
            (NOBODY, "claim_payout", (), 0),
            (STRANGER, "set_paused", (True,), GEN),
            (STRANGER, "transfer_ownership", (str(DAVE),), 0),
            (ALICE, "heartbeat", (99,), 0),
        ]

    def test_no_counter_moves(self):
        """Every counter but `total_rejected` is snapshotted and must be
        untouched by fifteen kinds of refusal. `total_rejected` is the one
        exception because it is a statistic ABOUT refusals."""
        c = self.make()
        self.make_will(c, owner=ALICE, beneficiary=BOB, days=1)
        before = self.counters(c)
        for who, fn, args, value in self.refusals(c):
            self.call(c, who, fn, *args, value=value)
        self.assertEqual(self.counters(c), before)

    def test_rejected_counter_does_move(self):
        c = self.make()
        self.make_will(c)
        before = int(c.total_rejected)
        self.call(c, STRANGER, "heartbeat", 1)
        self.assertEqual(int(c.total_rejected), before + 1)

    def test_ledger_survives_every_refusal(self):
        c = self.make()
        self.make_will(c, owner=ALICE, beneficiary=BOB, days=1)
        for who, fn, args, value in self.refusals(c):
            self.call(c, who, fn, *args, value=value)
            self.ledger(c)

    def test_no_will_is_created_by_a_refusal(self):
        c = self.make()
        for who, fn, args, value in self.refusals(c):
            self.call(c, who, fn, *args, value=value)
        self.assertEqual(len(c.wills), 0)

    def test_a_refused_claim_does_not_count_an_attempt(self):
        c = self.make()
        self.make_will(c, days=1)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        self.assertEqual(int(c.total_claims_attempted), 0)
        self.assertEqual(int(c.wills[0].claim_attempts), 0)

    def test_every_refusal_returns_the_rejected_shape(self):
        c = self.make()
        self.make_will(c, owner=ALICE, beneficiary=BOB, days=1)
        for who, fn, args, value in self.refusals(c):
            out = self.call(c, who, fn, *args, value=value)
            self.assertEqual(out["status"], "REJECTED", fn)
            self.assertIn("reason", out)
            self.assertIn("refunded_wei", out)
            self.assertEqual(out["refunded_wei"], str(value))


# ===========================================================================
# 14. rule 5 - a terminal will is frozen
# ===========================================================================


class TestFrozenAfterTerminal(Base):
    def snapshot(self, will):
        return {f: str(getattr(will, f)) for f in
                Will_FIELDS}

    def fire_everything(self, c, wid):
        self.call(c, ALICE, "heartbeat", wid)
        self.call(c, ALICE, "top_up", wid, value=GEN)
        self.call(c, ALICE, "cancel_will", wid)
        self.call(c, ALICE, "change_beneficiary", wid, str(DAVE))
        self.call(c, FINDER, "claim_inactive", wid)
        self.call(c, STRANGER, "settle_stalled", wid)
        self.call(c, OWNER, "set_paused", True)
        self.call(c, OWNER, "set_paused", False)

    def test_cancelled_will_is_frozen(self):
        c = self.make()
        self.make_will(c, days=1)
        self.call(c, ALICE, "cancel_will", 1)
        before = self.snapshot(c.wills[0])
        set_now(NOW + 10 * DAY)
        serve_empty()
        self.fire_everything(c, 1)
        self.assertEqual(self.snapshot(c.wills[0]), before)
        self.ledger(c)

    def test_executed_will_is_frozen(self):
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        before = self.snapshot(c.wills[0])
        set_now(NOW + 10 * DAY)
        self.fire_everything(c, 1)
        self.assertEqual(self.snapshot(c.wills[0]), before)
        self.ledger(c)

    def test_a_frozen_will_cannot_be_paid_twice(self):
        c = self.make()
        self.make_will(c, days=1, deposit=10 * GEN)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        owed = self.owed(c, BOB)
        self.call(c, FINDER, "claim_inactive", 1)
        self.assertEqual(self.owed(c, BOB), owed)
        self.ledger(c)

    def test_status_counts_are_consistent(self):
        c = self.make()
        self.make_will(c, owner=ALICE, days=1)
        self.make_will(c, owner=CAROL, days=1)
        self.call(c, ALICE, "cancel_will", 1)
        self.assertEqual(int(c.status_counts.get(C.W_ACTIVE) or 0), 1)
        self.assertEqual(int(c.status_counts.get(C.W_CANCELLED) or 0), 1)


# The Will struct's fields, read off the contract itself so a field added later
# is covered by the freeze tests without anybody remembering to list it.
Will_FIELDS = tuple(MOD.Will.__annotations__.keys())


# ===========================================================================
# 15. views
# ===========================================================================


class TestViews(Base):
    def test_get_will_found(self):
        c = self.make()
        self.make_will(c)
        out = self.view(c, "get_will", 1)
        self.assertTrue(out["found"])
        self.assertEqual(out["will"]["will_id"], 1)

    def test_get_will_missing(self):
        c = self.make()
        self.assertFalse(self.view(c, "get_will", 99)["found"])

    def test_get_will_junk_id(self):
        c = self.make()
        for bad in ("x", None, -1, 0, True):
            self.assertFalse(self.view(c, "get_will", bad)["found"])

    def test_get_will_reports_the_split(self):
        c = self.make()
        self.make_will(c, deposit=10 * GEN)
        out = self.view(c, "get_will", 1)["will"]
        self.assertEqual(out["if_executed"]["to_finder_wei"], str(5 * GEN // 10))
        self.assertEqual(out["if_executed"]["to_beneficiary_wei"],
                         str(95 * GEN // 10))

    def test_get_will_timeline(self):
        c = self.make()
        self.make_will(c)
        events = [e["event"] for e in self.view(c, "get_will", 1)["will"][
            "timeline"]]
        self.assertIn("CREATED", events)
        self.assertIn("CLAIMABLE_AT", events)

    def test_get_claimable_wills_empty(self):
        c = self.make()
        self.make_will(c, days=1)
        self.assertEqual(self.view(c, "get_claimable_wills")["count"], 0)

    def test_get_claimable_wills_lists_the_overdue(self):
        c = self.make()
        self.make_will(c, owner=ALICE, days=1)
        self.make_will(c, owner=CAROL, days=100)
        set_now(NOW + 3 * DAY)
        out = self.view(c, "get_claimable_wills")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["wills"][0]["will_id"], 1)

    def test_claimable_lists_the_finder_fee(self):
        c = self.make()
        self.make_will(c, days=1, deposit=10 * GEN)
        set_now(NOW + 3 * DAY)
        out = self.view(c, "get_claimable_wills")
        self.assertEqual(out["wills"][0]["finder_fee_wei"], str(5 * GEN // 10))

    def test_claimable_drops_executed_wills(self):
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        self.assertEqual(self.view(c, "get_claimable_wills")["count"], 0)

    def test_get_wills_paging(self):
        c = self.make()
        for who in (ALICE, BOB, CAROL, DAVE):
            self.make_will(c, owner=who, beneficiary=FINDER, days=1)
        out = self.view(c, "get_wills", 1, 2)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["total"], 4)
        self.assertEqual(out["wills"][0]["will_id"], 2)

    def test_get_wills_clamps(self):
        c = self.make()
        self.make_will(c)
        self.assertEqual(self.view(c, "get_wills", 999, 999)["count"], 0)
        self.assertLessEqual(self.view(c, "get_wills", 0, 999)["count"],
                             C.MAX_PAGE)

    def test_by_owner_bad_address(self):
        c = self.make()
        self.assertIn("error", self.view(c, "get_wills_by_owner", "nope"))

    def test_by_owner_unknown_address(self):
        c = self.make()
        self.assertEqual(self.view(c, "get_wills_by_owner",
                                   str(STRANGER))["count"], 0)

    def test_get_config_lists_the_compared_fields(self):
        c = self.make()
        cfg = self.view(c, "get_config")
        self.assertEqual(cfg["consensus"]["compared_fields"],
                         ["activity_status", "age_bucket", "count_bucket",
                          "src_ok", "content_hash"])

    def test_get_config_declares_no_model(self):
        c = self.make()
        self.assertFalse(self.view(c, "get_config")["consensus"][
            "uses_language_model"])

    def test_get_config_lists_the_immutables(self):
        c = self.make()
        cfg = self.view(c, "get_config")
        for field in ("min_interval_s", "max_interval_s", "interval_unit_s",
                      "missed_threshold", "finder_fee_bps", "stall_ttl_s"):
            self.assertIn(field, cfg["immutable"])

    def test_get_config_names_what_pause_does_not_touch(self):
        c = self.make()
        cfg = self.view(c, "get_config")
        for name in ("heartbeat", "claim_inactive", "cancel_will",
                     "settle_stalled", "claim_payout"):
            self.assertIn(name, cfg["unaffected_by_pause"])

    def test_get_config_publishes_the_ladders(self):
        c = self.make()
        cfg = self.view(c, "get_config")
        self.assertEqual(cfg["consensus"]["age_ladder_days"],
                         list(C.AGE_LADDER))
        self.assertEqual(cfg["consensus"]["count_ladder"],
                         list(C.COUNT_LADDER))

    def test_get_config_declares_signed_only(self):
        c = self.make()
        self.assertTrue(self.view(c, "get_config")["consensus"]["signed_only"])

    def test_real_bounds_are_the_brief_s(self):
        c = self.real()
        cfg = self.view(c, "get_config")
        self.assertEqual(cfg["interval_unit"], "days")
        self.assertEqual(cfg["min_interval_units"], 7)
        self.assertEqual(cfg["max_interval_units"], 365)

    def test_preview_will(self):
        c = self.real()
        out = self.view(c, "preview_will", 30, str(10 * GEN))
        self.assertTrue(out["acceptable"])
        self.assertEqual(out["to_finder_wei"], str(5 * GEN // 10))

    def test_preview_rejects_a_short_interval(self):
        c = self.real()
        out = self.view(c, "preview_will", 1, str(10 * GEN))
        self.assertFalse(out["acceptable"])
        self.assertFalse(out["interval_ok"])

    def test_preview_rejects_a_small_deposit(self):
        c = self.real()
        out = self.view(c, "preview_will", 30, "1")
        self.assertFalse(out["deposit_ok"])

    def test_verify_claim_before_any_claim(self):
        c = self.make()
        self.make_will(c)
        out = self.view(c, "verify_claim", 1)
        self.assertFalse(out["checked"])

    def test_verify_claim_unknown_will(self):
        c = self.make()
        self.assertFalse(self.view(c, "verify_claim", 99)["found"])

    def test_verify_claim_after_a_release(self):
        c = self.make()
        self.make_will(c, days=1, deposit=10 * GEN)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        out = self.view(c, "verify_claim", 1)
        self.assertTrue(out["verified"])
        self.assertTrue(out["released"])
        self.assertEqual(len(out["checks"]), 5)

    def test_verify_claim_after_an_alive_verdict(self):
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 3 * DAY)
        serve_txs([tx(NOW + 2 * DAY, ALICE)])
        self.call(c, FINDER, "claim_inactive", 1)
        out = self.view(c, "verify_claim", 1)
        self.assertTrue(out["verified"])
        self.assertFalse(out["released"])

    def test_verify_claim_detects_a_tampered_hash(self):
        """If a stored hash did not follow from the stored vector, this is what
        would say so."""
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        c.wills[0].last_content_hash = "deadbeefdeadbeef"
        self.assertFalse(self.view(c, "verify_claim", 1)["verified"])

    def test_verify_claim_detects_a_tampered_bucket(self):
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        c.wills[0].last_age_bucket = 2
        self.assertFalse(self.view(c, "verify_claim", 1)["verified"])

    def test_verify_claim_detects_a_tampered_reason(self):
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        c.wills[0].last_reason = "the owner is definitely dead"
        self.assertFalse(self.view(c, "verify_claim", 1)["verified"])

    def test_verify_claim_publishes_the_projection(self):
        c = self.make()
        self.make_will(c, days=1)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        out = self.view(c, "verify_claim", 1)
        self.assertIn("wallet=" + str(ALICE), out["canonical_projection"])

    def test_stats_shape(self):
        c = self.make()
        self.make_will(c)
        stats = self.view(c, "get_stats")
        for key in ("total_wills", "ledger", "verdicts", "totals",
                    "on_chain_balance_wei", "undelivered_wei"):
            self.assertIn(key, stats)

    def test_every_view_returns_json(self):
        c = self.make()
        self.make_will(c, days=1)
        for fn, args in [("get_will", (1,)), ("get_wills_by_owner",
                                              (str(ALICE),)),
                         ("get_wills_by_beneficiary", (str(BOB),)),
                         ("get_claimable_wills", ()), ("get_stats", ()),
                         ("get_config", ()), ("verify_claim", (1,)),
                         ("payout_of", (str(ALICE),)),
                         ("get_wills", (0, 10)),
                         ("preview_will", (30, str(GEN)))]:
            text = getattr(c, fn)(*args)
            self.assertIsInstance(text, str, fn)
            json.loads(text)


# ===========================================================================
# 16. ownership and pause
# ===========================================================================


class TestOwnership(Base):
    def test_deployer_is_owner(self):
        c = self.make()
        self.assertEqual(c.owner, OWNER)

    def test_only_owner_pauses(self):
        c = self.make()
        self.assertEqual(self.call(c, STRANGER, "set_paused", True)["status"],
                         "REJECTED")
        self.assertFalse(bool(c.paused))

    def test_owner_pauses(self):
        c = self.make()
        self.assertEqual(self.call(c, OWNER, "set_paused", True)["status"],
                         "OK")
        self.assertTrue(bool(c.paused))

    def test_unpause(self):
        c = self.make()
        self.call(c, OWNER, "set_paused", True)
        self.call(c, OWNER, "set_paused", False)
        self.assertFalse(bool(c.paused))

    def test_transfer_ownership(self):
        c = self.make()
        self.assertEqual(
            self.call(c, OWNER, "transfer_ownership", str(DAVE))["status"],
            "OK")
        self.assertEqual(c.owner, DAVE)

    def test_only_owner_transfers(self):
        c = self.make()
        self.assertEqual(
            self.call(c, STRANGER, "transfer_ownership", str(DAVE))["status"],
            "REJECTED")

    def test_cannot_transfer_to_zero(self):
        c = self.make()
        self.assertEqual(
            self.call(c, OWNER, "transfer_ownership", C.ZERO_ADDRESS)[
                "status"], "REJECTED")

    def test_cannot_transfer_to_junk(self):
        c = self.make()
        self.assertEqual(
            self.call(c, OWNER, "transfer_ownership", "nope")["status"],
            "REJECTED")

    def test_owner_cannot_touch_a_will(self):
        """The whole of what an owner can do is pause new wills and hand the
        switch on. There is no method by which they reach a deposit."""
        c = self.make()
        self.make_will(c, owner=ALICE, deposit=10 * GEN)
        for fn, args in [("heartbeat", (1,)), ("cancel_will", (1,)),
                         ("change_beneficiary", (1, str(DAVE))),
                         ("top_up", (1,))]:
            self.call(c, OWNER, fn, *args)
        self.assertEqual(int(c.wills[0].deposit_wei), 10 * GEN)
        self.assertEqual(c.wills[0].beneficiary, BOB)
        self.assertEqual(self.owed(c, OWNER), 0)
        self.ledger(c)

    def test_owner_earns_nothing_from_a_release(self):
        c = self.make()
        self.make_will(c, days=1, deposit=10 * GEN)
        set_now(NOW + 3 * DAY)
        serve_empty()
        self.call(c, FINDER, "claim_inactive", 1)
        self.assertEqual(self.owed(c, OWNER), 0)


class TestConstructor(Base):
    def test_defaults_are_the_brief_s(self):
        c = self.real()
        self.assertEqual(int(c.min_interval_s), 7 * DAY)
        self.assertEqual(int(c.max_interval_s), 365 * DAY)
        self.assertEqual(int(c.missed_threshold), 2)
        self.assertEqual(int(c.finder_fee_bps), 500)

    def test_arguments_are_clamped_not_rejected(self):
        """A deploy that fails on a mistyped constructor argument wastes a
        whole deploy, and the ceiling is the real rule either way."""
        c = MOD.WillExecutor(finder_fee_bps=99999, missed_threshold=999)
        self.assertEqual(int(c.finder_fee_bps), C.MAX_FINDER_FEE_BPS)
        self.assertEqual(int(c.missed_threshold), C.MAX_MISSED_THRESHOLD)

    def test_junk_arguments_fall_back(self):
        c = MOD.WillExecutor(finder_fee_bps="nope", missed_threshold=None)
        self.assertEqual(int(c.finder_fee_bps), C.DEFAULT_FINDER_FEE_BPS)
        self.assertEqual(int(c.missed_threshold),
                         C.DEFAULT_MISSED_THRESHOLD)

    def test_inverted_bounds_are_repaired(self):
        c = MOD.WillExecutor(min_interval_s=100 * DAY, max_interval_s=DAY)
        self.assertGreaterEqual(int(c.max_interval_s), int(c.min_interval_s))

    def test_the_default_unit_is_a_day(self):
        c = self.real()
        self.assertEqual(int(c.interval_unit_s), 86400)

    def test_a_second_scale_instance_shortens_the_clock(self):
        """The same source, deployed with the clock in seconds, so the release
        path can be WATCHED rather than only asserted."""
        c = MOD.WillExecutor(min_interval_s=60, max_interval_s=365 * DAY,
                             interval_unit_s=1)
        out = self.call(c, ALICE, "create_will", str(BOB), 90, "ethereum",
                        value=GEN)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["check_in_interval_s"], 90)
        self.assertEqual(out["claimable_at"], NOW + 180)

    def test_the_unit_cannot_exceed_a_day(self):
        """A unit longer than a day would let a deploy quietly restate what
        `check_in_days` means in the direction that DELAYS a claim."""
        c = MOD.WillExecutor(interval_unit_s=999999)
        self.assertEqual(int(c.interval_unit_s), 86400)

    def test_the_unit_cannot_be_zero(self):
        c = MOD.WillExecutor(interval_unit_s=0)
        self.assertEqual(int(c.interval_unit_s), 1)

    def test_config_names_the_unit_in_words(self):
        c = MOD.WillExecutor(min_interval_s=60, interval_unit_s=1)
        self.assertEqual(self.view(c, "get_config")["interval_unit"],
                         "seconds")

    def test_a_second_scale_instance_still_conserves(self):
        c = MOD.WillExecutor(min_interval_s=60, interval_unit_s=1)
        self.call(c, ALICE, "create_will", str(BOB), 60, "ethereum",
                  value=10 * GEN)
        set_now(NOW + 200)
        serve_empty()
        out = self.call(c, FINDER, "claim_inactive", 1)
        self.assertEqual(out["outcome"], C.A_INACTIVE)
        self.assertEqual(self.owed(c, BOB) + self.owed(c, FINDER), 10 * GEN)
        self.ledger(c)

    def test_ids_start_at_one(self):
        self.assertEqual(int(self.make().next_id), 1)

    def test_books_start_empty(self):
        c = self.make()
        self.assertEqual(int(c.balance_wei), 0)
        self.ledger(c)


# ===========================================================================
# 17. AST invariants - walked as syntax, never grepped
# ===========================================================================


class TestSourceInvariants(Base):
    """An audit that greps its own documentation cries wolf: this file's header
    mentions `str.replace()` in order to warn about it. Every check below walks
    the parse tree."""

    def writes(self):
        out = []
        for node in ast.walk(TREE):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                text = ast.unparse(dec)
                if text.startswith("gl.public.write"):
                    out.append(node)
        return out

    def views(self):
        out = []
        for node in ast.walk(TREE):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if ast.unparse(dec) == "gl.public.view":
                        out.append(node)
        return out

    def test_there_are_no_raise_statements(self):
        """RULE 2. Not one, anywhere - not in the payable methods, not in the
        owner methods, not in a private helper a write calls. A revert rolls
        back storage but not the value that came with the call."""
        found = [n.lineno for n in ast.walk(TREE) if isinstance(n, ast.Raise)]
        self.assertEqual(found, [], "raise at lines " + str(found))

    def test_no_str_replace_call(self):
        """`str.replace()` is rejected by the runner. Checked as an attribute
        call, so the header comment warning about it does not trip the test."""
        bad = []
        for node in ast.walk(TREE):
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                         ast.Attribute):
                if node.func.attr == "replace":
                    bad.append(node.lineno)
        self.assertEqual(bad, [])

    def test_the_header_is_exactly_two_comment_lines(self):
        """GenVM parses the contiguous leading `#` block as the runner header.
        A stray comment between line 1 and the imports makes the contract
        undeployable, reporting only `invalid_contract`. Lint does not catch
        it."""
        lines = SRC_TEXT.split("\n")
        self.assertEqual(lines[0], "# v0.3.0")
        self.assertTrue(lines[1].startswith('# { "Depends": "py-genlayer:'))
        self.assertFalse(lines[2].startswith("#"))
        self.assertTrue(lines[2].startswith("import genlayer"))

    def test_the_runner_hash_is_pinned(self):
        self.assertIn(
            "py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng",
            SRC_TEXT.split("\n")[1])

    def test_every_public_write_banks_first(self):
        """RULE 2 and RULE 7 together: value that arrives must be booked to its
        sender before anything else can refuse."""
        for node in self.writes():
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value,
                                                          ast.Constant):
                first = node.body[1]
            text = ast.unparse(first)
            self.assertIn("self._bank()", text,
                          node.name + " does not bank first: " + text[:80])

    def test_immutable_fields_are_never_assigned_outside_the_constructor(self):
        """RULE 4. There is no setter for any of these, and this is what keeps
        it that way when somebody adds one by accident."""
        immutable = {"min_interval_s", "max_interval_s", "interval_unit_s",
                     "missed_threshold", "finder_fee_bps", "stall_ttl_s"}
        bad = []
        for node in ast.walk(TREE):
            if not isinstance(node, ast.FunctionDef) or node.name == "__init__":
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, (ast.Assign, ast.AugAssign)):
                    continue
                targets = sub.targets if isinstance(sub, ast.Assign) else [
                    sub.target]
                for target in targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                            and target.attr in immutable):
                        bad.append((node.name, target.attr, sub.lineno))
        self.assertEqual(bad, [])

    def test_pause_gates_only_creation_and_top_up(self):
        """RULE 6, as syntax. A future edit that put `self.paused` into a
        withdrawal path would make an owner able to strand a deposit, and this
        is the check that refuses to let it land."""
        allowed = {"create_will", "top_up", "set_paused", "get_config",
                   "get_stats", "__init__"}
        for node in ast.walk(TREE):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in allowed:
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Attribute) and sub.attr == "paused"
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "self"):
                    self.fail(node.name + " reads self.paused at line "
                              + str(sub.lineno))

    def test_only_claim_payout_moves_money_out(self):
        """`_pay` is the one door, and exactly one method reaches it."""
        callers = []
        for node in ast.walk(TREE):
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func,
                                                             ast.Name)
                        and sub.func.id == "_pay"):
                    callers.append(node.name)
        self.assertEqual(sorted(set(callers)), ["claim_payout"])

    def test_emit_transfer_is_called_exactly_once(self):
        """The silent-`emit()` bug: `Proxy.emit()` returns a method GETTER, so
        `x.emit(value=...)` posts no message at all and every payout looks
        perfect while no wei moves. `emit_transfer` is the spelling that
        works."""
        calls = [n for n in ast.walk(TREE)
                 if isinstance(n, ast.Call) and isinstance(n.func,
                                                           ast.Attribute)
                 and n.func.attr == "emit_transfer"]
        self.assertEqual(len(calls), 1)

    def test_there_is_no_bare_emit_call(self):
        bad = [n.lineno for n in ast.walk(TREE)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "emit"]
        self.assertEqual(bad, [])

    def test_there_is_no_owner_withdraw_method(self):
        """RULE 7. The contract keeps nothing, so there is nothing to
        withdraw - and no method that could be repurposed into doing it."""
        names = {n.name for n in self.writes()}
        for banned in ("withdraw", "sweep", "collect_fees", "rescue",
                       "emergency_withdraw", "drain"):
            self.assertNotIn(banned, names)

    def test_no_float_literals(self):
        """A float anywhere near money puts a platform's rounding mode on the
        consensus axis, and a float in a nondet return is not calldata
        encodable."""
        bad = [n.lineno for n in ast.walk(TREE)
               if isinstance(n, ast.Constant) and isinstance(n.value, float)]
        self.assertEqual(bad, [])

    def test_no_wall_clock_read(self):
        """There is no block.timestamp on this chain, and a per-node wall-clock
        read would make one deadline expire at a different instant on every
        validator."""
        for name in ("time.time", "datetime.now", "datetime.utcnow",
                     "time.monotonic"):
            self.assertNotIn(name, SRC_TEXT)

    def test_the_only_clock_is_the_message(self):
        found = False
        for node in ast.walk(TREE):
            if isinstance(node, ast.FunctionDef) and node.name == "_now":
                self.assertIn("gl.message.raw", ast.unparse(node))
                found = True
        self.assertTrue(found)

    def test_no_randomness(self):
        for name in ("random.", "uuid", "os.urandom", "secrets."):
            self.assertNotIn(name, SRC_TEXT)

    def test_no_undefined_names(self):
        """A name error inside a `@gl.public.view` only fires when that view is
        called on chain - after a deploy, after a wait, on a network. A parser
        catches it in a millisecond."""
        problems = undefined_names(SOURCE)
        self.assertEqual(problems, [], str(problems))

    def test_every_brief_method_exists(self):
        names = {n.name for n in self.writes()} | {n.name for n in
                                                   self.views()}
        for required in ("create_will", "heartbeat", "top_up",
                         "claim_inactive", "cancel_will",
                         "change_beneficiary", "settle_stalled", "get_will",
                         "get_wills_by_owner", "get_wills_by_beneficiary",
                         "get_claimable_wills", "get_stats", "get_config",
                         "verify_claim"):
            self.assertIn(required, names, required + " is missing")

    def test_payable_methods_are_exactly_the_funding_ones(self):
        payable = set()
        for node in ast.walk(TREE):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if ast.unparse(dec) == "gl.public.write.payable":
                        payable.add(node.name)
        self.assertEqual(payable, {"create_will", "top_up"})

    def test_run_nondet_is_called_once(self):
        """One consensus round, in one place. A second would be a second axis
        nobody enumerated."""
        calls = [n for n in ast.walk(TREE)
                 if isinstance(n, ast.Call)
                 and ast.unparse(n.func).endswith("run_nondet")]
        self.assertEqual(len(calls), 1)

    def test_no_model_call_anywhere(self):
        self.assertNotIn("exec_prompt", SRC_TEXT)

    def test_no_caller_supplied_url(self):
        """RULE 10. `_url` is the only place a URL is built and `_http` is the
        only place one is fetched, and neither takes calldata."""
        builders = [n for n in ast.walk(TREE)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value.startswith("https://")]
        for node in builders:
            self.assertEqual(node.value, "https://")

    def test_storage_writes_of_evidence_live_in_one_place(self):
        """RULE 1's corollary. Every `last_*` field is written by `_record` and
        nowhere else, so there is no path by which an unc ompared value reaches
        storage."""
        evidence = [f for f in Will_FIELDS if f.startswith("last_")]
        writers = {}
        for node in ast.walk(TREE):
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Assign):
                    continue
                for target in sub.targets:
                    if (isinstance(target, ast.Attribute)
                            and target.attr in evidence):
                        writers.setdefault(target.attr, set()).add(node.name)
        for field in evidence:
            if field == "last_heartbeat":
                continue
            self.assertLessEqual(
                writers.get(field, set()) - {"create_will"}, {"_record"},
                field + " is written by " + str(writers.get(field)))

    def test_array_valued_maps_are_never_indexed(self):
        """THE BUG THIS TEST EXISTS FOR shipped to chain once. Indexing a
        TreeMap with a key it does not hold raises KeyError on the runner, so
        `self.by_owner[sender].append(...)` reverted inside `create_will` — on
        the one path that had already banked a deposit — while 431 offline
        tests passed, because the stub auto-created the entry.

        `get_or_insert_default` is the spelling that inserts. This walks the
        AST and fails on any subscript of an array-valued map."""
        maps = {"by_owner", "by_beneficiary"}
        bad = []
        for node in ast.walk(TREE):
            if not isinstance(node, ast.Subscript):
                continue
            target = node.value
            if (isinstance(target, ast.Attribute) and target.attr in maps
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                bad.append((target.attr, node.lineno))
        self.assertEqual(bad, [], "indexed instead of get_or_insert_default: "
                         + str(bad))

    def test_the_stub_raises_on_a_missing_key_like_the_runner(self):
        """A stub more forgiving than the runner certifies bugs. This asserts
        the harness itself has the runner's semantics."""
        m = _TreeMap[str, _DynArray]()
        with self.assertRaises(KeyError):
            m["nothing here"]
        self.assertEqual(len(m.get_or_insert_default("now there")), 0)

    def test_scalar_maps_answer_zero_not_none(self):
        """The other half of the same trap, in the other direction: a map with
        a SCALAR value type answers a missing key with that type's zero, so a
        presence check written `is not None` matches everything."""
        m = _TreeMap[str, int]()
        self.assertEqual(m.get("absent"), 0)
        self.assertIsNotNone(m.get("absent"))

    def test_every_owner_method_that_mutates_a_will_is_gated_on_an_in_flight_claim(self):
        """`top_up` shipped without this gate while its two siblings had it.
        Enumerate rather than remember: any owner-only method that writes to a
        will must consult `_claim_open`, or a live round can have the thing it
        is deciding about move underneath it.

        `heartbeat` is the deliberate exception and is named as one — it moves
        the anchor for the NEXT round on purpose, and NOTES.md §8 explains why
        it must not clear a marker either."""
        EXPECTED = {"top_up", "cancel_will", "change_beneficiary"}
        gated = set()
        for node in ast.walk(TREE):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not any(ast.unparse(d).startswith("gl.public.write")
                       for d in node.decorator_list):
                continue
            src = ast.unparse(node)
            if "_claim_open" in src and node.name != "claim_inactive" \
                    and node.name != "settle_stalled":
                gated.add(node.name)
        self.assertEqual(gated, EXPECTED,
                         "owner methods gated on an in-flight claim: "
                         + str(sorted(gated)))

    def test_contract_is_under_the_size_ceiling(self):
        """Studio Dev has taken 121 KB (measured). A previous project measured
        a far tighter ceiling on a different network - 53,000 bytes deploys,
        53,700 does not - so the number is recorded rather than guarded
        against."""
        self.assertLess(len(SRC_TEXT.encode("utf8")), 200_000)


class TestStructIsFullyBound(Base):
    """RULE 1, enumerated. Every field of the Will struct must be either a
    consensus-compared value, a pure function of one, or a value that was on
    chain before the round opened."""

    PRE_ROUND = {
        "will_id", "owner", "beneficiary", "chain", "deposit_wei",
        "check_in_interval_s", "last_heartbeat", "created_at", "status",
        "heartbeat_count", "top_up_count", "beneficiary_changes",
        "claim_attempts", "last_claim_at", "last_anchor_ts", "last_now_ts",
        "last_window", "executed_at", "finder", "finder_fee_bps",
        "paid_beneficiary_wei", "paid_finder_wei", "refunded_wei",
    }
    FROM_VECTOR = {
        "last_verdict", "last_age_bucket", "last_count_bucket", "last_src_ok",
        "last_content_hash",
    }
    DERIVED = {"last_reason"}

    def test_every_field_is_accounted_for(self):
        covered = self.PRE_ROUND | self.FROM_VECTOR | self.DERIVED
        self.assertEqual(set(Will_FIELDS), covered,
                         "unaccounted: " + str(set(Will_FIELDS) ^ covered))

    def test_the_compared_axis_covers_the_vector_fields(self):
        compared = {"activity_status", "age_bucket", "count_bucket", "src_ok",
                    "content_hash"}
        self.assertEqual(len(self.FROM_VECTOR), len(compared))

    def test_the_derived_field_is_a_pure_function(self):
        self.assertEqual(
            C._reason({"activity_status": C.A_INACTIVE, "age_bucket": 7,
                       "count_bucket": 0, "src_ok": True}, "ethereum", 10),
            C._reason({"activity_status": C.A_INACTIVE, "age_bucket": 7,
                       "count_bucket": 0, "src_ok": True}, "ethereum", 10))


class TestNoWriteRaises(Base):
    """RULE 2, exercised rather than parsed: every public write is fired with
    junk arguments and must RETURN rather than raise."""

    JUNK = (None, "", "x", -1, 0, True, [], {}, 10 ** 30,
            "0x" + "z" * 40, 3.5)

    def test_no_write_raises_on_junk(self):
        for bad in self.JUNK:
            c = self.make()
            self.make_will(c, owner=ALICE, beneficiary=BOB, days=1)
            serve_empty()
            for fn, arity in [("create_will", 3), ("heartbeat", 1),
                              ("top_up", 1), ("claim_inactive", 1),
                              ("cancel_will", 1), ("change_beneficiary", 2),
                              ("settle_stalled", 1), ("set_paused", 1),
                              ("transfer_ownership", 1)]:
                args = [bad] * arity
                try:
                    out = self.call(c, STRANGER, fn, *args, value=GEN)
                except Exception as e:
                    self.fail(fn + " raised on " + repr(bad) + ": " + repr(e))
                self.assertIsInstance(out, dict, fn)

    def test_claim_payout_never_raises(self):
        c = self.make()
        for _ in range(3):
            self.assertIsInstance(self.call(c, STRANGER, "claim_payout"), dict)

    def test_views_never_raise_on_junk(self):
        c = self.make()
        self.make_will(c, days=1)
        for bad in self.JUNK:
            for fn, arity in [("get_will", 1), ("get_wills_by_owner", 1),
                              ("get_wills_by_beneficiary", 1),
                              ("verify_claim", 1), ("payout_of", 1),
                              ("get_wills", 2), ("preview_will", 2)]:
                try:
                    getattr(c, fn)(*([bad] * arity))
                except Exception as e:
                    self.fail(fn + " raised on " + repr(bad) + ": " + repr(e))

    def test_every_junk_call_leaves_the_ledger_intact(self):
        c = self.make()
        self.make_will(c, owner=ALICE, beneficiary=BOB, days=1)
        serve_empty()
        for bad in self.JUNK:
            self.call(c, STRANGER, "heartbeat", bad, value=GEN)
            self.ledger(c)


if __name__ == "__main__":
    unittest.main(verbosity=1, buffer=False)
