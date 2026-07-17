# skill

Ephemeral remote skill runner for agent CLIs. Run a skill from any GitHub repo
**once** without permanently installing it into your agent skills directory.

## Install

```bash
npx skills add devlooped/skill
```

Use `-g` for a user-global install, or `--agent grok` / `--agent '*'` as needed.

## Usage

In an agent that supports skills (e.g. Grok Build):

```text
/skill <owner/repo> [args...]
/skill <owner/repo@skill-name> [args...]
/skill update <owner/repo>
/skill update
```

| Form | Behavior |
|------|----------|
| `owner/repo` | Download (or reuse cache); if one skill → run it; if many → ask which |
| `owner/repo@skill-name` | Run that skill; remaining tokens are skill args |
| `update owner/repo` | Refresh the whole repo cache only (no skill execution) |
| `update` (no source) | Refresh every previously cached repo under `$TEMP/skills/` (progress on stderr) |

Also accepts GitHub HTTPS / `git@` URLs (normalized to `owner/repo`).

### Examples

```text
/skill microsoft/playwright-cli
/skill vercel-labs/agent-skills
/skill vercel-labs/agent-skills@web-design-guidelines
/skill vercel-labs/agent-skills@web-design-guidelines src/**/*.tsx
/skill update vercel-labs/agent-skills
/skill update
```

## How it works

1. Clones the target repo into a disposable cache under `$TEMP/skills/` (or `%TEMP%\skills\` on Windows).
2. Discovers skills in common layouts (`skills/`, `.agents/skills/`, root `SKILL.md`, etc.).
3. Loads the selected `SKILL.md` and runs it as if it were installed — **without** copying into permanent skill roots.

Cache is reused across runs; refresh with `/skill update <owner/repo>` or `/skill update` (all cached).

Helpers (JSON on stdout; update-all progress on stderr):

```bash
python scripts/skill_run.py resolve owner/repo[@skill]
python scripts/skill_run.py update              # all cached repos
# or: npx --yes tsx scripts/skill_run.ts …
```

Commands: `ensure`, `update` (optional source), `list`, `resolve`. Prefer `gh repo clone`, fall back to `git clone --depth 1`.

## Requirements

- Python 3 **or** Node.js (for the helper scripts)
- `gh` (preferred) or `git` for cloning
- An agent host that can load this skill and call the helpers (Grok Build, etc.)

## Trust

Remote skills are untrusted third-party instructions (same class of risk as
`npx skills add`). Review `SKILL.md` before running sensitive workflows.

## License

MIT — see [license.txt](license.txt).
