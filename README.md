# Agents — Multi-Agent Coordination System

Enables multiple concurrent Claude sessions to work in parallel without conflicting.

---

## Architecture

```
agents/
  active/           ← Heartbeat files — who's alive, what they're doing
  locks/            ← Resource locks — prevent two agents touching same thing
  queue/
    pending/        ← Unclaimed tasks
    claimed/        ← In-progress tasks
    done/           ← Completed tasks (archived)
    failed/         ← Failed tasks with error info
  logs/             ← Per-agent session logs (JSONL)
  registry.json     ← All known agents, last seen, capabilities
```

---

## Session Start Protocol

Every new Claude session MUST do this at start after loading relay.py:

```python
exec(open('/tmp/agents/agent_lib.py').read())
AGENT_ID = register_agent()   # returns e.g. "agent-7f3a2b1c"
```

---

## Resource Locking

Before modifying any shared resource, claim a lock:

```python
lock_resource("webhook_pc.py", AGENT_ID, ttl=120)
owner = check_lock("webhook_pc.py")
if owner and owner != AGENT_ID:
    print(f"⚠️ Locked by {owner} — wait or skip")
release_lock("webhook_pc.py", AGENT_ID)
```

**Always lock before touching:**
- `webhook_pc.py`, `context.json`, `HANDOFF.md`
- `relay-requests` (git push)
- `docker-{container-name}` (any container being modified)

---

## Task Queue

```python
post_task({"title": "Install mcp-ansible", "type": "install", "priority": 2})
task = claim_next_task(AGENT_ID)
complete_task(task["id"], AGENT_ID, result="installed on 3059")
fail_task(task["id"], AGENT_ID, error="npm 404")
```

---

## Relay Repo Isolation (per-agent subdirs)

| Repo | Per-agent path |
|---|---|
| `relay-requests` | `requests/{agent-id}/req_XXX.json` |
| `relay-responses` | `responses/{agent-id}/resp_XXX.json` |
| `relay-logs` | `logs/{agent-id}/` |
| `agents/logs` | `logs/{agent-id}/` |

---

## Conflict Rules

1. Check `active/` before starting — if another agent has the task, skip.
2. Acquire locks before touching shared resources.
3. Heartbeat every 60s — dead if no update in >TTL seconds.
4. Never write to another agent's subdir.
5. Post intent to `active/` before acting.
6. On session end — delete heartbeat, release locks, flush logs.

---

*Last updated: 2026-03-12 session 6*
Multi-agent coordination — task queue, resource locks, heartbeats, session logs
