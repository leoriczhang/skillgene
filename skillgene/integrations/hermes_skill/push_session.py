#!/usr/bin/env python3
"""on_session_end hook: push the just-ended Hermes session into SkillGene.

Portable & config-driven — nothing about a specific machine is baked in. All
of server URL, username, DB path and API key resolve from (in priority order):

    1. explicit env vars (SKILLGENE_URL / SKILLGENE_USER / HERMES_STATE_DB /
       EVOLVE_INGEST_API_KEY)
    2. a ``feed.json`` next to this script (written by install.py)
    3. built-in fallbacks — only for DB path (Hermes default state.db).

The SkillGene service URL and username have NO built-in default: they must be
supplied explicitly. If either is missing the hook skips silently instead of
guessing a host.

Wire protocol (Hermes shell hooks):
- stdin  : JSON payload; we read ``session_id``.
- stdout : JSON status (ignored by Hermes for on_session_end).

Zero third-party dependencies on purpose — stdlib only (``sqlite3`` +
``urllib``) so it runs under whatever interpreter Hermes spawns it with.

Idempotent: SkillGene keys the queue by ``session_id`` (put_object overwrites),
so firing on every turn simply refreshes the latest, fullest snapshot.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

# --- constants -------------------------------------------------------------
# No default base_url on purpose: the SkillGene service address must be
# supplied explicitly (env / feed.json). If it is missing we skip silently
# rather than guess a host.
MAX_CHARS = 8000  # cap any single message body so payloads stay reasonable


def _log(msg: str) -> None:
    # Hooks capture stderr into ~/.hermes/errors.log; keep it terse.
    print(f"[skillgene-feed] {msg}", file=sys.stderr)


def _config_path() -> Path:
    override = os.environ.get("SKILLGENE_FEED_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().with_name("feed.json")


def _load_config() -> dict:
    path = _config_path()
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8")) or {}
        except (ValueError, OSError):
            return {}
    return {}


def _hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    # Mirror hermes_constants.get_hermes_home() default on Linux/macOS.
    return Path.home() / ".hermes"


def _state_db_path(cfg: dict) -> Path:
    override = os.environ.get("HERMES_STATE_DB", "").strip() or cfg.get("state_db")
    if override:
        return Path(override).expanduser()
    return _hermes_home() / "state.db"


def _fold_turns(rows: list[sqlite3.Row]) -> list[dict]:
    """Fold role/content messages into [{prompt_text, response_text}] turns.

    A turn opens on a user message and closes on the next assistant message.
    Tool/system messages are ignored for the prompt/response text (they add
    noise the summarizer does not need). Consecutive users or assistants are
    coalesced so nothing is dropped.
    """
    turns: list[dict] = []
    cur_prompt: list[str] = []
    cur_response: list[str] = []

    def _flush() -> None:
        prompt = "\n".join(p for p in cur_prompt if p).strip()[:MAX_CHARS]
        response = "\n".join(r for r in cur_response if r).strip()[:MAX_CHARS]
        if prompt or response:
            turns.append({"prompt_text": prompt, "response_text": response})
        cur_prompt.clear()
        cur_response.clear()

    for row in rows:
        role = (row["role"] or "").lower()
        content = (row["content"] or "").strip()
        if not content:
            continue
        if role == "user":
            if cur_response:  # a new user turn after we already have a reply
                _flush()
            cur_prompt.append(content)
        elif role == "assistant":
            cur_response.append(content)
        # system / tool messages are intentionally skipped for text.
    _flush()
    return turns


def _read_session(db_path: Path, session_id: str) -> dict | None:
    if not db_path.exists():
        _log(f"state db not found: {db_path}")
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        _log(f"cannot open state db: {exc}")
        return None
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content, timestamp FROM messages "
            "WHERE session_id = ? AND active = 1 ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        meta = conn.execute(
            "SELECT started_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    except sqlite3.Error as exc:
        _log(f"query failed: {exc}")
        return None
    finally:
        conn.close()

    turns = _fold_turns(rows)
    if not turns:
        return None

    title = turns[0]["prompt_text"].splitlines()[0][:120] if turns[0]["prompt_text"] else ""
    session: dict = {"session_id": session_id, "turns": turns}
    if title:
        session["title"] = title
    if meta and meta["started_at"]:
        session["timestamp"] = str(meta["started_at"])
    return session


def _post(base_url: str, session: dict, user: str, api_key: str) -> None:
    session = dict(session)
    session["user_alias"] = user
    url = base_url.rstrip("/") + "/ingest_session"
    data = json.dumps(session, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            _log(f"ingested {session['session_id']} ({len(session['turns'])} turns): {body[:200]}")
    except urllib.error.HTTPError as exc:
        _log(f"ingest failed HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as exc:
        _log(f"ingest unreachable at {url}: {exc.reason}")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        _log("no session_id on stdin; nothing to push")
        return 0

    cfg = _load_config()
    user = str(os.environ.get("SKILLGENE_USER") or cfg.get("user_alias") or "").strip()
    if not user:
        _log("no user_alias configured (run install.py or set SKILLGENE_USER)")
        return 0
    base_url = str(os.environ.get("SKILLGENE_URL") or cfg.get("base_url") or "").strip()
    if not base_url:
        _log("no base_url configured (run install.py --url ... or set SKILLGENE_URL)")
        return 0
    api_key = str(os.environ.get("EVOLVE_INGEST_API_KEY") or cfg.get("api_key") or "")

    session = _read_session(_state_db_path(cfg), session_id)
    if not session:
        _log(f"session {session_id} had no foldable turns; skipped")
        return 0

    _post(base_url, session, user, api_key)
    print(json.dumps({"action": "allow"}))  # for `hermes hooks test`
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
