---
name: skill
description: >
  Ephemerally download and run a remote agent skill from a GitHub repo without
  permanent install. Use when the user runs /skill, /skill update, wants to try
  a skill from owner/repo or owner/repo@name, or run a skill once without
  npx skills add.
argument-hint: "[update] [owner/repo[@skill-name]] [args...]"
disable-model-invocation: true
---

# /skill — ephemeral remote skill runner

Download a skill repo into a **temporary** cache, pick a skill, load its `SKILL.md`, and execute it as if it were pre-installed. Does **not** permanently install into agent skill directories (so other conversations keep a small skills listing).

## Usage

```text
/skill
/skill <owner/repo> [args...]
/skill <owner/repo@skill-name> [args...]
/skill update <owner/repo>
/skill update
```

| Form | Behavior |
|------|----------|
| *(no args)* | List cached skills; **(1)** ask which skill; **(2)** ask for args to pass it (may be empty); then run |
| `owner/repo` | Ensure cache; if one skill → run it; if many → ask user (name + description) |
| `owner/repo@skill-name` | Ensure cache; run that skill; remaining tokens are skill args |
| `update owner/repo` | Refresh the **whole** repo cache only (no skill execution). Strip `@…` if present |
| `update` (no source) | Refresh **every** previously cached repo under `$TEMP/skills/`; script prints progress |

Skill selection uses **`@` only** (npx skills style). There is no separate skill-name positional argument.

Also accept GitHub HTTPS / `git@` URLs and GitHub Gist URLs (`https://gist.github.com/user/id`); normalize to `owner/repo`.

## Paths

This skill’s directory (absolute): the folder containing this `SKILL.md`.

Helper scripts:

- `<skill-dir>/scripts/skill_run.py`
- `<skill-dir>/scripts/skill_run.ts`

Cache (shared, agent-agnostic):

```text
$TEMP/skills/<owner>__<repo>/
```

- Windows: `$env:TEMP\skills\...`
- Unix: `${TMPDIR:-/tmp}/skills/...`

Never copy skills into permanent roots (`~/.grok/skills`, `.agents/skills`, `.claude/skills`, etc.).

## Runtime selection

Run helpers via shell. Prefer **Python**, then **TypeScript**:

1. If `python` or `python3` works:
   ```bash
   python <skill-dir>/scripts/skill_run.py <cmd> <source>
   ```
   (On Windows try `python` first; on Unix `python3` then `python`.)

2. Else if Node is available:
   ```bash
   npx --yes tsx <skill-dir>/scripts/skill_run.ts <cmd> <source>
   ```

3. Else stop: need Python 3 or Node.js.

Always use **absolute paths** to the scripts.

### Commands (JSON on stdout)

| Command | Purpose |
|---------|---------|
| `ensure <owner/repo>` | Clone if missing; do **not** refresh if present |
| `update <owner/repo[@skill]>` | Pull or re-clone whole repo; ignore `@skill` |
| `update` (no source) | Update **all** dirs under `$TEMP/skills/`; progress on **stderr**; summary JSON on stdout |
| `list <owner/repo>` | Ensure + list skills in that repo |
| `list` (no source) | List **every** skill under previously cached repos (no network) |
| `resolve <owner/repo[@skill]>` | Ensure + select one skill or `needs_choice` |

Parse stdout as JSON. On `ok: false` or non-zero exit, show `message` (and per-repo `results` when present) and stop.

**Update-all JSON** (`mode: "all"`): `updated`, `failed`, `cache_root`, `results[]` each with `ok`, `source`, `action` or `message`. Progress lines on stderr look like `[1/3] owner/repo → pulled`.

**Cached-list JSON** (`mode: "cached"`): `count`, `cache_root`, `skills[]` each with `source`, `name`, `description`, `path`, `dir`, `ref` (`owner/repo@name`). Empty cache → `count: 0` and a `message`.

Download prefers **`gh repo clone`**, then **`git clone --depth 1`**. Private repos need `gh auth login`.

## Workflow

### 1. Parse the user invocation

Tokens after `/skill`:

- **`update` first** → update mode. Source = second token if present; if omitted, update **all** cached repos. Strip `@…` for a single-repo helper call (helper also strips). Do not run a remote skill afterward.
- **Otherwise** → run mode. Source = first token (`owner/repo` or `owner/repo@name`) if present. All remaining tokens = **skill args**.
- **No tokens** → cached-pick mode (list cache, ask user, then run).

Bare `/skill update` is valid (update-all). Bare `/skill` is valid (cached pick).

### 2. Call the helper

**Update mode (single repo):**

```text
skill_run update <owner/repo>
```

Report `cache_dir` and `action` (`pulled`, `recloned:gh`, etc.). Stop.

**Update mode (all cached repos — no source):**

```text
skill_run update
```

Do **not** invent a default owner/repo and do **not** loop in the agent — the script enumerates `$TEMP/skills/*`, updates each, and streams progress on stderr. After it finishes, summarize from the JSON (`updated` / `failed` / each `results[]` entry). Surface stderr progress to the user while it runs when practical (or report the final per-repo lines). Stop; do not execute a remote skill.

**Cached-pick mode (bare `/skill`):**

```text
skill_run list
```

Then follow **§3b**. Do **not** invent a default owner/repo.

**Run mode (source given):**

```text
skill_run resolve <owner/repo[@skill]>
```

### 3. Handle resolve result

| Result | Action |
|--------|--------|
| `ok: false` | Show `message` (and `skills` if present); stop |
| `needs_choice: false` | Use `skill.path` and `skill.dir` |
| `needs_choice: true` | Disambiguate (below) |
| Unknown `@name` | Error includes available `skills`; list them and stop or ask |

**Disambiguation:** When multiple skills and no `@name`, present each skill’s **name** and **description** (from the JSON). Use a structured choice prompt when available; otherwise a clear numbered list. After the user picks, use that skill’s `path` / `dir` from the list (or re-run `resolve owner/repo@chosen-name`).

### 3b. Handle cached-list result (bare `/skill`)

| Result | Action |
|--------|--------|
| `ok: false` | Show `message`; stop |
| `count: 0` / empty `skills` | Tell the user the ephemeral cache is empty. Show how to populate it (`/skill owner/repo`) and the usage block. Stop. |
| `count >= 1` | Run the **two-step prompt** below — skill pick, then args. **Do not skip step 2.** |

Cached-pick is **always two explicit questions**, in order. Do **not** fold args into the skill menu’s “Other” / free-text escape hatch as the only way to pass them.

#### Step 1 — Which skill?

For each entry in `skills[]`, show:

- **Label / pick key:** `ref` (`owner/repo@name`) — preferred; falls back to `source@name` if `ref` missing
- **Description:** `description`
- Optionally group by `source` if many entries

Use a structured choice prompt when available; otherwise a clear numbered list.  
This step is **skill only** — do not ask for args here and do not require the user to type `ref arg1 arg2` in “Other” just to supply args.

#### Step 2 — Args for that skill? (required prompt; empty allowed)

**After** the skill is chosen, always ask a **second, separate** question for arguments to forward to that skill. Examples of acceptable prompts:

- “Arguments to pass to `<ref>`? (leave empty for none)”
- “Any args for this skill? Press enter / leave blank for none.”

Rules:

1. **Always ask step 2** — even when you expect no args. Never jump straight to §4 after step 1.
2. Empty / blank / “none” / skip → skill args = empty list. That is a valid answer.
3. Non-empty reply → entire reply is the skill args string (tokenize if needed); do not re-parse it as a new `owner/repo`.
4. Prefer a free-text / open input for step 2 when the host supports it. If only multiple-choice exists, offer at least: **“No args”** (default) and **“Other…”** (type args).
5. Do **not** rely on step 1’s “Other” as the primary args path. “Other” on step 1 is only for typing a `ref` (or `owner/repo@name`) not listed.

After both answers:

1. Resolve the pick to `path` / `dir` from the matching `skills[]` entry when possible; otherwise `skill_run resolve <ref>` and continue from §3.
2. Continue to §4 with the args from step 2.

Do **not** clone new repos in this mode unless you re-enter run mode with an explicit `owner/repo` the user typed that is not in the cache.

### 4. Load and execute

1. `read_file` the selected skill’s `SKILL.md` **in full** (`skill.path`).
2. Treat that body as the active procedure — same as if the user had run `/<skill-name> <args>`.
3. Resolve relative paths (`scripts/`, `references/`, etc.) against `skill.dir`.
4. Briefly announce: source, skill name, cache path, and forwarded args.
5. Do **not** install into permanent skill dirs and do **not** ask the user to restart the CLI for “reload.”

### 5. Cache policy

- Reuse `$TEMP/skills/...` across runs (faster; other tools may share it).
- Refresh on `/skill update <owner/repo>` (one repo) or `/skill update` (every previously cached repo).
- Cache is disposable; the user may delete `$TEMP/skills` anytime.

## Trust note

Remote skills are untrusted third-party instructions (same class of risk as `npx skills add`). Follow the remote skill’s steps with normal tool/permission caution. Do not permanently install unless the user explicitly asks for a lasting install (then point them at `npx skills add` or their agent’s install path).

## Examples

```text
/skill
/skill microsoft/playwright-cli
/skill vercel-labs/agent-skills
/skill vercel-labs/agent-skills@web-design-guidelines
/skill vercel-labs/agent-skills@web-design-guidelines src/**/*.tsx
/skill update vercel-labs/agent-skills
/skill update vercel-labs/agent-skills@ignored
/skill update
```

Bare `/skill` lists cached skills, asks which to run, then **always** asks for args in a second prompt (empty = none).  
`update …@ignored` still refreshes the entire `vercel-labs/agent-skills` cache only. Bare `/skill update` refreshes every folder under `$TEMP/skills/`.
