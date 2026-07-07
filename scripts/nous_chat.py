#!/usr/bin/env python3
"""
NOUS Chat med tool-calling
Inspireret af Unsloth's tool-calling-guide (MIT) - se /srv/nous/docs/CREDITS.md
"""
import json
import sys
import httpx

import os as _os
LLM_URL = _os.environ.get("NOUS_NX_LLAMA_URL", "http://localhost:8081/v1/chat/completions")
PROXY_URL = "http://localhost:8090"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Returnerer aktuel dato og tid i Danmark",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Henter aktuelt vejr for en dansk by",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Bynavn, f.eks. 'Aarhus'"}
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Søger på internettet via SearXNG",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Søgeord"}
                },
                "required": ["query"],
            },
        },
    },
]

SYSTEM = """Du er NOUS, en dansk personlig AI-assistent.

VIGTIGT:
- Svar ALTID på dansk, aldrig svensk eller norsk
- Hvis du har brug for fakta du ikke kender, brug et tool\n- For NYHEDER eller AKTUELLE BEGIVENHEDER brug ALTID search_web tool\n- For VEJR brug get_weather\n- For TID/DATO brug get_time
- Hvis et tool returnerer data, formuler svaret kort og naturligt
- Find aldrig på fakta du ikke har bevis for"""


def call_tool(name: str, args: dict) -> str:
    """Eksekver tool og returner resultat som tekst."""
    try:
        if name == "get_time":
            r = httpx.get(f"{PROXY_URL}/time", timeout=5.0)
            return json.dumps(r.json(), ensure_ascii=False)
        elif name == "get_weather":
            r = httpx.get(f"{PROXY_URL}/weather", params={"location": args["location"]}, timeout=10.0)
            return json.dumps(r.json(), ensure_ascii=False)
        elif name == "search_web":
            r = httpx.get(f"{PROXY_URL}/search", params={"q": args["query"], "n": 3}, timeout=15.0)
            return json.dumps(r.json(), ensure_ascii=False)
        else:
            return json.dumps({"error": f"unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def chat(user_message: str, max_iterations: int = 5) -> str:
    """Multi-turn chat med tool-calling."""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_message},
    ]

    for iteration in range(max_iterations):
        response = httpx.post(
            LLM_URL,
            json={
                "model": "qwen3",
                "messages": messages,
                "tools": TOOLS,
                "temperature": 0.6,
            },
            timeout=60.0,
        )
        data = response.json()
        msg = data["choices"][0]["message"]

        # Check for tool calls
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            messages.append(msg)
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"]["arguments"] or "{}")
                print(f"  🔧 Kalder {fn_name}({fn_args})", flush=True)
                result = call_tool(fn_name, fn_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
            continue

        return msg.get("content", "(intet svar)")

    return "(for mange tool-iterationer)"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: nous_chat.py 'dit spørgsmål'")
        sys.exit(1)
    user_q = " ".join(sys.argv[1:])
    print(f"👤 {user_q}")
    answer = chat(user_q)
    print(f"🤖 {answer}")
