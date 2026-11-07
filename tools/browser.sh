#!/usr/bin/env bash
# Project browser commands always execute on the owner's MacBook.
set -euo pipefail

session="${IDEALISTA_BROWSER_SESSION:-idealista-${CODEX_THREAD_ID:-manual}}"
if [[ ! "$session" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo "Invalid IDEALISTA_BROWSER_SESSION; use letters, digits, _ or -." >&2
  exit 2
fi
if [[ $# -eq 0 ]]; then
  echo "Usage: tools/browser.sh --check-route | <playwright-cli command> [args...]" >&2
  exit 2
fi
for arg in "$@"; do
  case "$arg" in
    close-all|kill-all|--session*|-s|-s=*)
      echo "Use IDEALISTA_BROWSER_SESSION and close only that session." >&2
      exit 2
      ;;
  esac
done

# Quote every argument before passing it through SSH's remote shell.
# shellcheck disable=SC2016 # The hostname must be evaluated on the remote Mac.
remote='set -eu; test "$(/bin/hostname)" = MacBook-Pro007.local || { echo "Wrong browser host; refusing launch" >&2; exit 1; }; '
reader_source=""
if [[ "$1" == --read-listing-status ]]; then
  [[ $# -eq 3 ]] || { echo "Expected URL and nonce" >&2; exit 2; }
  reader_source="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/services/listing_browser.py"
  remote+='exec /usr/bin/python3 -'
elif [[ "$1" == --check-route ]]; then
  remote+='hostname; /usr/bin/curl --fail --silent --show-error --max-time 8 -o /dev/null -w "idealista_tunnel_http=%{http_code}\n" http://127.0.0.1:5001/properties'
else
  remote+='mkdir -p /Users/ss/Library/Caches/IdealistaRank/browser; cd /Users/ss/Library/Caches/IdealistaRank/browser; exec '
  command=(/usr/bin/env PATH=/opt/homebrew/bin:/usr/bin:/bin
    /opt/homebrew/bin/npx --no-install --package @playwright/cli
    playwright-cli "-s=$session" "$@")
  if [[ "$1" == open ]]; then
    command+=(--browser chrome --headed --idle-timeout 600000)
  fi
  printf -v quoted '%q ' "${command[@]}"
  remote+="$quoted"
fi

if [[ -n "$reader_source" ]]; then
  if [[ "$(/bin/hostname)" == MacBook-Pro007.local ]]; then
    exec /usr/bin/python3 "$reader_source" --read "$2" "$3"
  fi
  # Only validated Python/JSON goes over stdin. SSH argv is constant: never
  # interpolate a requested URL into the remote shell's command line.
  /usr/bin/python3 "$reader_source" --emit "$2" "$3" | \
    /usr/bin/ssh -T -o BatchMode=yes -o ConnectTimeout=8 \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=2 ss@100.127.248.71 "$remote"
  exit $?
fi

if [[ "$(/bin/hostname)" == MacBook-Pro007.local ]]; then
  exec /bin/bash -c "$remote"
fi
# No local-browser fallback: a sleeping/disconnected MacBook is an error.
exec /usr/bin/ssh -T -o BatchMode=yes -o ConnectTimeout=8 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
  ss@100.127.248.71 "$remote"
