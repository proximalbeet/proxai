# proxai-github

ProxAI reads your GitHub freely and changes it only with your yes.

| | What | Asks first? |
|---|---|---|
| Read | your repos, branches, files, commits, issues, PRs (files + comments), CI runs, code search | no |
| Write | open an issue, comment, create a `proxai/...` branch, commit a file to a `proxai/...` branch, open a **draft** PR from it | yes: your next message must be a plain "yes" (DMs, phone app or CLI only) |
| Never | push to `main` or any branch not named `proxai/...`, merge, delete, force-push, `.github/` workflow files, settings | refused in code |

The token stays on the PC in `~/.hermes/profiles/proxai/secrets/github.json`. The sandbox (where
ProxAI runs commands and Claude Code) never sees it, so `git push` / `gh` from there can't get around
these rules: the sandbox can only clone public repos.

## Set up (once, at the PC)

1. github.com → Settings → Developer settings → **Fine-grained personal access tokens** → Generate.
2. **Read token**: resource owner `proximalbeet`, repositories: All (or pick some), expiry as you like.
   Permissions, all **Read-only**: Contents, Issues, Pull requests, Actions, Metadata.
3. Optional **write token** (only if you want issues/branches/draft PRs): same repos;
   **Read and write**: Contents, Issues, Pull requests; Read-only: Metadata.
   Skip it and ProxAI stays read-only.
4. Save them (no spaces, nothing else in the file):

   ```bash
   mkdir -p ~/.hermes/profiles/proxai/secrets && chmod 700 ~/.hermes/profiles/proxai/secrets
   printf '{"read": "%s", "write": "%s"}\n' 'github_pat_READ…' 'github_pat_WRITE_or_empty' \
     > ~/.hermes/profiles/proxai/secrets/github.json
   chmod 600 ~/.hermes/profiles/proxai/secrets/github.json
   ```

   Changes to the file apply right away (no restart).

## Files

- `__init__.py`: tool `github`, the yes-check hook, a hint when you mention GitHub/repos/PRs.
- Log: `~/.local/state/proxai/github.log`; every write and refusal also goes to the security log (`GITHUB`).
- Tests: `python3 plugins/proxai-github/test_github.py` (no network).
