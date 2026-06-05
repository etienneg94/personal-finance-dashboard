"""Streamlit dashboard — Fun Money Budget Tracker powered by Monarch Money."""

import json
import tomllib
from calendar import monthrange
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from monarch_client import MonarchClient

# ---------------------------------------------------------------------------
# Page config & CSS
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Fun Money Tracker", page_icon="💸", layout="wide")

st.markdown("""
<style>
.block-container { padding-top: 1.5rem !important; }
[data-testid="metric-container"] {
    background: #f0faf2; border: 1px solid #b2dfbb;
    border-radius: 10px; padding: 12px 18px;
}
div.stDataFrame { border-radius: 8px; overflow: hidden; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Default budget categories
# ---------------------------------------------------------------------------

_DEFAULT_BUDGET = pd.DataFrame([
    {"Category": "Rent / Mortgage",      "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Electricity",           "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Water / Sewer",         "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Gas / Heating",         "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Internet",              "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Phone",                 "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Groceries",             "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Pet Food / Pet Care",   "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Health Insurance",      "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Car Insurance",         "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Car Payment",           "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Gas / Transportation",  "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Subscriptions",         "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Dining Out",            "Budget ($)": 0.0, "Monarch Category": ""},
    {"Category": "Personal Care",         "Budget ($)": 0.0, "Monarch Category": ""},
])

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

_BUDGET_FILE     = Path(__file__).parent / "budget_config.json"
_HIDDEN_TXN_FILE = Path(__file__).parent / "hidden_txns.json"


def _load_budget() -> pd.DataFrame:
    if _BUDGET_FILE.exists():
        try:
            return pd.DataFrame(json.loads(_BUDGET_FILE.read_text()))
        except Exception:
            pass
    return _DEFAULT_BUDGET.copy()


def _save_budget(df: pd.DataFrame) -> None:
    _BUDGET_FILE.write_text(json.dumps(df.to_dict(orient="records"), indent=2))


def _load_hidden_txns() -> set:
    if _HIDDEN_TXN_FILE.exists():
        try:
            return set(json.loads(_HIDDEN_TXN_FILE.read_text()))
        except Exception:
            pass
    return set()


def _save_hidden_txns(ids: set) -> None:
    _HIDDEN_TXN_FILE.write_text(json.dumps(sorted(ids)))


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

for _k, _v in [
    ("client", None),
    ("txn_cache", None),
    ("cache_key", None),
    ("manual_income", 0.0),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v

if "budget" not in st.session_state:
    st.session_state.budget = _load_budget()

if "hidden_txn_ids" not in st.session_state:
    st.session_state.hidden_txn_ids = _load_hidden_txns()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.markdown("## 💸 Fun Money Tracker")
st.caption("Set your monthly budget · connect to Monarch Money · know what's left for fun")
st.divider()

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc)
    return "429" in msg or "Too Many Requests" in msg or "rate limit" in msg.lower()


def _read_secrets() -> dict:
    secrets_file = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if secrets_file.exists():
        with open(secrets_file, "rb") as f:
            return tomllib.load(f)
    try:
        return dict(st.secrets)
    except Exception:
        return {}


def _try_secrets_login() -> bool:
    secrets  = _read_secrets()
    email    = secrets.get("MONARCH_EMAIL", "")
    password = secrets.get("MONARCH_PASSWORD", "")
    mfa      = secrets.get("MONARCH_MFA_SECRET", "")

    if not email or not password:
        return False

    try:
        c = MonarchClient()
        c.login(email, password, mfa or None)
        st.session_state.client = c
        return True
    except Exception as exc:
        if _is_rate_limit(exc):
            st.error("**Auto-login rate limited (429).** Please wait 2–5 minutes and reload.")
        else:
            st.error(f"**Auto-login failed:** {exc}")
        st.stop()


if st.session_state.client is None:
    if not _try_secrets_login():
        st.markdown("### Connect to Monarch Money")
        with st.form("login_form"):
            email_in = st.text_input("Email", placeholder="you@example.com")
            pw_in    = st.text_input("Password", type="password")
            mfa_in   = st.text_input(
                "MFA Secret *(optional)*",
                placeholder="Base-32 TOTP secret — only if 2FA is on",
                help="Raw TOTP secret from when you set up your authenticator, not the 6-digit code.",
            )
            submitted = st.form_submit_button("Connect →", type="primary", use_container_width=True)

        if submitted:
            if not email_in or not pw_in:
                st.error("Email and password are required.")
            else:
                with st.spinner("Connecting… (auto-retrying on rate limits)"):
                    try:
                        c = MonarchClient()
                        c.login(email_in, pw_in, mfa_in.strip() or None)
                        st.session_state.client = c
                        st.rerun()
                    except Exception as exc:
                        if _is_rate_limit(exc):
                            st.error("**Rate limited (429).** Please wait 2–5 minutes and try again.")
                        else:
                            st.error(f"Login failed: {exc}")

        with st.expander("ℹ️  MFA Secret help"):
            st.markdown("""
Use the **base-32 TOTP secret** from when you first set up your authenticator app —
not the rotating 6-digit code. If you've lost it, disable and re-enable 2FA in
Monarch Money settings to get a new one.
            """)
        st.stop()

client: MonarchClient = st.session_state.client

# ---------------------------------------------------------------------------
# Month selector + refresh
# ---------------------------------------------------------------------------

today = date.today()


def _month_list(n: int = 12) -> list[date]:
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


months       = _month_list(12)
month_labels = {d.strftime("%B %Y"): d for d in months}

col_m, col_r = st.columns([5, 1])
with col_m:
    selected_label = st.selectbox("Month", list(month_labels.keys()), index=0, label_visibility="collapsed")
with col_r:
    st.write(""); st.write("")
    force_refresh = st.button("🔄 Refresh", use_container_width=True)

selected_month = month_labels[selected_label]
last_day       = monthrange(selected_month.year, selected_month.month)[1]
start_str      = selected_month.strftime("%Y-%m-%d")
end_str        = selected_month.replace(day=last_day).strftime("%Y-%m-%d")
cache_key      = start_str

# ---------------------------------------------------------------------------
# Fetch transactions
# ---------------------------------------------------------------------------

if force_refresh or st.session_state.cache_key != cache_key:
    with st.spinner(f"Loading {selected_label} from Monarch Money…"):
        try:
            st.session_state.txn_cache = client.get_transactions(start_str, end_str)
            st.session_state.cache_key = cache_key
        except Exception as exc:
            st.error(f"Failed to fetch data: {exc}")
            st.stop()

txns_raw: list[dict] = st.session_state.txn_cache or []

# ---------------------------------------------------------------------------
# Parse transactions
# ---------------------------------------------------------------------------

def _parse(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for i, t in enumerate(raw):
        cat      = t.get("category") or {}
        merchant = t.get("merchant") or {}
        rows.append({
            "id":         str(t.get("id") or i),
            "date":       t.get("date", ""),
            "merchant":   merchant.get("name") or t.get("description", ""),
            "category":   cat.get("name", "Uncategorized"),
            "amount":     float(t.get("amount", 0)),
            "is_income":  bool(t.get("isIncome", False)),
            "is_pending": bool(t.get("isPending", False)),
            "notes":      t.get("notes", "") or "",
        })
    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["id", "date", "merchant", "category", "amount", "is_income", "is_pending", "notes"]
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


txns_df = _parse(txns_raw)
hidden  = st.session_state.hidden_txn_ids

income_df  = txns_df[ txns_df["is_income"] & ~txns_df["is_pending"] & ~txns_df["id"].isin(hidden)]
expense_df = txns_df[~txns_df["is_income"] & ~txns_df["is_pending"] & ~txns_df["id"].isin(hidden)]

monarch_income = income_df["amount"].sum()
monarch_cats   = sorted(expense_df["category"].dropna().unique().tolist()) if not expense_df.empty else []
cat_spending   = expense_df.groupby("category")["amount"].sum().to_dict() if not expense_df.empty else {}

# Reserve a spot at the top for the summary — filled in after computations below
summary_placeholder = st.container()

# ---------------------------------------------------------------------------
# Income
# ---------------------------------------------------------------------------

st.divider()
st.markdown("### 💰 Monthly Income")

if monarch_income > 0:
    use_monarch = st.toggle("Pull income from Monarch Money", value=True)
    if use_monarch:
        total_income = monarch_income
        st.metric(f"Income in Monarch ({selected_label})", f"${total_income:,.2f}")
    else:
        total_income = st.number_input(
            "Monthly take-home income ($)", min_value=0.0,
            value=float(st.session_state.manual_income or monarch_income),
            step=100.0, format="%.2f",
        )
        st.session_state.manual_income = total_income
else:
    st.info("No income transactions found in Monarch this month. Enter your take-home pay:")
    total_income = st.number_input(
        "Monthly take-home income ($)", min_value=0.0,
        value=float(st.session_state.manual_income or 0.0),
        step=100.0, format="%.2f",
    )
    st.session_state.manual_income = total_income

st.divider()

# ---------------------------------------------------------------------------
# Budget table
# ---------------------------------------------------------------------------

st.markdown("### 📋 Monthly Budget")
st.caption(
    "Enter your budgeted amount per category. "
    "In the **Monarch Category** column, select the matching category from your Monarch account "
    "so actual spending is pulled automatically. "
    "Leave it blank for things not tracked in Monarch (e.g. rent paid by bank transfer) — "
    "the budgeted amount will be assumed as spent."
)

budget_df = st.data_editor(
    st.session_state.budget,
    column_config={
        "Category": st.column_config.TextColumn("Category", width=210),
        "Budget ($)": st.column_config.NumberColumn(
            "Budget ($)", format="$%.2f", min_value=0.0, width=130,
        ),
        "Monarch Category": st.column_config.SelectboxColumn(
            "Monarch Category",
            options=[""] + monarch_cats,
            width=270,
            help="Monarch Money category to read actual spending from.",
        ),
    },
    num_rows="dynamic",
    use_container_width=True,
    hide_index=True,
    key="budget_editor",
)
st.session_state.budget = budget_df

if st.button("💾 Save Budget", help="Persist your budget so it reloads automatically next time."):
    _save_budget(budget_df)
    st.success("Budget saved!")

# ---------------------------------------------------------------------------
# Compute budget vs actual
# ---------------------------------------------------------------------------

matched_cats: set[str] = set()
breakdown_rows = []

for _, row in budget_df.iterrows():
    budget_amt = float(row.get("Budget ($)") or 0)
    mc         = str(row.get("Monarch Category") or "").strip()
    actual     = cat_spending.get(mc, 0.0) if mc else 0.0
    if mc:
        matched_cats.add(mc)
    breakdown_rows.append({
        "Category":      str(row.get("Category", "")),
        "Budget ($)":    budget_amt,
        "Actual ($)":    actual,
        "Remaining ($)": budget_amt - actual,
        "Status":        "✅" if actual <= budget_amt else "⚠️ Over",
        "_tracked":      bool(mc),
    })

bk = pd.DataFrame(breakdown_rows) if breakdown_rows else pd.DataFrame(
    columns=["Category", "Budget ($)", "Actual ($)", "Remaining ($)", "Status", "_tracked"]
)

total_budgeted      = float(bk["Budget ($)"].sum())
essential_tracked   = float(bk.loc[bk["_tracked"],  "Actual ($)"].sum())
essential_untracked = float(bk.loc[~bk["_tracked"] & (bk["Budget ($)"] > 0), "Budget ($)"].sum())
other_spending      = sum(v for c, v in cat_spending.items() if c not in matched_cats)

fun_money_budget    = total_income - total_budgeted
fun_money_remaining = total_income - essential_tracked - essential_untracked - other_spending

# ---------------------------------------------------------------------------
# Summary — rendered into the placeholder reserved at the top
# ---------------------------------------------------------------------------

with summary_placeholder:
    st.markdown(f"### 📊 {selected_label} Summary")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Income",           f"${total_income:,.2f}")
    m2.metric("Essential Budget", f"${total_budgeted:,.2f}",
              delta=f"${essential_tracked:,.2f} tracked · ${essential_untracked:,.2f} assumed",
              delta_color="off")
    m3.metric("Fun Money Budget", f"${fun_money_budget:,.2f}",
              help="Income minus your total essential budget")
    m4.metric("🎉 Fun Money Left",
              f"${fun_money_remaining:,.2f}",
              delta="✅ on track" if fun_money_remaining >= 0 else "⚠️ overspent",
              delta_color="normal" if fun_money_remaining >= 0 else "inverse")

    st.write("")

    if total_income > 0 and fun_money_budget > 0:
        gauge_col, formula_col = st.columns([1, 1])

        with gauge_col:
            safe_val  = max(fun_money_remaining, 0)
            axis_max  = max(fun_money_budget * 1.05, safe_val, 1)
            pct_used  = ((fun_money_budget - fun_money_remaining) / fun_money_budget * 100
                         if fun_money_budget > 0 else 0)
            bar_color = "#43A047" if pct_used < 60 else ("#FB8C00" if pct_used < 90 else "#E53935")

            fig = go.Figure(go.Indicator(
                mode="gauge+number",
                value=safe_val,
                title={"text": "<b>Fun Money Remaining</b>", "font": {"size": 16}},
                number={"prefix": "$", "font": {"size": 44, "color": bar_color}},
                gauge={
                    "axis":  {"range": [0, axis_max], "tickprefix": "$", "nticks": 5},
                    "bar":   {"color": bar_color, "thickness": 0.25},
                    "steps": [
                        {"range": [0,                    fun_money_budget * 0.35], "color": "#FFEBEE"},
                        {"range": [fun_money_budget*0.35, fun_money_budget * 0.65], "color": "#FFF9C4"},
                        {"range": [fun_money_budget*0.65, axis_max],               "color": "#E8F5E9"},
                    ],
                    "threshold": {
                        "line": {"color": "#388E3C", "width": 3},
                        "thickness": 0.85,
                        "value": fun_money_budget,
                    },
                },
            ))
            fig.update_layout(
                height=270, margin=dict(t=70, b=10, l=30, r=30),
                paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, use_container_width=True)

        with formula_col:
            st.markdown("**How it's calculated**")
            formula_df = pd.DataFrame({
                "Item": [
                    "Income",
                    "− Essential (Monarch-tracked)",
                    "− Essential (not in Monarch)",
                    "− Other spending",
                    "= Fun money left",
                ],
                "Amount": [
                    f"${total_income:,.2f}",
                    f"− ${essential_tracked:,.2f}",
                    f"− ${essential_untracked:,.2f}",
                    f"− ${other_spending:,.2f}",
                    f"${fun_money_remaining:,.2f}",
                ],
            })
            st.dataframe(
                formula_df, hide_index=True, use_container_width=True,
                column_config={
                    "Item":   st.column_config.TextColumn(width=270),
                    "Amount": st.column_config.TextColumn(width=130),
                },
            )
            st.caption(
                '"Not in Monarch" rows use the budgeted amount as a proxy '
                'since no transactions are tracked for them.'
            )

    elif total_income == 0:
        st.info("Enter your monthly income below to see the fun money gauge.")

st.divider()

# ---------------------------------------------------------------------------
# Budget vs actual breakdown table
# ---------------------------------------------------------------------------

st.markdown("### 💳 Budget vs. Actual")

budgeted_rows = bk[bk["Budget ($)"] > 0].drop(columns=["_tracked"]).reset_index(drop=True)

if not budgeted_rows.empty:
    st.dataframe(
        budgeted_rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Category":      st.column_config.TextColumn(width=210),
            "Budget ($)":    st.column_config.NumberColumn(format="$%.2f", width=120),
            "Actual ($)":    st.column_config.NumberColumn(format="$%.2f", width=120),
            "Remaining ($)": st.column_config.NumberColumn(format="$%.2f", width=120),
            "Status":        st.column_config.TextColumn(width=100),
        },
    )
else:
    st.info("Fill in budget amounts in the table above to see your breakdown here.")

st.divider()

# ---------------------------------------------------------------------------
# Other spending (unbudgeted Monarch categories)
# ---------------------------------------------------------------------------

st.markdown("### 🎯 Other Spending")
st.caption(
    "Monarch categories not mapped to any budget row. "
    "These are deducted from your fun money."
)

other_cats = {c: v for c, v in cat_spending.items() if c not in matched_cats}
if other_cats:
    other_df = (
        pd.DataFrame({"Category": c, "Spent ($)": v} for c, v in other_cats.items())
        .sort_values("Spent ($)", ascending=False)
        .reset_index(drop=True)
    )
    st.dataframe(
        other_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Category":  st.column_config.TextColumn(width=220),
            "Spent ($)": st.column_config.NumberColumn(format="$%.2f", width=120),
        },
    )
    st.markdown(f"**Total other spending: ${other_spending:,.2f}**")
elif matched_cats:
    st.success("All Monarch spending is mapped to a budget category. Nice!")
else:
    st.info("Connect budget rows to Monarch categories above to track spending here.")

st.divider()

# ---------------------------------------------------------------------------
# Transactions — with exclude toggles
# ---------------------------------------------------------------------------

def _txn_editor(df: pd.DataFrame, key: str) -> set:
    """Render an editable transaction table with an Exclude column.
    Returns the set of IDs the user has checked as excluded."""
    if df.empty:
        return set()

    show = df[["id", "date", "merchant", "category", "amount", "notes"]].copy()
    show["date"] = show["date"].dt.date
    show.insert(0, "Exclude", show["id"].isin(st.session_state.hidden_txn_ids))

    edited = st.data_editor(
        show,
        use_container_width=True,
        hide_index=True,
        key=key,
        column_config={
            "Exclude":  st.column_config.CheckboxColumn("Exclude", width=70,
                            help="Check to remove this transaction from all totals."),
            "id":       None,  # hide the id column
            "date":     st.column_config.DateColumn("Date",     width=110),
            "merchant": st.column_config.TextColumn("Merchant", width=210),
            "category": st.column_config.TextColumn("Category", width=170),
            "amount":   st.column_config.NumberColumn("Amount", format="$%.2f", width=110),
            "notes":    st.column_config.TextColumn("Notes",    width=190),
        },
        disabled=["id", "date", "merchant", "category", "amount", "notes"],
    )
    return set(edited.loc[edited["Exclude"], "id"].tolist())


hidden_after_edit: set = set()

with st.expander(f"📄 Expense transactions — {selected_label}", expanded=False):
    all_expense = txns_df[~txns_df["is_income"] & ~txns_df["is_pending"]].sort_values("date", ascending=False)
    if all_expense.empty:
        st.info("No expense transactions found for this month.")
    else:
        n_hidden = int(all_expense["id"].isin(hidden).sum())
        if n_hidden:
            st.caption(f"{n_hidden} transaction(s) currently excluded from totals.")
        hidden_after_edit |= _txn_editor(all_expense, "expense_editor")

        csv = (
            all_expense[["date", "merchant", "category", "amount", "notes"]]
            .assign(date=lambda d: d["date"].dt.date)
            .to_csv(index=False).encode()
        )
        st.download_button(
            "⬇️ Download CSV", csv,
            file_name=f"expenses_{start_str[:7]}.csv",
            mime="text/csv",
        )

with st.expander(f"💵 Income transactions — {selected_label}", expanded=False):
    all_income = txns_df[txns_df["is_income"] & ~txns_df["is_pending"]].sort_values("date", ascending=False)
    if all_income.empty:
        st.info("No income transactions found for this month.")
    else:
        n_hidden_inc = int(all_income["id"].isin(hidden).sum())
        if n_hidden_inc:
            st.caption(f"{n_hidden_inc} transaction(s) currently excluded from totals.")
        hidden_after_edit |= _txn_editor(all_income, "income_editor")

# Persist exclusion changes
new_hidden = hidden_after_edit | (hidden - set(txns_df["id"].tolist()))
if new_hidden != hidden:
    st.session_state.hidden_txn_ids = new_hidden
    _save_hidden_txns(new_hidden)
    st.rerun()

st.caption(f"Monarch Money · {selected_label} · press 🔄 to refresh")
