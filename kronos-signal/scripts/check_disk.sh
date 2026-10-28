#!/bin/sh
# Run on the Docker host BEFORE building the kronos-signal image. The image is about 1.5-2 GB (CPU torch ~0.8 GB, model weights 0.1 GB, base layers).
# Usage: sh kronos-signal/scripts/check_disk.sh [required_gb, default 5]
need_gb="${1:-5}"
root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
free_kb="$(df -Pk "$root" | awk 'NR==2 {print $4}')"
free_gb=$((free_kb / 1024 / 1024))
echo "docker root: $root, free: ${free_gb} GB, required: ${need_gb} GB"
docker system df 2>/dev/null || true
if [ "$free_gb" -lt "$need_gb" ]; then
  echo "NOT ENOUGH DISK: free at least ${need_gb} GB (docker image prune -f / docker builder prune -f) before building" >&2
  exit 1
fi
echo "disk ok"
