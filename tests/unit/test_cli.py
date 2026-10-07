import json

from typer.testing import CliRunner

from agent_desk.cli import app

R = CliRunner()


def test_init_detects_pnpm_scripts_and_never_overwrites(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"lint": "x", "test": "y", "typecheck": "z", "build": "b"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    r = R.invoke(app, ["init", "--repo", str(tmp_path)])
    assert r.exit_code == 0
    t = (tmp_path / ".agent-desk.yaml").read_text()
    assert 'unit: { command: "pnpm test", required: true }' in t and 'build: { command: "pnpm build", paths: [' in t and "approval:" in t
    assert 'typecheck: { command: "pnpm typecheck", paths: ["**/*.ts"' in t
    again = R.invoke(app, ["init", "--repo", str(tmp_path)])
    assert again.exit_code == 1 and (tmp_path / ".agent-desk.yaml").read_text() == t


def test_init_python_and_empty_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    R.invoke(app, ["init", "--repo", str(tmp_path)])
    assert "python3 -m pytest -q" in (tmp_path / ".agent-desk.yaml").read_text()
    e = tmp_path / "empty"; e.mkdir()
    r = R.invoke(app, ["init", "--repo", str(e)])
    assert "verification: {}" in (e / ".agent-desk.yaml").read_text() and "no checks detected" in r.output
    # the generated file must itself be valid config
    from agent_desk.config.loader import load
    from pathlib import Path
    assert load(e, global_path=Path("/nonexistent")).config.verification == {}


def test_ls_without_database(tmp_path):
    assert "no sessions yet" in R.invoke(app, ["ls", "--home", str(tmp_path)]).output


def test_init_scopes_biome_and_jest_to_changed_files(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"lint": "biome lint --write \"{src,test}/**/*.ts\"", "test": "jest"},
                                                       "devDependencies": {"@biomejs/biome": "2", "jest": "29", "typescript": "5"}}))
    (tmp_path / "yarn.lock").write_text("")
    R.invoke(app, ["init", "--repo", str(tmp_path)])
    t = (tmp_path / ".agent-desk.yaml").read_text()
    assert 'lint: { command: "yarn biome lint {changed}", paths: ["**/*.ts"' in t
    assert 'unit: { command: "yarn jest --findRelatedTests {changed} --passWithNoTests"' in t and "yarn install --frozen-lockfile" in t
    from agent_desk.config.loader import load
    from pathlib import Path
    v = load(tmp_path, global_path=Path("/nonexistent")).config.verification
    assert v["lint"].paths and "{changed}" in v["unit"].command


def test_headless_route_auto_prints_the_answer_without_a_worktree(tmp_path, monkeypatch):
    import subprocess
    from agent_desk.contracts import MAIN_FIELD
    from agent_desk.runtime.base import RunResult
    from agent_desk.runtime.mock import MockRuntime
    from agent_desk.runtime import registry
    repo = tmp_path / "r"; repo.mkdir(); (repo / "a.py").write_text("x=1\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=repo, check=True)
    def script(spec):
        out = {k: None for k in MAIN_FIELD.values()}
        out["route"] = {"kind": "answer", "text": "x is 1", "brief": None, "task": None, "reason": "r"}
        return RunResult("completed", json.dumps(out), "th")
    rt = MockRuntime(script)
    monkeypatch.setattr(registry, "RuntimeRouter", lambda cfg, reg, *a, **k: rt)
    r = R.invoke(app, ["headless", "what is x?", "--repo", str(repo), "--home", str(tmp_path / "h"), "--route", "auto"])
    assert r.exit_code == 0 and "x is 1" in r.output and "worktree=" not in r.output
