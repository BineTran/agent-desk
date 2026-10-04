"""Turn @mentions into session Inputs. In-repo = pointer@commit; outside repo = snapshot + sha256."""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from ..contracts import InputItem

URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"(?<!\S)@(\S+)")
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def parse_mentions(text: str) -> tuple[list[str], list[str]]:
    return [m.rstrip(".,;)") for m in MENTION_RE.findall(text)], [u.rstrip(".,;)") for u in URL_RE.findall(text)]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_inputs(text: str, repo: Path, session_dir: Path, commit: str, images: list[str] | None = None,
                 start: int = 1) -> list[InputItem]:
    mentions, urls = parse_mentions(text)
    items: list[InputItem] = []
    n = start

    def nid() -> str:
        nonlocal n
        n += 1
        return f"I-{n - 1}"

    for m in mentions:
        p = Path(m).expanduser()
        if not p.is_absolute():
            p = repo / m
        if not p.exists():
            raise FileNotFoundError(f"@{m}: not found")
        p = p.resolve()
        inside = repo.resolve() in p.parents or p == repo.resolve()
        if p.is_dir():
            items.append(InputItem(id=nid(), kind="dir", ref=str(p.relative_to(repo.resolve())) if inside else str(p)))
        elif inside:
            items.append(InputItem(id=nid(), kind="image" if p.suffix.lower() in IMG_EXT else "file",
                                   ref=f"{p.relative_to(repo.resolve())}@{commit}"))
        else:  # outside the repo: freeze a copy so later edits don't change what the agents saw
            dest = session_dir / "inputs"
            dest.mkdir(parents=True, exist_ok=True)
            iid = nid()
            snap = dest / f"{iid}{p.suffix}"
            shutil.copy2(p, snap)
            items.append(InputItem(id=iid, kind="image" if p.suffix.lower() in IMG_EXT else "doc",
                                   ref=str(p), snapshot=str(snap), sha256=sha(snap)))
    for u in urls:
        items.append(InputItem(id=nid(), kind="url", ref=u))
    for img in images or []:
        items.append(InputItem(id=nid(), kind="image", ref=img, snapshot=img, sha256=sha(Path(img))))
    return items
