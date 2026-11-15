# 🚀 Deployment Guide - Health Report v2.0

## Quick Start

### Prerequisites
- Portainer running with access to Docker
- Stack named `healthhub` already created
- Required environment variables configured in Portainer

### Deployment Steps

1. **Get the docker-compose.yml**
   ```bash
   # Option 1: Use the one in this repo
   cat docker-compose.yml
   
   # Option 2: Clone from GitHub
   git clone --depth 1 https://github.com/fvegas82/HealthReport.git
   cat HealthReport/docker-compose.yml
   ```

2. **In Portainer**
   - Go to: `Stacks > healthhub > Editor`
   - Select all content and delete it
   - Paste the complete `docker-compose.yml` file
   - Click `Update the stack`
   - Wait 5-10 minutes for:
     - Reporter to clone from GitHub
     - Dependencies to install
     - Container to start and begin generating reports

3. **Verify Deployment**
   - Check container status: Green ✅
   - Check logs for: `Iniciando Health Report Generator v2.0...`
   - Verify informe at: `https://reportHealth.fvegas.es/`
   - Look for new features:
     - 6 metric cards (vs 3 before)
     - AGP glucose graph
     - 7 daily profiles with insulin (red) and exercise (violet)
     - RENPHO body composition section
     - Enhanced activity table with insulin/exercise columns

---

## Environment Variables (Required in Portainer)

These must be configured as environment variables in your Portainer stack:

| Variable | Purpose | Example |
|----------|---------|---------|
| `HEALTHHUB_DB_PASSWORD` | PostgreSQL password | `SecurePass123!` |
| `NIGHTSCOUT_API_SECRET` | Nightscout authentication token | `your_secret_hash` |
| `HEALTHHUB_INGEST_KEY` | Data ingestion key | `ingestion_key_123` |
| `HEALTHHUB_BACKUP_ENCRYPTION_PASSWORD` | Backup encryption | `BackupPass123!` |

**⚠️ IMPORTANT**: These secrets should NEVER be committed to Git. Keep them only in Portainer.

---

## How It Works

### Reporter Service

The reporter service in `docker-compose.yml`:

1. **Starts with**: `image: python:3.12-slim`
2. **Installs git**: `apk add --no-cache git`
3. **Clones latest code**: From `https://github.com/fvegas82/HealthReport.git`
4. **Installs dependencies**: matplotlib, numpy, psycopg, Pillow, jinja2
5. **Executes**: `python report.py` with all environment variables

### Automatic Updates

When you push changes to the `master` branch on GitHub:

1. Next time the container restarts, it will clone the latest code
2. No manual intervention needed in Portainer
3. Perfect for bug fixes and feature updates

### Directory Structure

```
/reports/healthhub/reports/
  ├── healthhub-semanal.html          # Current week report
  ├── healthhub-semanal-YYYY-WNN.html # Timestamped historical reports
  ├── ultimo.html                     # Latest copy
  ├── day-1.png                       # Daily profile graphs
  ├── day-2.png
  ├── ... (7 total)
  └── agp.png                         # Ambulatory Glucose Profile
```

---

## Troubleshooting

### Container keeps restarting
- Check logs: `Stacks > healthhub > Containers > reporter > Logs`
- Common issues:
  - Missing environment variables → Add to Portainer
  - Git clone failure → Check internet connectivity
  - Python dependency error → Container should auto-install

### Reports not generating
- Check: `/reports/healthhub/reports/` directory exists
- Verify PostgreSQL connection: `DATABASE_URL` is correct
- Check Nightscout connectivity: Can access `http://nightscout:1337`

### No new features showing
- Hard refresh portal: `Ctrl+Shift+R`
- Clear browser cache
- Verify informe HTML updated: Check file timestamp in `/reports/healthhub/reports/`

### RENPHO composition not showing
- This is optional - if `/glucose-reports/renpho/ultimo.json` doesn't exist, section is skipped
- Graceful degradation: report works without it

---

## Rollback

If something breaks and you need to go back to the previous version:

1. In Portainer: `Stacks > healthhub > Editor`
2. Restore previous docker-compose from backup
3. Or checkout previous commit: `git checkout ca4f779` (before v2.0)
4. Update the stack

---

## What's New in v2.0

### Code
- Refactored from 44 lines to 611 lines of structured Python
- 9 modular functions for maintainability
- Comprehensive error handling and logging

### Features
- **8 glucose metrics** (vs 3 before): mean, SD, CV%, TIR, hypo%, hyper%, count, events
- **7 daily profiles** with insulin and exercise markers
- **AGP (Ambulatory Glucose Profile)** graph
- **RENPHO integration** for body composition
- **Responsive design** that works on mobile
- **Historical reports** with timestamps (YYYY-WNN format)
- **Enhanced activity table** with insulin and exercise columns

### Infrastructure
- Versionable code on GitHub (no more embedded Base64)
- Security: secrets in Portainer, not in repo
- Automatic updates: next restart gets latest code from GitHub
- Comprehensive logging for debugging

---

## Support

For issues or questions:
1. Check `/Users/FVegas/Dev/Health\ Report/docs/DEPLOYMENT_REPORTER_V2.md`
2. Review logs in Portainer
3. Check GitHub repository: https://github.com/fvegas82/HealthReport

---

## Commits Related to This Deployment

- `3cf5aaa` - chore(deploy): Add docker-compose for Health Report v2.0
- `ca4f779` - fix(reporter): Use internal Docker URL for Nightscout
- `3de11dc` - build: Update Dockerfile to clone from GitHub
- `e130d25` - refactor(reporter): Complete rewrite of Health Report Generator v2.0

See full history: `git log --oneline`
