#!/usr/bin/env bash
# one-time (idempotent) configuration for the AI auto-merge bot. run with a
# plind-junior owner token: `bash scripts/setup_branch_protection.sh [branch]`.
# prerequisites: repo secret ANTHROPIC_API_KEY set (Settings > Secrets > Actions).
#
# what it does:
#   - creates the auto-merge / ci: passing / ci: failing labels
#   - enables the repo "allow auto-merge" setting (so `gh pr merge --auto` works)
#   - protects <branch>: required checks (ci matrix + trust-gate) and code-owner
#     review (this is what keeps core changes owner-gated).
#
# note: claude-verify is deliberately NOT a required status check. it only runs
# on auto-merge-labeled prs, so requiring it globally would block every normal
# (unlabeled) pr from merging. the label flow still gates on it via the job
# dependency in auto-merge.yml (arm needs a green verify job), so claude's veto
# holds without blocking manual merges.
#
# confirm the required-check names still match .github/workflows/ci.yml's job
# names before relying on it (see the contexts block below).
set -euo pipefail

REPO="${REPO:-vouchdev/vouch}"
BRANCH="${1:-test}"

echo "==> labels"
gh label create "auto-merge" --repo "$REPO" --color 1d76db \
  --description "owner-authorized: claude code verifies, then auto-merge" --force
gh label create "ci: passing" --repo "$REPO" --color 2ecc71 --description "ci is green" --force
gh label create "ci: failing" --repo "$REPO" --color e74c3c --description "ci is red" --force

echo "==> allow auto-merge on the repo"
gh api --method PATCH "repos/$REPO" -F allow_auto_merge=true >/dev/null

echo "==> branch protection on $BRANCH"
gh api --method PUT "repos/$REPO/branches/$BRANCH/protection" --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "test (py3.11)",
      "test (py3.12)",
      "test (py3.13)",
      "build sdist + wheel",
      "trust-gate"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "require_code_owner_reviews": true,
    "required_approving_review_count": 0
  },
  "restrictions": null
}
JSON

echo "done. re-run any time; the API calls are idempotent."
