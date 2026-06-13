#!/usr/bin/env bash
# ayabot-auto-dev.sh — 自动开发脚本，用 OpenCode 逐阶段实现 DEV_GOALS.md
# 在后台运行：nohup bash scripts/auto-dev.sh > scripts/auto-dev.log 2>&1 &
set -e
cd "$(dirname "$0")/.."
export PATH="$HOME/.opencode/bin:$PATH"

OC="opencode run -m opencode/deepseek-v4-flash-free --dangerously-skip-permissions"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Phase 1: UID精准配置系统 ──
log "=== Phase 1: UID精准配置 ==="

$OC '
Complete the following changes to the ayabot B站弹幕机器人 project at /home/shinshi/ayabot.

CONTEXT: This is a B站 live-stream danmaku bot. It has keyword reply, welcome messages, blindbox stats.

TASK 1: UID-specific keyword reply rules
- In app/config.py, modify KeywordRule dataclass to add: allowed_uids: list[int] = None (default None = all users)
- In app/config.py FeatureConfig, ensure keyword_reply passes through
- In app/bot.py, modify _match_keyword_rule() to accept uid parameter
- In app/bot.py _on_danmaku, pass uid=uid to _match_keyword_rule()
- If allowed_uids is set and non-empty, only match if uid is in the list
- Also modify _parse_keyword_reply in config.py to handle allowed_uids from YAML

TASK 2: Per-UID welcome templates
- In app/config.py, add welcome_templates_for_uids: dict[int, str] = None to FeatureConfig
- In app/config.py, add guard_welcome_templates dict with keys: captain, commander, governor
- In app/bot.py _on_enter_room, check uid-specific template first, then guard-level, then default
- Add guard level check via _get_guard_level() helper (use bilibili-api to check guard status)
- Modify config_to_dict and update_config_from_dict to include these new fields

TASK 3: Web UI fields
- In app/web/server.py, add reactive refs for:
  - UID-specific config table (keyed by uid)
  - Guard welcome template inputs
- Add UI in the config tab for these
- Add load/save logic

Important: 
- Keep backward compatibility - unset fields = existing behavior
- ALL relative paths are relative to project root /home/shinshi/ayabot
- Do NOT modify storage.py unless absolutely needed
- After changes, run: cd /home/shinshi/ayabot && python3 -c "import ast; ast.parse(open(\"app/config.py\").read()); ast.parse(open(\"app/bot.py\").read()); ast.parse(open(\"app/web/server.py\").read()); print(\"All syntax OK\")"
- Then: git add -A && git commit -m "feat: UID-specific keyword reply and welcome templates" && git push
' 2>&1 || log "Phase 1 failed (continuing)"

log "=== Phase 1 complete ==="

# ── Phase 2: 权限修复 + 账号系统 ──
log "=== Phase 2: 权限系统 ==="

$OC '
Continue work on the ayabot project at /home/shinshi/ayabot.

CONTEXT: The Web UI has a basic auth system with AUTH_USER/AUTH_PASS globals. We need to upgrade it.

TASK 1: Multi-user system with roles
- In app/web/server.py, replace the simple `users` dict with a proper system:
  - Store users in a JSON file: data/users.json (path based on _CONFIG_YAML_PATH)
  - Each user: { username, password_hash, role: "admin"|"user", allowed_rooms: [] }
  - Admin users can: everything
  - Regular users can: view their allowed_room gift rankings and exports, change own password
  - Add /api/admin/users endpoint (CRUD) - admin only
  - Add /api/user/password endpoint - any logged in user can change own password
- Use hashlib.sha256 for password hashing (no external deps)

TASK 2: Permission middleware
- Add a permission check middleware for regular users
- Block access to /api/general_config POST, /api/llm_config POST, /api/admin/* for non-admin users
- Regular users CAN access: GET /api/general_config (read-only), /api/ranking, /api/export/*, POST /api/user/password

TASK 3: Fix wenwen account
- The default wenwen account (password: 31415926) should be role "user" with allowed_rooms: []
- Admin account (from config.yaml web_ui.username/password) remains admin

TASK 4: Web UI updates
- Add "修改密码" button in the user menu/dropdown
- Add admin user management page (tab) for admin users only
- Show "授权直播间" selection when admin creates/edits users

Important:
- Read the existing code carefully - server.py has the auth system at the top
- The login/session system uses cookies with random tokens
- Keep backward compatibility - existing config.yaml users should auto-migrate
- After changes, verify syntax and git commit
' 2>&1 || log "Phase 2 failed"

log "=== Phase 2 complete ==="

# ── Phase 3: 弹幕日志 ──
log "=== Phase 3: 弹幕日志 ==="

$OC '
Continue work on the ayabot project at /home/shinshi/ayabot.

TASK: Add danmaku log recording
- In app/storage.py, add a new table danmaku_log:
  CREATE TABLE IF NOT EXISTS danmaku_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    uname TEXT NOT NULL,
    content TEXT NOT NULL
  );
- Add method record_danmaku(ts, uid, uname, content) with max_entries enforcement
- Add method get_danmaku_log(limit=50, offset=0) for retrieval
- Add method clear_danmaku_log()

- In app/config.py FeatureConfig, add:
  danmaku_log_enabled: bool = False
  danmaku_log_max_entries: int = 1000

- In app/bot.py _on_danmaku, record danmaku when enabled (after all other processing)

- In app/web/server.py:
  - Add /api/danmaku_log GET endpoint with limit/offset params
  - Add /api/danmaku_log DELETE endpoint (clear)
  - Add danmaku log UI tab in the Vue frontend
  - Add toggle + max_entries in the config tab

After changes: verify syntax, git commit.
' 2>&1 || log "Phase 3 failed"

log "=== Phase 3 complete ==="

# ── Phase 4: 转发/点赞感谢 ──
log "=== Phase 4: 转发点赞 ==="

$OC '
Continue work on the ayabot project at /home/shinshi/ayabot.

TASK: Like/share thanks
- Research bilibili-api-python for SHARE and LIKE events
- In app/bot.py, add _on_share and _on_like handlers
- For LIKE: track per-UID like count, thank once at 50 likes, then stop
- For SHARE: thank for each share event
- Add config options: features.share_thanks_enabled, features.like_thanks_enabled
- Add templates: features.share_template, features.like_template
- Default values: share="感谢分享直播间~", like="感谢50个点赞~"
- Add UI fields for these in the config tab

After changes: verify syntax, git commit.
' 2>&1 || log "Phase 4 failed"

log "=== Phase 4 complete ==="

# ── Phase 5: UID配置表格UI ──
log "=== Phase 5: UID表格UI ==="

$OC '
Continue work on the ayabot project at /home/shinshi/ayabot.

TASK: Add UID configuration table UI in the config tab
- In app/web/server.py, add a section in the config tab for "UID 自定义配置"
- This should be a collapsible section with a table
- Each row: UID input, welcome template input, keyword rules (comma-separated)
- Add/remove row buttons
- Save sends array to backend
- Backend stores in config.yaml under features.uid_configs array
- Each entry: { uid: int, welcome_template: str, keyword_rules: [str] }

After changes: verify syntax, git commit.
' 2>&1 || log "Phase 5 failed"

log "=== Phase 5 complete ==="

log "=== ALL PHASES COMPLETE ==="
git push 2>/dev/null || true
