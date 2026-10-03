# -*- coding: utf-8 -*-
"""
FreeFire Level Up Bot - Web Dashboard & Real-Time EXP Tracker
Embedded Async Web Server (aiohttp)
"""

import asyncio
import json
import os
import time
from typing import Dict, List, Any, Optional
from aiohttp import web

ACCOUNTS_FILE = "accounts.json"
TOKEN_CACHE_FILE = "token_cache.json"
DEVICES_FILE = "devices.json"
STUCK_TIMEOUT = 300  # 5 minutes

EXTERNAL_CACHE_INVALIDATOR = None


# ============================================================
# BULLETPROOF DELETE HELPERS
# ============================================================
def _scan_cache_for_identifiers(given: str) -> set:
    """
    Read token_cache.json and return EVERY identifier that belongs to the
    same account as `given`. Handles the case where `given` is a guest uid
    but the cache key is the account_id, or vice versa.
    """
    found = set()
    if not given:
        return found
    if not os.path.exists(TOKEN_CACHE_FILE):
        return found
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        cache = json.loads(raw) if raw else {}
        if not isinstance(cache, dict):
            return found
    except Exception:
        return found

    for key, val in cache.items():
        if not isinstance(val, dict):
            continue
        # Collect every identifier this entry knows about
        entry_ids = {str(key).strip()}
        for field in ("account_id", "auth_uid", "open_id", "uid"):
            fv = val.get(field)
            if fv is not None:
                entry_ids.add(str(fv).strip())
        for field in ("auth_token", "access_token", "token"):
            fv = val.get(field)
            if fv:
                entry_ids.add(f"tok_{str(fv)[:20]}")

        # If any of them equals `given`, absorb the whole set
        if given in entry_ids:
            found.update(entry_ids)
    return found


def _scan_devices_for_identifiers(given: str) -> set:
    """
    Read devices.json and return EVERY key that belongs to the same account
    as `given`. devices.json is keyed by whatever identifier was passed to
    get_device_for_account() — could be guest uid OR open_id.
    """
    found = set()
    if not given:
        return found
    if not os.path.exists(DEVICES_FILE):
        return found
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        devices = json.loads(raw) if raw else {}
        if not isinstance(devices, dict):
            return found
    except Exception:
        return found

    # Also consult token_cache to map account_id <-> guest_uid <-> open_id
    related = _scan_cache_for_identifiers(given) | {given}

    for key, val in list(devices.items()):
        key_str = str(key).strip()
        if key_str == given or key_str in related:
            found.add(key)
            continue
        if isinstance(val, dict):
            for field in ("account_id", "auth_uid", "open_id", "uid"):
                fv = val.get(field)
                if fv is not None:
                    fv_str = str(fv).strip()
                    if fv_str == given or fv_str in related:
                        found.add(key)
                        break
    return found


def _nuke_cache_by_any_id(ids: set) -> list:
    """Aggressively remove any token_cache.json entry matching ANY id."""
    if not os.path.exists(TOKEN_CACHE_FILE):
        return []
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        cache = json.loads(raw) if raw else {}
        if not isinstance(cache, dict):
            return []
    except Exception as e:
        print(f"[NUKE] token_cache read error: {e}")
        return []

    lookup = set()
    for i in ids:
        if i is None:
            continue
        s = str(i).strip()
        if not s:
            continue
        lookup.add(s)
        if len(s) >= 20 and not s.isdigit():
            lookup.add(f"tok_{s[:20]}")

    removed = []
    for key, val in list(cache.items()):
        key_str = str(key).strip()
        hit = False

        if key_str in lookup:
            hit = True

        if not hit and key_str.startswith("tok_"):
            for lk in lookup:
                if key_str == f"tok_{lk[:20]}":
                    hit = True
                    break

        if not hit and isinstance(val, dict):
            for field in ("account_id", "auth_uid", "open_id", "uid",
                          "auth_token", "access_token", "token"):
                fv = val.get(field)
                if fv is None:
                    continue
                fv_str = str(fv).strip()
                if not fv_str:
                    continue
                if fv_str in lookup:
                    hit = True
                    break
                if field in ("auth_token", "access_token", "token"):
                    if len(fv_str) >= 20:
                        for lk in lookup:
                            if lk == f"tok_{fv_str[:20]}":
                                hit = True
                                break
                if hit:
                    break

        if hit:
            removed.append(key)

    if removed:
        for k in removed:
            cache.pop(k, None)
        tmp = TOKEN_CACHE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
            os.replace(tmp, TOKEN_CACHE_FILE)
            print(f"\033[93m[NUKE] token_cache.json removed keys: {removed}\033[0m")
        except Exception as e:
            print(f"[NUKE] token_cache write error: {e}")
    else:
        print(f"\033[93m[NUKE] token_cache.json matched nothing for ids: {lookup}\033[0m")
    return removed


def _nuke_devices_by_any_id(ids: set) -> list:
    """Aggressively remove any devices.json entry matching ANY id."""
    if not os.path.exists(DEVICES_FILE):
        return []
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        devices = json.loads(raw) if raw else {}
        if not isinstance(devices, dict):
            return []
    except Exception as e:
        print(f"[NUKE] devices read error: {e}")
        return []

    lookup = {str(i).strip() for i in ids if i is not None and str(i).strip()}
    # Also add tok_<20> forms
    for s in list(lookup):
        if len(s) >= 20 and not s.isdigit():
            lookup.add(f"tok_{s[:20]}")

    removed = []
    for key, val in list(devices.items()):
        key_str = str(key).strip()
        if key_str in lookup:
            removed.append(key)
            continue
        if isinstance(val, dict):
            hit = False
            for field in ("account_id", "auth_uid", "open_id", "uid"):
                fv = val.get(field)
                if fv is not None and str(fv).strip() in lookup:
                    hit = True
                    break
            if hit:
                removed.append(key)

    if removed:
        for k in removed:
            devices.pop(k, None)
        try:
            with open(DEVICES_FILE, "w", encoding="utf-8") as f:
                json.dump(devices, f, indent=4)
            print(f"\033[93m[NUKE] devices.json removed keys: {removed}\033[0m")
        except Exception as e:
            print(f"[NUKE] devices write error: {e}")
    return removed


class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 300
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.paused_accounts: set = set()
        self.deleted_accounts: set = set()
        self.identity_map: Dict[str, set] = {}

    def log(self, message: str, level: str = "info", uid: Optional[str] = None, category: str = None):
        if category is None:
            msg_lower = message.lower()
            if any(k in msg_lower for k in ['match', 'startmatch', 'lone wolf', 'udp', 'thunder', 'sharma',
                                             'loading', 'searching', 'parallel matches']):
                category = "match"
            elif any(k in msg_lower for k in ['login', 'connect', 'tcp', 'gateway', 'dns', 'cache', 'session',
                                               'reconnect', 'auth', 'token']):
                category = "connection"
            elif any(k in msg_lower for k in ['exp', 'level', 'gained', 'initial']):
                category = "exp"
            else:
                category = "system"

        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": uid,
            "category": category,
            "timestamp": time.time(),
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def _track_identity(self, account_data: Dict):
        try:
            acc_id = str(account_data.get("account_id", "")).strip()
            if not acc_id:
                return
            ids = self.identity_map.setdefault(acc_id, set())
            ids.add(acc_id)
            if account_data.get("auth_uid"):
                ids.add(str(account_data["auth_uid"]))
            if account_data.get("open_id"):
                ids.add(str(account_data["open_id"]))
            if account_data.get("auth_token"):
                ids.add(f"tok_{str(account_data['auth_token'])[:20]}")
        except Exception:
            pass

    def register_account(self, uid: str, nickname: str, region: str, level: int, exp: int, likes: int = 0):
        uid_str = str(uid)
        if uid_str in self.deleted_accounts:
            return
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": level or 1,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "status": "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_match_timestamp": time.time(),
                "connected_at": time.time(),
                "last_updated": time.strftime("%H:%M:%S"),
            }
        else:
            acc = self.accounts[uid_str]
            if nickname: acc["nickname"] = nickname
            if region: acc["region"] = region
            if level: acc["level"] = level
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
            if "last_match_timestamp" not in acc:
                acc["last_match_timestamp"] = time.time()
            if "connected_at" not in acc:
                acc["connected_at"] = time.time()
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.deleted_accounts:
            return
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            initial = acc["initial_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - initial)
            acc["last_updated"] = time.strftime("%H:%M:%S")
            diff = current_exp - old_exp
            if diff > 0:
                self.log(
                    f"EXP UPDATE [{acc['nickname']}] Initial: {initial:,} → Current: {current_exp:,} "
                    f"(+{diff:,} this cycle | Total gained: +{acc['gained_exp']:,}) | Level {acc['level']}",
                    "success", uid_str, "exp"
                )
            self.recalc_totals()

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.deleted_accounts:
            return
        if uid_str not in self.accounts:
            return
        if uid_str in self.paused_accounts and status != "PAUSED":
            return
        if self.accounts[uid_str]["status"] != status:
            self.accounts[uid_str]["status"] = status
        if active_matches is not None:
            self.accounts[uid_str]["active_matches"] = active_matches
        self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid: str):
        uid_str = str(uid)
        if uid_str in self.deleted_accounts:
            return
        self.total_matches += 1
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            acc["matches_played"] += 1
            acc["last_match_time"] = time.strftime("%H:%M:%S")
            acc["last_match_timestamp"] = time.time()
            acc["last_updated"] = time.strftime("%H:%M:%S")
            self.log(
                f"MATCH COMPLETED [{acc['nickname']}] Match #{acc['matches_played']} finished | "
                f"Total gained: +{acc['gained_exp']:,} EXP",
                "success", uid_str, "match"
            )

    def pause_account(self, uid: str):
        uid_str = str(uid)
        if uid_str in self.deleted_accounts:
            return
        self.paused_accounts.add(uid_str)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = "PAUSED"
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
        self.log(f"Account {uid_str} PAUSED - no new matches will be started.", "warning", uid_str, "system")

    def resume_account(self, uid: str):
        uid_str = str(uid)
        if uid_str in self.deleted_accounts:
            return
        self.paused_accounts.discard(uid_str)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = "ONLINE"
            self.accounts[uid_str]["connected_at"] = time.time()
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
        self.log(f"Account {uid_str} RESUMED - starting new matches.", "success", uid_str, "system")

    def is_paused(self, uid: str) -> bool:
        return str(uid) in self.paused_accounts

    def is_deleted(self, uid: str) -> bool:
        return str(uid) in self.deleted_accounts

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())

    # ============================================================
    # FULL DELETE
    # ============================================================
    def full_delete_account(self, uid: str) -> bool:
        given = str(uid).strip()
        removed_any = False

        # Build identifier set
        all_ids = {given}
        if given in self.identity_map:
            all_ids.update(self.identity_map[given])
        for acc_id, id_set in self.identity_map.items():
            if given in id_set:
                all_ids.add(acc_id)
                all_ids.update(id_set)
        for key, cred in self.account_credentials.items():
            if not isinstance(cred, dict):
                continue
            cred_ids = {
                str(cred.get("account_id", "")),
                str(cred.get("auth_uid", "")),
                str(cred.get("open_id", "")),
                str(key),
            }
            if given in cred_ids:
                all_ids.update(cred_ids)
                if cred.get("auth_token"):
                    all_ids.add(f"tok_{str(cred['auth_token'])[:20]}")
        for acc_key, acc in self.accounts.items():
            if str(acc.get("uid", "")) == given:
                all_ids.add(acc_key)
                cred = self.account_credentials.get(str(acc_key))
                if isinstance(cred, dict):
                    for f in ("account_id", "auth_uid", "open_id"):
                        v = cred.get(f)
                        if v:
                            all_ids.add(str(v))
                    if cred.get("auth_token"):
                        all_ids.add(f"tok_{str(cred['auth_token'])[:20]}")

        # Scan accounts.json
        try:
            if os.path.exists(ACCOUNTS_FILE):
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    accs = json.load(f)
                if isinstance(accs, list):
                    for a in accs:
                        if not isinstance(a, dict):
                            continue
                        if (str(a.get("uid", "")).strip() == given
                                or str(a.get("account_id", "")).strip() == given):
                            if a.get("uid"):
                                all_ids.add(str(a["uid"]))
                            if a.get("account_id"):
                                all_ids.add(str(a["account_id"]))
                            if a.get("token"):
                                all_ids.add(f"tok_{str(a['token'])[:20]}")
        except Exception:
            pass

        # Scan token_cache.json to bridge uid <-> account_id <-> open_id
        try:
            cache_related = _scan_cache_for_identifiers(given)
            all_ids.update(cache_related)
        except Exception:
            pass

        all_ids.discard("")
        all_ids.discard("None")

        print(f"\033[93m[DELETE] Identifier set for '{given}': {all_ids}\033[0m")

        for i in all_ids:
            self.deleted_accounts.add(i)

        # 1. accounts.json
        try:
            existing = []
            if os.path.exists(ACCOUNTS_FILE):
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            if not isinstance(existing, list):
                existing = []
            before = len(existing)
            existing = [
                acc for acc in existing
                if str(acc.get("uid", "")).strip() not in all_ids
                and str(acc.get("account_id", "")).strip() not in all_ids
            ]
            if len(existing) != before:
                with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(existing, f, indent=2)
                removed_any = True
                print(f"\033[93m[DELETE] accounts.json: removed {before - len(existing)}\033[0m")
        except Exception as e:
            print(f"[DELETE] accounts.json error: {e}")

        # 2. token_cache.json — hook + guaranteed nuke
        try:
            if EXTERNAL_CACHE_INVALIDATOR is not None:
                try:
                    EXTERNAL_CACHE_INVALIDATOR(list(all_ids))
                except Exception as e:
                    print(f"[DELETE] hook error (continuing): {e}")
            nuked = _nuke_cache_by_any_id(all_ids)
            if nuked:
                removed_any = True
        except Exception as e:
            print(f"[DELETE] token_cache error: {e}")

        # 3. devices.json — find keys via cache scan + value match, then nuke
        try:
            dev_keys = _scan_devices_for_identifiers(given)
            all_ids.update(dev_keys)  # absorb device keys into identifier set
            nuked_dev = _nuke_devices_by_any_id(all_ids)
            if nuked_dev:
                print(f"\033[93m[DELETE] devices.json: removed {nuked_dev}\033[0m")
        except Exception as e:
            print(f"[DELETE] devices error: {e}")

        # 4. Cancel workers
        for i in list(all_ids):
            if i in self.account_workers:
                try: self.account_workers[i].cancel()
                except Exception: pass
                try: del self.account_workers[i]
                except Exception: pass

        # 5. In-memory state
        for i in list(all_ids):
            self.accounts.pop(i, None)
            self.paused_accounts.discard(i)
            self.account_credentials.pop(i, None)
            self.identity_map.pop(i, None)

        # 6. Match counters
        try:
            from app import _match_counters
            for i in all_ids:
                _match_counters.pop(i, None)
        except Exception:
            pass

        self.recalc_totals()
        return removed_any

    def cleanup_stuck_accounts(self) -> List[str]:
        now = time.time()
        stuck_uids = []
        for uid_str, acc in list(self.accounts.items()):
            if uid_str in self.paused_accounts:
                continue
            if uid_str in self.deleted_accounts:
                continue
            status = acc.get("status", "")
            if status in ("SEARCHING", "ONLINE", "CONNECTING"):
                last_active = max(
                    acc.get("last_match_timestamp", 0),
                    acc.get("connected_at", 0)
                )
                if last_active > 0 and (now - last_active) > STUCK_TIMEOUT:
                    stuck_uids.append(uid_str)

        for uid in stuck_uids:
            nickname = self.accounts.get(uid, {}).get("nickname", "Unknown")
            self.log(
                f"AUTO-CLEANUP: {nickname} ({uid}) stuck for 5+ minutes without finding a match. "
                f"Removing from accounts.json, token_cache.json, devices.json permanently.",
                "error", uid, "system"
            )
            self.full_delete_account(uid)

        return stuck_uids


bot_state = BotState()


# ==================== BOOT-TIME CLEANUP ====================
async def boot_cleanup_stale():
    try:
        accounts = []
        if os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accounts = json.load(f)
                if not isinstance(accounts, list):
                    accounts = []

        valid_uids = set()
        for acc in accounts:
            uid = str(acc.get("uid", "")).strip()
            if uid:
                valid_uids.add(uid)

        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                if not isinstance(cache, dict):
                    cache = {}
                purged = 0
                keys_to_del = []
                for key, val in cache.items():
                    if not isinstance(val, dict):
                        keys_to_del.append(key)
                        continue
                    acc_id = str(val.get("account_id", "")).strip()
                    auth_uid = str(val.get("auth_uid", "")).strip()
                    if acc_id and acc_id not in valid_uids and auth_uid not in valid_uids:
                        keys_to_del.append(key)
                for k in keys_to_del:
                    cache.pop(k, None)
                    purged += 1
                if purged:
                    with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump(cache, f, indent=2)
                    print(f"\033[93m[BOOT-CLEANUP] Purged {purged} orphan entries from token_cache.json\033[0m")
            except Exception as e:
                print(f"[BOOT-CLEANUP] token_cache error: {e}")

        if os.path.exists(DEVICES_FILE):
            try:
                with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                    devices = json.load(f)
                if not isinstance(devices, dict):
                    devices = {}
                purged_dev = 0
                dev_keys_to_del = []
                for k, v in devices.items():
                    if not isinstance(v, dict):
                        dev_keys_to_del.append(k)
                        continue
                    acc_id = str(v.get("account_id", "")).strip()
                    if str(k) not in valid_uids and (not acc_id or acc_id not in valid_uids):
                        dev_keys_to_del.append(k)
                for k in dev_keys_to_del:
                    devices.pop(k, None)
                    purged_dev += 1
                if purged_dev:
                    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
                        json.dump(devices, f, indent=4)
                    print(f"\033[93m[BOOT-CLEANUP] Purged {purged_dev} orphan entries from devices.json\033[0m")
            except Exception as e:
                print(f"[BOOT-CLEANUP] devices error: {e}")

    except Exception as e:
        print(f"[BOOT-CLEANUP] Error: {e}")


# ==================== HTTP HANDLERS ====================

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")


async def handle_index(request: web.Request) -> web.Response:
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = "<h1>templates/index.html not found!</h1>"
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_get_stats(request: web.Request) -> web.Response:
    added_accounts = []
    try:
        if os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                added_accounts = json.load(f)
            if not isinstance(added_accounts, list):
                added_accounts = []
    except Exception:
        added_accounts = []

    runtime = list(bot_state.accounts.values())
    runtime_by_uid = {str(a["uid"]): a for a in runtime}

    merged: List[Dict[str, Any]] = []
    seen_uids = set()

    for acc in added_accounts:
        raw_uid = str(acc.get("uid", "")).strip()
        raw_token = acc.get("token", "")
        lookup_key = raw_uid if raw_uid else f"tok_{str(raw_token)[:20]}"

        if lookup_key and lookup_key in bot_state.deleted_accounts:
            continue

        runtime_acc = None
        if raw_uid and raw_uid in runtime_by_uid:
            runtime_acc = runtime_by_uid[raw_uid]
        else:
            for rt in runtime:
                rt_auth_uid = ""
                creds = bot_state.account_credentials.get(str(rt["uid"]), {})
                if isinstance(creds, dict):
                    rt_auth_uid = str(creds.get("auth_uid", ""))
                if raw_uid and rt_auth_uid == raw_uid:
                    runtime_acc = rt
                    break
                if raw_token and isinstance(creds, dict):
                    if creds.get("auth_token") == raw_token:
                        runtime_acc = rt
                        break

        if runtime_acc:
            merged.append(runtime_acc)
            seen_uids.add(str(runtime_acc["uid"]))
        else:
            placeholder_uid = raw_uid or f"pending_{lookup_key}"
            if placeholder_uid in bot_state.deleted_accounts:
                continue
            status = "CONNECTING"
            if raw_uid and raw_uid in bot_state.paused_accounts:
                status = "PAUSED"
            merged.append({
                "uid": placeholder_uid,
                "nickname": f"Pending_{placeholder_uid[:6]}" if placeholder_uid else "Pending",
                "region": "BD",
                "level": 1,
                "initial_exp": 0,
                "current_exp": 0,
                "gained_exp": 0,
                "likes": 0,
                "status": status,
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_match_timestamp": time.time(),
                "connected_at": time.time(),
                "last_updated": time.strftime("%H:%M:%S"),
                "_pending": True,
                "_source_uid": raw_uid,
                "_source_token": raw_token[:10] + "..." if raw_token else "",
            })
            seen_uids.add(placeholder_uid)

    for rt in runtime:
        if str(rt["uid"]) not in seen_uids:
            merged.append(rt)

    def sort_key(a):
        order = {"CONNECTING": 0, "PENDING": 1, "SEARCHING": 2, "ONLINE": 3, "IN_MATCH": 4,
                 "PAUSED": 5, "ERROR": 6, "OFFLINE": 7}
        return (order.get(a.get("status", "ONLINE"), 9), -a.get("gained_exp", 0))
    merged.sort(key=sort_key)

    return web.json_response({
        "total_accounts": len(merged),
        "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "accounts": merged,
        "logs": bot_state.logs[-100:],
        "uptime": int(time.time() - bot_state.start_time)
    })


async def handle_add_account(request: web.Request) -> web.Response:
    """
    Add account. If the account was previously deleted, we FULLY PURGE it from
    the kill-switch, token_cache.json and devices.json first, so the fresh
    login can create brand-new cache entries and the account does NOT stay
    stuck in PENDING.
    """
    try:
        data = await request.json()
        existing = []
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []

        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID and Password required"})

            # ---- FULL UN-DELETE / PURGE ----
            # 1) Remove uid AND every identifier related to it from kill-switch
            related = _scan_cache_for_identifiers(uid) | {uid}
            for rid in related:
                bot_state.deleted_accounts.discard(rid)
            # Also discard anything still in identity_map
            for acc_id, ids in list(bot_state.identity_map.items()):
                if uid in ids:
                    for i in ids:
                        bot_state.deleted_accounts.discard(i)

            # 2) Nuke ANY stale cache entry related to this uid so login is fresh
            _nuke_cache_by_any_id({uid} | related)
            _nuke_devices_by_any_id({uid} | related)

            # 3) Remove any runtime state for this account
            for rid in related:
                bot_state.accounts.pop(rid, None)
                bot_state.account_credentials.pop(rid, None)
                bot_state.paused_accounts.discard(rid)

            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            existing.append({"uid": uid, "password": pwd})
            bot_state.log(f"New account added: UID {uid}", "success", uid, "system")

        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token required"})

            tok_key = f"tok_{token[:20]}"
            related = _scan_cache_for_identifiers(tok_key) | {tok_key}
            for rid in related:
                bot_state.deleted_accounts.discard(rid)
            _nuke_cache_by_any_id({tok_key} | related)
            _nuke_devices_by_any_id({tok_key} | related)

            existing = [acc for acc in existing if acc.get("token") != token]
            existing.append({"token": token})
            bot_state.log(f"New account added: Token {token[:10]}...", "success", None, "system")
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})

        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

        if "on_account_added" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](data))

        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})

        extra_ids = data.get("extra_ids", []) or []
        account_id = data.get("account_id")
        if account_id:
            extra_ids.append(str(account_id))
        for extra_field in ("auth_uid", "open_id", "token"):
            v = data.get(extra_field)
            if v:
                extra_ids.append(str(v))

        for eid in list(extra_ids) + [uid]:
            if eid and str(eid).strip():
                bot_state.deleted_accounts.add(str(eid).strip())

        pending_workers = list(bot_state.account_workers.values())

        bot_state.full_delete_account(uid)

        if pending_workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending_workers, return_exceptions=True),
                    timeout=3.0
                )
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

        try:
            if EXTERNAL_CACHE_INVALIDATOR is not None:
                EXTERNAL_CACHE_INVALIDATOR([uid] + [str(e) for e in extra_ids if e])
        except Exception:
            pass

        # Final guaranteed nukes for both files
        try:
            _nuke_cache_by_any_id({uid} | {str(e) for e in extra_ids if e})
            _nuke_devices_by_any_id({uid} | {str(e) for e in extra_ids if e})
        except Exception:
            pass

        bot_state.log(
            f"Account {uid} permanently deleted from accounts.json + token_cache.json + devices.json.",
            "warning", uid, "system"
        )
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_pause_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        bot_state.pause_account(uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_resume_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        bot_state.resume_account(uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_refresh_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        if "on_refresh_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_refresh_account"](uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_cleanup_stuck(request: web.Request) -> web.Response:
    try:
        removed = bot_state.cleanup_stuck_accounts()
        return web.json_response({"status": "ok", "removed": removed})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def start_web_dashboard(host: str = "0.0.0.0", port: int = 5000):
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/pause", handle_pause_account)
    app.router.add_post("/api/account/resume", handle_resume_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/cleanup/stuck", handle_cleanup_stuck)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\033[92m[+] Web Dashboard running on http://localhost:{port}\033[0m")
