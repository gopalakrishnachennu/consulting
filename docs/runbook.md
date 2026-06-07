# GoCareers — Operations Runbook

## Deploy

```bash
# 1. Push code to main
git push origin main

# 2. Wait for Docker build (auto-triggered)
gh run watch --workflow="Build & publish Docker image"

# 3. Trigger deploy
gh workflow run deploy-vps.yml -f confirm=DEPLOY

# 4. Monitor
gh run watch --workflow="Deploy to Hetzner VPS"
```

## Rollback

```bash
# Find the image SHA of a known-good commit
gh api repos/gopalakrishnachennu/consulting/packages/container/consulting/versions \
  --jq '.[].metadata.container.tags'

# Trigger rollback with exact SHA
gh workflow run rollback.yml \
  -f image_sha="sha256:abc123..." \
  -f reason="Reverting bad deploy from 2026-06-07"
```

## Backup

```bash
# Manual backup
gh workflow run backup-production-db.yml

# Check backup on server
ssh root@62.238.6.14 'ls -lh /opt/backups/db/'

# Decrypt backup locally
openssl enc -d -aes-256-cbc -pbkdf2 \
  -pass pass:YOUR_BACKUP_PASSWORD \
  -in latest.pgdump.gz.enc | gzip -dc > restore.pgdump
```

## Restore

```bash
# Restore to local postgres (DANGER: overwrites DB)
pg_restore --host=localhost --dbname=consulting_restore \
  --no-owner --no-acl restore.pgdump
```

## Incidents

- View at: `https://chennu.co/core/incidents/`
- Django admin: `https://chennu.co/admin/core/errorlog/`

## Sentry Setup (one-time)

1. Go to https://sentry.io → Create project → Django
2. Copy DSN
3. On server: `chattr -i /opt/consulting/.env && echo "SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx" >> /opt/consulting/.env && chattr +i /opt/consulting/.env`
4. Restart web: `docker compose -f docker-compose.prod.yml restart web`

## Uptime Monitoring

- GitHub Actions pings `/health/` every 15 minutes
- Check status: https://github.com/gopalakrishnachennu/consulting/actions/workflows/uptime-monitor.yml

## SSH to Production

```bash
ssh -i ~/.ssh/github_actions_deploy root@62.238.6.14
cd /opt/consulting
docker compose -f docker-compose.prod.yml logs -f web
```

## Harvest Overload

```bash
# Stop harvest immediately
gh workflow run ops-stop-fetch-batch.yml

# Or via SSH:
ssh root@62.238.6.14 'cd /opt/consulting && docker compose -f docker-compose.prod.yml exec web python manage.py shell -c "from harvest.models import HarvestEngineConfig; c=HarvestEngineConfig.get(); c.enabled=False; c.save()"'
```
