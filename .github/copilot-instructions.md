# Property Manager - Copilot Instructions

## Project Overview
Python application to search and manage Australian property data via the Domain API.

## Stack
- **Language**: Python 3.10+
- **API**: Domain.com.au REST API (OAuth2)
- **Storage**: SQLite (local cache via `data/property_cache.db`)
- **CLI**: `rich` library for pretty terminal output

## Key Files
- `src/auth.py` — OAuth2 token management
- `src/api_client.py` — Domain API wrapper
- `src/database.py` — SQLite cache layer
- `src/cli.py` — CLI interface (entry point)
- `src/models.py` — Data models
- `.env` — API credentials (never commit)

## Coding Guidelines
- Use `httpx` for HTTP requests (async-friendly)
- Always cache API responses to avoid rate-limit exhaustion
- Use `python-dotenv` to load `.env` credentials
- Use `rich` for all terminal output formatting
