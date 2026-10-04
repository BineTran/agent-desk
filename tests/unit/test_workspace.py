import os
import subprocess
from pathlib import Path

import pytest

from agent_desk import gitws
from agent_desk.config.schema import WorkspaceCfg


def _repo(tmp_path, ignore="node_modules/\n"):
    r = tmp_path / "repo"; r.mkdir()
    (r / ".gitignore").write_text(ignore)
    (r / "package.json").write_text("{}")
    (r / "apps" / "web").mkdir(parents=True); (r / "apps" / "web" / "package.json").write_text("{}")
    for nm in (r / "node_modules" / ".bin", r / "apps" / "web" / "node_modules"):
        nm.mkdir(parents=True)
    tool = r / "node_modules" / ".bin" / "tool"
    tool.write_text("#!/bin/sh\necho tool-ok\n"); tool.chmod(0o755)
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=r, check=True)
    return r


async def test_link_deps_runs_tools_and_never_commits_links(tmp_path):
    r = _repo(tmp_path)
    ws = await gitws.create(r, tmp_path / "s", "s-1")
    assert not (ws.path / "node_modules").exists()
    made = await gitws.link_deps(ws, ["node_modules", ".venv"])
    assert set(made) == {"node_modules", "apps/web/node_modules"}
    assert subprocess.run(["node_modules/.bin/tool"], cwd=ws.path, capture_output=True, text=True).stdout.strip() == "tool-ok"
    assert await gitws.changed_files(ws) == []
    assert await gitws.commit_all(ws, "nothing") is None
    (ws.path / "a.ts").write_text("x\n")
    sha = await gitws.commit_all(ws, "add a")
    files = (await gitws.git(ws.path, "show", "--name-only", "--format=", sha)).split()
    assert files == ["a.ts"]
    ws2 = gitws.Workspace(ws.repo, ws.path, ws.branch, ws.base_commit)          # reopened session: links found on disk
    assert set(gitws.linked_paths(ws2)) == {"node_modules", "apps/web/node_modules"}
    assert "node_modules" not in await gitws.diff_since_base(ws2)


async def test_run_setup_reports_failure(tmp_path):
    r = _repo(tmp_path)
    ws = await gitws.create(r, tmp_path / "s", "s-2")
    ok, out = await gitws.run_setup(ws, "echo installing && exit 3")
    assert not ok and "installing" in out
    ok, _ = await gitws.run_setup(ws, "true")
    assert ok


async def test_links_already_ignored_by_gitignore_do_not_break_add(tmp_path):
    """Regression (s-8fb5): .gitignore 'node_modules' (no slash) ignores the symlink; an explicit exclude made git add fail."""
    r = _repo(tmp_path, "node_modules\n**/node_modules/\n")
    ws = await gitws.create(r, tmp_path / "s", "s-3")
    await gitws.link_deps(ws, ["node_modules"])
    (ws.path / "b.ts").write_text("y\n")
    sha = await gitws.commit_all(ws, "add b")
    assert (await gitws.git(ws.path, "show", "--name-only", "--format=", sha)).split() == ["b.ts"]
    assert "node_modules" not in await gitws.diff_since_base(ws)



def _husky_repo(tmp_path, hook_body):
    """husky v6: .husky/pre-commit sources .husky/_/husky.sh, which is gitignored (made by `husky install`)."""
    r = _repo(tmp_path)
    (r / ".husky" / "_").mkdir(parents=True)
    (r / ".husky" / ".gitignore").write_text("_\n")
    (r / ".husky" / "_" / "husky.sh").write_text("HUSKY_OK=1\n")
    hook = r / ".husky" / "pre-commit"
    hook.write_text('#!/bin/sh\n. "$(dirname "$0")/_/husky.sh"\n' + hook_body); hook.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "husky", "--no-verify"], cwd=r, check=True)
    subprocess.run(["git", "config", "core.hooksPath", ".husky"], cwd=r, check=True)
    return r


async def test_commit_runs_repo_hooks_with_linked_husky(tmp_path):
    r = _husky_repo(tmp_path, 'test "$HUSKY_OK" = 1 && touch "$(git rev-parse --show-toplevel)/../hook-ran"\n')
    ws = await gitws.create(r, tmp_path / "s", "s-h")
    (ws.path / "a.ts").write_text("x\n")
    with pytest.raises(gitws.GitError, match="husky.sh"):          # what s-69b1 hit: the worktree has no .husky/_
        await gitws.commit_all(ws, "add a")
    assert ".husky/_" in await gitws.link_deps(ws, WorkspaceCfg().link)
    sha = await gitws.commit_all(ws, "add a")
    assert sha and (ws.path.parent / "hook-ran").exists()            # the hook really ran
    files = (await gitws.git(ws.path, "show", "--name-only", "--format=", sha)).split()
    assert files == ["a.ts"]                                          # the link is never committed


async def test_failing_hook_raises(tmp_path):
    r = _husky_repo(tmp_path, 'echo "lint error in a.ts" >&2; exit 1\n')
    ws = await gitws.create(r, tmp_path / "s", "s-f")
    await gitws.link_deps(ws, WorkspaceCfg().link)
    (ws.path / "a.ts").write_text("x\n")
    with pytest.raises(gitws.GitError, match="lint error"):
        await gitws.commit_all(ws, "add a")


def test_missing_husky_runtime_is_an_environment_error():
    from agent_desk.decision.rules import environment_error
    assert environment_error([".husky/pre-commit: line 2: .husky/_/husky.sh: No such file or directory"])


def _with_remote(tmp_path):
    r = _repo(tmp_path)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=r, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD:main"], cwd=r, check=True)
    return r, bare


async def test_push_preview_new_branch_lists_only_unpushed_commits(tmp_path):
    r, bare = _with_remote(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "agent-desk/s1"], cwd=r, check=True)
    (r / "a.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "docs: add a"], cwd=r, check=True)
    pv = await gitws.push_preview(r, "/bin/zsh -lc 'git push origin agent-desk/s1:docs-x'")
    assert (pv["local"], pv["remote"], pv["target"], pv["url"], pv["new_branch"]) == ("agent-desk/s1", "origin", "docs-x", str(bare), True)
    assert len(pv["commits"]) == 1 and "docs: add a" in pv["commits"][0]          # the base commit is already on the remote


async def test_push_preview_plain_push_resolves_upstream_and_ignores_other_commands(tmp_path):
    r, _ = _with_remote(tmp_path)
    (r / "b.md").write_text("y\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "docs: b"], cwd=r, check=True)
    pv = await gitws.push_preview(r, "git push")
    assert (pv["remote"], pv["target"], pv["new_branch"]) == ("origin", "main", False) and len(pv["commits"]) == 1
    assert "unknown" in await gitws.push_preview(r, "git push --all origin")
    assert await gitws.push_preview(r, "git status") is None
    assert (await gitws.push_preview(r, "git fetch && git push origin main && echo ok"))["target"] == "main"


def test_missing_outputs_paths_globs_and_deleted(tmp_path):
    from agent_desk.orchestrator import missing_outputs
    (tmp_path / "docs").mkdir(); (tmp_path / "docs" / "a.md").write_text("x")
    changed = ["docs/a.md", "docs/gone.ts"]                                           # gone.ts: in the diff, but deleted
    assert missing_outputs(["docs/a.md", "docs/*.md", "docs/gone.ts", "docs/types.ts"], changed, tmp_path) == ["docs/gone.ts", "docs/types.ts"]


def test_legacy_plan_without_outputs_keeps_its_hash():
    import hashlib
    from agent_desk.contracts import Plan
    from agent_desk.studio import MATERIAL_TASK_FIELDS, plan_hash
    legacy = {"goal": "g", "constraints": [], "acceptance_criteria": ["AC"], "risks": [], "out_of_scope": [],
              "tasks": [{"id": "T1", "title": "t", "goal": "g", "kind": "implementation", "depends_on": [], "relevant_files": [],
                         "files_known": False, "requires_write": True, "acceptance_criteria": []}]}
    old = Plan.model_validate(legacy)
    stored = hashlib.sha256(old.model_dump_json(exclude={"tasks": {0: {"outputs"}}}).encode()).hexdigest()[:10]
    assert old.tasks[0].outputs == [] and plan_hash(old) == stored
    new = old.model_copy(deep=True); new.tasks[0].outputs = ["a.md"]
    assert plan_hash(new) != plan_hash(old) and "outputs" in MATERIAL_TASK_FIELDS
    assert "outputs" in Plan.model_json_schema()["$defs"]["TaskSpec"]["required"]     # Main must always fill it
