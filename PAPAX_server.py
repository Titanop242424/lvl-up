# -*- coding: utf-8 -*-
"""
FreeFire Level Up Bot - Professional Web Dashboard & Real-Time EXP Tracker
Embedded Async Web Server (aiohttp)
"""

import asyncio
import json
import os
import time
from typing import Dict, List, Any, Optional
from aiohttp import web

TOKEN_CACHE_FILE = "token_cache.json"
DEVICES_FILE = "devices.json"
ACCOUNTS_FILE = "accounts.json"


class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        # Map auth_uid / auth_token-prefix -> worker key (for cancellation)
        self.worker_aliases: Dict[str, str] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": uid
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid: str, nickname: str, region: str, level: int, exp: int, likes: int = 0):
        uid_str = str(uid)
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
                "last_updated": time.strftime("%H:%M:%S")
            }
        else:
            acc = self.accounts[uid_str]
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level:
                acc["level"] = level
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            diff = current_exp - old_exp
            if diff > 0:
                self.log(
                    f"Account {acc['nickname']} ({uid_str}) gained +{diff} EXP! "
                    f"Total Gained: +{acc['gained_exp']}", "success", uid_str
                )
            self.recalc_totals()

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] += 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            self.log(
                f"Account {self.accounts[uid_str]['nickname']} "
                f"finished Match #{self.accounts[uid_str]['matches_played']}",
                "info", uid_str
            )

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())


bot_state = BotState()

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")


async def handle_index(request: web.Request) -> web.Response:
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = "<h1>templates/index.html not found!</h1>"
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_get_stats(request: web.Request) -> web.Response:
    accounts_data = list(bot_state.accounts.values())
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "accounts": accounts_data,
        "logs": bot_state.logs[-60:],
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
            except Exception:
                existing = []

        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID and Password are required"})
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            existing.append({"uid": uid, "password": pwd})
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token is required"})
            existing = [acc for acc in existing if acc.get("token") != token]
            existing.append({"token": token})
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})

        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

        bot_state.log(f"New account added: {data.get('uid') or 'Token'}", "success")

        if "on_account_added" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](data))

        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


def _purge_token_cache(account_uid: str, auth_uid: Optional[str] = None,
                        auth_token: Optional[str] = None) -> int:
    """Remove all cache entries related to this account. Returns count removed."""
    removed = 0
    if not os.path.exists(TOKEN_CACHE_FILE):
        return 0
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            return 0

        keys_to_remove = set()
        for key, entry in list(cache.items()):
            if not isinstance(entry, dict):
                continue
            entry_acc_id = str(entry.get("account_id", ""))
            entry_auth_uid = str(entry.get("auth_uid", ""))
            entry_auth_tok = entry.get("auth_token", "")

            if key == account_uid or entry_acc_id == account_uid:
                keys_to_remove.add(key)
            if auth_uid and (key == auth_uid or entry_auth_uid == auth_uid
                             or key == f"tok_{auth_uid}"):
                keys_to_remove.add(key)
            if auth_token and (key == f"tok_{auth_token[:20]}"
                               or entry_auth_tok == auth_token):
                keys_to_remove.add(key)

        for k in keys_to_remove:
            if k in cache:
                del cache[k]
                removed += 1

        if removed:
            tmp = TOKEN_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
            os.replace(tmp, TOKEN_CACHE_FILE)
    except Exception as e:
        print(f"[-] _purge_token_cache error: {e}")
    return removed


def _purge_devices(account_uid: str, auth_uid: Optional[str] = None) -> int:
    """Remove device mapping for this account. Returns count removed."""
    removed = 0
    if not os.path.exists(DEVICES_FILE):
        return 0
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            devices = json.load(f)
        if not isinstance(devices, dict):
            return 0

        keys_to_remove = set()
        for key in list(devices.keys()):
            if key == account_uid or (auth_uid and key == auth_uid):
                keys_to_remove.add(key)

        for k in keys_to_remove:
            if k in devices:
                del devices[k]
                removed += 1

        if removed:
            tmp = DEVICES_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(devices, f, indent=4)
            os.replace(tmp, DEVICES_FILE)
    except Exception as e:
        print(f"[-] _purge_devices error: {e}")
    return removed


async def handle_delete_account(request: web.Request) -> web.Response:
    """
    FULLY removes an account:
      1. Cancel the running worker task
      2. Remove from bot_state.accounts (by account_id)
      3. Remove from bot_state.account_credentials (all aliases)
      4. Remove from bot_state.account_workers + worker_aliases
      5. Remove from accounts.json (by uid or token)
      6. Purge token_cache.json (all aliases)
      7. Purge devices.json (all aliases)
    """
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "uid is required"})

        # Look up credentials to find all aliases
        cred = bot_state.account_credentials.get(uid, {})
        auth_uid = str(cred.get("auth_uid", "") or "")
        auth_token = str(cred.get("auth_token", "") or "")
        account_id = str(cred.get("account_id", uid) or uid)

        aliases = {uid, account_id}
        if auth_uid:
            aliases.add(auth_uid)
        if auth_token:
            aliases.add(f"tok_{auth_token[:20]}")

        # 1. Cancel worker tasks for ALL aliases
        cancelled = 0
        for alias in list(aliases):
            wk = bot_state.worker_aliases.get(alias, alias)
            task = bot_state.account_workers.get(wk)
            if task and not task.done():
                task.cancel()
                cancelled += 1
            bot_state.account_workers.pop(wk, None)
        # Also scan workers dict for any key match
        for wk in list(bot_state.account_workers.keys()):
            if wk in aliases:
                t = bot_state.account_workers.pop(wk, None)
                if t and not t.done():
                    t.cancel()
                    cancelled += 1
        for alias in aliases:
            bot_state.worker_aliases.pop(alias, None)

        # Give tasks a moment to actually cancel
        await asyncio.sleep(0.15)

        # 2. Remove from accounts
        for alias in aliases:
            bot_state.accounts.pop(alias, None)
        bot_state.recalc_totals()

        # 3. Remove credentials
        for alias in aliases:
            bot_state.account_credentials.pop(alias, None)

        # 4. accounts.json — remove by uid OR token
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, list):
                    new_list = []
                    for acc in existing:
                        acc_uid = str(acc.get("uid", ""))
                        acc_tok = str(acc.get("token", ""))
                        if acc_uid and acc_uid in aliases:
                            continue
                        if acc_tok and acc_tok == auth_token:
                            continue
                        if acc_tok and f"tok_{acc_tok[:20]}" in aliases:
                            continue
                        new_list.append(acc)
                    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                        json.dump(new_list, f, indent=2)
            except Exception as e:
                print(f"[-] accounts.json purge error: {e}")

        # 5. token_cache.json purge
        purged_cache = _purge_token_cache(account_id, auth_uid or None, auth_token or None)
        # Also purge for raw uid (guest)
        if uid not in (account_id, auth_uid):
            purged_cache += _purge_token_cache(uid, None, None)

        # 6. devices.json purge
        purged_dev = _purge_devices(account_id, auth_uid or None)
        if uid not in (account_id, auth_uid):
            purged_dev += _purge_devices(uid, None)

        bot_state.log(
            f"Account {uid} permanently deleted "
            f"(workers cancelled: {cancelled}, cache entries: {purged_cache}, devices: {purged_dev})",
            "warning", uid
        )
        return web.json_response({
            "status": "ok",
            "cancelled_workers": cancelled,
            "purged_cache": purged_cache,
            "purged_devices": purged_dev
        })
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


async def start_web_dashboard(host: str = "0.0.0.0", port: int = 5000):
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\033[92m[+] Web Dashboard running on http://localhost:{port}\033[0m")
