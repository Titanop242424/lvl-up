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
        # 🔒 HARD KILL SWITCH: any UID in here can NEVER be re-registered
        self.deleted_accounts: set = set()
        # Map account_id -> all linked identifiers (uid, open_id, tokens)
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
        """Track every known identifier for this account so delete works on all of them."""
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
            if account_data.get("auth_password"):
                pass  # don't track password
        except Exception:
            pass

    def register_account(self, uid: str, nickname: str, region: str, level: int, exp: int, likes: int = 0):
        uid_str = str(uid)
        # 🔒 HARD BLOCK: deleted accounts can never come back
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

    # ==================== FULL DELETE (permanent, cross-file, multi-identifier) ====================
    def full_delete_account(self, uid: str) -> bool:
        """
        Completely remove account from ALL state + files.

        Works with ANY identifier: guest uid, account_id, open_id, or token prefix.
        Uses the identity map to expand the given uid into ALL known identifiers,
        then removes every matching entry from accounts.json / token_cache.json / devices.json.
        """
        given = str(uid).strip()
        removed_any = False

        # Build the full set of identifiers to remove
        all_ids = {given}
        # Expand via identity map (given might be account_id OR uid)
        if given in self.identity_map:
            all_ids.update(self.identity_map[given])
        # Also scan identity map for entries where given appears as a value
        for acc_id, id_set in self.identity_map.items():
            if given in id_set:
                all_ids.add(acc_id)
                all_ids.update(id_set)
        # Also check credentials dict
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
        # Also check registered accounts
        for acc_key, acc in self.accounts.items():
            if str(acc.get("uid", "")) == given:
                all_ids.add(acc_key)

        all_ids.discard("")
        all_ids.discard("None")

        # 🔒 Add ALL identifiers to kill switch FIRST
        for i in all_ids:
            self.deleted_accounts.add(i)

        # 1. Remove from accounts.json — match ANY known identifier
        try:
            existing = []
            if os.path.exists(ACCOUNTS_FILE):
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            if not isinstance(existing, list):
                existing = []
            original_len = len(existing)
            existing = [
                acc for acc in existing
                if str(acc.get("uid", "")).strip() not in all_ids
                and str(acc.get("account_id", "")).strip() not in all_ids
            ]
            if len(existing) != original_len:
                with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(existing, f, indent=2)
                removed_any = True
                print(f"\033[93m[DELETE] Removed {original_len - len(existing)} entry from accounts.json for ids: {all_ids}\033[0m")
        except Exception as e:
            print(f"[DELETE] Error removing from accounts.json: {e}")

        # 2. Remove from token_cache.json — match ANY known identifier
        try:
            if os.path.exists(TOKEN_CACHE_FILE):
                with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                if not isinstance(cache, dict):
                    cache = {}
                deleted_keys = set()
                # Direct key match
                for k in all_ids:
                    if k in cache:
                        deleted_keys.add(k)
                # Value-based match
                for key, val in list(cache.items()):
                    if not isinstance(val, dict):
                        continue
                    if str(val.get("account_id", "")).strip() in all_ids:
                        deleted_keys.add(key)
                    if str(val.get("auth_uid", "")).strip() in all_ids:
                        deleted_keys.add(key)
                    if str(val.get("open_id", "")).strip() in all_ids:
                        deleted_keys.add(key)
                    tok = val.get("auth_token")
                    if tok and f"tok_{str(tok)[:20]}" in all_ids:
                        deleted_keys.add(key)
                for k in deleted_keys:
                    cache.pop(k, None)
                    removed_any = True
                if deleted_keys:
                    with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump(cache, f, indent=2)
                    print(f"\033[93m[DELETE] Removed {len(deleted_keys)} keys from token_cache.json\033[0m")
        except Exception as e:
            print(f"[DELETE] Error removing from token_cache.json: {e}")

        # 3. Remove from devices.json — match ANY known identifier
        try:
            if os.path.exists(DEVICES_FILE):
                with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                    devices = json.load(f)
                if not isinstance(devices, dict):
                    devices = {}
                to_del = set()
                for k, v in list(devices.items()):
                    if str(k) in all_ids:
                        to_del.add(k)
                        continue
                    if isinstance(v, dict):
                        if str(v.get("account_id", "")).strip() in all_ids:
                            to_del.add(k)
                        if str(v.get("auth_uid", "")).strip() in all_ids:
                            to_del.add(k)
                        if str(v.get("open_id", "")).strip() in all_ids:
                            to_del.add(k)
                for k in to_del:
                    devices.pop(k, None)
                if to_del:
                    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
                        json.dump(devices, f, indent=4)
                    print(f"\033[93m[DELETE] Removed {len(to_del)} keys from devices.json\033[0m")
        except Exception as e:
            print(f"[DELETE] Error removing from devices.json: {e}")

        # 4. Cancel worker tasks for ALL identifiers
        for i in list(all_ids):
            if i in self.account_workers:
                try:
                    self.account_workers[i].cancel()
                except Exception:
                    pass
                try:
                    del self.account_workers[i]
                except Exception:
                    pass

        # 5. Remove from in-memory state
        for i in list(all_ids):
            self.accounts.pop(i, None)
            self.paused_accounts.discard(i)
            self.account_credentials.pop(i, None)
            self.identity_map.pop(i, None)

        # 6. Wipe match counters
        try:
            from app import _match_counters
            for i in all_ids:
                _match_counters.pop(i, None)
        except Exception:
            pass

        self.recalc_totals()
        return removed_any

    # ==================== STUCK ACCOUNT CLEANUP ====================
    def cleanup_stuck_accounts(self) -> List[str]:
        """Find accounts that haven't found a match in STUCK_TIMEOUT and FULLY delete them."""
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
    """
    On startup: purge orphan entries from token_cache.json and devices.json
    that don't correspond to any account in accounts.json.
    """
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
    # 1. Load accounts.json — the source of truth for "added accounts"
    added_accounts = []
    try:
        if os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                added_accounts = json.load(f)
            if not isinstance(added_accounts, list):
                added_accounts = []
    except Exception:
        added_accounts = []

    # 2. Runtime state from bot_state
    runtime = list(bot_state.accounts.values())
    runtime_by_uid = {str(a["uid"]): a for a in runtime}

    # 3. Build merged list
    merged: List[Dict[str, Any]] = []
    seen_uids = set()

    # First: every added account from accounts.json
    for acc in added_accounts:
        # Skip accounts marked deleted
        raw_uid = str(acc.get("uid", "")).strip()
        raw_token = acc.get("token", "")
        lookup_key = raw_uid if raw_uid else f"tok_{str(raw_token)[:20]}"

        if lookup_key and lookup_key in bot_state.deleted_accounts:
            continue

        # Try to match runtime by uid OR by password/token match
        runtime_acc = None
        if raw_uid and raw_uid in runtime_by_uid:
            runtime_acc = runtime_by_uid[raw_uid]
        else:
            # Try to find a runtime account whose auth_uid or auth_token matches
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
            # Not logged in yet → PENDING placeholder from accounts.json
            placeholder_uid = raw_uid or f"pending_{lookup_key}"
            if placeholder_uid in bot_state.deleted_accounts:
                continue
            status = "CONNECTING"
            # If it was explicitly added but never registered, mark as PENDING
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

    # Then: any runtime accounts not sourced from accounts.json (edge case)
    for rt in runtime:
        if str(rt["uid"]) not in seen_uids:
            merged.append(rt)

    # Sort: pending/connecting first, then by gained exp
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
            # User explicitly re-added: remove from kill-switch
            bot_state.deleted_accounts.discard(uid)
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            existing.append({"uid": uid, "password": pwd})
            bot_state.log(f"New account added: UID {uid}", "success", uid, "system")
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token required"})
            bot_state.deleted_accounts.discard(f"tok_{token[:20]}")
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

        # Also accept optional additional identifiers from the frontend
        extra_ids = data.get("extra_ids", [])
        if isinstance(extra_ids, list):
            for eid in extra_ids:
                if eid and str(eid).strip() != uid:
                    bot_state.deleted_accounts.add(str(eid).strip())

        bot_state.full_delete_account(uid)
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
