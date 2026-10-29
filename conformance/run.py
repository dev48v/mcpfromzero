#!/usr/bin/env python3
"""The conformance runner.

One spec, two implementations, and this is the thing that stops them drifting.

Every case file is language-neutral JSON: a list of steps, each a message to
send and an expectation about what comes back. The runner launches an
implementation as a subprocess, talks newline-delimited JSON-RPC over stdio,
and checks the replies. Python and TypeScript run the *same* files.

    python conformance/run.py --impl python
    python conformance/run.py --impl typescript
    python conformance/run.py --impl both --level 0

Why a separate runner rather than each language's own tests: a test written in
the same language as the thing it tests tends to inherit its author's reading
of the spec. Two implementations passing one external suite is the only way to
find out that the reading itself was wrong.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CASES = os.path.join(HERE, "cases")

IMPLS = {
    "python": {
        "cmd": [sys.executable, "-u", "-m", "mcpfromzero.serve"],
        "cwd": os.path.join(ROOT, "python"),
    },
    "typescript": {
        "cmd": ["node", os.path.join("dist", "serve.js")],
        "cwd": os.path.join(ROOT, "typescript"),
    },
}


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #

class Mismatch(Exception):
    pass


def match(expected, actual, path="$"):
    """Structural match. The expectation is a SUBSET of the reply, except for
    the directives below, so a case file never has to spell out every field a
    server is allowed to add.

        "<any>"              any value, as long as the key is present
        "<absent>"           the key must NOT be present
        {"<type>": "string"} the value must be of that JSON type
        {"<oneof>": [...]}   the value must equal one of these
        {"<contains>": "x"}  a string that contains x, or a list containing x
    """
    if isinstance(expected, str) and expected == "<any>":
        return
    if isinstance(expected, dict) and len(expected) == 1:
        key = next(iter(expected))
        if key == "<type>":
            want = expected[key]
            kinds = {
                "string": str, "number": (int, float), "boolean": bool,
                "object": dict, "array": list, "null": type(None),
            }
            if want not in kinds:
                raise Mismatch(f"{path}: unknown <type> {want!r}")
            # bool is a subclass of int in Python; keep them distinct
            if want == "number" and isinstance(actual, bool):
                raise Mismatch(f"{path}: expected number, got boolean")
            if not isinstance(actual, kinds[want]):
                raise Mismatch(f"{path}: expected {want}, got {type(actual).__name__}")
            return
        if key == "<oneof>":
            if actual not in expected[key]:
                raise Mismatch(f"{path}: {actual!r} is not one of {expected[key]!r}")
            return
        if key == "<contains>":
            needle = expected[key]
            if isinstance(actual, str) and needle in actual:
                return
            if isinstance(actual, list) and needle in actual:
                return
            raise Mismatch(f"{path}: {actual!r} does not contain {needle!r}")

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise Mismatch(f"{path}: expected an object, got {type(actual).__name__}")
        for k, v in expected.items():
            if v == "<absent>":
                if k in actual:
                    raise Mismatch(f"{path}.{k}: must be absent, but it is present")
                continue
            if k not in actual:
                raise Mismatch(f"{path}.{k}: missing")
            match(v, actual[k], f"{path}.{k}")
        return

    if isinstance(expected, list):
        if not isinstance(actual, list):
            raise Mismatch(f"{path}: expected an array, got {type(actual).__name__}")
        if len(expected) != len(actual):
            raise Mismatch(f"{path}: expected {len(expected)} items, got {len(actual)}")
        for i, (e, a) in enumerate(zip(expected, actual)):
            match(e, a, f"{path}[{i}]")
        return

    if expected != actual:
        raise Mismatch(f"{path}: expected {expected!r}, got {actual!r}")


# --------------------------------------------------------------------------- #
# the transport
# --------------------------------------------------------------------------- #

class Server:
    """An implementation under test, spoken to over stdio.

    MCP's stdio transport is NEWLINE-DELIMITED JSON. It is not LSP, and there
    are no Content-Length headers - a confusion common enough that the spec
    says so explicitly: messages are delimited by newlines and MUST NOT contain
    embedded newlines.
    """

    def __init__(self, impl: str, timeout: float = 10.0):
        spec = IMPLS[impl]
        self.impl = impl
        self.timeout = timeout
        self.proc = subprocess.Popen(
            spec["cmd"], cwd=spec["cwd"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )

    def send(self, message) -> None:
        """A case may send a dict, or a raw string prefixed "<raw>" so that
        deliberately unparseable input can be tested."""
        if isinstance(message, str) and message.startswith("<raw>"):
            self.proc.stdin.write(message[len("<raw>"):] + "\n")
            self.proc.stdin.flush()
            return
        line = json.dumps(message, separators=(",", ":"))
        if "\n" in line:
            raise AssertionError("a framed message must not contain a newline")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def recv(self) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line == "":
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise AssertionError(f"the server closed stdout. stderr:\n{err.strip()}")
            line = line.strip()
            if not line:
                continue
            return json.loads(line)
        raise AssertionError(f"no reply within {self.timeout}s")

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    def stderr(self) -> str:
        try:
            return self.proc.stderr.read() or ""
        except Exception:
            return ""


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #

def load_cases(level: int | None) -> list:
    out = []
    for name in sorted(os.listdir(CASES)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(CASES, name), "r", encoding="utf-8") as fh:
            case = json.load(fh)
        case["file"] = name
        if level is not None and case.get("level") != level:
            continue
        out.append(case)
    return out


def run_case(impl: str, case: dict, verbose: bool = False) -> tuple[int, int, list]:
    """Each case gets a FRESH server process.

    The protocol is stateless as of 2026-07-28, so a case that only passes
    because a previous case warmed the server up is a case that is lying.
    """
    passed = failed = 0
    failures = []
    server = Server(impl)
    try:
        for i, step in enumerate(case["steps"]):
            label = f"{case['file']}#{i} {step.get('name', '')}".strip()
            try:
                if "send" in step:
                    server.send(step["send"])
                if "expect" in step:
                    reply = server.recv()
                    match(step["expect"], reply)
                    if verbose:
                        print(f"      ok  {label}")
                elif "expectNoReply" in step:
                    # Proving a negative over a pipe needs a positive to measure
                    # against: send a probe with a distinctive id and require the
                    # NEXT line back to belong to the probe. If the server wrongly
                    # answered the notification, that reply arrives first and the
                    # ids do not match. Works on every platform, unlike polling a
                    # pipe for emptiness.
                    probe_id = "__probe_%d" % i
                    server.send({"jsonrpc": "2.0", "id": probe_id,
                                 "method": "server/discover", "params": {}})
                    reply = server.recv()
                    if reply.get("id") != probe_id:
                        raise Mismatch(
                            "the notification was answered - expected the probe %r "
                            "back first, got id %r" % (probe_id, reply.get("id")))
                    if verbose:
                        print("      ok  %s (silence confirmed by probe)" % label)
                passed += 1
            except (Mismatch, AssertionError, json.JSONDecodeError) as e:
                failed += 1
                failures.append(f"{label}: {e}")
                break  # a broken step invalidates everything after it in this case
    finally:
        server.close()
    return passed, failed, failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--impl", default="both", choices=["python", "typescript", "both"])
    ap.add_argument("--level", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    impls = ["python", "typescript"] if args.impl == "both" else [args.impl]
    cases = load_cases(args.level)
    if not cases:
        print("no cases matched")
        return 1

    overall_failed = 0
    for impl in impls:
        cwd = IMPLS[impl]["cwd"]
        if not os.path.isdir(cwd):
            print(f"{impl}: not present at {cwd}, skipping")
            continue
        if impl == "typescript" and not os.path.exists(os.path.join(cwd, "dist", "serve.js")):
            print(f"{impl}: dist/serve.js missing - run `npm run build` first")
            overall_failed += 1
            continue

        print(f"\n=== {impl} ===")
        tp = tf = 0
        for case in cases:
            p, f, failures = run_case(impl, case, args.verbose)
            tp += p
            tf += f
            status = "ok  " if f == 0 else "FAIL"
            print(f"  {status} L{case['level']}  {case['file']:<34} {case['title']}")
            for msg in failures:
                print(f"        {msg}")
        print(f"  -> {tp} steps passed, {tf} failed")
        overall_failed += tf

    print(f"\n{'CONFORMANT' if overall_failed == 0 else 'NOT CONFORMANT'}"
          f" - {overall_failed} failing steps")
    return 1 if overall_failed else 0


if __name__ == "__main__":
    sys.exit(main())
