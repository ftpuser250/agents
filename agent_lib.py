"""
agent_lib.py — Multi-agent coordination library for Claude relay sessions
Import via: exec(open('/tmp/agents/agent_lib.py').read())

Provides: register_agent, heartbeat, lock_resource, check_lock, release_lock,
          post_task, claim_next_task, complete_task, fail_task, log_event,
          list_active_agents, get_queue
"""

import uuid, json, os, subprocess, datetime, time, hashlib, threading
from datetime import timezone

# ─── Config ───────────────────────────────────────────────────────────────────

_AGENTS_REPO   = "/tmp/agents"
_RELAY_TOOLS   = "/tmp/relay-tools"
_HEARTBEAT_TTL = 300  # seconds — agent considered dead after this with no update
_LOCK_DEFAULT_TTL = 180  # seconds

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now():
    return datetime.datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def _read_json(path, default=None):
    try:
        return json.loads(open(path).read())
    except Exception:
        return default

def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    open(tmp, "w").write(json.dumps(data, indent=2))
    os.replace(tmp, path)

def _git_push(repo, message):
    """Add all, commit, push. Returns (success, output)."""
    try:
        subprocess.run(["git", "-C", repo, "pull", "--rebase", "--autostash"], 
                       capture_output=True, timeout=15)
        subprocess.run(["git", "-C", repo, "add", "-A"], 
                       capture_output=True, timeout=10)
        r = subprocess.run(["git", "-C", repo, "commit", "-m", message], 
                           capture_output=True, text=True, timeout=10)
        if "nothing to commit" in r.stdout:
            return True, "nothing to commit"
        p = subprocess.run(["git", "-C", repo, "push"], 
                           capture_output=True, text=True, timeout=30)
        return p.returncode == 0, p.stdout + p.stderr
    except Exception as e:
        return False, str(e)

# ─── Agent Identity & Heartbeat ───────────────────────────────────────────────

_AGENT_ID = None
_heartbeat_thread = None

def register_agent(capabilities=None):
    """Generate agent ID, write heartbeat, update registry. Returns agent ID."""
    global _AGENT_ID, _heartbeat_thread

    _AGENT_ID = "agent-" + uuid.uuid4().hex[:8]
    caps = capabilities or ["shell_exec", "git", "docker", "mcp"]

    beat = {
        "agent_id":   _AGENT_ID,
        "started_at": _now(),
        "last_seen":  _now(),
        "status":     "idle",
        "current_task": None,
        "ttl_seconds": _HEARTBEAT_TTL,
        "capabilities": caps,
    }
    path = f"{_AGENTS_REPO}/active/{_AGENT_ID}.json"
    _write_json(path, beat)

    # Update registry
    reg_path = f"{_AGENTS_REPO}/registry.json"
    reg = _read_json(reg_path, {"agents": [], "port_allocations": {"allocated": {}}, "shared_resources": []})
    reg["agents"].append({
        "agent_id":   _AGENT_ID,
        "first_seen": _now(),
        "last_seen":  _now(),
        "capabilities": caps,
    })
    _write_json(reg_path, reg)

    # Push to repo
    _git_push(_AGENTS_REPO, f"agent-register: {_AGENT_ID}")

    # Start background heartbeat thread
    def _hb_loop():
        while True:
            time.sleep(60)
            try:
                heartbeat()
            except Exception:
                pass

    _heartbeat_thread = threading.Thread(target=_hb_loop, daemon=True)
    _heartbeat_thread.start()

    print(f"[agent_lib] Registered as {_AGENT_ID}")
    return _AGENT_ID


def heartbeat(status=None, current_task=None):
    """Update last_seen in active file. Call every ~60s."""
    if not _AGENT_ID:
        return
    path = f"{_AGENTS_REPO}/active/{_AGENT_ID}.json"
    beat = _read_json(path, {})
    beat["last_seen"] = _now()
    if status:
        beat["status"] = status
    if current_task is not None:
        beat["current_task"] = current_task
    _write_json(path, beat)
    _git_push(_AGENTS_REPO, f"heartbeat: {_AGENT_ID}")


def set_status(status, task=None):
    """Update agent status: idle | working | waiting"""
    heartbeat(status=status, current_task=task)


def deregister_agent():
    """Call at session end. Removes heartbeat, releases all locks."""
    if not _AGENT_ID:
        return
    # Release all locks held by this agent
    locks_dir = f"{_AGENTS_REPO}/locks"
    if os.path.exists(locks_dir):
        for f in os.listdir(locks_dir):
            if f.endswith(".lock"):
                lock = _read_json(f"{locks_dir}/{f}", {})
                if lock.get("owner") == _AGENT_ID:
                    os.remove(f"{locks_dir}/{f}")

    # Remove heartbeat
    path = f"{_AGENTS_REPO}/active/{_AGENT_ID}.json"
    if os.path.exists(path):
        os.remove(path)

    _git_push(_AGENTS_REPO, f"agent-deregister: {_AGENT_ID}")
    print(f"[agent_lib] Deregistered {_AGENT_ID}")

# ─── Resource Locking ─────────────────────────────────────────────────────────

def _lock_path(resource):
    safe = resource.replace("/", "_").replace(".", "_")
    return f"{_AGENTS_REPO}/locks/{safe}.lock"


def check_lock(resource):
    """Returns owner agent_id if locked and not expired, else None."""
    path = _lock_path(resource)
    lock = _read_json(path)
    if not lock:
        return None
    # Check TTL
    try:
        acquired = datetime.datetime.fromisoformat(lock["acquired_at"].rstrip("Z"))
        age = (datetime.datetime.utcnow() - acquired).total_seconds()
        if age > lock.get("ttl_seconds", _LOCK_DEFAULT_TTL):
            return None  # expired
    except Exception:
        return None
    return lock.get("owner")


def lock_resource(resource, agent_id=None, ttl=None):
    """
    Attempt to acquire lock. Returns True if acquired, False if blocked.
    Steals expired locks automatically.
    """
    aid = agent_id or _AGENT_ID
    owner = check_lock(resource)
    if owner and owner != aid:
        print(f"[agent_lib] ⚠️  {resource} locked by {owner}")
        return False

    lock = {
        "resource":    resource,
        "owner":       aid,
        "acquired_at": _now(),
        "ttl_seconds": ttl or _LOCK_DEFAULT_TTL,
    }
    _write_json(_lock_path(resource), lock)
    _git_push(_AGENTS_REPO, f"lock-acquire: {aid} → {resource}")
    return True


def release_lock(resource, agent_id=None):
    """Release a lock. Only owner can release (or if expired)."""
    aid = agent_id or _AGENT_ID
    owner = check_lock(resource)
    if owner is None or owner == aid:
        path = _lock_path(resource)
        if os.path.exists(path):
            os.remove(path)
        _git_push(_AGENTS_REPO, f"lock-release: {aid} → {resource}")
        return True
    print(f"[agent_lib] ⚠️  Cannot release {resource} — owned by {owner}")
    return False


def with_lock(resource, fn, ttl=None):
    """Context helper: acquire lock, run fn, release. Returns fn result or None."""
    if not lock_resource(resource, ttl=ttl):
        return None
    try:
        return fn()
    finally:
        release_lock(resource)

# ─── Task Queue ───────────────────────────────────────────────────────────────

def post_task(title, task_type="general", priority=3, payload=None, requires=None, locks_needed=None):
    """Post a new task to the pending queue."""
    task = {
        "id":           "task-" + uuid.uuid4().hex[:8],
        "title":        title,
        "type":         task_type,
        "priority":     priority,  # 1=critical 2=high 3=normal 4=low
        "payload":      payload or {},
        "requires":     requires or [],
        "locks_needed": locks_needed or [],
        "posted_by":    _AGENT_ID or "rubait",
        "posted_at":    _now(),
        "status":       "pending",
    }
    path = f"{_AGENTS_REPO}/queue/pending/{task['id']}.json"
    _write_json(path, task)
    _git_push(_AGENTS_REPO, f"task-post: {task['id']} [{task['title'][:40]}]")
    print(f"[agent_lib] Posted task {task['id']}: {title}")
    return task


def claim_next_task(agent_id=None):
    """
    Claim the highest-priority unclaimed task.
    Returns task dict or None if queue empty.
    """
    aid = agent_id or _AGENT_ID
    pending_dir = f"{_AGENTS_REPO}/queue/pending"
    if not os.path.exists(pending_dir):
        return None

    tasks = []
    for f in os.listdir(pending_dir):
        if f.endswith(".json") and f != ".gitkeep":
            t = _read_json(f"{pending_dir}/{f}")
            if t:
                tasks.append(t)

    if not tasks:
        return None

    # Sort by priority (1=highest), then posted_at
    tasks.sort(key=lambda t: (t.get("priority", 3), t.get("posted_at", "")))
    task = tasks[0]

    # Claim: move pending → claimed
    task["claimed_by"]  = aid
    task["claimed_at"]  = _now()
    task["status"]      = "claimed"

    claimed_path = f"{_AGENTS_REPO}/queue/claimed/{task['id']}.json"
    pending_path = f"{_AGENTS_REPO}/queue/pending/{task['id']}.json"

    _write_json(claimed_path, task)
    if os.path.exists(pending_path):
        os.remove(pending_path)

    # Update agent heartbeat
    set_status("working", task["title"])
    _git_push(_AGENTS_REPO, f"task-claim: {aid} → {task['id']}")
    print(f"[agent_lib] Claimed task {task['id']}: {task['title']}")
    return task


def complete_task(task_id, agent_id=None, result=None):
    """Mark task as done. Moves claimed → done."""
    aid = agent_id or _AGENT_ID
    claimed_path = f"{_AGENTS_REPO}/queue/claimed/{task_id}.json"
    task = _read_json(claimed_path, {})
    task["status"]       = "done"
    task["completed_by"] = aid
    task["completed_at"] = _now()
    task["result"]       = result or "completed"

    done_path = f"{_AGENTS_REPO}/queue/done/{task_id}.json"
    _write_json(done_path, task)
    if os.path.exists(claimed_path):
        os.remove(claimed_path)

    set_status("idle", None)
    _git_push(_AGENTS_REPO, f"task-done: {aid} → {task_id}")
    print(f"[agent_lib] Completed task {task_id}")


def fail_task(task_id, agent_id=None, error=None):
    """Mark task as failed. Moves claimed → failed."""
    aid = agent_id or _AGENT_ID
    claimed_path = f"{_AGENTS_REPO}/queue/claimed/{task_id}.json"
    task = _read_json(claimed_path, {})
    task["status"]    = "failed"
    task["failed_by"] = aid
    task["failed_at"] = _now()
    task["error"]     = error or "unknown error"

    failed_path = f"{_AGENTS_REPO}/queue/failed/{task_id}.json"
    _write_json(failed_path, task)
    if os.path.exists(claimed_path):
        os.remove(claimed_path)

    set_status("idle", None)
    _git_push(_AGENTS_REPO, f"task-fail: {aid} → {task_id} [{str(error)[:60]}]")
    print(f"[agent_lib] Failed task {task_id}: {error}")

# ─── Logging ──────────────────────────────────────────────────────────────────

def log_event(event_type, data=None):
    """Append a JSONL log entry for this agent's session."""
    if not _AGENT_ID:
        return
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    log_dir = f"{_AGENTS_REPO}/logs/{_AGENT_ID}"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/session-{today}.jsonl"
    entry = {
        "ts":         _now(),
        "agent_id":   _AGENT_ID,
        "event":      event_type,
        "data":       data or {},
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

# ─── Observation ──────────────────────────────────────────────────────────────

def list_active_agents():
    """List all agents with valid heartbeats."""
    active_dir = f"{_AGENTS_REPO}/active"
    if not os.path.exists(active_dir):
        return []
    agents = []
    for f in os.listdir(active_dir):
        if not f.endswith(".json"):
            continue
        beat = _read_json(f"{active_dir}/{f}")
        if not beat:
            continue
        # Check if alive
        try:
            last = datetime.datetime.fromisoformat(beat["last_seen"].rstrip("Z"))
            age  = (datetime.datetime.utcnow() - last).total_seconds()
            if age <= beat.get("ttl_seconds", _HEARTBEAT_TTL):
                beat["age_seconds"] = int(age)
                agents.append(beat)
        except Exception:
            pass
    return agents


def get_queue():
    """Return summary of task queue."""
    def _count(subdir):
        d = f"{_AGENTS_REPO}/queue/{subdir}"
        if not os.path.exists(d):
            return 0
        return len([f for f in os.listdir(d) if f.endswith(".json")])

    return {
        "pending": _count("pending"),
        "claimed": _count("claimed"),
        "done":    _count("done"),
        "failed":  _count("failed"),
    }


def status_report():
    """Print a human-readable status of all agents and queue."""
    agents = list_active_agents()
    queue  = get_queue()
    print(f"\n{'='*50}")
    print(f"[agent_lib] Active agents: {len(agents)}")
    for a in agents:
        print(f"  {a['agent_id']} | {a['status']} | task: {a.get('current_task') or 'idle'} | age: {a.get('age_seconds',0)}s")
    print(f"[agent_lib] Queue: {queue['pending']} pending, {queue['claimed']} claimed, {queue['done']} done, {queue['failed']} failed")
    print(f"{'='*50}\n")

print("[agent_lib] v1.0 loaded — call register_agent() to start")
