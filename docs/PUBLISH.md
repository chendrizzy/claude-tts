# Publishing claude-tts

> **Boundary:** this repository ships *publish-readiness*. Creating the remote
> and pushing are **manual maintainer steps** — automated agents in this project
> never push or create remotes.

## One-time: create the remote

```bash
gh repo create chendrizzy/claude-tts --public --source=. --remote=origin --push
```

(or create the repo in the GitHub UI and `git remote add origin …` + `git push -u origin main`).

## Cut a release

1. Bump the version in `pyproject.toml` (`[project].version`).
2. Sync the manifests: `make manifests` (rewrites `.claude-plugin/{plugin,marketplace}.json`).
3. Verify: `make verify` (the drift-guard test fails if any version is out of sync).
4. Commit: `git commit -am "release: vX.Y.Z"`.
5. Tag and push: `git tag vX.Y.Z && git push origin main --tags`.

The tag push triggers `.github/workflows/release.yml`, which re-checks that the
tag equals `pyproject` version, runs the gate, and publishes a GitHub release
with a source tarball.

## Marketplace registration

`.claude-plugin/marketplace.json` declares the plugin. To make it installable via
the Claude Code marketplace, add this repo as a marketplace source in Claude Code
(`/plugin marketplace add chendrizzy/claude-tts`) or submit it per the current
marketplace process. The plugin installs from `source: "./"` at the tagged commit.

## What is intentionally NOT automated

- **No PyPI publish** — `pyproject.toml` sets `package = false` (this is an
  application/plugin, not an importable library).
- **No auto-push** — releases are maintainer-initiated tag pushes.
