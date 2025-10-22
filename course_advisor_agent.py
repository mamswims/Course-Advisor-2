from __future__ import annotations
import asyncio, json, os, re, sys, time
from contextlib import AsyncExitStack
from datetime import datetime, UTC
from typing import Any, Dict, Optional, Union, List

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

LOG_PATH = os.environ.get("CONVO_LOG", "conversations.jsonl")
SERVER_SCRIPT = os.environ.get("MCP_SERVER", "mcp_service.py")

SYSTEM_PROMPT = (
    "You are Course Advisor Bot 2.0 — a friendly, pragmatic advisor. Maintain context, "
    "ask concise clarifying questions when constraints are ambiguous, and call tools to fetch "
    "section-level results. Prefer a small, relevant set with clear next steps."
)

# Map user-friendly shorthands/variants -> YOUR dataset's Subject values
SUBJECT_ALIASES = {
    # cores
    "cs": "COMPUTER SCIENCE", "csci": "COMPUTER SCIENCE", "comp sci": "COMPUTER SCIENCE",
    "computer science": "COMPUTER SCIENCE",
    "engl": "ENGLISH", "english": "ENGLISH",
    "math": "MATHEMATICS", "mathematics": "MATHEMATICS",
    "phys": "PHYSICS", "physics": "PHYSICS",
    "bio": "BIOLOGY", "biology": "BIOLOGY",
    "chem": "CHEMISTRY", "chemistry": "CHEMISTRY",
    "phil": "PHILOSOPHY", "philosophy": "PHILOSOPHY",
    "econ": "ECONOMICS", "economics": "ECONOMICS",
    "soci": "SOCIOLOGY", "sociology": "SOCIOLOGY",
    "psych": "PSYCHOLOGY", "psychology": "PSYCHOLOGY",
    "hist": "HISTORY", "history": "HISTORY",
    # programs in your list
    "data": "DATA SCIENCE", "data science": "DATA SCIENCE",
    "is": "INFORMATION SYSTEMS (INFO)", "info": "INFORMATION SYSTEMS (INFO)", "information systems": "INFORMATION SYSTEMS (INFO)",
    "spaud": "SPEECH PATHOLOGY & AUDIOLOGY",
    "nursing": "NURSING",
    "comm": "COMMUNICATION", "communication": "COMMUNICATION",
    "pe": "PHYSICAL EDUCATION & RECREATION", "kines": "KINESIOLOGY", "kinesiology": "KINESIOLOGY",
    "bus": "BUSINESS", "business": "BUSINESS",
    "acct": "ACCOUNTING", "accounting": "ACCOUNTING",
    "stats": "STATISTICS", "statistics": "STATISTICS",
    "religion": "RELIGION",
    "music": "MUSIC",
    "studio art": "STUDIO ART", "art": "ART",
    "art history": "ART HISTORY",
    "theatre": "THEATRE", "theater": "THEATRE",
    "spanish": "SPANISH", "german": "GERMAN", "french": "FRENCH", "dutch": "DUTCH", "chinese": "CHINESE", "korean": "KOREAN",
    "geology": "GEOLOGY & GEOGRAPHY", "geography": "GEOLOGY & GEOGRAPHY",
    "env studies": "ENVIRONMENTAL STUDIES", "environmental studies": "ENVIRONMENTAL STUDIES",
    "public health": "PUBLIC HEALTH",
    "politics": "POLITICS",
    "finance": "FINANCE", "marketing": "MARKETING", "management": "MANAGEMENT",
    "supply chain": "SUPPLY CHAIN MANAGEMENT",
    "ministry leadership": "MINISTRY LEADERSHIP",
    "biochem": "BIOCHEMISTRY", "biochemistry": "BIOCHEMISTRY",
    "astronomy": "ASTRONOMY",
    "calvin core": "CALVIN CORE",
    # feel free to add more from your list if you use them
}

def _normalize_department(text: str) -> Optional[str]:
    """Return dataset Subject string if user text mentions a known dept name/alias."""
    ul = text.lower()
    # try longest multi-word matches first
    for key in sorted(SUBJECT_ALIASES.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", ul):
            return SUBJECT_ALIASES[key]
    return None

def detect_intent(utterance: str) -> Dict[str, Any]:
    u = utterance.lower()
    intent: Dict[str, Any] = {"tool": None, "args": {}}

    # Department detection via aliases or exact subject phrase
    dep = _normalize_department(utterance)

    # Also allow exact SUBJECT names as typed (e.g., “COMPUTER SCIENCE”)
    if not dep:
        m = re.search(r"\b([A-Z][A-Za-z&\s]+)\b", utterance)
        if m:
            candidate = m.group(1).strip().upper()
            # If user typed a real subject like "COMPUTER SCIENCE", accept it
            if candidate in SUBJECT_ALIASES.values():
                dep = candidate

    # Level detection
    lvl = None
    m = re.search(r"\b(1|2|3|4)00\b", u) or re.search(r"\b([1-4])\s*xx\b", u)
    if m:
        lvl = m.group(1) + "00" if len(m.groups()) == 1 and m.group(1) in "1234" else m.group(0)

    # Time-of-day detection
    tod = next((x for x in ("morning", "afternoon", "evening") if x in u), None)

    # Details — allow IDs like 'ENGL 300-A' or 'CSCI 262-01' or plain 5+ digits
    if "details" in u or "tell me more" in u or "id " in u:
        sid = (re.search(r"\b([A-Z]{2,10}\s*\d{2,3}-[A-Z0-9]{1,3})\b", utterance)
               or re.search(r"\b(\d{5,})\b", u))
        if sid:
            return {"tool": "get_section_details", "args": {"section_id": sid.group(1).strip()}}

    # Combined filter if present
    if dep or lvl or tod:
        return {"tool": "find_sections_filtered", "args": {"department": dep, "level": lvl, "time_of_day": tod, "limit": 20}}

    # Keyword search over titles/descriptions
    if re.search(r"\b(ai|data|ethics|security|writing|theatre|music|history|global|justice|psychology|network|biology|chemistry)\b", u):
        return {"tool": "find_courses", "args": {"query": utterance}}

    return intent

def log_event(role: str, text: str, meta: Optional[Dict[str, Any]] = None) -> None:
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(UTC).isoformat(),
                            "role": role, "text": text, "meta": meta or {}}) + "\n")

def _decode_tool_payload(content_items: List[Any]) -> Union[List[Dict[str, Any]], Dict[str, Any], str, None]:
    if not content_items: return None
    for item in content_items:
        data = getattr(item, "data", None)
        if data is not None: return data
    for item in content_items:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try: return json.loads(text)
            except Exception: return text
    return None

async def main() -> None:
    print("Course Advisor Bot 2.0 — type 'exit' to quit.\n")
    log_event("system", SYSTEM_PROMPT)

    params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT], env=None)
    async with AsyncExitStack() as stack:
        stdio = await stack.enter_async_context(stdio_client(params))
        read, write = stdio
        session: ClientSession = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_resp = await session.list_tools()
        tool_names = [t.name for t in tools_resp.tools]
        print(f"Loaded tools: {', '.join(tool_names)}\n")

        has_combined = "find_sections_filtered" in tool_names

        while True:
            user = input("You: ").strip()
            if user.lower() in {"exit", "quit"}:
                print("Bye!"); break
            if not user: continue

            log_event("user", user)
            intent = detect_intent(user)

            if intent["tool"] == "find_sections_filtered" and not has_combined:
                q   = intent["args"].get("query")
                dep = intent["args"].get("department")
                lvl = intent["args"].get("level")
                tod = intent["args"].get("time_of_day")
                if q:     intent = {"tool": "find_courses", "args": {"query": q}}
                elif dep: intent = {"tool": "find_sections_by_department", "args": {"department": dep}}
                elif lvl: intent = {"tool": "find_sections_by_level", "args": {"level": lvl}}
                elif tod: intent = {"tool": "find_sections_by_time", "args": {"time_of_day": tod}}
                else:     intent = {"tool": None, "args": {}}

            if not intent["tool"]:
                msg = ("Do you want to search by *interest* (e.g., 'AI', 'ethics'), "
                       "*department* (e.g., 'computer science', 'english'), or *constraints* (e.g., '300-level morning')?")
                print(f"Bot: {msg}\n"); log_event("assistant", msg, {"intent": intent}); continue

            t0 = time.time()
            result = await session.call_tool(intent["tool"], intent["args"])
            dt = time.time() - t0
            payload = _decode_tool_payload(result.content)

            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                head = payload[:5]
                lines = [
                    f"Found {len(payload)} match(es) in {dt:.2f}s. Here are a few:",
                    *[f"- {r.get('section_name','?')} — {r.get('course_title','?')} [{r.get('time','?')}] (id={r.get('section_id','?')})" for r in head],
                    "Ask for details with: details <section_id> or refine: 'morning 300-level computer science'",
                ]
                msg = "\n".join(lines)

            elif isinstance(payload, dict):
                if payload.get("error"):
                    msg = "Hmm, I couldn't find that section id. Double-check and try again."
                else:
                    name = payload.get("Section_Name") or payload.get("section_name") or "(unknown)"
                    title = payload.get("Section_Title") or payload.get("course_title") or "(untitled)"
                    when = payload.get("MtgPattern") or payload.get("time") or "(time n/a)"
                    desc = payload.get("Desc") or payload.get("description") or ""
                    msg = f"{name} — {title}\nTime: {when}\n\n{desc[:600]}"

            elif isinstance(payload, str):
                msg = payload if payload.strip() else "No results matched."
            else:
                msg = "No results matched. Try adding a keyword, department, or level."

            print(f"Bot: {msg}\n")
            log_event("assistant", msg, {"tool": intent["tool"], "args": intent["args"], "latency_s": round(dt, 3)})

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
