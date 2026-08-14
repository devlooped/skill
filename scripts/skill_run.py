#!/usr/bin/env python3
"""Ephemeral remote skill download / discover / resolve helper.

Usage:
    skill_run.py ensure  <owner/repo>
    skill_run.py update  [<owner/repo[@skill]>]
    skill_run.py list    [<owner/repo>]
    skill_run.py resolve <owner/repo[@skill]>

Prints JSON on stdout. Exit 0 on success, 1 on error.
With bare `update` (no source), refreshes every previously cached repo
and prints progress on stderr.
With bare `list` (no source), lists every skill under previously cached repos.
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


def normalize_source(raw: str) -> tuple[str, str | None, str | None]:
    """Return (owner/repo, optional_skill_name, optional_clone_url).

    Accepts owner/repo, owner/repo@skill, GitHub HTTPS / git@ URLs, and
    GitHub Gist URLs (https://gist.github.com/user/gist_id).  The
    clone_url is non-None only for gist sources.
    """
    s = raw.strip().rstrip("/")
    skill: str | None = None

    if "@" in s and not s.startswith("http"):
        # owner/repo@skill — only split on the last @ after a slash path
        base, maybe_skill = s.rsplit("@", 1)
        if "/" in base and maybe_skill and "/" not in maybe_skill:
            s = base
            skill = maybe_skill.strip() or None

    # https://gist.github.com/user/gist_id
    m = re.match(
        r"^(?:https?://)?gist\.github\.com/([^/]+)/([A-Za-z0-9]+)",
        s,
        re.IGNORECASE,
    )
    if m:
        user = m.group(1)
        gist_id = m.group(2)
        return f"{user}/{gist_id}", skill, f"https://gist.github.com/{gist_id}.git"

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
        return f"{owner}/{repo}", skill, None

    # git@github.com:owner/repo.git
    m = re.match(r"^git@github\.com:([^/]+)/([^/#?]+?)(?:\.git)?$", s, re.IGNORECASE)
    if m:
        return f"{m.group(1)}/{m.group(2)}", skill, None

    # owner/repo
    m = re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}", skill, None

    fail(
        f"Invalid source '{raw}'. Expected owner/repo, owner/repo@skill, "
        "a GitHub URL, or a GitHub Gist URL (https://gist.github.com/user/id)."
    )


def cache_root() -> Path:
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or os.environ.get("TMPDIR") or "/tmp"
    return Path(temp) / "skills"


def cache_dir_for(owner_repo: str) -> Path:
    safe = owner_repo.replace("/", "__")
    return cache_root() / safe


def owner_repo_from_cache_dir(cache: Path) -> str | None:
    """Reverse cache folder name ``owner__repo`` → ``owner/repo``."""
    name = cache.name
    if "__" not in name:
        return None
    owner, repo = name.split("__", 1)
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def list_cached_repos() -> list[str]:
    """Return owner/repo strings for every non-empty cache under $TEMP/skills."""
    root = cache_root()
    if not root.is_dir():
        return []
    repos: list[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or not has_cache(child):
            continue
        owner_repo = owner_repo_from_cache_dir(child)
        if owner_repo:
            repos.append(owner_repo)
    return repos


def progress(msg: str) -> None:
    """Human-readable progress on stderr (keeps stdout JSON-clean)."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


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


def clone_repo(owner_repo: str, cache: Path, clone_url: str | None = None) -> str:
    """Clone into cache. Returns method used: 'gh' or 'git'. Exits on failure."""
    method = soft_clone_repo(owner_repo, cache, clone_url)
    if method.startswith("error:"):
        fail(method[len("error:") :].strip(), source=owner_repo)
    return method


def ensure_repo(owner_repo: str, clone_url: str | None = None) -> tuple[Path, str]:
    """Ensure cache exists. Returns (cache_dir, action)."""
    cache = cache_dir_for(owner_repo)
    if has_cache(cache):
        return cache, "cached"
    method = clone_repo(owner_repo, cache, clone_url)
    return cache, f"cloned:{method}"


def update_repo(owner_repo: str, clone_url: str | None = None) -> tuple[Path, str]:
    """Refresh whole-repo cache. Strips skill selection — always repo-level.

    On failure calls fail() (exits). Prefer soft_update_repo for batch mode.
    """
    cache, action, err = soft_update_repo(owner_repo, clone_url)
    if err is not None:
        fail(err, source=owner_repo, cache_dir=str(cache.resolve()) if cache else None)
    assert cache is not None and action is not None
    return cache, action


def soft_update_repo(
    owner_repo: str, clone_url: str | None = None
) -> tuple[Path | None, str | None, str | None]:
    """Like update_repo but returns (cache, action, error) instead of exiting."""
    cache = cache_dir_for(owner_repo)
    git = which("git")

    if has_cache(cache) and (cache / ".git").exists() and git:
        result = run_cmd([git, "pull", "--ff-only"], cwd=cache)
        if result.returncode == 0:
            return cache, "pulled", None
        # fall through to re-clone

    try:
        method = soft_clone_repo(owner_repo, cache, clone_url)
        if method.startswith("error:"):
            return cache, None, method[len("error:") :].strip()
        return cache, f"recloned:{method}", None
    except Exception as e:  # noqa: BLE001
        return cache, None, str(e)


def remove_dir(path: Path) -> str | None:
    """Best-effort recursive delete. Returns error message or None on success."""
    if not path.exists():
        return None

    def _onerror(func: Any, p: str, _exc: Any) -> None:
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onerror=_onerror)
    except OSError:
        pass

    if not path.exists():
        return None

    # Windows fallback: cmd rmdir (handles stubborn dirs better than shutil alone)
    if os.name == "nt":
        result = run_cmd(["cmd", "/c", "rmdir", "/s", "/q", str(path)])
        if not path.exists():
            return None
        detail = (result.stderr or result.stdout or "").strip()
        return detail or f"Could not remove {path}"

    return f"Could not remove {path}"


def soft_clone_repo(owner_repo: str, cache: Path, clone_url: str | None = None) -> str:
    """Clone into cache. Returns method ('gh'|'git') or 'error: …'."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        err = remove_dir(cache)
        if err or cache.exists():
            return (
                f"error: Failed to clear cache dir {cache}"
                + (f": {err}" if err else "")
            )

    gh = which("gh")
    if gh:
        if clone_url and "gist.github.com" in clone_url:
            # Extract gist_id from clone_url (https://gist.github.com/<id>.git)
            gist_id = clone_url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
            result = run_cmd([gh, "gist", "clone", gist_id, str(cache)])
        else:
            result = run_cmd([gh, "repo", "clone", owner_repo, str(cache), "--", "--depth", "1"])
        if result.returncode == 0 and has_cache(cache):
            return "gh"
        # fall through to git on gh failure

    git = which("git")
    if not git:
        return (
            "error: Neither 'gh' nor 'git' is available. "
            "Install GitHub CLI (gh) or git to download skills."
        )

    url = clone_url or f"https://github.com/{owner_repo}.git"
    result = run_cmd([git, "clone", "--depth", "1", url, str(cache)])
    if result.returncode != 0 or not has_cache(cache):
        detail = (result.stderr or result.stdout or "").strip()
        return (
            f"error: Failed to clone {owner_repo}. {detail or 'Unknown error'}. "
            "For private repos, run: gh auth login"
        )
    return "git"


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
    owner_repo, _, clone_url = normalize_source(source_raw)
    cache, action = ensure_repo(owner_repo, clone_url)
    emit(
        {
            "ok": True,
            "source": owner_repo,
            "cache_dir": str(cache.resolve()),
            "action": action,
        }
    )


def cmd_update(source_raw: str | None) -> None:
    """Update one repo, or every cached repo when source is omitted."""
    if source_raw is None or not source_raw.strip():
        cmd_update_all()
        return

    owner_repo, _, clone_url = normalize_source(source_raw)  # strips @skill
    cache, action = update_repo(owner_repo, clone_url)
    emit(
        {
            "ok": True,
            "mode": "single",
            "source": owner_repo,
            "cache_dir": str(cache.resolve()),
            "action": action,
        }
    )


def cmd_update_all() -> None:
    """Refresh every previously cached skill repo; progress on stderr."""
    repos = list_cached_repos()
    root = cache_root()
    root_str = str(root.resolve()) if root.exists() else str(root)

    if not repos:
        progress("No cached skill repos found.")
        emit(
            {
                "ok": True,
                "mode": "all",
                "message": "No cached skill repos found under $TEMP/skills.",
                "cache_root": root_str,
                "updated": 0,
                "failed": 0,
                "results": [],
            }
        )

    total = len(repos)
    progress(f"Updating {total} cached skill repo(s)…")
    results: list[dict[str, Any]] = []
    failed = 0

    for i, owner_repo in enumerate(repos, start=1):
        progress(f"[{i}/{total}] {owner_repo} …")
        cache, action, err = soft_update_repo(owner_repo)
        if err is not None:
            failed += 1
            results.append(
                {
                    "ok": False,
                    "source": owner_repo,
                    "cache_dir": str(cache.resolve()) if cache else None,
                    "message": err,
                }
            )
            progress(f"[{i}/{total}] {owner_repo} → error: {err}")
            continue

        assert cache is not None and action is not None
        results.append(
            {
                "ok": True,
                "source": owner_repo,
                "cache_dir": str(cache.resolve()),
                "action": action,
            }
        )
        progress(f"[{i}/{total}] {owner_repo} → {action}")

    updated = total - failed
    progress(f"Done: {updated} updated, {failed} failed (of {total}).")
    emit(
        {
            "ok": failed == 0,
            "mode": "all",
            "cache_root": root_str,
            "updated": updated,
            "failed": failed,
            "results": results,
        },
        exit_code=0 if failed == 0 else 1,
    )


def cmd_list(source_raw: str | None) -> None:
    """List skills in one repo, or every cached skill when source is omitted."""
    if source_raw is None or not source_raw.strip():
        cmd_list_cached()
        return

    owner_repo, _, clone_url = normalize_source(source_raw)
    cache, action = ensure_repo(owner_repo, clone_url)
    skills = discover_skills(cache)
    emit(
        {
            "ok": True,
            "mode": "single",
            "source": owner_repo,
            "cache_dir": str(cache.resolve()),
            "action": action,
            "skills": skills,
            "needs_choice": len(skills) > 1,
        }
    )


def cmd_list_cached() -> None:
    """Discover skills in every previously cached repo (no network)."""
    repos = list_cached_repos()
    root = cache_root()
    root_str = str(root.resolve()) if root.exists() else str(root)

    skills_out: list[dict[str, str]] = []
    for owner_repo in repos:
        cache = cache_dir_for(owner_repo)
        if not has_cache(cache):
            continue
        for s in discover_skills(cache):
            name = s["name"]
            skills_out.append(
                {
                    "source": owner_repo,
                    "name": name,
                    "description": s["description"],
                    "path": s["path"],
                    "dir": s["dir"],
                    # Unambiguous pick key for agents / users
                    "ref": f"{owner_repo}@{name}",
                }
            )

    if not skills_out:
        emit(
            {
                "ok": True,
                "mode": "cached",
                "message": "No cached skills found under $TEMP/skills.",
                "cache_root": root_str,
                "count": 0,
                "skills": [],
            }
        )

    emit(
        {
            "ok": True,
            "mode": "cached",
            "cache_root": root_str,
            "count": len(skills_out),
            "skills": skills_out,
        }
    )


def cmd_resolve(source_raw: str) -> None:
    owner_repo, skill_name, clone_url = normalize_source(source_raw)
    cache, action = ensure_repo(owner_repo, clone_url)
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
        "Usage: skill_run.py <ensure|update|list|resolve> [owner/repo[@skill]]\n"
        "  update with no source refreshes every previously cached repo.\n"
        "  list with no source lists every skill under previously cached repos.",
    )


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        usage()
    cmd = argv[1].lower()
    source = argv[2] if len(argv) >= 3 else None

    if cmd == "ensure":
        if not source:
            fail("Usage: skill_run.py ensure <owner/repo>")
        cmd_ensure(source)
    elif cmd == "update":
        cmd_update(source)
    elif cmd == "list":
        cmd_list(source)
    elif cmd == "resolve":
        if not source:
            fail("Usage: skill_run.py resolve <owner/repo[@skill]>")
        cmd_resolve(source)
    else:
        fail(f"Unknown command '{cmd}'. Use ensure, update, list, or resolve.")


if __name__ == "__main__":
    main(sys.argv)
