"""UI state as a pure projection of the event stream. Idempotent on seq, so reconnect/replay never duplicates anything."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from ..contracts import Event

LIVE_CAP = 4000
SHELL_RE = re.compile(r"""^\S*/(?:zsh|bash|sh)\s+-l?c\s+(["'])(.*)\1\s*$""", re.S)


def clean_cmd(s: str) -> str:
    """`/bin/zsh -lc "rg foo"` -> `rg foo` (codex wraps every command in a login shell)."""
    s = (s or "").strip()
    m = SHELL_RE.match(s)
    return m.group(2).replace('\\"', '"') if m else s


def clean_md(s: str) -> str:
    return re.sub(r"\*\*|__|`", "", s or "")


def message_text(s: str) -> str:
    """Interim or final agent message; structured reports (JSON) show their summary/text."""
    t = (s or "").strip()
    if t.startswith("{"):
        return partial_text(t, "summary") or partial_text(t) or ""
    return clean_md(t)


def partial_text(buf: str, key: str = "text") -> str | None:
    """Value of `key` from a JSON object that is still streaming in (tolerates truncation and escapes). None if absent."""
    i = buf.find(f'"{key}"')
    if i < 0:
        return None
    j = buf.find('"', buf.find(":", i + len(key) + 2) + 1)
    if j < 0:
        return ""
    out, k = [], j + 1
    while k < len(buf):
        c = buf[k]
        if c == '"':
            break
        if c == "\\":
            if k + 1 >= len(buf):
                break
            nxt = buf[k + 1]
            if nxt == "u":
                if k + 6 > len(buf):
                    break
                try:
                    out.append(chr(int(buf[k + 2:k + 6], 16)))
                except ValueError:
                    pass
                k += 6
                continue
            out.append({"n": "\n", "t": "\t", "r": ""}.get(nxt, nxt))
            k += 2
            continue
        out.append(c)
        k += 1
    return "".join(out)


@dataclass
class AgentView:
    run_id: str
    role: str
    task: str | None
    provider: str = ""
    model: str = ""
    status: str = "running"          # running | done | failed | interrupted | quota | wait
    note: str = ""
    usage: dict = field(default_factory=dict)
    label: str = ""
    started: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    activity: str = ""                # last tool / command / message, one line
    live_text: str = ""
    live_reasoning: str = ""
    live_output: str = ""

    @property
    def running(self) -> bool:
        return self.status == "running"

    def reply_preview(self) -> str:
        """What Main is writing right now: the `text` field of structured output, else plain text."""
        t = self.live_text.lstrip()
        if not t:
            return ""
        if t.startswith("{"):
            v = partial_text(t)
            return v if v is not None else f"writing {len(t):,} chars…"
        return t


@dataclass
class Projection:
    last_seq: int = 0
    session_id: str = ""
    branch: str = ""
    worktree: str = ""
    status: str = "CREATED"
    mode: str = "CHAT"               # CHAT | PLAN | RUN | RESULT
    started_ts: str = ""
    last_ts: str = ""
    plan_version: int = 0
    plan_hash: str = ""
    plan: dict | None = None
    versions: list[dict] = field(default_factory=list)
    approved: bool = False
    pending: dict | None = None      # open proposal {material, changes, source}
    questions: dict = field(default_factory=dict)       # id -> {text, options, evidence, answered}
    chat: list[dict] = field(default_factory=list)      # {who, text, kind?}
    decisions_log: list[dict] = field(default_factory=list)   # control decisions
    decisions_count: int = 0
    design_decisions: list[dict] = field(default_factory=list)
    agents: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)           # task id -> status
    log: list[tuple] = field(default_factory=list)
    verify: dict = field(default_factory=dict)          # check -> {passed, required}
    approvals: int = 0
    architect: dict = field(default_factory=lambda: {"calls": 0, "tokens": 0, "advice": "—", "active": None, "reviews": []})
    tokens: dict = field(default_factory=dict)          # (provider, role) -> output+input tokens
    token_detail: dict = field(default_factory=dict)    # (provider, role) -> {fresh, cached, output}: fresh input is what the quota feels most
    quota_hit: dict | None = None
    gate_unmet: list = field(default_factory=list)
    summary: dict | None = None
    followups: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    overrides: list = field(default_factory=list)       # role/engine switches made in-session
    limits: dict = field(default_factory=dict)          # provider -> last rate_limit_info (claude: status + resetsAt)
    annotations: dict = field(default_factory=dict)     # plan item id -> note (what changed it, where a question came from)

    # ------------------------------------------------------------------
    def apply_live(self, e: Event) -> bool:
        """Ephemeral deltas: not seq-gated, never logged."""
        a = self.agents.get(e.agent_run_id or "")
        if not a or not a.running:
            return False
        d = str(e.payload.get("delta", ""))
        attr = {"live.text": "live_text", "live.reasoning": "live_reasoning", "live.output": "live_output"}.get(e.type)
        if not attr:
            return False
        setattr(a, attr, (getattr(a, attr) + d)[-LIVE_CAP:] if attr != "live_text" else getattr(a, attr) + d)
        a.last_activity = time.monotonic()
        return True

    def running_agents(self) -> list[AgentView]:
        return [a for a in self.agents.values() if a.running]

    def queued_tasks(self) -> list[dict]:
        if not self.plan:
            return []
        busy = {a.task for a in self.running_agents()}
        return [t for t in self.plan["tasks"] if self.tasks.get(t["id"], "pending") in ("pending", "stale") and t["id"] not in busy]

    # ------------------------------------------------------------------
    def apply(self, e: Event) -> bool:
        if e.type.startswith("live."):
            return self.apply_live(e)
        if e.seq <= self.last_seq:
            return False                                 # duplicate (replay/reconnect overlap) or never persisted
        self.last_seq = max(self.last_seq, e.seq)
        self.last_ts = e.ts
        self.started_ts = self.started_ts or e.ts
        h = getattr(self, "_on_" + e.type.replace(".", "_"), None)
        if h:
            h(e)
        if e.type not in NOISY:
            who, msg = humanize(e, self)
            self.log.append((e.ts[11:19], who, e.type, msg))
            self.log = self.log[-200:]
        return True

    # ---- session / plan ----
    def _on_session_created(self, e):
        p = e.payload
        self.session_id, self.branch, self.worktree = e.session_id, p.get("branch") or "", p.get("worktree") or ""
        self.status = "CREATED"

    def _on_workspace_prepared(self, e):
        self.workspace = {**getattr(self, "workspace", {}), "linked": e.payload.get("linked", [])}

    def _on_workspace_setup(self, e):
        self.workspace = {**getattr(self, "workspace", {}), "setup": e.payload}

    def _on_workspace_created(self, e):
        self.branch, self.worktree = e.payload.get("branch", ""), e.payload.get("worktree", "")

    def _on_chat_routed(self, e): pass                      # shown by the log line and the chat.main bubble

    def _on_job_started(self, e):
        """A new quick task / plan in the same conversation: per-job state starts over; chat, tokens and log stay."""
        p = e.payload
        self.plan = self.pending = self.summary = self.quota_hit = None
        self.plan_version, self.plan_hash, self.approved = 0, "", False
        self.versions, self.agents, self.tasks, self.verify, self.questions = [], {}, {}, {}, {}
        self.gate_unmet, self.errors, self.followups, self.annotations = [], [], [], {}
        self.architect = {"calls": 0, "tokens": 0, "advice": "—", "active": None, "reviews": []}
        self.mode, self.status = ("RUN" if p.get("kind") == "quick" else "PLAN"), ("RUNNING" if p.get("kind") == "quick" else "PLANNING")
        self.chat.append({"who": "sys", "text": f"— J{p.get('job')} {p.get('kind')}: {str(p.get('brief') or '')[:100]}"})

    def _on_job_finished(self, e): self.status = e.payload.get("outcome", self.status)

    def _on_session_planning(self, e): self.status, self.mode = "PLANNING", "PLAN"

    def _on_plan_version(self, e):
        p = e.payload
        if self.plan:
            for iid in _changed_items(self.plan, p["plan"]):
                self.annotations[iid] = f"changed in v{p['version']} ({p.get('source') or 'main'})"
        self.plan_version, self.plan_hash, self.plan = p["version"], p["hash"], p["plan"]
        self.pending = None                                  # a new version supersedes (accepts) any open proposal
        self.versions.append({"n": p["version"], "hash": p["hash"], "source": p.get("source"), "note": p.get("note", ""), "plan": p["plan"]})
        for t in p["plan"]["tasks"]:
            self.tasks.setdefault(t["id"], "pending")

    def _on_plan_approved(self, e):
        self.approved, self.mode, self.status = True, "RUN", "RUNNING"
        self.pending = None
        self.chat.append({"who": "sys", "text": f"plan v{e.payload['version']} approved ({e.payload['hash']})"})

    def _on_plan_applied(self, e):
        p = e.payload
        self.plan_hash = p["hash"]
        self.chat.append({"who": "sys", "text": f"plan v{p['version']} applied ({'material, you accepted' if p['material'] else 'non-material, auto'})"})
        for t in p.get("changed_tasks", []):
            self.tasks[t] = "stale"

    def _on_plan_abandoned(self, e): self.status = "CANCELLED"

    def _on_proposal_created(self, e): self.pending = dict(e.payload)
    def _on_proposal_rejected(self, e): self.pending = None; self.chat.append({"who": "sys", "text": "proposal rejected"})

    def _on_question_opened(self, e): self.questions[e.payload["id"]] = {**e.payload, "answered": False}

    def _on_clarify_asked(self, e):
        for q in e.payload.get("questions", []):
            self.questions[q["id"]] = {"id": q["id"], "text": q["text"], "options": q["options"], "evidence": q.get("evidence", ""),
                                       "answered": False, "recommended": q.get("recommended", 0)}

    def _on_decision_recorded(self, e):
        d = e.payload
        self.design_decisions.append(d)
        if d.get("question") in self.questions:
            self.questions[d["question"]]["answered"] = True
        for q in self.questions.values():                # clarify answers carry no question id
            if not q["answered"] and d.get("text", "").startswith(q["text"]):
                q["answered"] = True

    def _on_chat_user(self, e): self.chat.append({"who": "you", "text": e.payload["text"], "item": e.payload.get("item")})
    def _on_chat_main(self, e): self.chat.append({"who": "main", "text": e.payload["text"], "kind": e.payload.get("kind")})

    # ---- agents / tasks ----
    def _on_agent_started(self, e):
        rid = e.agent_run_id or f"{e.source}-{e.seq}"
        p = e.payload
        self.agents[rid] = AgentView(rid, p.get("role", e.source), e.task_id, p.get("provider", ""), p.get("model", ""), label=p.get("label", ""))
        if p.get("role") == "architect" or e.source == "architect":
            self.architect["calls"] += 1
            self.architect["active"] = rid
        self.tasks[e.task_id] = "running" if e.task_id and self.tasks.get(e.task_id) != "running" else self.tasks.get(e.task_id)

    def _on_agent_finished(self, e):
        a = self.agents.get(e.agent_run_id or "")
        if not a:
            return
        st = e.payload.get("status", "completed")
        if a.note and not a.note.startswith("commit"):
            a.note = ""                                       # finished: the last tool command is noise
        a.status = {"completed": "done", "failed": "failed", "interrupted": "interrupted", "quota": "quota"}.get(st, st)
        a.usage = e.payload.get("usage") or {}
        a.live_text = a.live_reasoning = a.live_output = ""
        a.activity = ""
        tot = sum(a.usage.get(k, 0) for k in ("inputTokens", "outputTokens"))      # runtimes report per-run usage
        key = f"{a.provider}|{a.role}"
        self.tokens[key] = self.tokens.get(key, 0) + tot
        d = self.token_detail.setdefault(key, {"fresh": 0, "cached": 0, "output": 0})
        cached = a.usage.get("cachedInputTokens", 0)
        d["fresh"] += a.usage.get("inputTokens", 0) - cached
        d["cached"] += cached
        d["output"] += a.usage.get("outputTokens", 0)
        if a.role == "architect":
            self.architect["tokens"] += tot
            self.architect["active"] = None

    def _on_tool_started(self, e):
        a = self.agents.get(e.agent_run_id or "")
        if a:
            a.note = clean_cmd(str(e.payload.get("command") or e.payload.get("tool") or ""))[:60]
            a.activity = (f"{e.payload['tool']} " if e.payload.get("tool") else "$ ") + clean_cmd(str(e.payload.get("command") or ""))[:120]
            a.live_output = ""
            a.last_activity = time.monotonic()

    def _on_tool_completed(self, e):
        a = self.agents.get(e.agent_run_id or "")
        if a:
            a.last_activity = time.monotonic()

    def _on_agent_message(self, e):
        a = self.agents.get(e.agent_run_id or "")
        if a:
            msg = message_text(str(e.payload.get("text", "")))
            a.activity = msg.splitlines()[0][:120] if msg.strip() else a.activity
            a.live_text = ""
            a.last_activity = time.monotonic()

    def _on_agent_reasoning(self, e):
        a = self.agents.get(e.agent_run_id or "")
        if a:
            a.last_activity = time.monotonic()

    def _on_task_done(self, e):
        self.tasks[e.task_id] = "done"
        if e.payload.get("commit"):
            self._note(e, "commit " + str(e.payload["commit"]))
    def _on_task_retry(self, e): self.tasks[e.task_id] = "pending"; self._note(e, "retrying")
    def _on_task_escalate(self, e): self.tasks[e.task_id] = "failed"
    def _on_task_stop(self, e): self.tasks[e.task_id] = "failed"; self.errors.append(e.payload.get("reason", ""))
    def _on_task_replanned(self, e): self.tasks[e.task_id] = "pending"

    def _note(self, e, text):
        for a in self.agents.values():
            if a.task == e.task_id and a.status in ("running", "done"):
                a.note = text

    # ---- decisions / verification / policy ----
    def _on_decision_made(self, e):
        self.decisions_count += 1
        self.decisions_log.append(dict(e.payload, task=e.task_id))
        self.decisions_log = self.decisions_log[-40:]

    def _on_verification_result(self, e):
        p = e.payload
        self.verify[p["name"]] = {"passed": p["passed"], "required": p.get("required", True), "fp": p.get("fingerprint"),
                                  "status": p.get("status") or ("pass" if p["passed"] else "regression"), "summary": p.get("summary", ""),
                                  "new": p.get("new_failures") or [], "failures": p.get("failures", 0), "skip": p.get("skip_reason", ""),
                                  "wrote": p.get("wrote") or [], "command": p.get("command", "")}

    def _on_verification_baseline(self, e):
        self.baseline = {**getattr(self, "baseline", {}), e.payload["name"]: e.payload}

    def _on_approval_classified(self, e):
        if e.payload.get("verdict") == "approve":
            self.approvals += 1

    def _on_architect_reviewed(self, e):
        p = e.payload
        self.chat.append({"who": "arch", "text": p.get("verdict") or ("unavailable: " + str(p.get("error") or "")), "findings": p.get("findings") or [],
                          "trigger": p["trigger"]})
        for f in p.get("findings") or []:
            if f.get("file"):
                self.annotations.setdefault(f"F:{f.get('id')}", f"{f.get('severity')} {f.get('file')}")
        self.architect["reviews"].append({"trigger": p["trigger"], "verdict": p["verdict"], "n": len(p["findings"]), "fallback": p.get("via_fallback")})
        if p["findings"]:
            self.architect["advice"] = p["findings"][0]["message"][:80]
        elif p["verdict"]:
            self.architect["advice"] = p["verdict"]

    def _on_architect_triaged(self, e):
        p = e.payload
        parts = []
        for it in p.get("items") or []:
            s = f"{it['finding_id']} {it['verdict']}"
            if it["verdict"] == "rejected" and it.get("reason"):
                s += f": {it['reason'][:80]}"
            elif it["verdict"] == "question" and it.get("question"):
                s += f" {it['question']}"
            parts.append(s)
        change = f"v{p.get('from')}→v{p['to']}" if p.get("to") else "no plan change"
        self.chat.append({"who": "main", "text": " · ".join([f"{p.get('trigger')} triage", change, *parts])})

    def _on_architect_invalidated(self, e):
        self.chat.append({"who": "sys", "text": f"plan v{e.payload.get('version')} changed → architect re-reviewing (/approve --skip-review to skip)"})

    def _on_usage_updated(self, e): pass
    def _on_provider_limit(self, e): self.limits[e.payload.get("provider", "")] = dict(e.payload)

    # ---- ending ----
    def _on_quota_hit(self, e): self.quota_hit = dict(e.payload)
    def _on_session_waiting_quota(self, e): self.status = "WAITING_QUOTA"
    def _on_session_resumed(self, e): self.status, self.mode = "RUNNING", "RUN"
    def _on_session_reopened(self, e): self.status = e.payload.get("status", self.status)
    def _on_completion_gate(self, e): self.gate_unmet = e.payload.get("unmet", []); self.status = "VERIFYING"
    def _on_final_summary(self, e): self.summary = e.payload; self.status, self.mode = "COMPLETED", "RESULT"
    def _on_error(self, e): self.errors.append(e.payload.get("message", ""))
    def _on_role_switched(self, e): self.overrides.append(e.payload)
    def _on_engine_switched(self, e): self.overrides.append(e.payload)

    def finish(self, outcome: str) -> None:
        """Called when the graph returns; sets the final mode even when no summary was produced."""
        self.status = outcome
        self.mode = "RESULT" if outcome in ("COMPLETED", "FAILED", "CANCELLED", "WAITING_QUOTA", "INTERRUPTED") else self.mode


def _changed_items(old: dict, new: dict) -> list[str]:
    out = []
    for key, pre in (("acceptance_criteria", "AC"), ("risks", "R"), ("out_of_scope", "OUT")):
        a, b = old.get(key) or [], new.get(key) or []
        out += [f"{pre}-{i}" for i in range(1, len(b) + 1) if i > len(a) or a[i - 1] != b[i - 1]]
    ot = {t["id"]: t for t in old.get("tasks") or []}
    out += [t["id"] for t in new.get("tasks") or [] if ot.get(t["id"]) != t]
    return out


NOISY = {"main.thread", "provider.limit", "agent.reasoning", "usage.updated", "diff.updated", "tool.completed", "plan.version", "chat.user", "chat.main", "agent.finished"}


ENGINE_WHO = {"rule": "rule", "llm": "haiku", "jev": "jev", "main": "main", "user": "you"}


def humanize(e: Event, pj: "Projection") -> tuple[str, str]:
    """One readable session-log line: (who, message). Mirrors the prototype's log, never raw JSON."""
    p, t, tid = e.payload, e.type, e.task_id or ""
    who = {"user": "you", "harness": "session", "policy": "policy"}.get(e.source, e.source)
    a = pj.agents.get(e.agent_run_id or "")
    if t == "agent.started":
        what = tid or p.get("label") or ""
        return who, f"{what} started · {p.get('provider', '')}·{p.get('model', '')}".strip()
    if t == "agent.message":
        return who, message_text(str(p.get("text", ""))).replace("\n", " ")[:160]
    if t == "tool.started":
        return who, ((p["tool"] + " ") if p.get("tool") else "$ ") + clean_cmd(str(p.get("command") or ""))[:150]
    if t == "decision.made":
        eng = p.get("engine", "")
        w = ENGINE_WHO.get(eng, eng)
        if eng == "llm" and p.get("model"):
            w = str(p["model"])
        msg = f"{p.get('type')} {tid} → {p.get('selected')}".replace("  ", " ")
        if p.get("confidence") is not None:
            msg += f"  p={p['confidence']:.2f} " + ("sharp → code" if p.get("sharp") else "split → main")
        elif p.get("reason"):
            msg += f"  ({str(p['reason'])[:60]})"
        return w, msg
    if t == "verification.result":
        st = p.get("status") or ("pass" if p.get("passed") else "regression")
        if st == "pass":
            return "verify", f"✓ {p.get('name')}"
        if st == "skipped":
            return "verify", f"– {p.get('name')} skipped: {p.get('skip_reason', '')}"
        if st == "pre-existing":
            return "verify", f"• {p.get('name')} already failing on base ({p.get('failures', 0)} failures, none new)"
        new = ", ".join((p.get("new_failures") or [])[:3]) or p.get("summary", "")
        return "verify", f"✗ {p.get('name')} {st}: {new}"[:200]
    if t == "verification.baseline":
        if p.get("error"):
            return "verify", f"baseline {p.get('name')}: unavailable ({p['error']})"
        return "verify", f"baseline {p.get('name')}: " + ("passes on base" if p.get("passed") else f"already fails on base — {p.get('summary', '')}"[:160])
    if t == "task.done":
        return "git" if p.get("commit") else "scheduler", f"{tid} done" + (f" · commit {p['commit']}" if p.get("commit") else "")
    if t in ("task.retry", "task.escalate", "task.escalated", "task.stop", "task.replanned"):
        return "scheduler", f"{tid} {t.split('.')[1]} · " + str(p.get("summary") or p.get("reason") or p.get("advice") or "")[:120]
    if t == "plan.approved":
        return "you", f"/approve · plan v{p.get('version')} locked ({p.get('hash')})"
    if t == "plan.applied":
        return who, f"plan v{p.get('version')} applied · " + ("material, you accepted" if p.get("material") else "non-material, auto")
    if t == "architect.reviewed":
        n = len(p.get("findings") or [])
        return "architect", f"{p.get('trigger')} → {p.get('verdict') or 'unavailable'}" + (f" · {n} finding(s)" if n else "")
    if t == "architect.triaged":
        its = p.get("items") or []
        counts = {v: sum(1 for i in its if i.get("verdict") == v) for v in ("fixed", "rejected", "question")}
        return "main", f"triage {p.get('trigger')} · " + ", ".join(f"{n} {v}" for v, n in counts.items() if n) + \
            (f" · v{p.get('from')}→v{p['to']}" if p.get("to") else " · no plan change")
    if t == "architect.invalidated":
        return "you", f"plan v{p.get('version')} changed · architect review reset"
    if t == "approval.classified":
        return "policy", f"{clean_cmd(str(p.get('command') or ''))[:100]} → {p.get('verdict')}"
    if t == "approval.gated":
        return p.get("by") or "debug", f"{clean_cmd(str(p.get('command') or ''))[:90]} → {p.get('decision')}" + (f" · {p['reason'][:100]}" if p.get("reason") else "")
    if t == "approval.resolved":
        return "you", ("approved " if p.get("approved") else "rejected ") + clean_cmd(str(p.get("command") or ""))[:100]
    if t == "quota.hit":
        return "rule", f"quota {p.get('provider')} · resets {p.get('reset')} → WAITING_QUOTA"
    if t == "completion.gate":
        un = p.get("unmet") or []
        return "gate", "all checks met ✓" if not un else "unmet: " + "; ".join(un)[:140]
    if t == "final.summary":
        return "main", "final summary ready"
    if t == "agent.limit":
        return who, f"budget reached ({p.get('reason')}) → asked for a report with what it has"
    if t == "architect.skipped":
        return ("you", "architect review skipped") if p.get("by") else ("architect", f"{p.get('trigger')} skipped · {p.get('reason')}")
    if t == "session.reopened":
        return "session", f"reopened ({p.get('status')})" + (" · Main continues its thread" if p.get("main_thread") else " · Main starts from session.md")
    if t == "chat.routed":
        return "main", f"→ {p.get('kind')} (by {p.get('by')})" + (f" · {p['reason'][:100]}" if p.get("reason") else "")
    if t == "job.started":
        return "session", f"job {p.get('job')} · {p.get('kind')}"
    if t == "job.finished":
        return "session", f"job {p.get('job')} · {p.get('outcome')}"
    if t == "workspace.created":
        return "git", f"worktree on branch {p.get('branch')}"
    if t == "workspace.stashed":
        return "git", f"leftover changes of job {p.get('job')} stashed ({p.get('stash')})"
    if t == "main.rotated":
        return "main", f"fresh thread seeded from session.md ({p.get('reason')})"
    return who, (t + " " + _short(e)).strip()


def _short(e: Event) -> str:
    p = e.payload
    for k in ("summary", "reason", "command", "text", "message", "name"):
        if p.get(k):
            return str(p[k])[:100]
    return ""
