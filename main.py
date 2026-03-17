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
MAX_PER_TRADE           = 2.50    # Max $ per trade
MAX_TOTAL_EXPOSURE      = 20.00   # Max $ in open positions at once
MIN_EDGE                = 0.08    # Min edge (8%) to consider a trade
CONFIDENCE_THRESHOLD    = 0.70    # Min confidence (70%) to place a trade
SCAN_INTERVAL           = 300     # Scan every 5 minutes
AUTO_TRADE              = False   # False = ask Jay first, True = fully autonomous
TRADING_PAUSED          = False   # Can be toggled via Telegram command

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Pecci, Jay's personal AI assistant. Jay is a young adult who works as an automotive and light diesel mechanic doing side jobs, and he is studying Economics at Texas Tech University. His long-term goal is to open his own mechanic shop. He also trades on Kalshi prediction markets.

You help Jay with:
- Tracking his mechanic jobs (customers, vehicles, parts, costs, payment status, job status)
- Searching the web for any question he has
- Remembering important information from all past conversations
- Managing his day-to-day life as a young adult
- Monitoring and trading on Kalshi prediction markets

Trading personality: You are a sharp, data-driven trader. You use your web search to research markets before recommending trades. You look for mispriced odds, consider Jay's economics background when evaluating macro markets, and always respect the risk limits ($2.50 max per trade, $20 max exposure).

Telegram trading commands Jay can use:
- "pause trading" - stop all new trades
- "resume trading" - resume trading
- "show my positions" - list open Kalshi positions
- "show my balance" - check Kalshi balance
- "show trade history" - view past trades
- "auto trade on" - enable fully autonomous trading
- "auto trade off" - require approval for each trade

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

def get_kalshi_markets(limit: int = 100) -> dict:
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
                return {"success": True, "markets": r.json().get("markets", [])}
            return {"success": False, "error": r.text}
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_kalshi_market(ticker: str) -> dict:
    try:
        path = f"/trade-api/v2/markets/{ticker}"
        with httpx.Client() as client:
            r = client.get(f"{KALSHI_BASE_URL}/markets/{ticker}", headers=sign_kalshi_request("GET", path), timeout=10)
            if r.status_code == 200:
                return {"success": True, "market": r.json().get("market", {})}
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

def calculate_contracts(price_cents: int) -> int:
    """Calculate how many contracts to buy given our max per trade."""
    price_dollars = price_cents / 100
    if price_dollars <= 0:
        return 0
    contracts = int(MAX_PER_TRADE / price_dollars)
    return max(1, contracts)

def get_current_exposure() -> float:
    """Get total $ currently at risk in open positions."""
    try:
        result = db.table("trades").select("estimated_cost").eq("status", "open").execute()
        return sum(t.get("estimated_cost", 0) or 0 for t in result.data)
    except:
        return 0.0

def save_trade(ticker, title, side, price, contracts, cost, order_id, reasoning):
    try:
        db.table("trades").insert({
            "market_ticker": ticker,
            "market_title": title,
            "side": side,
            "price": price,
            "contracts": contracts,
            "estimated_cost": cost,
            "kalshi_order_id": order_id,
            "status": "open",
            "reasoning": reasoning,
        }).execute()
    except Exception as e:
        logger.error(f"Failed to save trade: {e}")

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
]

# ── Tool execution ────────────────────────────────────────────────────────────
def run_tool(name: str, inputs: dict) -> str:
    global TRADING_PAUSED, AUTO_TRADE

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
            exposure = get_current_exposure()
            return f"Kalshi Balance: ${result['balance']:.2f}\nCurrent exposure in open trades: ${exposure:.2f}\nRemaining available: ${max(0, MAX_TOTAL_EXPOSURE - exposure):.2f}"
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

        exposure = get_current_exposure()
        if exposure >= MAX_TOTAL_EXPOSURE:
            return f"Max exposure reached (${exposure:.2f}/${MAX_TOTAL_EXPOSURE}). Cannot place new trades until some close."

        contracts = calculate_contracts(price_cents)
        cost      = round((price_cents / 100) * contracts, 2)

        if cost > MAX_PER_TRADE:
            return f"Trade cost ${cost:.2f} exceeds max per trade ${MAX_PER_TRADE}."

        result = place_kalshi_order(ticker, side, price_cents, contracts)
        if result["success"]:
            order = result["order"]
            save_trade(ticker, title, side, price_cents/100, contracts, cost, order.get("order_id",""), reasoning)
            return f"Trade placed! ✅\nMarket: {title}\nSide: {side.upper()} | Price: {price_cents}¢ | Contracts: {contracts} | Cost: ${cost:.2f}\nReasoning: {reasoning}"
        return f"Trade failed: {result['error']}"

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


# ── Autonomous market scanner ─────────────────────────────────────────────────
async def autonomous_scanner(app):
    global TRADING_PAUSED, AUTO_TRADE
    await asyncio.sleep(30)  # Wait 30s after startup before first scan
    logger.info("Autonomous scanner started.")

    while True:
        try:
            if not TRADING_PAUSED and KALSHI_API_KEY:
                logger.info("Scanning Kalshi markets...")
                markets_result = get_kalshi_markets(limit=50)

                if markets_result["success"]:
                    markets = markets_result["markets"]
                    logger.info(f"Got {len(markets)} markets. Sample tickers: {[m.get('ticker') for m in markets[:5]]}")
                    opportunities = []

                    # Keywords that indicate good tradeable markets (economics, politics, finance)
                    GOOD_KEYWORDS = [
                        "fed", "rate", "inflation", "gdp", "recession", "unemployment",
                        "trump", "election", "president", "congress", "senate", "house",
                        "bitcoin", "crypto", "stock", "market", "dow", "nasdaq", "sp500",
                        "oil", "gold", "dollar", "economy", "job", "cpi", "fomc",
                        "war", "china", "russia", "iran", "tariff", "trade",
                        "supreme", "court", "law", "bill", "policy",
                    ]
                    # Keywords to skip (sports, gaming, entertainment)
                    SKIP_KEYWORDS = [
                        "sports", "game", "nfl", "nba", "mlb", "nhl", "soccer", "football",
                        "basketball", "baseball", "hockey", "tennis", "golf", "racing",
                        "esport", "gaming", "championship", "tournament", "league",
                        "oscar", "grammy", "emmy", "award", "celebrity", "actor",
                        "kxmve", "kxmvsports", "multigame",
                    ]

                    for market in markets:  # Check all 50
                        ticker    = market.get("ticker", "")
                        title     = market.get("title", "")
                        yes_price = market.get("yes_bid", 0) or market.get("yes_ask", 0) or market.get("last_price", 0) or 0

                        if not ticker or not title or yes_price <= 5 or yes_price >= 95:
                            continue

                        title_lower  = title.lower()
                        ticker_lower = ticker.lower()

                        # Skip sports/entertainment markets
                        if any(kw in title_lower or kw in ticker_lower for kw in SKIP_KEYWORDS):
                            continue

                        # Only trade markets we can research well
                        if not any(kw in title_lower for kw in GOOD_KEYWORDS):
                            continue

                        no_price  = 100 - yes_price
                        min_price = min(yes_price, no_price)

                        if min_price < 10:
                            continue

                        opportunities.append({
                            "ticker": ticker,
                            "title": title,
                            "yes_price": yes_price,
                            "no_price": no_price,
                        })

                    logger.info(f"Found {len(opportunities)} tradeable opportunities after filtering.")

                    for opp in opportunities[:5]:  # Deep analyze top 5
                        exposure = get_current_exposure()
                        if exposure >= MAX_TOTAL_EXPOSURE:
                            logger.info("Max exposure reached, skipping scan.")
                            break

                        analysis_text = run_tool("scan_and_analyze_market", {
                            "ticker": opp["ticker"],
                            "title": opp["title"],
                            "current_yes_price": opp["yes_price"],
                        })

                        lines = analysis_text.strip().split("\n")
                        parsed = {}
                        for line in lines:
                            if ":" in line:
                                k, v = line.split(":", 1)
                                parsed[k.strip()] = v.strip()

                        bet        = parsed.get("BET", "SKIP").upper()
                        confidence = float(parsed.get("CONFIDENCE", "0").replace("%",""))
                        reason     = parsed.get("REASON", "")
                        true_prob  = float(parsed.get("TRUE_PROB", "0"))

                        if bet == "SKIP" or confidence < (CONFIDENCE_THRESHOLD * 100):
                            continue

                        side        = "yes" if bet == "YES" else "no"
                        price_cents = opp["yes_price"] if side == "yes" else opp["no_price"]

                        if AUTO_TRADE:
                            result = run_tool("execute_trade", {
                                "ticker": opp["ticker"],
                                "title": opp["title"],
                                "side": side,
                                "price_cents": price_cents,
                                "reasoning": reason,
                            })
                            message = f"🤖 Auto-trade placed!\n\n{opp['title']}\nBet: {side.upper()} at {price_cents}¢\nConfidence: {confidence:.0f}%\nReason: {reason}\n\nResult: {result}"
                        else:
                            message = (
                                f"📊 Trade opportunity found!\n\n"
                                f"Market: {opp['title']}\n"
                                f"Bet: {side.upper()} at {price_cents}¢\n"
                                f"My estimated probability: {true_prob:.0f}%\n"
                                f"Confidence: {confidence:.0f}%\n"
                                f"Reason: {reason}\n\n"
                                f"Reply 'yes place it' to execute, or ignore to skip."
                            )

                        try:
                            chats = db.table("conversations").select("*").order("timestamp", desc=True).limit(1).execute()
                            if chats.data:
                                pass
                        except:
                            pass

                        await app.bot.send_message(chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""), text=message)
                        await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Scanner error: {e}")

        await asyncio.sleep(SCAN_INTERVAL)


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TRADING_PAUSED, AUTO_TRADE
    user_text = update.message.text.lower().strip()

    # Store chat ID for scanner notifications
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
        await update.message.reply_text("Trading resumed! I'm back to scanning for opportunities.")
        return

    if "auto trade on" in user_text:
        AUTO_TRADE = True
        await update.message.reply_text("⚡ Fully autonomous trading ON. I'll place trades without asking you first.")
        return

    if "auto trade off" in user_text:
        AUTO_TRADE = False
        await update.message.reply_text("Manual mode ON. I'll alert you before placing any trades.")
        return

    if "yes place it" in user_text or "yes, place it" in user_text:
        save_message("user", update.message.text)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        final = await run_claude("Place the last trade opportunity you found. Use execute_trade to do it now.")
        save_message("assistant", final)
        await update.message.reply_text(final)
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
            asyncio.create_task(autonomous_scanner(app))
            logger.info("Kalshi autonomous scanner started.")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
