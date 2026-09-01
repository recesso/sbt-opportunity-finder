#!/usr/bin/env python3
"""Load plan/backlog.yaml into Beads. Idempotent.

plan/backlog.yaml is the SOURCE OF TRUTH. Beads is derived state.
Edit the YAML, run `make backlog`, and the tracker catches up.

Matching is by title prefix: every bead is titled "<story-id> — <title>", so
"E0.S1" identifies exactly one bead. Re-running updates existing beads in place
rather than creating duplicates.

Usage:
    python scripts/load_backlog.py            # create/update
    python scripts/load_backlog.py --dry-run  # show what would happen
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BACKLOG = ROOT / "plan" / "backlog.yaml"


def bd(*args: str, capture: bool = True) -> str:
    """Run a bd command in the repo root."""
    cmd = ["bd", "-C", str(ROOT), *args]
    proc = subprocess.run(cmd, capture_output=capture, text=True)
    if proc.returncode != 0:
        sys.stderr.write(f"bd failed: {' '.join(cmd)}\n{proc.stderr}\n")
        raise SystemExit(1)
    return (proc.stdout or "").strip()


def existing_beads() -> dict[str, str]:
    """Map story/epic id -> bead id, by title prefix."""
    raw = bd("list", "--json")
    if not raw:
        return {}
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(items, dict):
        items = items.get("issues", items.get("items", []))
    out: dict[str, str] = {}
    for it in items:
        title = it.get("title", "")
        bead_id = it.get("id") or it.get("ID")
        key = title.split(" ", 1)[0].strip()
        if key and bead_id:
            out[key] = bead_id
    return out


def render_body(story: dict) -> str:
    """Build a self-contained description.

    A stranger must be able to execute this without reading a chat transcript.
    """
    parts: list[str] = []
    parts.append(story.get("description", "").strip())

    if story.get("files"):
        parts.append("\n## Files\n" + "\n".join(f"- `{f}`" for f in story["files"]))

    if story.get("tasks"):
        parts.append("\n## Tasks\n" + "\n".join(f"- [ ] {t}" for t in story["tasks"]))

    if story.get("steps"):
        numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(story["steps"], 1))
        parts.append("\n## Atomic steps\n" + numbered)

    parts.append(
        "\n## Definition of done\n"
        "- The acceptance criterion passes, demonstrated with command output.\n"
        "- Tests exist that would fail if the code were broken.\n"
        "- The suite runs offline; `make check` passes.\n"
        "- `CONTINUE_HERE.md` updated if project state changed.\n"
    )
    parts.append(
        "\n---\nSpec: `docs/00-build-spec.md` · Plan: `docs/01-delivery-plan.md` · "
        "Rules: `CLAUDE.md`\n"
        "Source of truth for this task: `plan/backlog.yaml`"
    )
    return "\n".join(p for p in parts if p).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = yaml.safe_load(BACKLOG.read_text(encoding="utf-8"))
    epics = data["epics"]

    have = existing_beads()
    created: dict[str, str] = dict(have)
    n_new = n_upd = 0

    # ---- epics -------------------------------------------------------------
    for epic in epics:
        eid = epic["id"]
        title = f"{eid} — {epic['title']}"
        body = epic.get("description", "").strip()
        prio = str(epic.get("priority", 2))

        if eid in have:
            if not args.dry_run:
                bd("update", have[eid], "--description", body, "--priority", prio)
            n_upd += 1
        else:
            if args.dry_run:
                print(f"CREATE epic  {title}")
                continue
            new_id = bd(
                "create", title, "-t", "epic", "-p", prio,
                "-d", body, "-l", f"epic,{eid}", "--silent",
            ).splitlines()[-1].strip()
            created[eid] = new_id
            n_new += 1

    # ---- stories -----------------------------------------------------------
    for epic in epics:
        eid = epic["id"]
        for story in epic.get("stories", []):
            sid = story["id"]
            title = f"{sid} — {story['title']}"
            body = render_body(story)
            prio = str(story.get("priority", 2))
            size = story.get("size", "M")
            labels = f"story,{eid},size-{size}"
            acceptance = story.get("acceptance", "").strip()

            if sid in have:
                if not args.dry_run:
                    bd("update", have[sid], "--description", body,
                       "--priority", prio, "--acceptance", acceptance)
                n_upd += 1
            else:
                if args.dry_run:
                    print(f"CREATE story {title}  (blocked by: {story.get('depends') or 'nothing'})")
                    continue
                parent = created.get(eid)
                extra = ["--parent", parent] if parent else []
                new_id = bd(
                    "create", title, "-t", "task", "-p", prio,
                    "-d", body, "--acceptance", acceptance,
                    "-l", labels, *extra, "--silent",
                ).splitlines()[-1].strip()
                created[sid] = new_id
                n_new += 1

    # ---- dependencies ------------------------------------------------------
    if not args.dry_run:
        for epic in epics:
            for story in epic.get("stories", []):
                sid = story["id"]
                target = created.get(sid)
                if not target:
                    continue
                for dep in story.get("depends", []) or []:
                    blocker = created.get(dep)
                    if not blocker:
                        sys.stderr.write(f"WARN: {sid} depends on unknown {dep}\n")
                        continue
                    # blocker blocks target
                    subprocess.run(
                        ["bd", "-C", str(ROOT), "dep", "add", target, blocker],
                        capture_output=True, text=True,
                    )

    n_stories = sum(len(e.get("stories", [])) for e in epics)
    print(
        f"{'DRY RUN: ' if args.dry_run else ''}"
        f"{len(epics)} epics, {n_stories} stories "
        f"({n_new} created, {n_upd} updated)"
    )
    if not args.dry_run:
        print("\nNext:  bd ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
