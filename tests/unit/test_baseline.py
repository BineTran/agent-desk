import subprocess
from pathlib import Path

from agent_desk.baseline import BaselineRunner, doctor
from agent_desk.config.schema import Check


def _repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    (r / "a.ts").write_text("ok\n"); (r / "b.ts").write_text("bad\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=r, check=True)
    return r, subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True).stdout.strip()


LINT = Check(command="for f in {changed}; do grep -q bad $f && echo \"$f:1:1 lint/x/bad\"; done | grep . && exit 1 || exit 0", paths=["**/*.ts"])


async def test_baseline_runs_on_base_in_its_own_worktree_and_caches(tmp_path):
    r, head = _repo(tmp_path)
    b = BaselineRunner(r, head, tmp_path / "s" / "baseline", tmp_path / "cache")
    full = Check(command="grep -l bad *.ts && exit 1 || exit 0")
    res = await b.get("unit", full, None)
    assert res["passed"] is False and res["keys"]
    (r / "b.ts").write_text("fixed\n")                                    # the user's checkout changes: baseline is pinned to the commit
    again = await BaselineRunner(r, head, tmp_path / "s2" / "baseline", tmp_path / "cache").get("unit", full, None)
    assert again == res                                                   # cache hit across sessions
    scoped = await b.get("lint", LINT, ["a.ts", "new.ts"])                # new.ts does not exist on base -> only a.ts runs
    assert scoped["passed"] is True and "new.ts" not in scoped["command"]
    await b.close()
    assert not (tmp_path / "s" / "baseline").exists()


async def test_doctor_flags_broken_and_rewriting_checks(tmp_path):
    r, head = _repo(tmp_path)
    rows = await doctor(BaselineRunner(r, head, tmp_path / "d", tmp_path / "cache"), {
        "ok": Check(command="true"),
        "broken": Check(command="echo 'No files were processed'; exit 1"),
        "writer": Check(command="echo x >> a.ts"),
        "lint": LINT})
    st = {x["name"]: x for x in rows}
    assert st["ok"]["state"] == "ok" and st["broken"]["state"] == "fails on clean checkout"
    assert "No files were processed" in st["broken"]["detail"] and st["writer"]["notes"]
    assert st["lint"]["state"] == "works (existing failures)" and "b.ts" in st["lint"]["command"]   # runs, finds old debt
