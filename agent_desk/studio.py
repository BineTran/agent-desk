"""Plan Studio logic (UI-independent): versions, discuss, comment->proposal, diff, material classification, readiness, approval."""
from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field

from .contracts import ArchitectReview, Decision, MainReply, Plan, TaskSpec, Triage
from .reasoner import render_packet


class StudioError(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def plan_hash(p: Plan) -> str:
    empty = {i: {"outputs"} for i, t in enumerate(p.tasks) if not t.outputs}     # plans from before `outputs` keep their hash
    return hashlib.sha256(p.model_dump_json(exclude={"tasks": empty} if empty else None).encode()).hexdigest()[:10]


@dataclass
class Change:
    id: str
    op: str                      # add | modify | remove
    before: str | None
    after: str | None
    material: bool


@dataclass
class PlanDiff:
    changes: list[Change]

    @property
    def material(self) -> bool:
        return any(c.material for c in self.changes)

    @property
    def empty(self) -> bool:
        return not self.changes


MATERIAL_TASK_FIELDS = ("kind", "goal", "depends_on", "requires_write", "acceptance_criteria", "outputs")


def _list_diff(prefix: str, old: list[str], new: list[str], material: bool) -> list[Change]:
    out: list[Change] = []
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        olds, news = old[i1:i2], new[j1:j2]
        for k in range(max(len(olds), len(news))):
            o = olds[k] if k < len(olds) else None
            n = news[k] if k < len(news) else None
            idx = (j1 + k + 1) if n is not None else (i1 + k + 1)
            out.append(Change(f"{prefix}-{idx}", "modify" if o and n else "add" if n else "remove", o, n, material))
    return out


def diff_plans(old: Plan, new: Plan) -> PlanDiff:
    ch: list[Change] = []
    if old.goal != new.goal:
        ch.append(Change("GOAL", "modify", old.goal, new.goal, True))
    ch += _list_diff("AC", old.acceptance_criteria, new.acceptance_criteria, True)
    ch += _list_diff("OUT", old.out_of_scope, new.out_of_scope, True)
    ch += _list_diff("C", old.constraints, new.constraints, False)
    ch += _list_diff("R", old.risks, new.risks, False)
    ot, nt = {t.id: t for t in old.tasks}, {t.id: t for t in new.tasks}
    for tid in ot.keys() - nt.keys():
        ch.append(Change(tid, "remove", ot[tid].title, None, True))
    for tid in nt.keys() - ot.keys():
        ch.append(Change(tid, "add", None, nt[tid].title, True))
    for tid in ot.keys() & nt.keys():
        a, b = ot[tid], nt[tid]
        if a == b:
            continue
        mat = any(getattr(a, f) != getattr(b, f) for f in MATERIAL_TASK_FIELDS)
        ch.append(Change(tid, "modify", a.title, b.title if a.title != b.title else b.goal, mat))
    return PlanDiff(ch)


@dataclass
class PlanVersion:
    n: int
    plan: Plan
    hash: str
    source: str                  # main | user-comment | user-question | replan
    note: str = ""


@dataclass
class Proposal:
    plan: Plan
    diff: PlanDiff
    text: str
    user_text: str | None
    item: str | None
    source: str = "user"             # user | architect


@dataclass
class Question:
    id: str
    text: str
    options: list[str]
    evidence: str
    recommended: int = 0
    answered: bool = False


@dataclass
class Check:
    label: str
    ok: bool
    blocking: bool = True
    detail: str = ""


class Studio:
    def __init__(self, session):
        self.s = session
        self.versions: list[PlanVersion] = []
        self.pending: Proposal | None = None
        self.questions: dict[str, Question] = {}
        self.locked = False
        self._pins: list[dict] = []
        self.seen_root: dict[str, str] = {}        # root_cause -> verdict, to stop reviewer/author ping-pong

    # ---------- versions ----------
    @property
    def current(self) -> PlanVersion:
        return self.versions[-1]

    async def start(self, plan: Plan, source: str = "main", note: str = "") -> PlanVersion:
        return await self._push(plan, source, note)

    async def _push(self, plan: Plan, source: str, note: str) -> PlanVersion:
        v = PlanVersion(len(self.versions) + 1, plan, plan_hash(plan), source, note)
        self.versions.append(v)
        await self.s.emit("plan.version", {"version": v.n, "hash": v.hash, "source": source, "note": note, "plan": plan.model_dump()}, source="main")
        return v

    def diff(self, a: int, b: int) -> PlanDiff:
        return diff_plans(self.versions[a - 1].plan, self.versions[b - 1].plan)

    # ---------- questions ----------
    async def add_question(self, q: Question) -> None:
        self.questions[q.id] = q
        await self.s.emit("question.opened", {"id": q.id, "text": q.text, "options": q.options, "evidence": q.evidence}, source="main")

    async def answer(self, qid: str, option: int | None = None, text: str | None = None) -> Decision:
        q = self.questions[qid]
        verbatim = text if text is not None else q.options[option]
        d = Decision(id=await self.s.mem.next_decision_id(), text=f"{q.text} -> {verbatim}", verbatim=verbatim, source="user")
        await self.s.mem.add_decision(d)
        q.answered = True
        await self.s.emit("decision.recorded", {**d.model_dump(), "question": qid}, source="user")
        return d

    # ---------- conversation with Main ----------
    def _prompt(self, text: str, item: str | None) -> str:
        cur = self.current
        pins = [n["text"] for n in self._pins]
        return ("You are in the plan conversation. Current plan (v%d, JSON):\n%s\n\nPinned notes: %s\nOpen questions: %s\n\n%sUser message: %s\n\n"
                "Reply with kind=answer to explain/discuss WITHOUT changing the plan (cite file:line when you rely on code); kind=proposal with the COMPLETE "
                "revised plan in `plan` when the user asks for a change; kind=stop if the user wants to stop. Never change anything the user did not ask for."
                % (cur.n, self.s.reasoner.plan_json(cur.plan), json.dumps(pins, ensure_ascii=False), [q.text for q in self.questions.values() if not q.answered],
                   f"The user is commenting on item {item}.\n" if item else "", text))

    async def ask(self, text: str, item: str | None = None) -> MainReply:
        if self.pending:
            raise StudioError(["a proposal is waiting: accept, edit or reject it first"])
        await self.s.emit("chat.user", {"text": text, "item": item}, source="user")      # L0 only: chat never enters packets
        reply = await self.s.reasoner.ask(self._prompt(text, item), MainReply, "studio")
        await self.s.emit("chat.main", {"kind": reply.kind, "text": reply.text}, source="main")
        if reply.kind == "proposal":
            if reply.plan is None:
                raise StudioError(["main proposed a change without a plan"])
            d = diff_plans(self.current.plan, reply.plan)
            if d.empty:
                reply = MainReply(kind="answer", text=reply.text or "No change needed.", plan=None)
            else:
                self.pending = Proposal(reply.plan, d, reply.text, text, item)
                await self.s.emit("proposal.created", {"material": d.material, "changes": [c.__dict__ for c in d.changes], "item": item}, source="main")
        elif reply.kind == "stop":
            await self.s.emit("user.stop", {"text": text}, source="user")
        return reply

    async def accept(self) -> PlanVersion:
        p = self.pending
        if p is None:
            raise StudioError(["nothing to accept"])
        d = Decision(id=await self.s.mem.next_decision_id(), text=p.text or f"plan changed ({p.source})", verbatim=p.user_text if p.source == "user" else None, source=p.source)
        await self.s.mem.add_decision(d)                                     # the user's own words are stored verbatim
        self.pending = None
        v = await self._push(p.plan, "user-comment" if p.item else "user-question", f"{d.id}: {'material' if p.diff.material else 'non-material'}")
        await self.s.emit("decision.recorded", d.model_dump(), source="user")
        if not self.locked and p.diff.material and "before_plan" in self.s.triggers:
            self.s.architect_reviewed = False                                # the architect never saw this version
            await self.s.emit("architect.invalidated", {"version": v.n}, source="user")
        if self.locked:
            await self.s.apply_plan(v, p.diff)
        return v

    async def propose_external(self, plan: Plan, text: str, source: str = "architect") -> Proposal | None:
        """A change that did not come from the user's chat (reviewer advice -> Main). Returns None when nothing changes."""
        d = diff_plans(self.current.plan, plan)
        if d.empty:
            return None
        self.pending = Proposal(plan, d, text, None, None, source)
        await self.s.emit("proposal.created", {"material": d.material, "changes": [c.__dict__ for c in d.changes], "source": source}, source="main")
        return self.pending

    async def integrate_review(self, trigger: str, review: ArchitectReview, tri: Triage) -> dict:
        """Record findings with their triage; open questions for the user; apply Main's fixes (only before approval; later fixes go through propose_external)."""
        by_id = {f.id: f for f in review.findings}
        out = {"fixed": 0, "rejected": 0, "questions": [], "version": None}
        v_from, items = self.current.n, []
        changed = tri.plan is not None and not diff_plans(self.current.plan, tri.plan).empty
        for it in tri.items:
            f = by_id.get(it.finding_id)
            if f is None:
                continue
            verdict = it.verdict
            if verdict == "fixed" and self.seen_root.get(f.root_cause) == "fixed":
                verdict = "question"                                   # fixed once already and raised again: stop looping, ask the human
                it = it.model_copy(update={"question": f"The reviewer keeps raising '{f.root_cause}' ({f.message[:120]}). How should we proceed?",
                                           "options": ["accept the current plan as is", "make the change the reviewer asks for", "something else"]})
            elif verdict == "fixed" and not changed and not self.locked:
                verdict = "question"                                   # claimed fixed but the plan did not change: nothing was fixed, ask the human
                it = it.model_copy(update={"reason": f"claimed fixed but plan unchanged ({it.reason})",
                                           "question": f"The reviewer found: {f.message[:200]} Main said it fixed this but the plan did not change. How should we proceed?",
                                           "options": ["make the change the reviewer asks for", "accept the current plan as is", "something else"]})
            self.seen_root[f.root_cause] = verdict
            items.append({"finding_id": f.id, "severity": f.severity, "verdict": verdict, "reason": it.reason})
            await self.s.mem.add_note("finding", f.id, f"[{f.severity}] {f.file}: {f.message} -> {verdict}: {it.reason}", trigger)
            if verdict == "fixed":
                out["fixed"] += 1
            elif verdict == "rejected":
                out["rejected"] += 1
            else:
                q = Question(f"Q-{len(self.questions) + 1}", it.question or f.message, (it.options if len(it.options) >= 2 else ["yes", "no"]),
                             f"{f.file} ({trigger} finding {f.id})")
                await self.add_question(q)
                out["questions"].append(q.id)
                items[-1]["question"] = q.id
        if changed and not self.locked:
                out["version"] = (await self._push(tri.plan, "architect", f"{trigger}: {out['fixed']} fixed")).n
        await self.s.emit("architect.triaged", {"trigger": trigger, "items": items, "from": v_from, "to": out["version"]}, source="main")
        return out

    async def reject(self) -> None:
        if self.pending:
            await self.s.emit("proposal.rejected", {"item": self.pending.item}, source="user")
        self.pending = None

    async def pin(self, text: str) -> str:
        nid = f"N-{len(self._pins) + 1}"
        self._pins.append({"id": nid, "text": text})
        await self.s.mem.add_note("note", nid, text, "plan-studio")
        return nid

    # ---------- readiness + approval ----------
    def checklist(self) -> list[Check]:
        cur = self.current.plan
        s = self.s
        open_q = [q for q in self.questions.values() if not q.answered]
        return [
            Check("no open blocking questions", not open_q, True, ", ".join(q.id for q in open_q)),
            Check("no pending proposal", self.pending is None, True),
            Check("plan has acceptance criteria", bool(cur.acceptance_criteria), True),
            Check("tasks" if cur.tasks else "no tasks: Main answers directly, nothing runs in the repo", True, False),
            Check("architect review done", getattr(s, "architect_reviewed", True), True),
            Check("verification configured", bool(s.cfg.verification), False, "without checks nothing proves the work"),
        ]

    def ready(self) -> bool:
        return all(c.ok for c in self.checklist() if c.blocking)

    async def approve(self, skip_review: bool = False) -> PlanVersion:
        if skip_review and not getattr(self.s, "architect_reviewed", True):
            self.s.architect_reviewed = True
            await self.s.emit("architect.skipped", {"by": "user"}, source="user")
        bad = [f"{c.label}{': ' + c.detail if c.detail else ''}" for c in self.checklist() if c.blocking and not c.ok]
        if bad:
            raise StudioError(bad)
        self.locked = True
        await self.s.lock_plan(self.current)
        return self.current
