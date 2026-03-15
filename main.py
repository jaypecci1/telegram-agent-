import os
import asyncio
import logging
from datetime import datetime

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
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()
SUPABASE_URL      = os.environ["SUPABASE_URL"].strip()
SUPABASE_KEY      = os.environ["SUPABASE_KEY"].strip()
TAVILY_API_KEY    = os.environ["TAVILY_API_KEY"].strip()
# ── Clients ───────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
db     = create_client(SUPABASE_URL, SUPABASE_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Jay's personal AI assistant. Jay is a young adult who works as an automotive and light diesel mechanic doing side jobs, and he is studying Economics at Texas Tech University. His long-term goal is to open his own mechanic shop.

You help Jay with:
- Tracking his mechanic jobs (customers, vehicles, parts, costs, payment status, job status)
- Searching the web for any question he has
- Remembering important information from all past conversations
- Managing his day-to-day life as a young adult

Personality: Be conversational and friendly. Talk to Jay like a smart assistant who actually knows him. Keep responses clear and to the point — he's often busy. When Jay mentions a new job or customer, proactively offer to save it. When he asks about a past job or customer, use the recall tool.

Current date and time: {datetime}"""

# ── Tool definitions ──────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "search_web",
        "description": "Search the internet for any information, news, research topics, part prices, how-to guides, etc.",
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
                "customer_name":   {"type": "string",  "description": "Customer's full name"},
                "customer_phone":  {"type": "string",  "description": "Customer's phone number"},
                "vehicle_year":    {"type": "string",  "description": "Year of the vehicle"},
                "vehicle_make":    {"type": "string",  "description": "Make of the vehicle (e.g. Ford, Chevy, Toyota)"},
                "vehicle_model":   {"type": "string",  "description": "Model of the vehicle"},
                "job_description": {"type": "string",  "description": "What work is being done"},
                "parts_used":      {"type": "string",  "description": "Parts used or needed for the job"},
                "cost":            {"type": "number",  "description": "Total cost charged to the customer"},
                "status":          {"type": "string",  "description": "Job status: pending, in_progress, waiting_parts, or completed"},
            },
            "required": ["customer_name", "job_description"],
        },
    },
    {
        "name": "update_job",
        "description": "Update an existing job — change status, mark as paid, add notes, update cost, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id":          {"type": "integer", "description": "The ID number of the job to update"},
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
                "status": {
                    "type": "string",
                    "description": "Filter by: pending, in_progress, waiting_parts, completed. Leave blank for all jobs.",
                }
            },
        },
    },
    {
        "name": "get_customer_jobs",
        "description": "Look up all jobs ever done for a specific customer",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "The customer's name"}
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "save_memory",
        "description": "Save an important piece of information to long-term memory so it can be recalled later",
        "input_schema": {
            "type": "object",
            "properties": {
                "key":   {"type": "string", "description": "A short label or category for this info"},
                "value": {"type": "string", "description": "The information to remember"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "recall_memory",
        "description": "Search through all saved memories and past conversations to find information",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"}
            },
            "required": ["query"],
        },
    },
]


# ── Tool execution ────────────────────────────────────────────────────────────
def run_tool(name: str, inputs: dict) -> str:

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
            row = {
                "customer_name":   inputs.get("customer_name"),
                "customer_phone":  inputs.get("customer_phone"),
                "vehicle_year":    inputs.get("vehicle_year"),
                "vehicle_make":    inputs.get("vehicle_make"),
                "vehicle_model":   inputs.get("vehicle_model"),
                "job_description": inputs.get("job_description"),
                "parts_used":      inputs.get("parts_used"),
                "cost":            inputs.get("cost"),
                "status":          inputs.get("status", "pending"),
                "paid":            False,
            }
            res = db.table("jobs").insert(row).execute()
            j = res.data[0]
            return (
                f"Job saved! Job ID: {j['id']}\n"
                f"Customer: {j['customer_name']}\n"
                f"Status: {j['status']}"
            )
        except Exception as e:
            return f"Failed to add job: {e}"

    if name == "update_job":
        try:
            job_id = inputs.pop("job_id")
            updates = {k: v for k, v in inputs.items() if v is not None}
            updates["updated_at"] = datetime.now().isoformat()
            res = db.table("jobs").update(updates).eq("id", job_id).execute()
            if res.data:
                return f"Job {job_id} updated!"
            return f"Job {job_id} not found."
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
                lines.append(
                    f"ID {j['id']} | {j['customer_name']} | "
                    f"{j.get('vehicle_year','')} {j.get('vehicle_make','')} {j.get('vehicle_model','')}\n"
                    f"  {j['job_description']}\n"
                    f"  Status: {j['status']} | Paid: {j.get('paid',False)} | Cost: ${j.get('cost','N/A')}"
                )
            return "\n\n".join(lines)
        except Exception as e:
            return f"Failed to list jobs: {e}"

    if name == "get_customer_jobs":
        try:
            res = db.table("jobs").select("*").ilike("customer_name", f"%{inputs['customer_name']}%").execute()
            if not res.data:
                return f"No jobs found for {inputs['customer_name']}."
            lines = []
            for j in res.data:
                lines.append(
                    f"ID {j['id']} | {j.get('vehicle_year','')} {j.get('vehicle_make','')} {j.get('vehicle_model','')}\n"
                    f"  Job: {j['job_description']}\n"
                    f"  Parts: {j.get('parts_used','N/A')}\n"
                    f"  Cost: ${j.get('cost','N/A')} | Paid: {j.get('paid',False)}\n"
                    f"  Status: {j['status']} | Date: {str(j['created_at'])[:10]}"
                )
            return f"Jobs for {inputs['customer_name']}:\n\n" + "\n\n".join(lines)
        except Exception as e:
            return f"Failed to get jobs: {e}"

    if name == "save_memory":
        try:
            db.table("memory").insert({"key": inputs["key"], "value": inputs["value"]}).execute()
            return f"Got it, I'll remember: {inputs['key']} — {inputs['value']}"
        except Exception as e:
            return f"Failed to save: {e}"

    if name == "recall_memory":
        try:
            query = inputs["query"].lower()
            mems   = db.table("memory").select("*").execute()
            convos = (
                db.table("conversations")
                .select("*")
                .ilike("content", f"%{query}%")
                .order("timestamp", desc=True)
                .limit(10)
                .execute()
            )
            results = []
            for m in mems.data:
                if query in m["key"].lower() or query in m["value"].lower():
                    results.append(f"[Memory] {m['key']}: {m['value']}  (saved {str(m['created_at'])[:10]})")
            for c in convos.data:
                results.append(f"[{c['role'].upper()} on {str(c['timestamp'])[:10]}]: {c['content'][:250]}...")
            if not results:
                return f"Nothing found for '{query}'."
            return f"Found {len(results)} result(s):\n\n" + "\n\n".join(results[:15])
        except Exception as e:
            return f"Failed to recall: {e}"

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


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    save_message("user", user_text)

    history = get_history(limit=20)
    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_text:
        history = history[:-1]

    messages = history + [{"role": "user", "content": user_text}]
    system   = SYSTEM_PROMPT.format(datetime=datetime.now().strftime("%A, %B %d, %Y at %I:%M %p"))

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
                    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
                    result = run_tool(block.name, dict(block.input))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            final = "".join(b.text for b in response.content if hasattr(b, "text"))
            save_message("assistant", final)
            for i in range(0, max(len(final), 1), 4096):
                await update.message.reply_text(final[i : i + 4096])
            break


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey Jay! I'm your personal assistant. I can track your mechanic jobs, "
        "search the web, remember everything we talk about, and help you stay on top of things. "
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
        await asyncio.Event().wait()  # Run forever
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
