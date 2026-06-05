# Deploy to Production

One-command deploy pipeline for GoCareers. Handles the full push → build → wait → deploy → verify cycle.

## Steps

1. **Pre-flight checks**
   - Run `git status` to confirm no uncommitted changes (warn if dirty)
   - Run `git log --oneline -1` to confirm HEAD is what we expect

2. **Push to remote**
   ```bash
   git push origin main
   ```

3. **Wait for Docker image build**
   ```bash
   # Get the build run ID
   gh run list --workflow="Build & publish Docker image" --limit 1 --json databaseId,headSha,status
   # Watch it
   gh run watch <RUN_ID> --exit-status
   ```
   If build fails, STOP and report the error. Do NOT proceed to deploy.

4. **Trigger deploy**
   ```bash
   gh workflow run deploy-vps.yml -f confirm=DEPLOY
   ```

5. **Wait for deploy**
   ```bash
   # Get deploy run ID (wait 3s for it to register)
   sleep 3
   gh run list --workflow="deploy-vps.yml" --limit 1 --json databaseId,status
   gh run watch <RUN_ID> --exit-status
   ```

6. **Report**
   - Print commit hash, build status, deploy status
   - Print "✅ Live on production" or "❌ Deploy failed: <reason>"

## Important
- NEVER deploy if the build failed
- NEVER skip the build wait — deploying before the new image is ready pulls the OLD image
- If `--dry-run` is passed, only do steps 1-2 (push but don't deploy)
