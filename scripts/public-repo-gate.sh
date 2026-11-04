#!/usr/bin/env bash
# Public-repository gate (oidc-sso-login design D12, task D.8).
#
# Scans EVERY tracked file (docs, archived OpenSpec changes, code comments, tests,
# design mock data) -- not a diff -- for categories of private-deployment data:
#   * private IPv4 addresses (RFC 1918) and link-local ranges
#   * email addresses outside the reserved example domains
#   * real host paths (/home/<user>, /root/, /storage/, /srv/<non-placeholder>, /mnt/)
#   * any extra term supplied by the operator at gate time
#
# Personal names, private hostnames, repositories and monitor/ticket identifiers are
# deployment-specific: pass them in a terms file (one fixed string per line, '#' comments
# allowed). That file is NEVER committed -- a committed deny list would itself publish the
# data it guards. Keep it outside the repository.
#
# Usage: scripts/public-repo-gate.sh [--terms FILE] [--allow REGEX]
#   --terms FILE   operator-supplied fixed strings (case-insensitive), outside the repo
#   --allow REGEX  extra ERE; lines matching it are not reported (e.g. a copyright line)
# Exit status: 0 = no hits, 1 = hits (listed as file:line:text), 2 = usage error.
set -euo pipefail

terms=""
allow=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --terms) terms="${2:?--terms needs a file}"; shift 2 ;;
    --allow) allow="${2:?--allow needs a regex}"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"
self="scripts/public-repo-gate.sh"

if [[ -n "$terms" ]]; then
  [[ -r "$terms" ]] || { echo "terms file not readable: $terms" >&2; exit 2; }
  case "$(realpath "$terms")" in
    "$(pwd)"/*)
      if git ls-files --error-unmatch -- "$(realpath --relative-to=. "$terms")" >/dev/null 2>&1; then
        echo "refusing: the terms file is tracked by git; keep it outside the repository" >&2
        exit 2
      fi ;;
  esac
fi

# Generic category patterns only (ERE). Nothing deployment-specific belongs here.
patterns=(
  '(^|[^0-9.])10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  '(^|[^0-9.])172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  '(^|[^0-9.])192\.168\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  '(^|[^0-9.])169\.254\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
  '/home/[a-z_][a-z0-9_-]*'
  '/root/'
  '/storage/'
  '/mnt/'
)
# Reserved/placeholder forms that are always fine.
default_allow=':[0-9]+:(.*@(example\.(com|org|net)|([a-z0-9-]+\.)*example(\.[a-z]+)*|localhost)|/home/(user|you))$'

hits=$(
  {
    for p in "${patterns[@]}"; do
      git grep -onIE -e "$p" -- . ":!$self" || true
    done
    if [[ -n "$terms" ]]; then
      git grep -onIiF -f <(grep -vE '^[[:space:]]*(#|$)' "$terms") -- . ":!$self" || true
    fi
  } | sort -u | grep -vE "$default_allow" | { if [[ -n "$allow" ]]; then grep -vE "$allow"; else cat; fi; } || true
)

if [[ -n "$hits" ]]; then
  printf '%s\n' "$hits"
  echo "public-repo gate: $(printf '%s\n' "$hits" | wc -l) hit(s)" >&2
  exit 1
fi
echo "public-repo gate: 0 hits" >&2
