#!/usr/bin/env python3
"""Ephemeral remote skill download / discover / resolve helper.

Usage:
    skill_run.py ensure  <owner/repo>
    skill_run.py update  <owner/repo[@skill]>
    skill_run.py list    <owner/repo>
    skill_run.py resolve <owner/repo[@skill]>

Prints JSON on stdout. Exit 0 on success, 1 on error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_CONTAINERS = (
    "skills",
    ".agents/skills",
    ".grok/skills",
    ".claude/skills",
    ".cursor/skills",
)

DESC_MAX = 280


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    sys.exit(exit_code)


def fail(message: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"ok": False, "message": message}
    payload.update(extra)
    emit(payload, 1)


# ---------------------------------------------------------------------------
# Source / cache paths
# ---------------------------------------------------------------------------


def normalize_source(raw: str) -> tuple[str, str | None]:
    """Return (owner/repo, optional_skill_name).

    Accepts owner/repo, owner/repo@skill, and GitHub HTTPS URLs.
    """
    s = raw.strip().rstrip("/")
    skill: str | None = None

    if "@" in s and not s.startswith("http"):
        # owner/repo@skill — only split on the last @ after a slash path
        base, maybe_skill = s.rsplit("@", 1)
        if "/" in base and maybe_skill and "/" not in maybe_skill:
            s = base
            skill = maybe_skill.strip() or None

    # https://github.com/owner/repo[/tree/...] or https://github.com/owner/repo.git
    m = re.match(
        r"^(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/#?]+)",
        s,
        re.IGNORECASE,
    )
    if m:
        owner = m.group(1)
        repo = m.group(2)
        if repo.endswith(".git"):
            repo = repo[: -len(".git")]
        return f"{owner}/{repo}", skill

    # git@github.com:owner/repo.git
    m = re.match(r"^git@github\.com:([^/]+)/([^/#?]+?)(?:\.git)?$", s, re.IGNORECASE)
    if m:
        return f"{m.group(1)}/{m.group(2)}", skill

    # owner/repo
    m = re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}", skill

    fail(f"Invalid source '{raw}'. Expected owner/repo, owner/repo@skill, or a GitHub URL.")


def cache_root() -> Path:
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or os.environ.get("TMPDIR") or "/tmp"
    return Path(temp) / "skills"


def cache_dir_for(owner_repo: str) -> Path:
    safe = owner_repo.replace("/", "__")
    return cache_root() / safe


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def run_cmd(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ---------------------------------------------------------------------------
# Clone / update
# ---------------------------------------------------------------------------


def has_cache(cache: Path) -> bool:
    if not cache.is_dir():
        return False
    if (cache / ".git").exists():
        return True
    try:
        next(cache.iterdir())
        return True
    except StopIteration:
        return False


def clone_repo(owner_repo: str, cache: Path) -> str:
    """Clone into cache. Returns method used: 'gh' or 'git'."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)

    gh = which("gh")
    if gh:
        result = run_cmd([gh, "repo", "clone", owner_repo, str(cache), "--", "--depth", "1"])
        if result.returncode == 0 and has_cache(cache):
            return "gh"
        # fall through to git on gh failure

    git = which("git")
    if not git:
        fail(
            "Neither 'gh' nor 'git' is available. Install GitHub CLI (gh) or git to download skills.",
            source=owner_repo,
        )

    url = f"https://github.com/{owner_repo}.git"
    result = run_cmd([git, "clone", "--depth", "1", url, str(cache)])
    if result.returncode != 0 or not has_cache(cache):
        detail = (result.stderr or result.stdout or "").strip()
        fail(
            f"Failed to clone {owner_repo}. {detail or 'Unknown error'}. "
            "For private repos, run: gh auth login",
            source=owner_repo,
        )
    return "git"


def ensure_repo(owner_repo: str) -> tuple[Path, str]:
    """Ensure cache exists. Returns (cache_dir, action)."""
    cache = cache_dir_for(owner_repo)
    if has_cache(cache):
        return cache, "cached"
    method = clone_repo(owner_repo, cache)
    return cache, f"cloned:{method}"


def update_repo(owner_repo: str) -> tuple[Path, str]:
    """Refresh whole-repo cache. Strips skill selection — always repo-level."""
    cache = cache_dir_for(owner_repo)
    git = which("git")

    if has_cache(cache) and (cache / ".git").exists() and git:
        result = run_cmd([git, "pull", "--ff-only"], cwd=cache)
        if result.returncode == 0:
            return cache, "pulled"
        # fall through to re-clone

    method = clone_repo(owner_repo, cache)
    return cache, f"recloned:{method}"


# ---------------------------------------------------------------------------
# Frontmatter + discovery
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal YAML-ish frontmatter: key: value and folded description."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end].strip("\n")
    fields: dict[str, str] = {}
    current_key: str | None = None
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_key, current_parts
        if current_key is not None:
            fields[current_key] = "\n".join(current_parts).strip()
        current_key = None
        current_parts = []

    for line in block.splitlines():
        # continuation of folded/literal block or multi-line plain
        if current_key and (line.startswith("  ") or line.startswith("\t") or line.startswith(">") or line.strip() == ""):
            cleaned = line.strip()
            if cleaned.startswith(">"):
                cleaned = cleaned[1:].strip()
            current_parts.append(cleaned)
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if m:
            flush()
            current_key = m.group(1).strip().lower()
            val = m.group(2).strip()
            if val in (">", "|", ">-", "|-"):
                current_parts = []
            elif (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                current_parts = [val[1:-1]]
                flush()
            else:
                current_parts = [val] if val else []
                if val:
                    # single-line value complete unless empty (could be block)
                    flush()
            continue
        if current_key:
            current_parts.append(line.strip())
    flush()
    return fields


def first_paragraph(body: str) -> str:
    lines = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            if lines:
                break
            continue
        if s.startswith("#"):
            continue
        lines.append(s)
    return " ".join(lines)


def truncate(text: str, max_len: int = DESC_MAX) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def skill_entry(skill_md: Path, cache: Path) -> dict[str, str]:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {
            "name": skill_md.parent.name,
            "description": f"(unreadable: {e})",
            "path": str(skill_md.resolve()),
            "dir": str(skill_md.parent.resolve()),
        }

    fields = parse_frontmatter(text)
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            body = text[end + 4 :]

    name = fields.get("name") or skill_md.parent.name
    if skill_md.parent.resolve() == cache.resolve():
        # root SKILL.md — prefer frontmatter name, else repo folder name
        name = fields.get("name") or cache.name.split("__")[-1]

    desc = fields.get("description") or first_paragraph(body) or "(no description)"
    return {
        "name": name.strip(),
        "description": truncate(desc),
        "path": str(skill_md.resolve()),
        "dir": str(skill_md.parent.resolve()),
    }


def discover_skills(cache: Path) -> list[dict[str, str]]:
    found: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp in seen or not p.is_file():
            return
        seen.add(rp)
        found.append(p)

    root_skill = cache / "SKILL.md"
    if root_skill.is_file():
        add(root_skill)

    for container in SKILL_CONTAINERS:
        base = cache / container
        if not base.is_dir():
            continue
        # skills/<name>/SKILL.md
        for child in sorted(base.iterdir()):
            if child.is_dir():
                sm = child / "SKILL.md"
                if sm.is_file():
                    add(sm)
                else:
                    # skills/<category>/<name>/SKILL.md (one nested level)
                    try:
                        for nested in sorted(child.iterdir()):
                            if nested.is_dir():
                                nsm = nested / "SKILL.md"
                                if nsm.is_file():
                                    add(nsm)
                    except OSError:
                        pass
            elif child.name == "SKILL.md" and child.is_file():
                add(child)

    skills = [skill_entry(p, cache) for p in found]
    # de-dupe by name preferring first occurrence
    by_name: dict[str, dict[str, str]] = {}
    for s in skills:
        key = s["name"].lower()
        if key not in by_name:
            by_name[key] = s
    return list(by_name.values())


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_ensure(source_raw: str) -> None:
    owner_repo, _ = normalize_source(source_raw)
    cache, action = ensure_repo(owner_repo)
    emit(
        {
            "ok": True,
            "source": owner_repo,
            "cache_dir": str(cache.resolve()),
            "action": action,
        }
    )


def cmd_update(source_raw: str) -> None:
    owner_repo, _ = normalize_source(source_raw)  # strips @skill
    cache, action = update_repo(owner_repo)
    emit(
        {
            "ok": True,
            "source": owner_repo,
            "cache_dir": str(cache.resolve()),
            "action": action,
        }
    )


def cmd_list(source_raw: str) -> None:
    owner_repo, _ = normalize_source(source_raw)
    cache, action = ensure_repo(owner_repo)
    skills = discover_skills(cache)
    emit(
        {
            "ok": True,
            "source": owner_repo,
            "cache_dir": str(cache.resolve()),
            "action": action,
            "skills": skills,
            "needs_choice": len(skills) > 1,
        }
    )


def cmd_resolve(source_raw: str) -> None:
    owner_repo, skill_name = normalize_source(source_raw)
    cache, action = ensure_repo(owner_repo)
    skills = discover_skills(cache)

    if not skills:
        fail(
            f"No SKILL.md found in {owner_repo} (cache: {cache}).",
            source=owner_repo,
            cache_dir=str(cache.resolve()),
        )

    if skill_name:
        match = next((s for s in skills if s["name"].lower() == skill_name.lower()), None)
        if not match:
            fail(
                f"Skill '{skill_name}' not found in {owner_repo}.",
                source=owner_repo,
                cache_dir=str(cache.resolve()),
                skills=skills,
                requested=skill_name,
            )
        emit(
            {
                "ok": True,
                "source": owner_repo,
                "cache_dir": str(cache.resolve()),
                "action": action,
                "skill": match,
                "needs_choice": False,
            }
        )

    if len(skills) == 1:
        emit(
            {
                "ok": True,
                "source": owner_repo,
                "cache_dir": str(cache.resolve()),
                "action": action,
                "skill": skills[0],
                "needs_choice": False,
            }
        )

    emit(
        {
            "ok": True,
            "source": owner_repo,
            "cache_dir": str(cache.resolve()),
            "action": action,
            "skills": skills,
            "needs_choice": True,
        }
    )


def usage() -> None:
    fail(
        "Usage: skill_run.py <ensure|update|list|resolve> <owner/repo[@skill]>",
    )


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        usage()
    cmd = argv[1].lower()
    source = argv[2]
    if cmd == "ensure":
        cmd_ensure(source)
    elif cmd == "update":
        cmd_update(source)
    elif cmd == "list":
        cmd_list(source)
    elif cmd == "resolve":
        cmd_resolve(source)
    else:
        fail(f"Unknown command '{cmd}'. Use ensure, update, list, or resolve.")


if __name__ == "__main__":
    main(sys.argv)
