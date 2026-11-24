#!/usr/bin/env bash
# Verification test suite for Gmail read-only access with gog CLI.
# Enforces strictly read-only flags: --readonly, --gmail-no-send, and restricted commands.

set -euo pipefail

account="${1:-${GOG_ACCOUNT:-}}"
sender_query="${2:-google}"

if [[ -z "$account" ]]; then
  echo "Usage: $0 <gog-account-or-alias> [optional-sender-filter]" >&2
  echo "Example: $0 you@example.com no-reply" >&2
  exit 64
fi

if ! command -v gog >/dev/null 2>&1; then
  echo "Error: gog CLI is not installed or not on PATH." >&2
  exit 127
fi

echo "=== 1. Liệt kê các email gần nhất (inbox) ==="
recent_json=$(gog \
  --readonly \
  --gmail-no-send \
  --no-input \
  --enable-commands-exact gmail.search \
  --account "$account" \
  --json \
  --wrap-untrusted \
  gmail search 'in:inbox' --max 3)

echo "$recent_json"
echo ""

# Lấy 1 message ID đầu tiên nếu có
first_id=$(echo "$recent_json" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    # gog search output might be a list or a dict with messages/threads
    messages = data if isinstance(data, list) else data.get('messages', data.get('items', []))
    if messages and len(messages) > 0:
        first = messages[0]
        print(first.get('id', first.get('messageId', '')))
    else:
        print('')
except Exception:
    print('')
")

echo "=== 2. Đọc chi tiết một email ==="
if [[ -n "$first_id" ]]; then
  echo "Đang đọc email ID: $first_id"
  gog \
    --readonly \
    --gmail-no-send \
    --no-input \
    --enable-commands-exact gmail.get \
    --account "$account" \
    --json \
    --wrap-untrusted \
    gmail get "$first_id" --sanitize-content
else
  echo "Không tìm thấy ID email nào trong hộp thư đến để đọc thử (hộp thư có thể đang trống)."
fi
echo ""

echo "=== 3. Tìm email theo người gửi (sender: '$sender_query') ==="
gog \
  --readonly \
  --gmail-no-send \
  --no-input \
  --enable-commands-exact gmail.search \
  --account "$account" \
  --json \
  --wrap-untrusted \
  gmail search "from:$sender_query" --max 3

echo ""
echo "=== Hoàn thành kiểm tra Gmail read-only! ==="
