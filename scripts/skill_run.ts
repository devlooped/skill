#!/usr/bin/env npx tsx
/**
 * Ephemeral remote skill download / discover / resolve helper.
 *
 * Usage:
 *   skill_run.ts ensure  <owner/repo>
 *   skill_run.ts update  [<owner/repo[@skill]>]
 *   skill_run.ts list    [<owner/repo>]
 *   skill_run.ts resolve <owner/repo[@skill]>
 *
 * Prints JSON on stdout. Exit 0 on success, 1 on error.
 * Bare `update` (no source) refreshes every previously cached repo
 * and prints progress on stderr.
 * Bare `list` (no source) lists every skill under previously cached repos.
 */

import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve as pathResolve } from "node:path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type SkillEntry = {
  name: string;
  description: string;
  path: string;
  dir: string;
};

type JsonPayload = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SKILL_CONTAINERS = [
  "skills",
  ".agents/skills",
  ".grok/skills",
  ".claude/skills",
  ".cursor/skills",
] as const;

const DESC_MAX = 280;

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

function emit(payload: JsonPayload, exitCode = 0): never {
  process.stdout.write(JSON.stringify(payload, null, 2) + "\n");
  process.exit(exitCode);
}

function fail(message: string, extra: JsonPayload = {}): never {
  emit({ ok: false, message, ...extra }, 1);
}

// ---------------------------------------------------------------------------
// Source / cache
// ---------------------------------------------------------------------------

function normalizeSource(raw: string): { ownerRepo: string; skill: string | null } {
  let s = raw.trim().replace(/\/+$/, "");
  let skill: string | null = null;

  if (s.includes("@") && !s.startsWith("http")) {
    const at = s.lastIndexOf("@");
    const base = s.slice(0, at);
    const maybe = s.slice(at + 1).trim();
    if (base.includes("/") && maybe && !maybe.includes("/")) {
      s = base;
      skill = maybe || null;
    }
  }

  let m = s.match(/^(?:https?:\/\/)?(?:www\.)?github\.com\/([^/]+)\/([^/#?]+)/i);
  if (m) {
    let repo = m[2];
    if (repo.endsWith(".git")) repo = repo.slice(0, -4);
    return { ownerRepo: `${m[1]}/${repo}`, skill };
  }

  m = s.match(/^git@github\.com:([^/]+)\/([^/#?]+?)(?:\.git)?$/i);
  if (m) {
    return { ownerRepo: `${m[1]}/${m[2]}`, skill };
  }

  m = s.match(/^([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/);
  if (m) {
    return { ownerRepo: `${m[1]}/${m[2]}`, skill };
  }

  fail(
    `Invalid source '${raw}'. Expected owner/repo, owner/repo@skill, or a GitHub URL.`,
  );
}

function cacheRoot(): string {
  const temp =
    process.env.TEMP || process.env.TMP || process.env.TMPDIR || tmpdir();
  return join(temp, "skills");
}

function cacheDirFor(ownerRepo: string): string {
  return join(cacheRoot(), ownerRepo.replace("/", "__"));
}

/** Reverse cache folder name ``owner__repo`` → ``owner/repo``. */
function ownerRepoFromCacheDir(cache: string): string | null {
  const name = cache.split(/[/\\]/).pop() || "";
  if (!name.includes("__")) return null;
  const idx = name.indexOf("__");
  const owner = name.slice(0, idx);
  const repo = name.slice(idx + 2);
  if (!owner || !repo) return null;
  return `${owner}/${repo}`;
}

/** Return owner/repo strings for every non-empty cache under $TEMP/skills. */
function listCachedRepos(): string[] {
  const root = cacheRoot();
  if (!existsSync(root)) return [];
  let children: string[] = [];
  try {
    children = readdirSync(root).sort((a, b) =>
      a.toLowerCase().localeCompare(b.toLowerCase()),
    );
  } catch {
    return [];
  }
  const repos: string[] = [];
  for (const name of children) {
    const child = join(root, name);
    if (!hasCache(child)) continue;
    const ownerRepo = ownerRepoFromCacheDir(child);
    if (ownerRepo) repos.push(ownerRepo);
  }
  return repos;
}

/** Human-readable progress on stderr (keeps stdout JSON-clean). */
function progress(msg: string): void {
  process.stderr.write(msg + "\n");
}

function which(cmd: string): string | null {
  const isWin = process.platform === "win32";
  const checker = isWin ? "where" : "which";
  const r = spawnSync(checker, [cmd], { encoding: "utf-8" });
  if (r.status !== 0) return null;
  const line = (r.stdout || "").split(/\r?\n/).find((l) => l.trim());
  return line?.trim() || null;
}

function runCmd(
  args: string[],
  cwd?: string,
): { status: number; stdout: string; stderr: string } {
  const [cmd, ...rest] = args;
  const r = spawnSync(cmd, rest, {
    cwd,
    encoding: "utf-8",
    shell: false,
  });
  return {
    status: r.status ?? 1,
    stdout: r.stdout || "",
    stderr: r.stderr || "",
  };
}

// ---------------------------------------------------------------------------
// Clone / update
// ---------------------------------------------------------------------------

function hasCache(cache: string): boolean {
  if (!existsSync(cache)) return false;
  try {
    if (!statSync(cache).isDirectory()) return false;
  } catch {
    return false;
  }
  if (existsSync(join(cache, ".git"))) return true;
  try {
    return readdirSync(cache).length > 0;
  } catch {
    return false;
  }
}

/** Best-effort recursive delete. Returns error message or null on success. */
function removeDir(path: string): string | null {
  if (!existsSync(path)) return null;
  try {
    rmSync(path, { recursive: true, force: true });
  } catch {
    /* try fallback below */
  }
  if (!existsSync(path)) return null;

  if (process.platform === "win32") {
    const result = runCmd(["cmd", "/c", "rmdir", "/s", "/q", path]);
    if (!existsSync(path)) return null;
    const detail = (result.stderr || result.stdout || "").trim();
    return detail || `Could not remove ${path}`;
  }
  return `Could not remove ${path}`;
}

/** Clone into cache. Returns method ('gh'|'git') or 'error: …'. */
function softCloneRepo(ownerRepo: string, cache: string): string {
  mkdirSync(pathResolve(cache, ".."), { recursive: true });
  if (existsSync(cache)) {
    const err = removeDir(cache);
    if (err || existsSync(cache)) {
      return (
        `error: Failed to clear cache dir ${cache}` + (err ? `: ${err}` : "")
      );
    }
  }

  const gh = which("gh");
  if (gh) {
    const result = runCmd([
      gh,
      "repo",
      "clone",
      ownerRepo,
      cache,
      "--",
      "--depth",
      "1",
    ]);
    if (result.status === 0 && hasCache(cache)) return "gh";
  }

  const git = which("git");
  if (!git) {
    return (
      "error: Neither 'gh' nor 'git' is available. " +
      "Install GitHub CLI (gh) or git to download skills."
    );
  }

  const url = `https://github.com/${ownerRepo}.git`;
  const result = runCmd([git, "clone", "--depth", "1", url, cache]);
  if (result.status !== 0 || !hasCache(cache)) {
    const detail = (result.stderr || result.stdout || "").trim();
    return (
      `error: Failed to clone ${ownerRepo}. ${detail || "Unknown error"}. ` +
      "For private repos, run: gh auth login"
    );
  }
  return "git";
}

function cloneRepo(ownerRepo: string, cache: string): string {
  const method = softCloneRepo(ownerRepo, cache);
  if (method.startsWith("error:")) {
    fail(method.slice("error:".length).trim(), { source: ownerRepo });
  }
  return method;
}

function ensureRepo(ownerRepo: string): { cache: string; action: string } {
  const cache = cacheDirFor(ownerRepo);
  if (hasCache(cache)) return { cache, action: "cached" };
  const method = cloneRepo(ownerRepo, cache);
  return { cache, action: `cloned:${method}` };
}

function softUpdateRepo(
  ownerRepo: string,
): { cache: string | null; action: string | null; error: string | null } {
  const cache = cacheDirFor(ownerRepo);
  const git = which("git");

  if (hasCache(cache) && existsSync(join(cache, ".git")) && git) {
    const result = runCmd([git, "pull", "--ff-only"], cache);
    if (result.status === 0) return { cache, action: "pulled", error: null };
  }

  try {
    const method = softCloneRepo(ownerRepo, cache);
    if (method.startsWith("error:")) {
      return {
        cache,
        action: null,
        error: method.slice("error:".length).trim(),
      };
    }
    return { cache, action: `recloned:${method}`, error: null };
  } catch (e) {
    return { cache, action: null, error: String(e) };
  }
}

function updateRepo(ownerRepo: string): { cache: string; action: string } {
  const { cache, action, error } = softUpdateRepo(ownerRepo);
  if (error !== null) {
    fail(error, {
      source: ownerRepo,
      cache_dir: cache ? pathResolve(cache) : null,
    });
  }
  return { cache: cache!, action: action! };
}

// ---------------------------------------------------------------------------
// Frontmatter + discovery
// ---------------------------------------------------------------------------

function parseFrontmatter(text: string): Record<string, string> {
  if (!text.startsWith("---")) return {};
  const end = text.indexOf("\n---", 3);
  if (end < 0) return {};
  const block = text.slice(3, end).replace(/^\n/, "").replace(/\n$/, "");
  const fields: Record<string, string> = {};
  let currentKey: string | null = null;
  let currentParts: string[] = [];

  const flush = () => {
    if (currentKey !== null) {
      fields[currentKey] = currentParts.join("\n").trim();
    }
    currentKey = null;
    currentParts = [];
  };

  for (const line of block.split(/\r?\n/)) {
    if (
      currentKey &&
      (line.startsWith("  ") ||
        line.startsWith("\t") ||
        line.startsWith(">") ||
        line.trim() === "")
    ) {
      let cleaned = line.trim();
      if (cleaned.startsWith(">")) cleaned = cleaned.slice(1).trim();
      currentParts.push(cleaned);
      continue;
    }
    const m = line.match(/^([A-Za-z0-9_-]+)\s*:\s*(.*)$/);
    if (m) {
      flush();
      currentKey = m[1].trim().toLowerCase();
      const val = m[2].trim();
      if (val === ">" || val === "|" || val === ">-" || val === "|-") {
        currentParts = [];
      } else if (
        (val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'"))
      ) {
        currentParts = [val.slice(1, -1)];
        flush();
      } else {
        currentParts = val ? [val] : [];
        if (val) flush();
      }
      continue;
    }
    if (currentKey) currentParts.push(line.trim());
  }
  flush();
  return fields;
}

function firstParagraph(body: string): string {
  const lines: string[] = [];
  for (const line of body.split(/\r?\n/)) {
    const s = line.trim();
    if (!s) {
      if (lines.length) break;
      continue;
    }
    if (s.startsWith("#")) continue;
    lines.push(s);
  }
  return lines.join(" ");
}

function truncate(text: string, maxLen = DESC_MAX): string {
  const t = text.replace(/\s+/g, " ").trim();
  if (t.length <= maxLen) return t;
  return t.slice(0, maxLen - 1).replace(/\s+$/, "") + "…";
}

function skillEntry(skillMd: string, cache: string): SkillEntry {
  let text = "";
  try {
    text = readFileSync(skillMd, "utf-8");
  } catch (e) {
    return {
      name: pathResolve(skillMd, "..").split(/[/\\]/).pop() || "unknown",
      description: `(unreadable: ${e})`,
      path: pathResolve(skillMd),
      dir: pathResolve(skillMd, ".."),
    };
  }

  const fields = parseFrontmatter(text);
  let body = text;
  if (text.startsWith("---")) {
    const end = text.indexOf("\n---", 3);
    if (end >= 0) body = text.slice(end + 4);
  }

  const parentName =
    pathResolve(skillMd, "..").split(/[/\\]/).pop() || "skill";
  let name = (fields.name || parentName).trim();
  if (pathResolve(skillMd, "..") === pathResolve(cache)) {
    name = (fields.name || cache.split("__").pop() || parentName).trim();
  }

  const desc =
    fields.description || firstParagraph(body) || "(no description)";

  return {
    name,
    description: truncate(desc),
    path: pathResolve(skillMd),
    dir: pathResolve(skillMd, ".."),
  };
}

function discoverSkills(cache: string): SkillEntry[] {
  const found: string[] = [];
  const seen = new Set<string>();

  const add = (p: string) => {
    try {
      const rp = pathResolve(p);
      if (seen.has(rp) || !existsSync(p) || !statSync(p).isFile()) return;
      seen.add(rp);
      found.push(p);
    } catch {
      /* ignore */
    }
  };

  const rootSkill = join(cache, "SKILL.md");
  if (existsSync(rootSkill)) add(rootSkill);

  for (const container of SKILL_CONTAINERS) {
    const base = join(cache, container);
    if (!existsSync(base) || !statSync(base).isDirectory()) continue;

    let children: string[] = [];
    try {
      children = readdirSync(base).sort();
    } catch {
      continue;
    }

    for (const name of children) {
      const child = join(base, name);
      try {
        const st = statSync(child);
        if (st.isDirectory()) {
          const sm = join(child, "SKILL.md");
          if (existsSync(sm)) {
            add(sm);
          } else {
            let nested: string[] = [];
            try {
              nested = readdirSync(child).sort();
            } catch {
              nested = [];
            }
            for (const n of nested) {
              const nestedPath = join(child, n);
              try {
                if (statSync(nestedPath).isDirectory()) {
                  const nsm = join(nestedPath, "SKILL.md");
                  if (existsSync(nsm)) add(nsm);
                }
              } catch {
                /* ignore */
              }
            }
          }
        } else if (name === "SKILL.md" && st.isFile()) {
          add(child);
        }
      } catch {
        /* ignore */
      }
    }
  }

  const skills = found.map((p) => skillEntry(p, cache));
  const byName = new Map<string, SkillEntry>();
  for (const s of skills) {
    const key = s.name.toLowerCase();
    if (!byName.has(key)) byName.set(key, s);
  }
  return [...byName.values()];
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

function cmdEnsure(sourceRaw: string): never {
  const { ownerRepo } = normalizeSource(sourceRaw);
  const { cache, action } = ensureRepo(ownerRepo);
  emit({
    ok: true,
    source: ownerRepo,
    cache_dir: pathResolve(cache),
    action,
  });
}

function cmdUpdate(sourceRaw: string | null): never {
  if (sourceRaw === null || !sourceRaw.trim()) {
    cmdUpdateAll();
  }

  const { ownerRepo } = normalizeSource(sourceRaw!);
  const { cache, action } = updateRepo(ownerRepo);
  emit({
    ok: true,
    mode: "single",
    source: ownerRepo,
    cache_dir: pathResolve(cache),
    action,
  });
}

function cmdUpdateAll(): never {
  const repos = listCachedRepos();
  const root = cacheRoot();
  const rootStr = existsSync(root) ? pathResolve(root) : root;

  if (!repos.length) {
    progress("No cached skill repos found.");
    emit({
      ok: true,
      mode: "all",
      message: "No cached skill repos found under $TEMP/skills.",
      cache_root: rootStr,
      updated: 0,
      failed: 0,
      results: [],
    });
  }

  const total = repos.length;
  progress(`Updating ${total} cached skill repo(s)…`);
  const results: JsonPayload[] = [];
  let failed = 0;

  for (let i = 0; i < total; i++) {
    const ownerRepo = repos[i];
    const n = i + 1;
    progress(`[${n}/${total}] ${ownerRepo} …`);
    const { cache, action, error } = softUpdateRepo(ownerRepo);
    if (error !== null) {
      failed++;
      results.push({
        ok: false,
        source: ownerRepo,
        cache_dir: cache ? pathResolve(cache) : null,
        message: error,
      });
      progress(`[${n}/${total}] ${ownerRepo} → error: ${error}`);
      continue;
    }
    results.push({
      ok: true,
      source: ownerRepo,
      cache_dir: pathResolve(cache!),
      action,
    });
    progress(`[${n}/${total}] ${ownerRepo} → ${action}`);
  }

  const updated = total - failed;
  progress(`Done: ${updated} updated, ${failed} failed (of ${total}).`);
  emit(
    {
      ok: failed === 0,
      mode: "all",
      cache_root: rootStr,
      updated,
      failed,
      results,
    },
    failed === 0 ? 0 : 1,
  );
}

function cmdList(sourceRaw: string | null): never {
  if (sourceRaw === null || !sourceRaw.trim()) {
    cmdListCached();
  }

  const { ownerRepo } = normalizeSource(sourceRaw!);
  const { cache, action } = ensureRepo(ownerRepo);
  const skills = discoverSkills(cache);
  emit({
    ok: true,
    mode: "single",
    source: ownerRepo,
    cache_dir: pathResolve(cache),
    action,
    skills,
    needs_choice: skills.length > 1,
  });
}

function cmdListCached(): never {
  const repos = listCachedRepos();
  const root = cacheRoot();
  const rootStr = existsSync(root) ? pathResolve(root) : root;

  const skillsOut: JsonPayload[] = [];
  for (const ownerRepo of repos) {
    const cache = cacheDirFor(ownerRepo);
    if (!hasCache(cache)) continue;
    for (const s of discoverSkills(cache)) {
      skillsOut.push({
        source: ownerRepo,
        name: s.name,
        description: s.description,
        path: s.path,
        dir: s.dir,
        // Unambiguous pick key for agents / users
        ref: `${ownerRepo}@${s.name}`,
      });
    }
  }

  if (!skillsOut.length) {
    emit({
      ok: true,
      mode: "cached",
      message: "No cached skills found under $TEMP/skills.",
      cache_root: rootStr,
      count: 0,
      skills: [],
    });
  }

  emit({
    ok: true,
    mode: "cached",
    cache_root: rootStr,
    count: skillsOut.length,
    skills: skillsOut,
  });
}

function cmdResolve(sourceRaw: string): never {
  const { ownerRepo, skill: skillName } = normalizeSource(sourceRaw);
  const { cache, action } = ensureRepo(ownerRepo);
  const skills = discoverSkills(cache);

  if (!skills.length) {
    fail(`No SKILL.md found in ${ownerRepo} (cache: ${cache}).`, {
      source: ownerRepo,
      cache_dir: pathResolve(cache),
    });
  }

  if (skillName) {
    const match = skills.find(
      (s) => s.name.toLowerCase() === skillName.toLowerCase(),
    );
    if (!match) {
      fail(`Skill '${skillName}' not found in ${ownerRepo}.`, {
        source: ownerRepo,
        cache_dir: pathResolve(cache),
        skills,
        requested: skillName,
      });
    }
    emit({
      ok: true,
      source: ownerRepo,
      cache_dir: pathResolve(cache),
      action,
      skill: match,
      needs_choice: false,
    });
  }

  if (skills.length === 1) {
    emit({
      ok: true,
      source: ownerRepo,
      cache_dir: pathResolve(cache),
      action,
      skill: skills[0],
      needs_choice: false,
    });
  }

  emit({
    ok: true,
    source: ownerRepo,
    cache_dir: pathResolve(cache),
    action,
    skills,
    needs_choice: true,
  });
}

function main(argv: string[]): void {
  // argv: [node, script, cmd, source?] when run via node/tsx
  if (argv.length < 3) {
    fail(
      "Usage: skill_run.ts <ensure|update|list|resolve> [owner/repo[@skill]]\n" +
        "  update with no source refreshes every previously cached repo.\n" +
        "  list with no source lists every skill under previously cached repos.",
    );
  }
  const cmd = argv[2].toLowerCase();
  const source = argv.length >= 4 ? argv[3] : null;

  if (cmd === "ensure") {
    if (!source) fail("Usage: skill_run.ts ensure <owner/repo>");
    cmdEnsure(source);
  } else if (cmd === "update") {
    cmdUpdate(source);
  } else if (cmd === "list") {
    cmdList(source);
  } else if (cmd === "resolve") {
    if (!source) fail("Usage: skill_run.ts resolve <owner/repo[@skill]>");
    cmdResolve(source);
  } else {
    fail(`Unknown command '${cmd}'. Use ensure, update, list, or resolve.`);
  }
}

main(process.argv);
