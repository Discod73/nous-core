# Stash

**Your AI has amnesia. We fixed it.**

Every LLM starts every conversation from zero. Stash gives your agent persistent memory — it remembers, recalls, consolidates, and learns across sessions. No more explaining yourself from scratch.

Open source. Self-hosted. Works with any MCP-compatible agent.

## Quick Start

```bash
git clone https://github.com/alash3al/stash.git
cd stash
cp .env.example .env   # edit with your API key + model
docker compose up
```

That's it. Postgres + pgvector, migrations, MCP server with background consolidation — all in one command.

## What It Does

Stash is a cognitive layer between your AI agent and the world. Episodes become facts. Facts become relationships. Relationships become patterns. Patterns become wisdom.

An 8-stage consolidation pipeline turns raw observations into structured knowledge — facts, relationships, causal links, goal tracking, failure patterns, hypothesis verification, and confidence decay. Each stage only processes new data since the last run.

Works with Claude Desktop, Cursor, Windsurf, Cline, Continue, OpenAI Agents, Ollama, OpenRouter — anything MCP.

## Learn More

**[alash3al.github.io/stash →](https://alash3al.github.io/stash/)**

## License

Apache 2.0
