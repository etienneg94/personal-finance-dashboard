"""Monarch Money synchronous client — wraps the async monarchmoney library."""

import asyncio
import time
from typing import Optional


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc)
    return "429" in msg or "Too Many Requests" in msg or "rate limit" in msg.lower()


class MonarchClient:
    """Thin synchronous wrapper around the async monarchmoney library."""

    def __init__(self):
        self._mm = None

    def login(self, email: str, password: str, mfa_secret: Optional[str] = None) -> None:
        token = _run(self._login_async(email, password, mfa_secret))
        from monarchmoney import MonarchMoney
        mm = MonarchMoney()
        mm.set_token(token)
        mm._headers["Authorization"] = f"Token {token}"
        self._mm = mm

    @staticmethod
    async def _login_async(email: str, password: str, mfa_secret: Optional[str]) -> str:
        """Single-session two-step login that preserves Cloudflare cookies."""
        import aiohttp
        import oathtool

        headers = {
            "Accept": "application/json",
            "Client-Platform": "web",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        url = "https://api.monarch.com/auth/login/"

        async with aiohttp.ClientSession(headers=headers) as session:
            # Step 1 — email + password only
            payload = {
                "username": email,
                "password": password,
                "supports_mfa": True,
                "trusted_device": False,
            }
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return (await resp.json())["token"]
                if resp.status != 403:
                    text = await resp.text()
                    raise RuntimeError(f"Login failed ({resp.status}): {text}")

            # Step 2 — same session (Cloudflare cookie preserved), add TOTP
            if not mfa_secret:
                raise RuntimeError("Multi-Factor Auth Required but no MFA secret configured.")
            payload["totp"] = oathtool.generate_otp(mfa_secret.strip())
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return (await resp.json())["token"]
                text = await resp.text()
                raise RuntimeError(f"MFA login failed ({resp.status}): {text}")

    @property
    def is_logged_in(self) -> bool:
        return self._mm is not None

    # ------------------------------------------------------------------
    # Budget data
    # ------------------------------------------------------------------

    def get_budgets(self, start_date: str, end_date: str) -> list[dict]:
        """Return list of budget items for the date range (YYYY-MM-DD)."""
        raw = _run(self._mm.get_budgets(start_date=start_date, end_date=end_date))
        return _unwrap_budgets(raw)

    # ------------------------------------------------------------------
    # Transaction data
    # ------------------------------------------------------------------

    def get_transactions(self, start_date: str, end_date: str, limit: int = 500) -> list[dict]:
        """Return list of transaction dicts for the date range."""
        raw = _run(self._mm.get_transactions(
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        ))
        return _unwrap_transactions(raw)


# ---------------------------------------------------------------------------
# Response normalisation helpers
# ---------------------------------------------------------------------------

def _unwrap_budgets(raw) -> list[dict]:
    """Extract the budget list from various possible response shapes."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        # {"budgets": [...]}
        budgets = raw.get("budgets", raw)
        if isinstance(budgets, list):
            return budgets
        # {"budgets": {"summary": [...]}}
        if isinstance(budgets, dict):
            for key in ("summary", "data", "items"):
                if key in budgets and isinstance(budgets[key], list):
                    return budgets[key]
            # last resort: first list value
            for v in budgets.values():
                if isinstance(v, list):
                    return v
    return []


def _unwrap_transactions(raw) -> list[dict]:
    """Extract the transactions list from the API response."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        all_txns = raw.get("allTransactions", raw)
        if isinstance(all_txns, dict):
            return all_txns.get("results", [])
        if isinstance(all_txns, list):
            return all_txns
    return []


# ---------------------------------------------------------------------------
# Data helpers used by the app
# ---------------------------------------------------------------------------

def budget_amount(b: dict) -> float:
    for key in ("budgetAmount", "totalBudgeted", "budgetedAmount", "amount"):
        if b.get(key) is not None:
            return float(b[key])
    return 0.0


def actual_amount(b: dict) -> float:
    for key in ("actualAmount", "totalActual", "spentAmount", "spent"):
        if b.get(key) is not None:
            return float(b[key])
    return 0.0


def category_name(b: dict) -> str:
    cat = b.get("category") or {}
    return cat.get("name", b.get("name", "Unknown"))


def category_group_type(b: dict) -> str:
    cat = b.get("category") or {}
    group = cat.get("group") or {}
    return (group.get("type") or "").lower()


FUN_MONEY_KEYWORDS = ("fun", "entertainment", "leisure", "recreation",
                      "personal", "discretionary", "spending money")


def guess_fun_categories(budgets: list[dict]) -> list[str]:
    """Return names of budget categories that look like 'fun money' buckets."""
    fun, other_expense = [], []
    for b in budgets:
        name = category_name(b).lower()
        gtype = category_group_type(b)
        if gtype in ("expense", "") or not gtype:
            if any(kw in name for kw in FUN_MONEY_KEYWORDS):
                fun.append(category_name(b))
            elif gtype == "expense":
                other_expense.append(category_name(b))
    return fun if fun else other_expense
