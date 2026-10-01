#!/bin/bash
# 网络断续时的推送重试：直连与代理交替，最多 12 轮
cd "D:\摆渡计划"
push_one() {
  local ref="$1"
  for mode in direct proxy; do
    for i in 1 2 3; do
      if [ "$mode" = "proxy" ]; then
        out=$(GIT_TERMINAL_PROMPT=0 git -c http.proxy=http://127.0.0.1:7897 push origin "$ref" 2>&1)
      else
        out=$(GIT_TERMINAL_PROMPT=0 git push origin "$ref" 2>&1)
      fi
      if echo "$out" | grep -qE "$ref( ->|\.0\b)|new tag|\.\. "; then
        echo "OK($mode): $out" | tail -1
        return 0
      fi
      echo "fail($mode/$i): $(echo "$out" | tail -1)"
      sleep 15
    done
  done
  return 1
}
for ref in main v1.7.0 bridge-v1.0.0; do
  if push_one "$ref"; then continue; fi
  echo "GIVE UP: $ref"
  exit 1
done
echo "ALL PUSHED"
