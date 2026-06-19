# Consistent Release

One-command, repeatable production release for Consulting. The SAME steps run every time:
verify CI is green → version + changelog → deploy → health-verify → auto-rollback on failure → record it.

Use this instead of a bare `/deploy` when you want a tracked, versioned release.

**Arg (optional):** `patch` (default) · `minor` · `major` · or an explicit `vX.Y.Z`.
**`--dry-run`:** do steps 1–3 locally (version + changelog + tag) but do NOT push the tag or deploy.

## Steps

1. **Pre-flight**
   - `git fetch origin` — confirm local `main` == `origin/main` (abort if behind/ahead unexpectedly).
   - Confirm on `main` and the working tree is clean (abort if dirty — commit first).
   - Capture HEAD: `HEAD=$(git rev-parse HEAD)`.

2. **Verify CI is GREEN for HEAD** (never release a red build)
   - Find the CI + "Build & publish Docker image" runs for `$HEAD`:
     `gh run list --branch main --limit 8 --json databaseId,name,headSha,conclusion`
   - Both must have `conclusion == success`. If CI is still running, wait (`gh run watch`). If either failed, **STOP** and report. (A flaky Docker rate-limit can be re-run with `gh run rerun <id> --failed`.)

3. **Version + changelog**
   - `LAST=$(git describe --tags --abbrev=0)`  (e.g. `v4.0.0`)
   - Compute the new version from the arg (default = patch bump): `v4.0.0` → `v4.0.1`.
   - Build the changelog: `git log $LAST..$HEAD --pretty='- %s (%h)'` (drop pure-chore/merge noise).
   - Prepend an entry to `RELEASES.md`:
     ```
     ## vX.Y.Z — <UTC date> (<short HEAD>)
     <changelog lines>
     Status: pending
     ```
   - Commit it: `git add RELEASES.md && git commit -m "release: vX.Y.Z"`.
   - Tag it: `git tag -a vX.Y.Z -m "vX.Y.Z"`.
   - **If `--dry-run`: stop here** (don't push the tag, don't deploy). Otherwise push: `git push origin main --follow-tags`.
   - Note: the release commit re-triggers CI/build — wait for that build to be green before deploying (step 4).

4. **Deploy**
   - Wait for the Docker image build of the release commit to finish green (`gh run watch <build-id> --exit-status`).
   - `gh workflow run deploy-vps.yml --ref main -f confirm=DEPLOY`
   - `sleep 8`, find the run, `gh run watch <deploy-id> --exit-status`.

5. **Health-verify** (never skip)
   - Poll the health endpoint for HTTP 200 (use the IP `--resolve` fallback so local DNS flakes don't lie):
     ```bash
     for i in $(seq 1 6); do
       code=$(curl -s -o /dev/null -w '%{http_code}' --resolve chennu.co:443:62.238.6.14 https://chennu.co/core/health/)
       [ "$code" = "200" ] && break; sleep 5
     done
     ```
   - 200 → release succeeded.

6. **Auto-rollback on failure**
   - If the deploy failed OR health never returned 200:
     - Find the previous good image: the GHCR image digest built for `$LAST`'s commit
       (`git rev-list -n1 $LAST`), via `gh api` on the GHCR package versions, or the `deploy.log` on the VPS.
     - `gh workflow run rollback.yml --ref main -f image_sha=<sha256:...> -f reason="release vX.Y.Z failed health check"`
     - Watch it, then re-run the health check (step 5).
   - If the previous image SHA can't be resolved automatically, **STOP**, print the failing deploy logs (`gh run view <id> --log-failed`) and the exact manual rollback command, and alert the user. Do NOT leave prod in an unknown state silently.

7. **Record + report**
   - Update the `RELEASES.md` entry's `Status:` to `deployed` (or `rolled back to <prev>`), commit + push.
   - Print: version, commit, CI status, deploy status, health code, and `https://chennu.co`.
   - End with `✅ Released vX.Y.Z` or `❌ Release failed — rolled back / needs attention: <reason>`.

## Rules
- NEVER release if CI or the image build for the commit isn't `success`.
- NEVER skip the post-deploy health check.
- Tag + changelog happen BEFORE the deploy so every production release is traceable in `RELEASES.md` and git tags.
- One release at a time (deploy + rollback share the `deploy-production` concurrency group).
