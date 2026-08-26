# IDEA-006: Run `ruff format` and enforce it in CI

- **Status:** Proposed
- **Created:** 2026-08-26
- **Updated:** 2026-08-26

## Context

`pyproject.toml` declares `[tool.ruff] line-length = 120`, but the formatter has
never been run against this repo. The code is wrapped at ruff's **default 88**
columns, so the declared configuration and the actual code disagree. Nothing
surfaces the mismatch: `.gitlab-ci.yml`'s `lint` job runs `ruff check .` but not
`ruff format --check`, and this repo has no `.claude/pre-commit-check.py` at all.
Measured 2026-08-26:

```console
$ uv run ruff format --check .
5 files would be reformatted, 3 files already formatted
```

This came out of `certinext-spec-watch`, whose copied pre-commit hook included a
`ruff format --check` step that failed against its own tree from the very first
commit — a gate that could never pass. `certinext` has the same mismatch at much
larger scale (65 of 79 files); see its `IDEA-013`, which carries the full option
comparison and the measurement showing that the inverse fix — setting
`line-length = 88` — is *more* work rather than less.

## The idea

1. Run `uv run ruff format .` and land it as a single `style:` commit.
2. Add `.git-blame-ignore-revs` at the repo root naming that commit.
3. Add `uv run ruff format --check .` to the `lint` job in `.gitlab-ci.yml`, and
   to `.github/workflows/ci.yml`'s `lint` job.

`certinext-spec-watch` is the worked example of the end state.

## Why not now

Nothing here is expensive — 5 files is not the 65 that makes `certinext` a
scheduling problem, and this repo could absorb the churn on any quiet day.

The reason to hold is **consistency across the two repos**, not cost. This repo
was split from `certinext` and deliberately tracks its conventions
([ADR 0002](../adr/0002-copy-now-canonical-later.md)); formatting one and not the
other creates exactly the copy-a-hook-and-it-fails trap in reverse — a hook
copied *from* here into `certinext` would then be the broken one. Doing both at
once costs nothing extra and keeps a single answer to "how is this code
formatted".

**What would change this:** `certinext` reaching the quiet point its IDEA-013
waits for. Land both in the same sitting. If that idea is instead rejected, this
one should be too, for the same reason.

## Pros

- 5 files, so the churn argument barely applies here.
- Makes the declared `line-length = 120` true rather than aspirational.
- One mechanical rule for contributors instead of a reviewer's judgement about
  line wrapping — this repo push-mirrors to public GitHub.
- Closes the copied-hook trap in both directions.

## Cons / costs

- Minor `git blame` noise, mitigated by `.git-blame-ignore-revs` (whether
  GitLab's blame view honours it on this instance is unverified).
- Some 88-column wrapping reads better than the 120-column collapse the
  formatter produces.
- Coupling to `certinext`'s schedule means this stays parked longer than its own
  cost justifies.

## Effort

Very small: one formatter run, one commit, two CI lines, one
`.git-blame-ignore-revs`. The coupling to `certinext` is the only reason it is
not done already.

## Open questions & caveats

- Should this repo also gain a `.claude/pre-commit-check.py`? It is the only one
  of the related repos without one, so local checks run only via the global
  dispatcher's pyproject-driven fallback.
- Does GitLab's blame view here honour a repo-root `.git-blame-ignore-revs`?
  Unverified.

## Next steps

None until `certinext` IDEA-013 is scheduled. Land both together.

## References

- [ruff formatter](https://docs.astral.sh/ruff/formatter/)
- [ruff configuration](https://docs.astral.sh/ruff/configuration/) — `line-length`
- [git blame `--ignore-revs-file`](https://git-scm.com/docs/git-blame#Documentation/git-blame.txt---ignore-revs-fileltfilegt)
- `certinext` `docs/wishlist/IDEA-013-enforce-ruff-format.md` — the fuller
  analysis this idea defers to (GitLab: `sysadmin/python-libs/certinext#33`).

---

> **AI-assistant disclaimer:** Drafted by Claude Code (Claude Opus 5 (1M context),
> `claude-opus-5[1m]`) from a conversation with Tod Detre. May contain inaccuracies
> or hallucinated details; verify specifics against current sources before relying
> on them.
