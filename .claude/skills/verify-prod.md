# Verify Production

Quick health check of the production deployment at chennu.co.

## Steps

1. **HTTP health check**
   ```bash
   curl -s -o /dev/null -w "HTTP %{http_code}" https://chennu.co/
   ```

2. **Container status**
   ```bash
   ssh -i ~/.ssh/github_actions_deploy root@62.238.6.14 "cd /opt/consulting && docker ps --format 'table {{.Names}}\t{{.Status}}' | grep consulting"
   ```

3. **Recent logs (check for errors)**
   ```bash
   ssh -i ~/.ssh/github_actions_deploy root@62.238.6.14 "docker logs consulting-web-1 2>&1 | tail -20"
   ```

4. **Git commit on server matches local**
   ```bash
   ssh -i ~/.ssh/github_actions_deploy root@62.238.6.14 "cd /opt/consulting && git log --oneline -1"
   ```
   Compare with local `git log --oneline -1`.

5. **Report**
   - HTTP status (should be 200 or 302)
   - All containers running (web, celery_worker, celery_beat)
   - No ERROR in recent logs
   - Commit hash matches
