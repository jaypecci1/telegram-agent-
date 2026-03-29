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
BET_SIZES               = [10.00, 20.00, 40.00]   # Martingale sequence ($)
STREAK_REQUIRED         = 5                       # Consecutive same-direction to trigger
COOLDOWN_MINUTES        = 60                      # Pause after 3 straight losses
CHECK_INTERVAL          = 60                      # Check every 60 seconds
TRADING_PAUSED          = False                   # Can be toggled via Telegram command
# Markets to run the strategy on: (series_ticker, state_key, display_name)
CRYPTO_MARKETS = [
    ("KXBTC15M",  "btc15m_state",  "BTC"),
    ("KXETH15M",  "eth15m_state",  "ETH"),
    ("KXSOL15M",  "sol15m_state",  "SOL"),
]
# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Pecci, Jay's personal AI assistant. Jay is a young adult who works as an automotive and light diesel mechanic doing side jobs, and he is studying Economics at Texas Tech University. His long-term goal is to open his own mechanic shop. He also trades on Kalshi prediction markets.
You help Jay with:
- Tracking his mechanic jobs (customers, vehicles, parts, costs, payment status, job status)
- Searching the web for any question he has
- Remembering important information from all past conversations
- Managing his day-to-day life as a young adult
- Monitoring and trading on Kalshi prediction markets
Trading strategy: You run an autonomous 15-minute mean-reversion + martingale strategy on three Kalshi crypto markets simultaneously: BTC (KXBTC15M), ETH (KXETH15M), and SOL (KXSOL15M). For each coin independently, you watch for 5 consecutive UP or DOWN results, then bet the reversal on the next open market. Bet sizes are $10 → $20 → $40. If all three bets lose, you pause that coin for 1 hour and reset. Any win resets that coin's cycle. You notify Jay automatically when bets are placed, won, or lost.
IMPORTANT: When Jay asks about streaks, trading status, what phase any coin is in, or anything related to the current state of trading, you MUST ALWAYS call the get_trading_status tool. NEVER estimate or guess streak numbers. The real data is stored in the database — use the tool to fetch it.
IMPORTANT: When Jay asks about trade history or past trades, ALWAYS call the get_trade_history tool. All trades placed by the background strategy are logged in the database with full details including outcome and profit/loss.
Telegram trading commands Jay can use:
- "pause trading" - stop all new trades
- "resume trading" - resume trading
- "show my positions" - list open Kalshi positions
- "show my balance" - check Kalshi balance
- "show trade history" - view past trades
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
    try:
        path = "/trade-api/v2/portfolio/orders"
        payload = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy",
            "type": "limit",
            "side": side,
            "count": count,
            f"{side}_price": price_cents,
        }
        logger.info(f"Placing order: ticker={ticker} side={side} price={price_cents}c count={count}")
        with httpx.Client() as client:
            r = client.post(
                f"{KALSHI_BASE_URL}/portfolio/orders",
                headers=sign_kalshi_request("POST", path),
                json=payload,
                timeout=15,
            )
            logger.info(f"Order response {r.status_code}: {r.text[:300]}")
            if r.status_code in (200, 201):
                return {"success": True, "order": r.json().get("order", {})}
            return {"success": False, "error": r.text}
    except Exception as e:
        return {"success": False, "error": str(e)}
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
        "description": "Get the current live trading status for all crypto markets — shows streak count, direction, phase (watching/betting/cooldown), and active bet info for BTC, ETH, and SOL",
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
            db.table("memory").insert({"key": inputs["key"], "value": inputs["value"]}).execute()
            return f"Got it: {inputs['key']} — {inputs['value']}"
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
            lines = [f"📊 Live Trading Status ({STREAK_REQUIRED}-streak trigger)\n"]
            for series_ticker, state_key, coin in CRYPTO_MARKETS:
                s = get_market_state(state_key)
                phase = s.get("phase", "watching")
                streak_dir   = s.get("streak_direction") or "—"
                streak_count = s.get("streak_count", 0)
                bet_index    = s.get("bet_index", 0)
                losses       = s.get("consecutive_losses", 0)
                active       = s.get("active_bet_ticker")
                active_side  = s.get("active_bet_side")
                cooldown     = s.get("cooldown_until", "")
                if phase == "watching":
                    status = f"👀 Watching — {streak_count} consecutive {streak_dir}"
                elif phase == "betting":
                    if active:
                        status = f"🎯 Bet active on {active} ({active_side.upper()}) — ${BET_SIZES[bet_index]:.2f} (bet #{bet_index+1})"
                    else:
                        status = f"🎯 Betting mode — waiting for next open market (bet #{bet_index+1})"
                elif phase == "cooldown":
                    status = f"⏸ Cooldown until ~{cooldown[:16]} UTC"
                else:
                    status = phase
                lines.append(f"{'₿' if coin=='BTC' else '⟠' if coin=='ETH' else '◎'} {coin}: {status}")
            return "\n".join(lines)
        except Exception as e:
            return f"Failed to get trading status: {e}"
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
# ── Claude agent loop ─────────────────────────────────────────────────────────
async def run_claude(user_text: str, extra_system: str = "") -> str:
    history  = get_history(limit=20)
    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_text:
        history = history[:-1]
    messages = history + [{"role": "user", "content": user_text}]
    system   = SYSTEM_PROMPT.format(datetime=datetime.now().strftime("%A, %B %d, %Y at %I:%M %p"))
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
# ── Crypto 15m strategy helpers ───────────────────────────────────────────────
import json as _json
def _default_state() -> dict:
    return {
        "phase": "watching",          # watching | betting | cooldown
        "streak_direction": None,     # "UP" | "DOWN" | None
        "streak_count": 0,
        "last_processed_ticker": None,
        "bet_index": 0,               # 0=$2, 1=$4, 2=$10
        "consecutive_losses": 0,
        "cooldown_until": None,       # ISO timestamp string
        "active_bet_ticker": None,
        "active_bet_side": None,
    }
def get_market_state(state_key: str) -> dict:
    """Load strategy state for a given market from Supabase."""
    try:
        res = db.table("memory").select("*").eq("key", state_key).execute()
        if res.data:
            return _json.loads(res.data[0]["value"])
    except Exception as e:
        logger.warning(f"Could not load {state_key}: {e}")
    return _default_state()
def save_market_state(state_key: str, state: dict):
    """Persist strategy state for a given market to Supabase."""
    try:
        val      = _json.dumps(state)
        existing = db.table("memory").select("id").eq("key", state_key).execute()
        if existing.data:
            db.table("memory").update({"value": val}).eq("key", state_key).execute()
        else:
            db.table("memory").insert({"key": state_key, "value": val}).execute()
    except Exception as e:
        logger.error(f"Failed to save {state_key}: {e}")
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
# ── Trade logging helper ──────────────────────────────────────────────────────
def log_trade_to_db(ticker: str, coin: str, side: str, price_cents: int,
                    contracts: int, bet_size: float, bet_index: int,
                    streak_direction: str, streak_count: int):
    """Save a trade placed by the background strategy to the trades table."""
    try:
        db.table("trades").insert({
            "market_ticker": ticker,
            "market_title": f"{coin} 15m Reversal Bet #{bet_index + 1}",
            "side": side,
            "price_cents": price_cents,
            "contracts": contracts,
            "estimated_cost": round((price_cents / 100) * contracts, 2),
            "status": "open",
            "profit_loss": 0,
            "strategy": f"{coin}_15m_martingale",
            "streak_direction": streak_direction,
            "streak_count": streak_count,
        }).execute()
        logger.info(f"Trade logged to DB: {ticker} {side} ${bet_size:.2f}")
    except Exception as e:
        logger.error(f"Failed to log trade to DB: {e}")

def update_trade_result(ticker: str, status: str, profit_loss: float):
    """Update a trade's outcome (won/lost) in the trades table."""
    try:
        db.table("trades").update({
            "status": status,
            "profit_loss": round(profit_loss, 2),
        }).eq("market_ticker", ticker).eq("status", "open").execute()
        logger.info(f"Trade updated: {ticker} → {status} (P/L: ${profit_loss:.2f})")
    except Exception as e:
        logger.error(f"Failed to update trade result: {e}")
# ── Generic crypto 15m mean-reversion + martingale strategy ───────────────────
async def crypto15m_strategy(app, series_ticker: str, state_key: str, coin: str):
    """
    Mean-reversion + capped martingale for any Kalshi crypto 15-min series.
    - Watches for STREAK_REQUIRED consecutive UP or DOWN results
    - Bets the reversal on the next open market
    - Martingale: $2 → $4 → $10
    - 3 straight losses → 1-hour cooldown, then reset
    - Any win → full reset back to watching
    """
    global TRADING_PAUSED
    await asyncio.sleep(30)
    logger.info(f"{coin} 15m strategy started.")
    while True:
        try:
            if TRADING_PAUSED or not KALSHI_API_KEY:
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            state   = get_market_state(state_key)
            now_str = datetime.utcnow().isoformat()
            tag     = f"{coin}15m"
            # ── 1. Cooldown check ──────────────────────────────────────────────
            if state["phase"] == "cooldown":
                cooldown_until = state.get("cooldown_until") or ""
                if now_str >= cooldown_until:
                    logger.info(f"{tag}: cooldown expired. Resuming watch.")
                    state.update({
                        "phase": "watching",
                        "streak_direction": None,
                        "streak_count": 0,
                        "bet_index": 0,
                        "consecutive_losses": 0,
                        "cooldown_until": None,
                        "active_bet_ticker": None,
                        "active_bet_side": None,
                    })
                    save_market_state(state_key, state)
                    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
                    if chat_id:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=f"⏰ {coin} 15m: Cooldown over. Back to watching for streaks!"
                        )
                else:
                    logger.info(f"{tag}: in cooldown until {cooldown_until}")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
            # ── 2. Fetch recently settled markets ─────────────────────────────
            settled = get_settled_markets(series_ticker)
            if not settled:
                logger.info(f"{tag}: no settled markets found yet.")
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            logger.info(f"{tag}: fetched {len(settled)} settled markets. Phase={state['phase']}")
            # ── 3. Check if active bet resolved ───────────────────────────────
            if state["phase"] == "betting" and state.get("active_bet_ticker"):
                active_ticker = state["active_bet_ticker"]
                active_side   = state["active_bet_side"]
                resolved_bet  = next((m for m in settled if m.get("ticker") == active_ticker), None)
                if resolved_bet:
                    result_raw = (resolved_bet.get("result") or "").upper()
                    won        = (result_raw == active_side.upper())
                    bet_size   = BET_SIZES[state["bet_index"]]
                    chat_id    = os.environ.get("TELEGRAM_CHAT_ID", "")
                    if won:
                        logger.info(f"{tag}: ✅ WON on {active_ticker}! Resetting.")
                        # ── LOG WIN TO DATABASE ──
                        payout = round(bet_size * (1 - (state.get("active_bet_price", 50) / 100)), 2)
                        update_trade_result(active_ticker, "won", payout)
                        if chat_id:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"✅ {coin} 15m WIN!\n"
                                    f"Market: {active_ticker}\n"
                                    f"Bet: {active_side.upper()} | Size: ${bet_size:.2f}\n\n"
                                    f"Resetting — back to streak watch."
                                )
                            )
                        state.update({
                            "phase": "watching",
                            "streak_direction": None,
                            "streak_count": 0,
                            "bet_index": 0,
                            "consecutive_losses": 0,
                            "active_bet_ticker": None,
                            "active_bet_side": None,
                            "active_bet_price": None,
                        })
                    else:
                        state["consecutive_losses"] += 1
                        logger.info(f"{tag}: ❌ LOST on {active_ticker}. Losses={state['consecutive_losses']}")
                        # ── LOG LOSS TO DATABASE ──
                        update_trade_result(active_ticker, "lost", -bet_size)
                        if state["consecutive_losses"] >= 3:
                            from datetime import timedelta
                            cooldown_until = (
                                datetime.utcnow() + timedelta(minutes=COOLDOWN_MINUTES)
                            ).isoformat()
                            state.update({
                                "phase": "cooldown",
                                "cooldown_until": cooldown_until,
                                "active_bet_ticker": None,
                                "active_bet_side": None,
                                "active_bet_price": None,
                            })
                            logger.info(f"{tag}: all 3 bets lost. Cooldown until {cooldown_until}")
                            if chat_id:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=(
                                        f"❌ {coin} 15m: All 3 bets lost.\n"
                                        f"Pausing for {COOLDOWN_MINUTES} min "
                                        f"(until ~{cooldown_until[:16]} UTC)."
                                    )
                                )
                        else:
                            next_idx = min(state["bet_index"] + 1, 2)
                            state["bet_index"]        = next_idx
                            state["active_bet_ticker"] = None
                            state["active_bet_side"]   = None
                            state["active_bet_price"]  = None
                            logger.info(f"{tag}: escalating to bet #{next_idx+1} — ${BET_SIZES[next_idx]:.2f}")
                            if chat_id:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=(
                                        f"❌ {coin} 15m LOSS on {active_ticker}.\n"
                                        f"Escalating to ${BET_SIZES[next_idx]:.2f}..."
                                    )
                                )
                    save_market_state(state_key, state)
            # ── 4. Update streak (watching phase only) ─────────────────────────
            if state["phase"] == "watching":
                last_ticker = state.get("last_processed_ticker")
                start_idx   = 0
                if last_ticker:
                    for i, m in enumerate(settled):
                        if m.get("ticker") == last_ticker:
                            start_idx = i + 1
                            break
                for m in settled[start_idx:]:
                    ticker     = m.get("ticker", "")
                    result_raw = (m.get("result") or "").upper()
                    if result_raw not in ("YES", "NO"):
                        continue
                    direction = "UP" if result_raw == "YES" else "DOWN"
                    if state["streak_direction"] == direction:
                        state["streak_count"] += 1
                    else:
                        state["streak_direction"] = direction
                        state["streak_count"]     = 1
                    state["last_processed_ticker"] = ticker
                    logger.info(
                        f"{tag}: {ticker} → {direction} | "
                        f"Streak: {state['streak_count']} consecutive {direction}"
                    )
                if state["streak_count"] >= STREAK_REQUIRED:
                    logger.info(
                        f"{tag}: 🎯 streak of {state['streak_count']} {state['streak_direction']}! "
                        f"Switching to betting mode."
                    )
                    state["phase"]              = "betting"
                    state["bet_index"]          = 0
                    state["consecutive_losses"] = 0
                save_market_state(state_key, state)
            # ── 5. Place bet if in betting mode with no active bet ─────────────
            if state["phase"] == "betting" and not state.get("active_bet_ticker"):
                open_market = get_open_market(series_ticker)
                if not open_market:
                    logger.info(f"{tag}: no open market available yet.")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                ticker   = open_market.get("ticker", "")
                bet_side = "no" if state["streak_direction"] == "UP" else "yes"
                yes_ask_d = float(open_market.get("yes_ask_dollars") or 0)
                last_d    = float(open_market.get("last_price_dollars") or 0)
                yes_bid_d = float(open_market.get("yes_bid_dollars") or 0)
                mid_d     = yes_ask_d or last_d or yes_bid_d or 0.50
                if bet_side == "yes":
                    price_cents = max(1, min(99, round(mid_d * 100)))
                else:
                    price_cents = max(1, min(99, round((1.0 - mid_d) * 100)))
                bet_dollars = BET_SIZES[state["bet_index"]]
                contracts   = max(1, int(bet_dollars / (price_cents / 100)))
                logger.info(
                    f"{tag}: placing reversal bet — {ticker} | {bet_side.upper()} | "
                    f"{price_cents}¢ | {contracts} contracts | ${bet_dollars:.2f}"
                )
                result  = place_kalshi_order(ticker, bet_side, price_cents, contracts)
                chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
                if result["success"]:
                    state["active_bet_ticker"] = ticker
                    state["active_bet_side"]   = bet_side
                    state["active_bet_price"]  = price_cents  # Save price for P/L calc later
                    save_market_state(state_key, state)
                    # ── LOG TRADE TO DATABASE ──
                    log_trade_to_db(
                        ticker=ticker,
                        coin=coin,
                        side=bet_side,
                        price_cents=price_cents,
                        contracts=contracts,
                        bet_size=bet_dollars,
                        bet_index=state["bet_index"],
                        streak_direction=state["streak_direction"],
                        streak_count=state["streak_count"],
                    )
                    if chat_id:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"🎯 {coin} 15m Reversal Bet!\n\n"
                                f"Streak: {state['streak_count']} consecutive {state['streak_direction']}\n"
                                f"Betting: {bet_side.upper()} on {ticker}\n"
                                f"Price: {price_cents}¢ | Contracts: {contracts} | ~${bet_dollars:.2f}\n"
                                f"Bet #{state['bet_index']+1} of 3"
                            )
                        )
                else:
                    logger.error(f"{tag}: order failed — {result['error']}")
                    if chat_id:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=f"⚠️ {coin} 15m: Order failed — {result['error'][:200]}"
                        )
        except Exception as e:
            logger.error(f"{tag} strategy error: {e}")
        await asyncio.sleep(CHECK_INTERVAL)
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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        os.environ["TELEGRAM_CHAT_ID"] = str(update.effective_chat.id)
    except:
        pass
    await update.message.reply_text(
        "Hey Jay! I'm Pecci, your personal assistant. I can track your mechanic jobs, "
        "search the web, remember everything we talk about, monitor Kalshi markets, and help you trade. "
        "What do you need?"
    )
# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running...")
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        if KALSHI_API_KEY:
            for series_ticker, state_key, coin in CRYPTO_MARKETS:
                asyncio.create_task(crypto15m_strategy(app, series_ticker, state_key, coin))
            logger.info(f"Started 15m strategies for: {[c for _,_,c in CRYPTO_MARKETS]}")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()
if __name__ == "__main__":
    asyncio.run(main())
