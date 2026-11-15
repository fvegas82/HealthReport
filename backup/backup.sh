#!/bin/sh
set -eu
while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h postgres -U healthhub -d healthhub | gzip | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 600000 -pass env:BACKUP_ENCRYPTION_PASSWORD -out "/backups/healthhub-${stamp}.sql.gz.enc"
  find /backups -type f -name 'healthhub-*.sql.gz.enc' -mtime +30 -delete
  sleep 86400
done
