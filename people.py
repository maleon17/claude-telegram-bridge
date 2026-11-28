"""Shared, dependency-free registry of Telegram people."""

import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone

PEOPLE_FILE = os.environ.get("BRIDGE_PEOPLE_FILE", "/var/lib/telegram-people/people.json")
ROLE_HELPER = "/usr/local/sbin/tenant-role"
_cache = None
_cache_key = None
_admins_cache = set()
_admins_until = 0.0


class ReadOnlyError(OSError):
    "people.json is read-only"


def _owner():
    return str(os.environ.get("OWNER_ID", ""))


def _valid_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool) and bool(re.fullmatch(r"[0-9]{5,15}", str(value)))


def _path():
    return os.environ.get("BRIDGE_PEOPLE_FILE", PEOPLE_FILE)


def _empty():
    return {"version": 1, "people": {}}


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _legacy_path():
    state = os.environ.get("BRIDGE_STATE_FILE")
    return os.path.join(os.path.dirname(os.path.abspath(state)) if state else os.path.dirname(__file__), "whitelist.txt")


def _legacy():
    data = _empty()
    try:
        with open(_legacy_path(), encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return data
    for token in re.split(r"[,\n]", raw):
        token = token.strip()
        if _valid_id(token):
            data["people"][token] = {"added": _stamp(), "added_by": _owner(), "note": "", "enabled": True, "rejected_until": None}
    return data


def _key():
    try:
        st = os.stat(_path())
        return (_path(), st.st_mtime_ns, st.st_size, st.st_ino)
    except FileNotFoundError:
        return (_path(), None)


def _read():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("version") != 1 or not isinstance(data.get("people"), dict):
            raise ValueError("invalid people.json")
        return data
    except FileNotFoundError:
        return _legacy()


def _write(data):
    path = _path()
    directory = os.path.dirname(os.path.abspath(path))
    tmp = None
    try:
        if os.path.exists(path) and not os.stat(path).st_mode & 0o222:
            raise ReadOnlyError(f"Cannot write {path}: file has no write permission")
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".people-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o640)
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
    except ReadOnlyError:
        raise
    except OSError as exc:
        raise ReadOnlyError(f"Cannot write {path}: {exc}") from exc
    finally:
        if tmp is not None:
            os.unlink(tmp)


def load():
    global _cache, _cache_key
    key = _key()
    if _cache is None or key != _cache_key:
        data = _read()
        if key[1] is None:
            try:
                _mutate(lambda current: None)
                data = _read()
            except ReadOnlyError:
                pass
        _cache, _cache_key = data, _key()
    return json.loads(json.dumps(_cache))


def _mutate(change):
    global _cache, _cache_key
    lock_path = _path() + ".lock"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
        with open(lock_path, "a+") as lock:
            os.fchmod(lock.fileno(), 0o640)
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = _read()
            result = change(data)
            _write(data)
            _cache, _cache_key = data, _key()
            return result
    except OSError as exc:
        if isinstance(exc, ReadOnlyError):
            raise
        raise ReadOnlyError(f"Cannot write {_path()}: {exc}") from exc


def is_allowed(value):
    if not _valid_id(value):
        return False
    value = str(value)
    if value == _owner():
        return True
    try:
        return load()["people"].get(value, {}).get("enabled") is True
    except (ValueError, AttributeError, TypeError):
        return False  # corrupt registry: fail closed, never crash the bridge


def add(value, added_by, note=""):
    if not _valid_id(value) or not _valid_id(added_by):
        raise ValueError("invalid Telegram ID")
    value = str(value)
    def change(data):
        if data["people"].get(value, {}).get("enabled") is True:
            return False
        data["people"][value] = {"added": _stamp(), "added_by": str(added_by), "note": str(note), "enabled": True, "rejected_until": None}
        return True
    return _mutate(change)


def remove(value):
    if not _valid_id(value):
        raise ValueError("invalid Telegram ID")
    return _mutate(lambda data: data["people"].pop(str(value), None) is not None)


def set_note(value, note):
    if not _valid_id(value):
        raise ValueError("invalid Telegram ID")
    def change(data):
        if str(value) not in data["people"]:
            raise KeyError(value)
        data["people"][str(value)]["note"] = str(note)
    _mutate(change)


def reject(value, days=30):
    if not _valid_id(value):
        raise ValueError("invalid Telegram ID")
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    def change(data):
        record = data["people"].setdefault(str(value), {"added": _stamp(), "added_by": _owner(), "note": "", "enabled": False, "rejected_until": None})
        record["enabled"] = False
        record["rejected_until"] = until
    _mutate(change)


def is_rejected(value):
    if not _valid_id(value):
        return False
    try:
        until = load()["people"].get(str(value), {}).get("rejected_until")
        return bool(until and datetime.fromisoformat(until) > datetime.now(timezone.utc))
    except (ValueError, TypeError):
        return False


def admins():
    global _admins_cache, _admins_until
    now = time.monotonic()
    if now < _admins_until:
        return set(_admins_cache)
    try:
        result = subprocess.run(["sudo", "-n", ROLE_HELPER, "list"], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise ValueError(result.stderr)
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            ids = list(data.get("static", [])) + list(data.get("dynamic", []))
        else:
            ids = data
        _admins_cache = {str(item) for item in ids if _valid_id(item)}
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        _admins_cache = set()
    _admins_until = now + 15
    return set(_admins_cache)


def role_of(value):
    if str(value) == _owner() and _valid_id(value):
        return "owner"
    if not _valid_id(value):
        return "none"
    if str(value) in admins():
        return "admin"
    return "member" if is_allowed(value) else "none"


def _role_change(action, value):
    global _admins_until
    if not _valid_id(value):
        return False, "Invalid Telegram ID"
    try:
        result = subprocess.run(["sudo", "-n", ROLE_HELPER, action, str(value)], capture_output=True, text=True, timeout=120)
        _admins_until = 0
        message = (result.stdout or result.stderr).strip()
        return result.returncode == 0, message or ("OK" if result.returncode == 0 else "tenant-role failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _admins_until = 0
        return False, str(exc)


def grant_admin(value):
    return _role_change("grant", value)


def revoke_admin(value):
    return _role_change("revoke", value)
