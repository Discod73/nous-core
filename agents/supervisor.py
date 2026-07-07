#!/usr/bin/env python3
"""
NOUS Supervisor Agent — router der bestemmer hvilken agent der svarer.
Bruger qwen2.5:7b (hurtig model) — routing skal være under 2 sekunder.
"""
import httpx

from agent_base import NousAgent, OLLAMA_URL, LLM_7B, AGENT_TIMEOUT_FAST, load_role_params

LLM_SMALL = LLM_7B

_ROUTING_PROMPT = """\
Du er NOUS supervisor. Din eneste opgave er at beslutte hvilken agent der skal håndtere en forespørgsel.

Svar KUN med ét ord:
- "household" — hvis forespørgslen handler om rutiner, madplan, kalender, hjem, familie, hverdagsopgaver
- "legal" — hvis forespørgslen handler om jura, love, regler, dokumenter, sager, rettigheder
- "legacy" — hvis forespørgslen handler om hvad far tænkte, sagde eller ville, erindringer om far, fars stemme
- "supervisor" — hvis du selv kan svare kort uden at delegere (f.eks. "hvad kan du?")

Forespørgsel: {query}"""

_SUPERVISOR_SYSTEM = (
    "Du er NOUS, en dansk personlig AI-assistent for Dan. "
    "Svar kort og præcist på dansk."
)


class SupervisorAgent(NousAgent):
    name = "supervisor"
    allowed_wings: list[str] = []
    scope = "PRIVATE"
    model = LLM_SMALL
    timeout = AGENT_TIMEOUT_FAST

    def route(self, query: str) -> str:
        """Bestemmer routing. Returnerer 'household', 'legal' eller 'supervisor'."""
        prompt = _ROUTING_PROMPT.format(query=query)
        try:
            r = httpx.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model":   LLM_SMALL,
                    "stream":  False,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.0},
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            raw = r.json()["message"]["content"].strip().lower()
            # Tag første ord og fjern eventuelle tegn
            word = raw.split()[0].strip(".,!?\"'") if raw.split() else ""
            if word in ("household", "legal", "legacy", "supervisor"):
                return word
        except Exception:
            pass
        return "supervisor"  # fallback ved fejl

    def answer(self, query: str) -> str:
        """Supervisor svarer selv på enkle forespørgsler."""
        p = load_role_params("day")
        try:
            r = httpx.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model":   LLM_SMALL,
                    "stream":  False,
                    "messages": [
                        {"role": "system", "content": _SUPERVISOR_SYSTEM},
                        {"role": "user",   "content": query},
                    ],
                    "options": {
                        "temperature": p["temperature"],
                        "num_ctx":     p["num_ctx"],
                        "num_gpu":     p["num_gpu"],
                    },
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()["message"]["content"]
        except Exception as e:
            return f"Beklager, jeg kunne ikke svare: {e}"
