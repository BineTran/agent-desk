"""What the harness needs from a front end. TUI and headless both implement this."""
from __future__ import annotations

from typing import Protocol

from .contracts import Questions
from .runtime.base import Approval


class UI(Protocol):
    async def answer_questions(self, qs: Questions) -> dict[str, tuple[int, str]]: ...   # qid -> (option index, verbatim)
    async def run_studio(self, studio) -> bool: ...          # drive the Plan Studio until approved (True) or abandoned (False)
    async def resolve_proposal(self, studio) -> bool: ...    # a material plan change during RUN: accept (True) or reject (False)
    async def approve_command(self, a: Approval, advice: str) -> bool: ...


class HeadlessUI:
    """No human: accept recommended answers, approve the plan, refuse anything that needs approval."""

    async def answer_questions(self, qs: Questions):
        return {q.id: (q.recommended, q.options[q.recommended]) for q in qs.questions}

    async def run_studio(self, studio):
        await studio.approve(skip_review=True)      # no human to skip it: an unavailable reviewer must not block an unattended run
        return True

    async def resolve_proposal(self, studio) -> bool: ...    # a material plan change during RUN: accept (True) or reject (False)
    async def resolve_proposal(self, studio):
        return True

    async def approve_command(self, a, advice):
        return False
