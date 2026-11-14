#!/usr/bin/env bash
# 样例 4：观测变更流 —— 分析库持续同步的取数方式。
#   自启用起，上报/裁决改变结论即在同一事务中按提交次序记录完整快照（含删除标记）；
#   拒绝与纯重传不产生记录；按序号游标限量领取，下一游标只越过已返回的记录；
#   离线补传改值、并发分支撤回旧值都会出现在流中，分析库可据此撤回失效值。
set -euo pipefail

API_A="${API_A:-http://localhost:8001}"
API_B="${API_B:-http://localhost:8002}"
RUN="${RUN_ID:-$$}"           # 每次运行使用独立观测号，脚本可重复执行
OBS="OBS-CS-${RUN}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

json_get() {  # json_get <jq 表达式> <python 表达式>：从 stdin 的 JSON 中取字段值
  if command -v jq >/dev/null 2>&1; then jq -r "$1"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import sys,json; print(json.load(sys.stdin)$2)"
  else
    echo "需要 jq 或 python3 解析 JSON" >&2; exit 1
  fi
}

echo "== 0) 取当前流末尾作为基线游标（只领取基线之后的变更） =="
CURSOR=$(curl -sS "$API_B/changes?since=0&limit=1000" | json_get ".next_cursor" "['next_cursor']")
echo "基线游标: $CURSOR（观测号: $OBS）"

echo
echo "== 1) 经 api-a 上报首条修订（valid，count=3） =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d "{
  \"revisions\": [
    {\"observation_id\": \"$OBS\", \"revision_id\": \"cs-$RUN-r1\", \"parents\": [],
     \"species\": \"Cervus elaphus\", \"count\": 3}
  ]
}" | pretty

echo
echo "== 2) 离线补传改变有效值（count 3 → 5） =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d "{
  \"revisions\": [
    {\"observation_id\": \"$OBS\", \"revision_id\": \"cs-$RUN-r2\",
     \"parents\": [\"cs-$RUN-r1\"], \"species\": \"Cervus elaphus\", \"count\": 5}
  ]
}" | pretty

echo
echo "== 3) 并发分支撤回旧值（另一台设备对 r1 标记删除）→ conflict，有效值失效 =="
curl -sS -X POST "$API_B/packages" -H 'Content-Type: application/json' -d "{
  \"revisions\": [
    {\"observation_id\": \"$OBS\", \"revision_id\": \"cs-$RUN-r3\",
     \"parents\": [\"cs-$RUN-r1\"], \"delete\": true}
  ]
}" | pretty

echo
echo "== 4) 纯重传与被拒绝的请求不产生记录 =="
curl -sS -X POST "$API_A/packages" -H 'Content-Type: application/json' -d "{
  \"revisions\": [
    {\"observation_id\": \"$OBS\", \"revision_id\": \"cs-$RUN-r1\", \"parents\": [],
     \"species\": \"Cervus elaphus\", \"count\": 3}
  ]
}" | pretty
curl -sS -o /dev/null -w '同号异内容 → HTTP %{http_code}（422 整包拒绝，无记录）\n' \
  -X POST "$API_A/packages" -H 'Content-Type: application/json' -d "{
  \"revisions\": [
    {\"observation_id\": \"$OBS\", \"revision_id\": \"cs-$RUN-r1\", \"parents\": [],
     \"species\": \"Cervus elaphus\", \"count\": 99}
  ]
}"

echo
echo "== 5) 人工裁决（以全部分支为父）→ valid =="
curl -sS -X POST "$API_A/observations/$OBS/resolutions" \
  -H 'Content-Type: application/json' -d "{
    \"revision_id\": \"cs-$RUN-r4\", \"parents\": [\"cs-$RUN-r2\", \"cs-$RUN-r3\"],
    \"species\": \"Cervus elaphus\", \"count\": 5
  }" | pretty

echo
echo "== 6) 从 api-b 按游标翻页领取变更（limit=2，演示多页；下一游标只越过已返回记录） =="
PAGE=1
while : ; do
  RESP=$(curl -sS "$API_B/changes?since=$CURSOR&limit=2")
  N=$(printf '%s' "$RESP" | json_get ".changes | length" "['changes'].__len__()")
  echo "-- 第 $PAGE 页（since=$CURSOR，$N 条） --"
  printf '%s' "$RESP" | pretty
  [ "$N" = "0" ] && break
  CURSOR=$(printf '%s' "$RESP" | json_get ".next_cursor" "['next_cursor']")
  PAGE=$((PAGE + 1))
done

echo
echo "== 7) 旧记录仍保留原结论：第 1 条的有效值仍是 count=3（当前结论已不同） =="
echo "   分析库据此撤回失效值：count=3 被 count=5 取代，随后又因并发删除分支转入冲突失效。"
