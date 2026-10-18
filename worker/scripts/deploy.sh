#!/bin/bash
# Worker + D1 deploy. Dry run by default; only --apply executes. Config: scripts/deploy.conf.
# The Worker lives in the repo's worker/ subdirectory; its name comes from NAME in deploy.conf.
#
#   scripts/deploy.sh            dry run: preflight checks, pending migrations, diff --stat (last deploy → this commit),
#                                compile check
#   scripts/deploy.sh --apply    back up D1 → migrate → deploy (injects VERSION=short SHA) → smoke test →
#                                write deploy.log → Bark
#
# Failure handling:
#   - Smoke test fails after deploy → automatic wrangler rollback to the pre-deploy version, then check /healthz again.
#   - D1 is not rolled back automatically (a Time Travel restore would lose call records written afterwards):
#     print the restore command and let a human decide.
#   - Any failure pushes a Bark notification (kind: deploy) and is written to deploy.log.
# Secrets: the Bark key is read from the Keychain and passed to curl via stdin, never on the command line.
set -uo pipefail

die()  { echo "✗ $*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

APPLY=0
case "${1:-}" in
  "") ;;
  --apply) APPLY=1 ;;
  -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  *) die "未知参数：$1（只接受 --apply）" ;;
esac
[ $# -le 1 ] || die "参数太多（只接受 --apply）"

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1
git rev-parse --show-toplevel >/dev/null 2>&1 || die "不在 git 仓库里"

# ---------- config: parsed strictly as KEY=VALUE, never sourced ----------
CONF="scripts/deploy.conf"
[ -r "$CONF" ] || die "缺少 $CONF"
conf() { sed -n "s/^$1=//p" "$CONF" | tail -1; }
HEALTH_URL=$(conf HEALTH_URL); D1_NAME=$(conf D1_NAME); D1_ID=$(conf D1_ID)
SMOKE_SQL=$(conf SMOKE_SQL); BARK_KEYCHAIN=$(conf BARK_KEYCHAIN)
[[ "$HEALTH_URL" =~ ^https://[A-Za-z0-9.-]+/[A-Za-z0-9/_-]*$ ]] || die "HEALTH_URL 格式不对：$HEALTH_URL"
[ -z "$D1_NAME" ] || [[ "$D1_NAME" =~ ^[A-Za-z0-9_-]+$ ]] || die "D1_NAME 格式不对"
[ -z "$D1_ID" ] || [[ "$D1_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || die "D1_ID 格式不对"
[[ "$BARK_KEYCHAIN" =~ ^[A-Za-z0-9._-]*$ ]] || die "BARK_KEYCHAIN 格式不对"

W() { npx --no-install wrangler "$@"; }
json_get() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d[int(k)] if k.isdigit() else d[k]
print(d)' "$1"; }

NAME=$(conf NAME); [[ "$NAME" =~ ^[A-Za-z0-9_-]+$ ]] || die "NAME 格式不对"
LOG="$ROOT/deploy.log"
TS=$(date '+%Y%m%d-%H%M%S')

bark() {  # bark title body [level, default timeSensitive] (pushes are always in English, 2026-10-16)
  local key body level=${3:-timeSensitive}
  [ -n "$BARK_KEYCHAIN" ] || return 0
  key=$(security find-generic-password -s "$BARK_KEYCHAIN" -w 2>/dev/null) \
    || { echo "（Keychain 里没有 $BARK_KEYCHAIN，跳过 Bark）"; return 0; }
  body=$(python3 -c 'import json,sys; print(json.dumps({"title":sys.argv[1],"body":sys.argv[2],"group":"deploy","level":sys.argv[3]}, ensure_ascii=False))' "$1" "$2" "$level")
  printf 'url = "https://api.day.app/%s"\n' "$key" \
    | curl -sS -m 10 -o /dev/null -w 'Bark %{http_code}\n' -K - \
        -H 'Content-Type: application/json; charset=utf-8' -d "$body" \
    || echo "（Bark 推送失败）"
}

log() { printf '%s %s\n' "$(date '+%F %T%z')" "$*" >>"$LOG"; }

health() {  # prints "ok version" or "fail ..."
  local r
  r=$(curl -sS -m 10 "$HEALTH_URL" 2>&1) || { echo "fail curl: $r"; return; }
  python3 -c 'import json,sys
try:
    d=json.loads(sys.argv[1]); print(("ok" if d.get("ok") is True else "fail"), d.get("version"))
except Exception: print("fail non-JSON:", sys.argv[1][:80])' "$r"
}

# ---------- preflight checks ----------
step "前置检查（$NAME）"
[ -z "$(git status --porcelain)" ] || { git status -s; die "工作区不干净"; }
git fetch -q origin || die "git fetch 失败"
SHA=$(git rev-parse --short=7 HEAD)
git branch -r --contains HEAD | grep -q 'origin/' || die "$SHA 还没推送到 origin"
echo "commit $SHA 已推送：$(git log -1 --format=%s)"

W whoami 2>&1 | grep -q "You are logged in" || die "wrangler 没有登录（npx wrangler login）"
echo "wrangler 已登录"

if [ -n "$D1_NAME" ]; then
  grep -Eq "\"database_id\": *\"$D1_ID\"" wrangler.jsonc || die "wrangler.jsonc 里没有 D1 $D1_ID"
  uuid=$(W d1 info "$D1_NAME" --json 2>/dev/null | json_get uuid) || die "读不到线上 D1 $D1_NAME"
  [ "$uuid" = "$D1_ID" ] || die "线上 D1 $D1_NAME 的 uuid 是 $uuid，不是 $D1_ID"
  echo "D1 $D1_NAME = $D1_ID"
fi

# ---------- dry-run contents ----------
step "待执行的迁移"
PENDING=""
if [ -n "$D1_NAME" ]; then
  mlist=$(W d1 migrations list "$D1_NAME" --remote 2>&1) || { echo "$mlist"; die "migrations list 失败"; }
  PENDING=$(printf '%s\n' "$mlist" | sed -n 's/^│ *\([0-9A-Za-z_.-]*\.sql\) *│$/\1/p')
  echo "${PENDING:-（无）}"
else
  echo "（没有 D1）"
fi

step "版本"
read -r hstate PREV_SHA <<<"$(health)"
if [ "$hstate" != ok ] || ! [[ "$PREV_SHA" =~ ^[0-9a-f]{7,12}$ ]]; then
  PREV_SHA=$(sed -n 's/.* ok sha=\([0-9a-f]\{7,12\}\) .*/\1/p' "$LOG" 2>/dev/null | tail -1)
  echo "线上 /healthz 没给出版本（$hstate），按 deploy.log 取上次部署：${PREV_SHA:-未知}"
fi
PREV_VER=$(W deployments status --json 2>/dev/null | json_get versions.0.version_id 2>/dev/null) || PREV_VER=""
if [ -z "$PREV_VER" ]; then
  [ -s "$LOG" ] && die "读不到当前部署版本"
  echo "线上还没有这个 Worker（首次部署：冒烟失败时没有可回滚的版本）"
fi
echo "线上：commit ${PREV_SHA:-未知}，Worker 版本 $PREV_VER"
echo "本次：commit $SHA"
if [ -n "$PREV_SHA" ] && git cat-file -e "$PREV_SHA^{commit}" 2>/dev/null; then
  [ "$PREV_SHA" = "$SHA" ] && echo "（与线上同一 commit）" || git --no-pager diff --stat "$PREV_SHA" "$SHA"
fi

step "编译检查（wrangler deploy --dry-run）"
W deploy --dry-run --outdir "$(mktemp -d)" >/dev/null 2>&1 || die "编译失败：npx wrangler deploy --dry-run"
echo "通过"

if [ $APPLY -eq 0 ]; then
  printf '\n预演结束。加 --apply 执行。\n'
  exit 0
fi

# ---------- execute ----------
BACKUP="" BOOKMARK=""
MIGRATED=""   # set once migrations have run (a later failure won't roll D1 back automatically; the push must say so)
fail() {  # fail stage reason [action already taken]
  local st msg
  case "$1" in backup) st="backup" ;; migrate) st="migration" ;; deploy) st="deploy" ;; smoke) st="smoke test" ;; *) st=$1 ;; esac
  # Push in English, impact first: if an action was already taken (e.g. auto-rollback after a failed
  # smoke test), state the action first, then the reason
  if [ -n "${3:-}" ]; then msg="$NAME $SHA failed at $st; $3. Reason: $2."; else msg="$NAME $SHA failed at $st: $2."; fi
  [ -n "$MIGRATED" ] && msg="$msg D1 not rolled back; restore command in terminal / deploy.log."
  echo; echo "✗ $msg"
  if [ -n "$D1_NAME" ]; then
    echo "D1 恢复（仅在需要时，会丢掉之后写入的数据）："
    [ -n "$BOOKMARK" ] && echo "  npx wrangler d1 time-travel restore $D1_NAME --bookmark=$BOOKMARK"
    [ -n "$BACKUP" ] && echo "  备份文件：$BACKUP"
  fi
  echo "Worker 回滚：npx wrangler rollback $PREV_VER"
  log "FAIL sha=$SHA stage=$1 prev_ver=$PREV_VER bookmark=${BOOKMARK:-} backup=${BACKUP:-} msg=$2"
  bark "Deploy failed: $NAME" "$msg" timeSensitive
  exit 1
}

if [ -n "$D1_NAME" ]; then
  step "1. 备份 D1"
  BOOKMARK=$(W d1 time-travel info "$D1_NAME" --json 2>/dev/null | json_get bookmark) || fail backup "cannot get Time Travel bookmark"
  echo "bookmark $BOOKMARK"
  mkdir -p backups
  BACKUP="backups/$D1_NAME-$TS-$SHA.sql"
  # Occasional failure (seen once on 2026-09-27; a manual rerun succeeded): retry once, and if it still fails
  # include wrangler's last lines in the failure message
  eout=$(W d1 export "$D1_NAME" --remote --output "$BACKUP" 2>&1) \
    || { echo "d1 export 失败，5 秒后重试一次"; sleep 5; eout=$(W d1 export "$D1_NAME" --remote --output "$BACKUP" 2>&1); } \
    || fail backup "d1 export failed: $(printf '%s\n' "$eout" | grep -v '^\s*$' | tail -3 | tr '\n' ' ')"
  [ -s "$BACKUP" ] || fail backup "backup file is empty"
  echo "$BACKUP（$(wc -c <"$BACKUP" | tr -d ' ') 字节）"

  step "2. 迁移"
  if [ -n "$PENDING" ]; then
    MIGRATED=1
    CI=1 W d1 migrations apply "$D1_NAME" --remote 2>&1 | tail -15
    left=$(W d1 migrations list "$D1_NAME" --remote 2>&1 | sed -n 's/^│ *\([0-9A-Za-z_.-]*\.sql\) *│$/\1/p')
    [ -z "$left" ] || fail migrate "migrations still pending after apply: $left"
    echo "迁移完成"
  else
    echo "（无）"
  fi
fi

step "3. 部署"
W deploy --var "VERSION:$SHA" --message "$SHA $(git log -1 --format=%s | cut -c1-80)" 2>&1 | tail -8
NEW_VER=$(W deployments status --json 2>/dev/null | json_get versions.0.version_id)
[ -n "$NEW_VER" ] && [ "$NEW_VER" != "$PREV_VER" ] || fail deploy "version unchanged after deploy (still $PREV_VER)"
echo "新版本 $NEW_VER"

step "4. 冒烟"
ok=0
for i in 1 2 3 4 5 6 7 8 9 10; do
  read -r hstate hver <<<"$(health)"
  if [ "$hstate" = ok ] && [ "$hver" = "$SHA" ]; then ok=1; break; fi
  sleep 3
done
smoke_err="/healthz returned $hstate $hver (expected ok $SHA)"
if [ $ok -eq 1 ] && [ -n "$D1_NAME" ] && [ -n "$SMOKE_SQL" ]; then
  row=$(W d1 execute "$D1_NAME" --remote --json --command "$SMOKE_SQL" 2>/dev/null | json_get 0.results.0) \
    && echo "只读查询：$row" || { ok=0; smoke_err="read-only query failed: $SMOKE_SQL"; }
fi
if [ $ok -ne 1 ]; then
  [ -n "$PREV_VER" ] || fail smoke "$smoke_err" "first deploy, nothing to roll back to"
  echo "✗ $smoke_err —— 自动回滚到 $PREV_VER"
  W rollback "$PREV_VER" -y -m "deploy.sh 自动回滚：$SHA 冒烟失败" 2>&1 | tail -3
  read -r hstate hver <<<"$(health)"
  echo "回滚后 /healthz：$hstate $hver"
  fail smoke "$smoke_err" "Worker auto-rolled back to $PREV_VER (/healthz after rollback: $hstate $hver)"
fi
echo "/healthz ok，version $SHA"

step "5. 记录"
log "ok sha=$SHA ver=$NEW_VER prev_ver=$PREV_VER prev_sha=${PREV_SHA:-} bookmark=${BOOKMARK:-} backup=${BACKUP:-} migrations=$(echo $PENDING | tr ' ' ',')"
tail -1 "$LOG"
bark "Deployed: $NAME $SHA" "$(git log -1 --format=%s)${PENDING:+; migrations: $(echo $PENDING)}" active   # commit subject as-is; success only confirms, no interruption
printf '\n✓ 完成\n'
