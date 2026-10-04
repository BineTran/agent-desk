import subprocess
from pathlib import Path

from agent_desk.config.schema import Check
from agent_desk.verifier import classify, expand, failure_keys, matching, run_check

JEST = """PASS src/ok.spec.ts
FAIL src/modules/payment/a.spec.ts
  ● Test suite failed to run
FAIL src/pull.spec.ts (10.13 s)
  ● PullSyncEngine › per-puller timeout › fires and marks module failed
Tests: 3 failed, 10 passed"""
PYTEST = "FAILED tests/test_a.py::test_one - assert 1 == 2\nFAILED tests/test_b.py::test_two\n"
BIOME = "src/a.ts:12:5 lint/style/noVar  FIXABLE  ━━━━━\n  × Use let or const instead of var.\n"
TSC = "src/b.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"


def test_failure_keys_per_tool():
    assert failure_keys(JEST) == ["src/modules/payment/a.spec.ts", "Test suite failed to run", "src/pull.spec.ts",
                                  "PullSyncEngine › per-puller timeout › fires and marks module failed"]
    assert failure_keys(PYTEST) == ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
    assert failure_keys(BIOME)[0] == "src/a.ts:12:lint/style/noVar"
    assert failure_keys(TSC) == ["src/b.ts:3:TS2322"]


def test_paths_and_changed_placeholder():
    c = Check(command="yarn biome lint {changed}", paths=["**/*.ts"])
    assert expand(c, ["docs/x.md"]) == (None, "no matching changes (**/*.ts)")
    cmd, _ = expand(c, ["docs/x.md", "src/a b.ts", "a.ts"])
    assert cmd == "yarn biome lint 'src/a b.ts' a.ts"
    assert matching(["a.ts", "src/x/y.ts", "y.tsx"], ["src/**/*.ts"]) == ["src/x/y.ts"]
    assert expand(Check(command="yarn build", paths=["src/**"]), ["src/a.ts"]) == ("yarn build", "")


def _r(keys, tail=("AssertionError",)):
    from agent_desk.verifier import CheckResult
    return CheckResult("unit", "c", True, False, 1, 0, list(tail), "fp", keys=list(keys), output="\n".join(keys))


def test_classify_pre_existing_regression_unknown_and_env():
    assert classify(_r(["a", "b"]), {"passed": False, "keys": ["a", "b", "c"]}).status == "pre-existing"
    r = classify(_r(["a", "d"]), {"passed": False, "keys": ["a"]})
    assert r.status == "regression" and r.new_failures == ["d"] and r.blocking
    assert classify(_r(["a"]), {"passed": True, "keys": []}).status == "regression"
    assert classify(_r(["a"]), None).status == "regression"
    env = classify(_r(["x"], tail=["/bin/sh: biome: command not found"]), {"passed": False, "keys": ["x"]})
    assert env.status == "environment" and env.blocking                     # never "pre-existing": nothing was verified


async def test_write_guard_reverts_what_a_check_rewrites(tmp_path):
    r = tmp_path / "r"; r.mkdir()
    (r / "a.ts").write_text("var x\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=r, check=True)
    (r / "b.ts").write_text("mine\n")                                       # the agent's own change: must survive
    res = await run_check("lint", Check(command="echo let x > a.ts; echo junk > new.txt; echo changed >> b.ts"), str(r))
    assert sorted(res.wrote) == ["a.ts", "b.ts", "new.txt"]
    assert (r / "a.ts").read_text() == "var x\n" and (r / "b.ts").read_text() == "mine\n" and not (r / "new.txt").exists()
    kept = await run_check("fmt", Check(command="echo let x > a.ts", writes=True), str(r))
    assert kept.wrote == [] and (r / "a.ts").read_text() == "let x\n"


async def test_skipped_check_does_not_run(tmp_path):
    res = await run_check("lint", Check(command="exit 1", paths=["**/*.ts"]), str(tmp_path), changed=["README.md"])
    assert res.status == "skipped" and res.passed and not res.blocking


def test_first_error_prefers_runner_summary():
    from agent_desk.verifier import first_error
    out = "[Nest] ERROR [OrgSync] PUT failed: undefined\nTest Suites: 9 failed, 300 passed, 309 total\nTests:       14 failed, 2000 passed\n"
    assert first_error(out) == "Test Suites: 9 failed, 300 passed, 309 total · Tests:       14 failed, 2000 passed"
    assert first_error("× No such file or directory (os error 2)\n") == "× No such file or directory (os error 2)"
