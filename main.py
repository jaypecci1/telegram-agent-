import os
import asyncio
import logging
import httpx
import time
import base64
import uuid
from datetime import datetime
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
import anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from supabase import create_client
from tavily import TavilyClient
# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# ── Environment variables ─────────────────────────────────────────────────────
TELEGRAM_TOKEN    = "".join(os.environ["TELEGRAM_TOKEN"].split())
ANTHROPIC_API_KEY = "".join(os.environ["ANTHROPIC_API_KEY"].split())
SUPABASE_URL      = os.environ["SUPABASE_URL"].strip()
SUPABASE_KEY      = os.environ["SUPABASE_KEY"].strip()
TAVILY_API_KEY    = "".join(os.environ["TAVILY_API_KEY"].split())
KALSHI_API_KEY        = "".join(os.environ.get("KALSHI_API_KEY", "").split())
KALSHI_PRIVATE_KEY_B64 = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
# ── Clients ───────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
db     = create_client(SUPABASE_URL, SUPABASE_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)
# ── Trading Config ────────────────────────────────────────────────────────────
KALSHI_BASE_URL         = "https://api.elections.kalshi.com/trade-api/v2"
CHECK_INTERVAL          = 300                     # Scan markets every 5 minutes
SMART_TRADE_INTERVAL    = 300                     # How often to scan for opportunities
MIN_EDGE                = 10                      # Minimum edge % to consider a trade
MIN_CONFIDENCE          = 55                      # Minimum confidence % to trade
BASE_BET                = 5.00                    # Base bet size in dollars
MAX_BET                 = 25.00                   # Maximum bet size
MAX_OPEN_TRADES         = 3                       # Max simultaneous open positions
TRADING_PAUSED          = False                   # Can be toggled via Telegram command
# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Pecci, Jay's personal AI assistant. Jay is a young adult who works as an automotive and light diesel mechanic doing side jobs, and he is studying Economics at Texas Tech University. His long-term goal is to open his own mechanic shop. He also trades on Kalshi prediction markets.
You help Jay with:
- Tracking his mechanic jobs (customers, vehicles, parts, costs, payment status, job status)
- Searching the web for any question he has
- Remembering important information from all past conversations
- Managing his day-to-day life as a young adult
- Autonomously trading on Kalshi prediction markets with a smart AI strategy

Trading strategy: You run a fully autonomous smart trading bot that scans ALL open Kalshi markets every 5 minutes. For each market, you:
1. Research it with 2-3 web searches to gather real data, news, and relevant context
2. Estimate the TRUE probability of YES resolving based on that research
3. Compare your estimate to the market price to calculate your edge
4. Only place trades with edge > 10% AND confidence > 55%
5. Bet size scales with confidence: low confidence = $5, high confidence = up to $25
6. After every settlement, you generate a lesson from the outcome and store it
7. Before analyzing any new market, you retrieve past lessons from the same category
8. You track win rates by category and avoid categories where you consistently lose

This strategy learns and improves with every trade. It builds a memory of what works.

IMPORTANT: When Jay asks about trading status, ALWAYS call the get_trading_status tool.
IMPORTANT: When Jay asks about trade history, ALWAYS call the get_trade_history tool.
IMPORTANT: When Jay asks what the bot has learned, ALWAYS call the get_trading_lessons tool.

Telegram trading commands Jay can use:
- "pause trading" - stop all new trades
- "resume trading" - resume trading
- "show my positions" - list open Kalshi positions
- "show my balance" - check Kalshi balance
- "show trade history" - view past trades
- "what have you learned" - show lessons from past trades

Personality: Be conversational and friendly. Talk to Jay like a smart assistant who actually knows him. Keep responses clear and to the point.
Current date and time: {datetime}"""
# ── Kalshi API helpers ────────────────────────────────────────────────────────
def sign_kalshi_request(method: str, path: str) -> dict:
    """Generate signed headers for Kalshi API requests using RSA-PSS."""
    try:
        timestamp   = str(int(time.time() * 1000))
        message     = f"{timestamp}{method}{path}"
        pem         = base64.b64decode(KALSHI_PRIVATE_KEY_B64)
        private_key = serialization.load_pem_private_key(pem, password=None, backend=default_backend())
        signature   = private_key.sign(
            message.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(signature).decode()
        return {
            "KALSHI-ACCESS-KEY":       KALSHI_API_KEY,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "Content-Type":            "application/json",
        }
    except Exception as e:
        logger.error(f"Kalshi signing error: {e}")
        return {"Content-Type": "application/json"}
def get_kalshi_balance() -> dict:
    try:
        path = "/trade-api/v2/portfolio/balance"
        with httpx.Client() as client:
            r = client.get(f"{KALSHI_BASE_URL}/portfolio/balance", headers=sign_kalshi_request("GET", path), timeout=10)
            if r.status_code == 200:
                balance = r.json().get("balance", 0) / 100
                return {"success": True, "balance": balance}
            return {"success": False, "error": r.text}
    except Exception as e:
        return {"success": False, "error": str(e)}
def get_kalshi_positions() -> dict:
    try:
        path = "/trade-api/v2/portfolio/positions"
        with httpx.Client() as client:
            r = client.get(f"{KALSHI_BASE_URL}/portfolio/positions", headers=sign_kalshi_request("GET", path), timeout=10)
            if r.status_code == 200:
                return {"success": True, "positions": r.json().get("market_positions", [])}
            return {"success": False, "error": r.text}
    except Exception as e:
        return {"success": False, "error": str(e)}
def place_kalshi_order(ticker: str, side: str, price_cents: int, count: int) -> dict:
    """Place an order on Kalshi. Uses aggressive pricing to ensure fills."""
    try:
        path = "/trade-api/v2/portfolio/orders"
        # Use aggressive pricing to guarantee fill:
        # If buying YES, bid high (99c) to fill immediately at best available
        # If buying NO, bid high (99c) to fill immediately at best available
        fill_price = 99  # Bid max to act like a market order — fills at best available price
        payload = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy",
            "type": "limit",
            "side": side,
            "count": count,
            f"{side}_price": fill_price,
        }
        logger.info(f"Placing order: ticker={ticker} side={side} price={fill_price}c (market mid={price_cents}c) count={count}")
        with httpx.Client() as client:
            r = client.post(
                f"{KALSHI_BASE_URL}/portfolio/orders",
                headers=sign_kalshi_request("POST", path),
                json=payload,
                timeout=15,
            )
            logger.info(f"Order response {r.status_code}: {r.text[:500]}")
            if r.status_code in (200, 201):
                order = r.json().get("order", {})
                # Check if the order actually filled
                fill_count = float(order.get("fill_count_fp", "0"))
                if fill_count > 0:
                    logger.info(f"Order FILLED: {fill_count} contracts")
                    return {"success": True, "filled": True, "order": order, "fill_count": fill_count}
                else:
                    logger.warning(f"Order created but NOT FILLED (0 contracts). Likely no liquidity.")
                    return {"success": True, "filled": False, "order": order, "fill_count": 0}
            return {"success": False, "filled": False, "error": r.text}
    except Exception as e:
        return {"success": False, "filled": False, "error": str(e)}
# ── Tool definitions ──────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "search_web",
        "description": "Search the internet for any information, news, research topics, part prices, how-to guides, prediction market research, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "add_job",
        "description": "Add a new mechanic job to the tracking system",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name":   {"type": "string"},
                "customer_phone":  {"type": "string"},
                "vehicle_year":    {"type": "string"},
                "vehicle_make":    {"type": "string"},
                "vehicle_model":   {"type": "string"},
                "job_description": {"type": "string"},
                "parts_used":      {"type": "string"},
                "cost":            {"type": "number"},
                "status":          {"type": "string", "description": "pending, in_progress, waiting_parts, or completed"},
            },
            "required": ["customer_name", "job_description"],
        },
    },
    {
        "name": "update_job",
        "description": "Update an existing job",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id":          {"type": "integer"},
                "customer_name":   {"type": "string"},
                "customer_phone":  {"type": "string"},
                "vehicle_year":    {"type": "string"},
                "vehicle_make":    {"type": "string"},
                "vehicle_model":   {"type": "string"},
                "job_description": {"type": "string"},
                "parts_used":      {"type": "string"},
                "cost":            {"type": "number"},
                "paid":            {"type": "boolean"},
                "status":          {"type": "string"},
                "notes":           {"type": "string"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "list_jobs",
        "description": "List all mechanic jobs, optionally filtered by status",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"}
            },
        },
    },
    {
        "name": "get_customer_jobs",
        "description": "Get all jobs for a specific customer",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"}
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "save_memory",
        "description": "Save an important piece of information to long-term memory",
        "input_schema": {
            "type": "object",
            "properties": {
                "key":   {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "recall_memory",
        "description": "Search through all saved memories and past conversations",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_kalshi_balance",
        "description": "Check Jay's current Kalshi account balance",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_kalshi_positions",
        "description": "Get all of Jay's currently open Kalshi positions",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_trade_history",
        "description": "Get history of all trades Pecci has placed",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many recent trades to show, default 10"}
            },
        },
    },
    {
        "name": "scan_and_analyze_market",
        "description": "Search the web for information about a specific Kalshi market, analyze it, and determine if it's a good trade",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":       {"type": "string", "description": "The Kalshi market ticker"},
                "title":        {"type": "string", "description": "The market title/question"},
                "current_yes_price": {"type": "number", "description": "Current yes price in cents (1-99)"},
            },
            "required": ["ticker", "title", "current_yes_price"],
        },
    },
    {
        "name": "execute_trade",
        "description": "Place a trade on Kalshi after analysis confirms it's a good opportunity",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":    {"type": "string", "description": "Kalshi market ticker"},
                "title":     {"type": "string", "description": "Market title"},
                "side":      {"type": "string", "description": "yes or no"},
                "price_cents": {"type": "integer", "description": "Price in cents (1-99)"},
                "reasoning": {"type": "string", "description": "Why this is a good trade"},
            },
            "required": ["ticker", "title", "side", "price_cents", "reasoning"],
        },
    },
    {
        "name": "get_trading_status",
        "description": "Get the current live trading status — shows open trades, recent analyses, win rates by category, and whether trading is paused",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_trading_lessons",
        "description": "Get lessons the bot has learned from past trades — what worked, what didn't, and win rates by market category",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter lessons by category (optional)"},
                "limit":    {"type": "integer", "description": "How many lessons to show, default 10"},
            },
        },
    },
    {
        "name": "get_income_summary",
        "description": "Get a summary of Jay's mechanic income — total earned, total owed by customers, and a breakdown by status",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_job",
        "description": "Permanently delete a mechanic job by its ID",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "The ID of the job to delete"}
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "search_kalshi_markets",
        "description": "Search and browse available Kalshi markets. Use this to find markets to potentially trade on.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":  {"type": "string", "description": "Keyword to filter market titles (optional)"},
                "series": {"type": "string", "description": "Series ticker to filter by, e.g. KXBTC15M (optional)"},
                "status": {"type": "string", "description": "open, closed, or settled (default: open)"},
            },
        },
    },
    {
        "name": "get_daily_summary",
        "description": "Get a comprehensive daily brief: pending/unpaid jobs, today's trading P/L, Kalshi balance, and any active positions",
        "input_schema": {"type": "object", "properties": {}},
    },
]
# ── Tool execution ────────────────────────────────────────────────────────────
def run_tool(name: str, inputs: dict) -> str:
    global TRADING_PAUSED
    if name == "search_web":
        try:
            results = tavily.search(inputs["query"], max_results=5)
            out = []
            for r in results.get("results", []):
                out.append(f"{r.get('title','')}\n{r.get('content','')}\nSource: {r.get('url','')}")
            return "\n\n".join(out) or "No results found."
        except Exception as e:
            return f"Search error: {e}"
    if name == "add_job":
        try:
            row = {k: inputs.get(k) for k in ["customer_name","customer_phone","vehicle_year","vehicle_make","vehicle_model","job_description","parts_used","cost"]}
            row["status"] = inputs.get("status", "pending")
            row["paid"] = False
            res = db.table("jobs").insert(row).execute()
            j = res.data[0]
            return f"Job saved! ID: {j['id']} | Customer: {j['customer_name']} | Status: {j['status']}"
        except Exception as e:
            return f"Failed to add job: {e}"
    if name == "update_job":
        try:
            job_id = inputs.pop("job_id")
            updates = {k: v for k, v in inputs.items() if v is not None}
            updates["updated_at"] = datetime.now().isoformat()
            res = db.table("jobs").update(updates).eq("id", job_id).execute()
            return f"Job {job_id} updated!" if res.data else f"Job {job_id} not found."
        except Exception as e:
            return f"Failed to update: {e}"
    if name == "list_jobs":
        try:
            q = db.table("jobs").select("*").order("created_at", desc=True)
            if inputs.get("status"):
                q = q.eq("status", inputs["status"])
            res = q.execute()
            if not res.data:
                return "No jobs found."
            lines = []
            for j in res.data:
                lines.append(f"ID {j['id']} | {j['customer_name']} | {j.get('vehicle_year','')} {j.get('vehicle_make','')} {j.get('vehicle_model','')}\n  {j['job_description']}\n  Status: {j['status']} | Paid: {j.get('paid',False)} | Cost: ${j.get('cost','N/A')}")
            return "\n\n".join(lines)
        except Exception as e:
            return f"Failed: {e}"
    if name == "get_customer_jobs":
        try:
            res = db.table("jobs").select("*").ilike("customer_name", f"%{inputs['customer_name']}%").execute()
            if not res.data:
                return f"No jobs found for {inputs['customer_name']}."
            lines = []
            for j in res.data:
                lines.append(f"ID {j['id']} | {j.get('vehicle_year','')} {j.get('vehicle_make','')} {j.get('vehicle_model','')}\n  Job: {j['job_description']}\n  Parts: {j.get('parts_used','N/A')}\n  Cost: ${j.get('cost','N/A')} | Paid: {j.get('paid',False)}\n  Status: {j['status']} | Date: {str(j['created_at'])[:10]}")
            return f"Jobs for {inputs['customer_name']}:\n\n" + "\n\n".join(lines)
        except Exception as e:
            return f"Failed: {e}"
    if name == "save_memory":
        try:
            existing = db.table("memory").select("id").eq("key", inputs["key"]).execute()
            if existing.data:
                db.table("memory").update({"value": inputs["value"]}).eq("key", inputs["key"]).execute()
                return f"Updated memory: {inputs['key']} — {inputs['value']}"
            else:
                db.table("memory").insert({"key": inputs["key"], "value": inputs["value"]}).execute()
                return f"Saved: {inputs['key']} — {inputs['value']}"
        except Exception as e:
            return f"Failed: {e}"
    if name == "recall_memory":
        try:
            query = inputs["query"].lower()
            mems   = db.table("memory").select("*").execute()
            convos = db.table("conversations").select("*").ilike("content", f"%{query}%").order("timestamp", desc=True).limit(10).execute()
            results = []
            for m in mems.data:
                if query in m["key"].lower() or query in m["value"].lower():
                    results.append(f"[Memory] {m['key']}: {m['value']}  ({str(m['created_at'])[:10]})")
            for c in convos.data:
                results.append(f"[{c['role'].upper()} on {str(c['timestamp'])[:10]}]: {c['content'][:250]}...")
            return (f"Found {len(results)} result(s):\n\n" + "\n\n".join(results[:15])) if results else f"Nothing found for '{query}'."
        except Exception as e:
            return f"Failed: {e}"
    if name == "get_kalshi_balance":
        result = get_kalshi_balance()
        if result["success"]:
            return f"Kalshi Balance: ${result['balance']:.2f}"
        return f"Couldn't fetch balance: {result['error']}"
    if name == "get_kalshi_positions":
        result = get_kalshi_positions()
        if result["success"]:
            positions = result["positions"]
            if not positions:
                return "No open positions on Kalshi right now."
            lines = []
            for p in positions:
                lines.append(f"Market: {p.get('ticker','')}\nYes contracts: {p.get('position',0)} | Value: ~${p.get('market_exposure',0)/100:.2f}")
            return "Open Kalshi positions:\n\n" + "\n\n".join(lines)
        return f"Couldn't fetch positions: {result['error']}"
    if name == "get_trade_history":
        try:
            limit = inputs.get("limit", 10)
            res = db.table("trades").select("*").order("created_at", desc=True).limit(limit).execute()
            if not res.data:
                return "No trades in history yet."
            lines = []
            total_pl = 0
            for t in res.data:
                pl = t.get("profit_loss") or 0
                total_pl += pl
                lines.append(f"{'✅' if pl > 0 else '❌' if pl < 0 else '⏳'} {t['market_title'][:50]}\n  Side: {t['side']} | Cost: ${t.get('estimated_cost',0):.2f} | P/L: ${pl:.2f} | Status: {t['status']}\n  Date: {str(t['created_at'])[:10]}")
            return f"Last {len(res.data)} trades (Total P/L: ${total_pl:.2f}):\n\n" + "\n\n".join(lines)
        except Exception as e:
            return f"Failed: {e}"
    if name == "scan_and_analyze_market":
        try:
            ticker = inputs["ticker"]
            title  = inputs["title"]
            price  = inputs["current_yes_price"]
            search_results = run_tool("search_web", {"query": f"{title} prediction odds 2025"})
            search_results2 = run_tool("search_web", {"query": f"{title} latest news"})
            analysis_prompt = f"""You are a sharp prediction market analyst. Analyze this Kalshi market:
Market: {title}
Ticker: {ticker}
Current YES price: {price} cents ({price}% implied probability)
Recent research:
{search_results[:1500]}
{search_results2[:1500]}
Based on this information:
1. What is your estimated TRUE probability of YES? (0-100)
2. What is the edge? (your estimate minus current price)
3. Should we bet YES, NO, or SKIP?
4. Confidence level 0-100?
5. Brief reasoning (2-3 sentences)
Respond in this exact format:
TRUE_PROB: [number]
EDGE: [number]
BET: [YES/NO/SKIP]
CONFIDENCE: [number]
REASON: [reasoning]"""
            analysis = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": analysis_prompt}]
            )
            return analysis.content[0].text
        except Exception as e:
            return f"Analysis failed: {e}"
    if name == "execute_trade":
        if TRADING_PAUSED:
            return "Trading is currently paused. Tell Jay and ask him to resume."
        ticker      = inputs["ticker"]
        title       = inputs["title"]
        side        = inputs["side"].lower()
        price_cents = inputs["price_cents"]
        reasoning   = inputs["reasoning"]
        contracts   = max(1, int(2.00 / (price_cents / 100)))  # Default $2 bet
        cost        = round((price_cents / 100) * contracts, 2)
        result = place_kalshi_order(ticker, side, price_cents, contracts)
        if result["success"]:
            return f"Trade placed! ✅\nMarket: {title}\nSide: {side.upper()} | Price: {price_cents}¢ | Contracts: {contracts} | Cost: ${cost:.2f}\nReasoning: {reasoning}"
        return f"Trade failed: {result['error']}"
    if name == "get_trading_status":
        try:
            lines = ["📊 Smart Trading Status\n"]
            lines.append(f"Bot: {'⏸ PAUSED' if TRADING_PAUSED else '🟢 ACTIVE'}")
            # Open trades
            open_trades = db.table("trades").select("*").eq("status", "open").execute().data or []
            if open_trades:
                lines.append(f"\nOpen positions ({len(open_trades)}):")
                for t in open_trades:
                    lines.append(f"  • {t.get('market_ticker')} — {t.get('side','').upper()} @ {t.get('price_cents',0)}¢ | ${t.get('estimated_cost',0):.2f}")
            else:
                lines.append("\nNo open positions.")
            # Category stats
            stats = db.table("category_stats").select("*").order("total_trades", desc=True).limit(5).execute().data or []
            if stats:
                lines.append("\nWin rates by category:")
                for s in stats:
                    wr = round(s["wins"] / s["total_trades"] * 100) if s["total_trades"] > 0 else 0
                    lines.append(f"  • {s['category']}: {wr}% ({s['wins']}/{s['total_trades']}) | P/L: ${s['total_pl']:.2f}")
            # Recent analysis
            recent = db.table("market_analyses").select("*").order("created_at", desc=True).limit(3).execute().data or []
            if recent:
                lines.append("\nLast 3 markets analyzed:")
                for a in recent:
                    traded = "✅ traded" if a.get("trade_placed") else "⏭ skipped"
                    lines.append(f"  • {a.get('title','')[:50]} | edge={a.get('edge',0)}% | {traded}")
            return "\n".join(lines)
        except Exception as e:
            return f"Failed to get trading status: {e}"
    if name == "get_trading_lessons":
        try:
            limit = inputs.get("limit", 10)
            q = db.table("trading_lessons").select("*").order("created_at", desc=True)
            if inputs.get("category"):
                q = q.eq("category", inputs["category"])
            lessons = q.limit(limit).execute().data or []
            if not lessons:
                return "No lessons learned yet — the bot needs to make some trades first."
            stats = db.table("category_stats").select("*").order("total_trades", desc=True).execute().data or []
            lines = ["📚 What I've learned from past trades:\n"]
            if stats:
                lines.append("Win rates by category:")
                for s in stats:
                    wr = round(s["wins"] / s["total_trades"] * 100) if s["total_trades"] > 0 else 0
                    lines.append(f"  {s['category']}: {wr}% win rate | avg edge: {s['avg_edge']:.1f}% | P/L: ${s['total_pl']:.2f}")
                lines.append("")
            lines.append("Recent lessons:")
            for l in lessons:
                icon = "✅" if l.get("was_correct") else "❌"
                lines.append(f"{icon} [{l.get('category','?')}] {l.get('lesson','')}")
            return "\n".join(lines)
        except Exception as e:
            return f"Failed: {e}"
    if name == "get_income_summary":
        try:
            res = db.table("jobs").select("*").execute()
            jobs = res.data or []
            total_earned   = sum(j.get("cost") or 0 for j in jobs if j.get("paid"))
            total_owed     = sum(j.get("cost") or 0 for j in jobs if not j.get("paid") and j.get("cost"))
            by_status = {}
            for j in jobs:
                s = j.get("status", "unknown")
                by_status[s] = by_status.get(s, 0) + 1
            lines = [
                f"💰 Income Summary",
                f"Collected: ${total_earned:.2f}",
                f"Outstanding (unpaid): ${total_owed:.2f}",
                f"Total jobs: {len(jobs)}",
            ]
            for status, count in sorted(by_status.items()):
                lines.append(f"  • {status}: {count}")
            return "\n".join(lines)
        except Exception as e:
            return f"Failed: {e}"
    if name == "delete_job":
        try:
            job_id = inputs["job_id"]
            res = db.table("jobs").delete().eq("id", job_id).execute()
            return f"Job {job_id} deleted." if res.data else f"Job {job_id} not found."
        except Exception as e:
            return f"Failed: {e}"
    if name == "search_kalshi_markets":
        try:
            status = inputs.get("status", "open")
            params = {"limit": 20, "status": status}
            if inputs.get("series"):
                params["series_ticker"] = inputs["series"]
            path = "/trade-api/v2/markets"
            with httpx.Client() as client:
                r = client.get(f"{KALSHI_BASE_URL}/markets", headers=sign_kalshi_request("GET", path), params=params, timeout=15)
                if r.status_code != 200:
                    return f"Kalshi API error: {r.text[:200]}"
                markets = r.json().get("markets", [])
            query = (inputs.get("query") or "").lower()
            if query:
                markets = [m for m in markets if query in (m.get("title") or "").lower()]
            if not markets:
                return "No markets found."
            lines = []
            for m in markets[:10]:
                yes_price = round(float(m.get("yes_ask_dollars") or m.get("last_price_dollars") or 0) * 100)
                lines.append(f"{m.get('ticker')} — {m.get('title','')[:60]}\n  YES: {yes_price}¢ | Closes: {str(m.get('close_time',''))[:16]}")
            return f"Found {len(markets)} market(s):\n\n" + "\n\n".join(lines)
        except Exception as e:
            return f"Failed: {e}"
    if name == "get_daily_summary":
        try:
            parts = []
            # Jobs
            jobs = db.table("jobs").select("*").execute().data or []
            pending = [j for j in jobs if j.get("status") in ("pending", "in_progress", "waiting_parts")]
            unpaid  = [j for j in jobs if not j.get("paid") and j.get("cost")]
            owed    = sum(j.get("cost") or 0 for j in unpaid)
            parts.append(f"🔧 Jobs: {len(pending)} active | ${owed:.2f} outstanding from {len(unpaid)} unpaid")
            # Today's trades
            from datetime import date
            today = date.today().isoformat()
            trades = db.table("trades").select("*").gte("created_at", today).execute().data or []
            day_pl = sum(t.get("profit_loss") or 0 for t in trades)
            parts.append(f"📈 Today's trades: {len(trades)} | P/L: ${day_pl:.2f}")
            # Balance
            bal = get_kalshi_balance()
            if bal["success"]:
                parts.append(f"💵 Kalshi balance: ${bal['balance']:.2f}")
            # Trading status summary
            open_t = db.table("trades").select("id").eq("status", "open").execute().data or []
            parts.append(f"📊 Smart trader: {'⏸ paused' if TRADING_PAUSED else '🟢 active'} | {len(open_t)} open position(s)")
            return "\n".join(parts)
        except Exception as e:
            return f"Failed: {e}"
    return f"Unknown tool: {name}"
# ── Conversation helpers ───────────────────────────────────────────────────────
def get_history(limit: int = 20) -> list:
    try:
        res = db.table("conversations").select("*").order("timestamp", desc=True).limit(limit).execute()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(res.data)]
    except:
        return []
def save_message(role: str, content: str):
    try:
        db.table("conversations").insert({"role": role, "content": content}).execute()
    except Exception as e:
        logger.error(f"Could not save message: {e}")
def get_live_context() -> str:
    """Build a brief live-context snippet injected into every Claude call."""
    lines = []
    try:
        jobs = db.table("jobs").select("*").execute().data or []
        active = [j for j in jobs if j.get("status") in ("pending", "in_progress", "waiting_parts")]
        unpaid = [j for j in jobs if not j.get("paid") and j.get("cost")]
        owed   = sum(j.get("cost") or 0 for j in unpaid)
        if active:
            lines.append(f"Active jobs ({len(active)}): " + ", ".join(
                f"#{j['id']} {j['customer_name']} – {j['job_description'][:40]}" for j in active[:5]
            ))
        if unpaid:
            lines.append(f"Unpaid: {len(unpaid)} job(s) totalling ${owed:.2f}")
    except Exception:
        pass
    try:
        open_trades = db.table("trades").select("market_ticker,side").eq("status", "open").execute().data or []
        if open_trades:
            lines.append("Open trades: " + ", ".join(f"{t['market_ticker']} {t['side'].upper()}" for t in open_trades[:3]))
    except Exception:
        pass
    if not lines:
        return ""
    return "LIVE CONTEXT:\n" + "\n".join(lines)
# ── Claude agent loop ─────────────────────────────────────────────────────────
async def run_claude(user_text: str, extra_system: str = "") -> str:
    history  = get_history(limit=20)
    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_text:
        history = history[:-1]
    messages = history + [{"role": "user", "content": user_text}]
    system   = SYSTEM_PROMPT.format(datetime=datetime.now().strftime("%A, %B %d, %Y at %I:%M %p"))
    live_ctx = get_live_context()
    if live_ctx:
        system += f"\n\n{live_ctx}"
    if extra_system:
        system += f"\n\n{extra_system}"
    for _ in range(10):
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = run_tool(block.name, dict(block.input))
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": tool_results})
        else:
            return "".join(b.text for b in response.content if hasattr(b, "text"))
    return "I got stuck in a loop — try again."
# ── Smart trader helpers ──────────────────────────────────────────────────────
import json as _json
def get_settled_markets(series_ticker: str, limit: int = 25) -> list:
    """Fetch recently settled markets for a series, sorted oldest→newest."""
    try:
        path = "/trade-api/v2/markets"
        with httpx.Client() as client:
            r = client.get(
                f"{KALSHI_BASE_URL}/markets",
                headers=sign_kalshi_request("GET", path),
                params={"limit": limit, "status": "settled", "series_ticker": series_ticker},
                timeout=15,
            )
            if r.status_code == 200:
                markets = r.json().get("markets", [])
                markets.sort(key=lambda m: m.get("close_time", ""))
                return markets
            logger.warning(f"{series_ticker} settled fetch {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.error(f"get_settled_markets({series_ticker}) error: {e}")
    return []
def get_open_market(series_ticker: str) -> dict:
    """Fetch the current open market for a series (soonest to close)."""
    try:
        path = "/trade-api/v2/markets"
        with httpx.Client() as client:
            r = client.get(
                f"{KALSHI_BASE_URL}/markets",
                headers=sign_kalshi_request("GET", path),
                params={"limit": 5, "status": "open", "series_ticker": series_ticker},
                timeout=15,
            )
            if r.status_code == 200:
                markets = r.json().get("markets", [])
                if markets:
                    markets.sort(key=lambda m: m.get("open_time", ""))
                    return markets[0]
    except Exception as e:
        logger.error(f"get_open_market({series_ticker}) error: {e}")
    return {}
# ── Smart trading helpers ─────────────────────────────────────────────────────
def fetch_open_markets(limit: int = 50) -> list:
    """Fetch open Kalshi markets across all series."""
    try:
        path = "/trade-api/v2/markets"
        with httpx.Client() as client:
            r = client.get(
                f"{KALSHI_BASE_URL}/markets",
                headers=sign_kalshi_request("GET", path),
                params={"limit": limit, "status": "open"},
                timeout=15,
            )
            if r.status_code == 200:
                return r.json().get("markets", [])
    except Exception as e:
        logger.error(f"fetch_open_markets error: {e}")
    return []

def fetch_settled_market(ticker: str) -> dict:
    """Fetch a single settled market by ticker."""
    try:
        path = f"/trade-api/v2/markets/{ticker}"
        with httpx.Client() as client:
            r = client.get(f"{KALSHI_BASE_URL}/markets/{ticker}",
                           headers=sign_kalshi_request("GET", path), timeout=10)
            if r.status_code == 200:
                return r.json().get("market", {})
    except Exception as e:
        logger.error(f"fetch_settled_market({ticker}) error: {e}")
    return {}

def get_past_lessons(category: str, limit: int = 5) -> str:
    """Retrieve past lessons for a category to inform current analysis."""
    try:
        lessons = db.table("trading_lessons").select("*") \
            .eq("category", category).order("created_at", desc=True).limit(limit).execute().data or []
        stats = db.table("category_stats").select("*").eq("category", category).execute().data
        if not lessons and not stats:
            return ""
        lines = [f"Past experience in '{category}':"]
        if stats:
            s = stats[0]
            wr = round(s["wins"] / s["total_trades"] * 100) if s["total_trades"] > 0 else 0
            lines.append(f"  Win rate: {wr}% ({s['wins']}/{s['total_trades']}) | Avg edge: {s['avg_edge']:.1f}% | P/L: ${s['total_pl']:.2f}")
        for l in lessons:
            icon = "✅" if l.get("was_correct") else "❌"
            lines.append(f"  {icon} {l['lesson']}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"get_past_lessons error: {e}")
        return ""

def categorize_market(title: str, ticker: str) -> str:
    """Categorize a market based on its title/ticker."""
    title_lower = title.lower()
    ticker_upper = ticker.upper()
    if any(w in title_lower for w in ["btc", "bitcoin", "eth", "ethereum", "sol", "solana", "crypto"]):
        return "crypto"
    if any(w in title_lower for w in ["fed", "rate", "cpi", "inflation", "gdp", "unemployment", "jobs"]):
        return "economics"
    if any(w in title_lower for w in ["president", "congress", "senate", "election", "vote", "trump", "biden", "harris"]):
        return "politics"
    if any(w in title_lower for w in ["nfl", "nba", "mlb", "nhl", "soccer", "sport", "game", "super bowl"]):
        return "sports"
    if any(w in title_lower for w in ["stock", "s&p", "nasdaq", "dow", "market"]):
        return "stocks"
    if any(w in title_lower for w in ["weather", "temperature", "rain", "snow", "hurricane"]):
        return "weather"
    return "other"

def analyze_market_with_claude(market: dict) -> dict:
    """
    Core intelligence: research a market, estimate true probability, decide if worth trading.
    Returns dict with: predicted_prob, edge, confidence, bet, reasoning, category
    """
    ticker    = market.get("ticker", "")
    title     = market.get("title", "")
    yes_price = round(float(market.get("yes_ask_dollars") or market.get("last_price_dollars") or 0.5) * 100)
    category  = categorize_market(title, ticker)

    # Skip markets expiring in under 30 minutes (not enough time to act)
    close_time = market.get("close_time", "")
    try:
        from datetime import timezone, timedelta
        close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        mins_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 60
        if mins_left < 30:
            return {"bet": "SKIP", "reasoning": "Market closes too soon"}
    except Exception:
        pass

    # Do 2 web searches
    try:
        r1 = tavily.search(f"{title} prediction odds probability", max_results=3)
        research1 = "\n".join(f"{x['title']}: {x['content'][:300]}" for x in r1.get("results", []))
        r2 = tavily.search(f"{title} latest news 2025", max_results=3)
        research2 = "\n".join(f"{x['title']}: {x['content'][:300]}" for x in r2.get("results", []))
        web_research = research1 + "\n\n" + research2
    except Exception as e:
        web_research = f"Web search failed: {e}"

    # Get past lessons for this category
    past_lessons = get_past_lessons(category)

    # Ask Claude to analyze
    prompt = f"""You are a sharp prediction market analyst with a track record of finding edges.

Market: {title}
Ticker: {ticker}
Category: {category}
Current YES price: {yes_price}¢ ({yes_price}% implied probability)
Time to close: {int(mins_left) if 'mins_left' in dir() else '?'} minutes

{f'--- PAST EXPERIENCE ---{chr(10)}{past_lessons}{chr(10)}' if past_lessons else ''}
--- WEB RESEARCH ---
{web_research[:3000]}

Based on all of this:
1. What is your estimated TRUE probability YES resolves? (0-100)
2. What edge do you have? (your estimate minus current price, can be negative)
3. Should you bet YES, NO, or SKIP?
4. Confidence in your estimate (0-100)?
5. Brief reasoning (2-3 sentences max)

Rules:
- Only recommend YES or NO if |edge| > 10 AND confidence > 55
- Otherwise SKIP
- If past experience shows consistent losses in this category, be more conservative
- Be honest about uncertainty

Respond in EXACTLY this format (no other text):
TRUE_PROB: [number]
EDGE: [number]
BET: [YES/NO/SKIP]
CONFIDENCE: [number]
REASON: [reasoning]"""

    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        lines = {l.split(":")[0].strip(): ":".join(l.split(":")[1:]).strip()
                 for l in text.split("\n") if ":" in l}
        return {
            "ticker":        ticker,
            "title":         title,
            "category":      category,
            "yes_price":     yes_price,
            "predicted_prob": int(lines.get("TRUE_PROB", 50)),
            "edge":          int(lines.get("EDGE", 0)),
            "bet":           lines.get("BET", "SKIP").upper(),
            "confidence":    int(lines.get("CONFIDENCE", 0)),
            "reasoning":     lines.get("REASON", ""),
            "web_research":  web_research[:2000],
        }
    except Exception as e:
        logger.error(f"Claude analysis failed for {ticker}: {e}")
        return {"bet": "SKIP", "reasoning": str(e)}

def log_analysis(analysis: dict, trade_placed: bool, trade_side: str = None):
    """Save market analysis to DB."""
    try:
        db.table("market_analyses").insert({
            "ticker":         analysis.get("ticker"),
            "title":          analysis.get("title"),
            "category":       analysis.get("category"),
            "predicted_prob": analysis.get("predicted_prob"),
            "market_price":   analysis.get("yes_price"),
            "edge":           analysis.get("edge"),
            "confidence":     analysis.get("confidence"),
            "reasoning":      analysis.get("reasoning"),
            "web_research":   analysis.get("web_research", "")[:2000],
            "trade_placed":   trade_placed,
            "trade_side":     trade_side,
        }).execute()
    except Exception as e:
        logger.error(f"log_analysis error: {e}")

def update_trade_result(ticker: str, status: str, profit_loss: float):
    """Update a trade's outcome in the trades table."""
    try:
        db.table("trades").update({
            "status": status,
            "profit_loss": round(profit_loss, 2),
        }).eq("market_ticker", ticker).eq("status", "open").execute()
    except Exception as e:
        logger.error(f"update_trade_result error: {e}")

def save_lesson(ticker: str, category: str, lesson: str, outcome: str, was_correct: bool):
    """Save a lesson learned from a settled trade."""
    try:
        db.table("trading_lessons").insert({
            "ticker":      ticker,
            "category":    category,
            "lesson":      lesson,
            "outcome":     outcome,
            "was_correct": was_correct,
        }).execute()
    except Exception as e:
        logger.error(f"save_lesson error: {e}")

def update_category_stats(category: str, won: bool, edge: float, pl: float):
    """Update running win rate stats for a category."""
    try:
        existing = db.table("category_stats").select("*").eq("category", category).execute().data
        if existing:
            s = existing[0]
            new_total  = s["total_trades"] + 1
            new_wins   = s["wins"] + (1 if won else 0)
            new_pl     = s["total_pl"] + pl
            new_avg_edge = (s["avg_edge"] * s["total_trades"] + edge) / new_total
            db.table("category_stats").update({
                "total_trades": new_total,
                "wins":         new_wins,
                "total_pl":     round(new_pl, 2),
                "avg_edge":     round(new_avg_edge, 1),
                "updated_at":   datetime.utcnow().isoformat(),
            }).eq("category", category).execute()
        else:
            db.table("category_stats").insert({
                "category":     category,
                "total_trades": 1,
                "wins":         1 if won else 0,
                "total_pl":     round(pl, 2),
                "avg_edge":     round(edge, 1),
            }).execute()
    except Exception as e:
        logger.error(f"update_category_stats error: {e}")

# ── Smart trading loop ────────────────────────────────────────────────────────
async def smart_trader_loop(app):
    """
    Main AI trading loop. Every 5 minutes:
    1. Scan all open Kalshi markets
    2. Research each one with web search
    3. Ask Claude for edge estimate
    4. Trade if edge > MIN_EDGE and confidence > MIN_CONFIDENCE
    5. Check settled trades for outcomes and generate lessons
    """
    global TRADING_PAUSED
    await asyncio.sleep(60)  # Wait for bot to fully start
    logger.info("Smart trader loop started.")

    while True:
        try:
            if not KALSHI_API_KEY:
                await asyncio.sleep(SMART_TRADE_INTERVAL)
                continue

            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

            # ── 1. Check open trades for settlements ──────────────────────────
            open_trades = db.table("trades").select("*").eq("status", "open").execute().data or []
            for trade in open_trades:
                try:
                    market = fetch_settled_market(trade["market_ticker"])
                    if not market or market.get("status") != "settled":
                        continue

                    result     = (market.get("result") or "").upper()
                    side       = (trade.get("side") or "").upper()
                    won        = (result == side)
                    cost       = trade.get("estimated_cost", 0)
                    price_c    = trade.get("price_cents", 50)
                    pl         = round(cost * (100 - price_c) / price_c, 2) if won else -cost
                    category   = trade.get("strategy", "other")

                    update_trade_result(trade["market_ticker"], "won" if won else "lost", pl)

                    # Generate a lesson with Claude
                    analysis_row = db.table("market_analyses") \
                        .select("*").eq("ticker", trade["market_ticker"]).execute().data
                    reasoning = analysis_row[0]["reasoning"] if analysis_row else "No reasoning stored"
                    lesson_prompt = f"""A Kalshi trade just settled. Generate one short lesson (1-2 sentences) from this outcome.

Market: {trade.get('market_title', trade['market_ticker'])}
Category: {category}
My prediction reasoning: {reasoning}
I bet: {side}
Market resolved: {result}
Result: {'WIN' if won else 'LOSS'} | P/L: ${pl:.2f}

What should I learn or remember for future trades in this category? Be specific and actionable."""

                    lesson_resp = claude.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=150,
                        messages=[{"role": "user", "content": lesson_prompt}]
                    )
                    lesson = lesson_resp.content[0].text.strip()
                    save_lesson(trade["market_ticker"], category, lesson, result, won)
                    update_category_stats(category, won, trade.get("price_cents", 50) - 50, pl)

                    if chat_id:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"{'✅ WIN' if won else '❌ LOSS'}: {trade.get('market_title', trade['market_ticker'])[:60]}\n"
                                f"Bet {side} | Result: {result} | P/L: ${pl:+.2f}\n\n"
                                f"📚 Lesson: {lesson}"
                            )
                        )
                    logger.info(f"Settled: {trade['market_ticker']} → {'won' if won else 'lost'} ${pl:+.2f}")
                except Exception as e:
                    logger.error(f"Settlement check error for {trade.get('market_ticker')}: {e}")

            # ── 2. Skip scanning if paused or too many open trades ────────────
            if TRADING_PAUSED:
                await asyncio.sleep(SMART_TRADE_INTERVAL)
                continue

            open_count = len(db.table("trades").select("id").eq("status", "open").execute().data or [])
            if open_count >= MAX_OPEN_TRADES:
                logger.info(f"Smart trader: {open_count} open trades, at max. Skipping scan.")
                await asyncio.sleep(SMART_TRADE_INTERVAL)
                continue

            # ── 3. Scan open markets ──────────────────────────────────────────
            markets = fetch_open_markets(limit=30)
            logger.info(f"Smart trader: scanning {len(markets)} open markets.")

            # Skip tickers we already have open trades on
            open_tickers = {t["market_ticker"] for t in open_trades}

            for market in markets:
                ticker = market.get("ticker", "")
                if ticker in open_tickers:
                    continue
                if open_count >= MAX_OPEN_TRADES:
                    break

                try:
                    analysis = analyze_market_with_claude(market)
                    bet      = analysis.get("bet", "SKIP")
                    edge     = analysis.get("edge", 0)
                    conf     = analysis.get("confidence", 0)
                    category = analysis.get("category", "other")

                    if bet == "SKIP" or abs(edge) < MIN_EDGE or conf < MIN_CONFIDENCE:
                        log_analysis(analysis, trade_placed=False)
                        logger.info(f"SKIP {ticker}: edge={edge}% conf={conf}%")
                        continue

                    # Size bet based on confidence
                    bet_size = BASE_BET + ((conf - MIN_CONFIDENCE) / (100 - MIN_CONFIDENCE)) * (MAX_BET - BASE_BET)
                    bet_size = round(min(bet_size, MAX_BET), 2)
                    side     = bet.lower()  # "yes" or "no"

                    yes_price = analysis.get("yes_price", 50)
                    price_cents = yes_price if side == "yes" else (100 - yes_price)
                    price_cents = max(1, min(99, price_cents))
                    contracts   = max(1, int(bet_size / (price_cents / 100)))

                    result = place_kalshi_order(ticker, side, price_cents, contracts)

                    if result["success"] and result.get("filled"):
                        actual_cost = round((price_cents / 100) * int(result.get("fill_count", contracts)), 2)
                        db.table("trades").insert({
                            "market_ticker":  ticker,
                            "market_title":   analysis.get("title", ticker)[:100],
                            "side":           side,
                            "price_cents":    price_cents,
                            "contracts":      int(result.get("fill_count", contracts)),
                            "estimated_cost": actual_cost,
                            "status":         "open",
                            "profit_loss":    0,
                            "strategy":       category,
                        }).execute()
                        log_analysis(analysis, trade_placed=True, trade_side=side)
                        open_count += 1
                        open_tickers.add(ticker)

                        if chat_id:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"🎯 New Trade Placed!\n\n"
                                    f"{analysis['title'][:70]}\n"
                                    f"Bet: {side.upper()} @ {price_cents}¢\n"
                                    f"Edge: {edge:+d}% | Confidence: {conf}%\n"
                                    f"Size: ${actual_cost:.2f}\n\n"
                                    f"Reasoning: {analysis['reasoning']}"
                                )
                            )
                        logger.info(f"TRADE: {ticker} {side.upper()} edge={edge}% conf={conf}% ${actual_cost:.2f}")
                    else:
                        log_analysis(analysis, trade_placed=False)
                        logger.info(f"Order failed/unfilled for {ticker}: {result.get('error','no liquidity')}")

                    await asyncio.sleep(5)  # Throttle between analyses

                except Exception as e:
                    logger.error(f"Error analyzing {ticker}: {e}")

        except Exception as e:
            logger.error(f"Smart trader loop error: {e}")

        await asyncio.sleep(SMART_TRADE_INTERVAL)
# ── Telegram handlers ─────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TRADING_PAUSED
    user_text = update.message.text.lower().strip()
    # Store chat ID for strategy notifications
    try:
        os.environ["TELEGRAM_CHAT_ID"] = str(update.effective_chat.id)
    except:
        pass
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    # Trading control commands
    if "pause trading" in user_text:
        TRADING_PAUSED = True
        await update.message.reply_text("Trading paused. I won't place any new trades until you say 'resume trading'.")
        return
    if "resume trading" in user_text:
        TRADING_PAUSED = False
        await update.message.reply_text("Trading resumed! All three strategies are back on.")
        return
    save_message("user", update.message.text)
    final = await run_claude(update.message.text)
    save_message("assistant", final)
    for i in range(0, max(len(final), 1), 4096):
        await update.message.reply_text(final[i : i + 4096])
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Let Jay send a photo and ask a question about it (car damage, parts, etc.)."""
    try:
        os.environ["TELEGRAM_CHAT_ID"] = str(update.effective_chat.id)
    except:
        pass
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    caption = update.message.caption or "What do you see in this image? Give me useful details."
    photo   = update.message.photo[-1]  # highest resolution
    file    = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()
    import base64 as _b64
    img_b64 = _b64.b64encode(photo_bytes).decode()
    system  = SYSTEM_PROMPT.format(datetime=datetime.now().strftime("%A, %B %d, %Y at %I:%M %p"))
    live_ctx = get_live_context()
    if live_ctx:
        system += f"\n\n{live_ctx}"
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text",  "text": caption},
            ],
        }],
    )
    reply = "".join(b.text for b in response.content if hasattr(b, "text"))
    save_message("user", f"[Photo] {caption}")
    save_message("assistant", reply)
    await update.message.reply_text(reply)
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    result = run_tool("get_trading_status", {})
    await update.message.reply_text(result)
async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    result = run_tool("list_jobs", {"status": "pending"}) + "\n\n" + run_tool("list_jobs", {"status": "in_progress"})
    await update.message.reply_text(result[:4096])
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    result = run_tool("get_kalshi_balance", {})
    await update.message.reply_text(result)
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey Jay! Here's what I can do:\n\n"
        "Quick commands:\n"
        "  /status — trading status for BTC, ETH, SOL\n"
        "  /jobs — your active mechanic jobs\n"
        "  /balance — Kalshi account balance\n"
        "  /help — this message\n\n"
        "Just chat with me to:\n"
        "  • Add, update, or look up mechanic jobs\n"
        "  • Track payments and parts\n"
        "  • Search the web\n"
        "  • Check trade history or income summary\n"
        "  • Browse Kalshi markets\n"
        "  • Send a photo and ask me about it\n\n"
        "Say 'pause trading' or 'resume trading' to control the strategy."
    )
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        os.environ["TELEGRAM_CHAT_ID"] = str(update.effective_chat.id)
    except:
        pass
    await update.message.reply_text(
        "Hey Jay! I'm Pecci, your personal assistant. I can track your mechanic jobs, "
        "search the web, remember everything we talk about, monitor Kalshi markets, and help you trade. "
        "What do you need? Type /help to see everything I can do."
    )
# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("jobs",    cmd_jobs))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running...")
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        if KALSHI_API_KEY:
            asyncio.create_task(smart_trader_loop(app))
            logger.info("Smart AI trader started — scanning all Kalshi markets every 5 minutes.")
        else:
            logger.warning("No KALSHI_API_KEY — trading disabled.")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()
if __name__ == "__main__":
    asyncio.run(main())
