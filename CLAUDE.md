# CLAUDE.md

## Branches and deployment

- Commit on `main`, then push every update to **both** branches:

  ```bash
  git push -u origin main
  git push origin main:claude/sleepy-allen-npyjnv
  ```

- GitHub Pages (https://oh-da.github.io/HubsScraping/) is published from
  `claude/sleepy-allen-npyjnv`, and the `github-pages` environment only accepts deploys from
  that branch. A Pages run on `main` therefore fails or is cancelled; that is expected. The
  site updates from the push to `claude/sleepy-allen-npyjnv`.
- Keep `claude/sleepy-allen-npyjnv` identical to `main` (fast-forward only, never force-push).
  If it has commits `main` lacks, stop and ask.
