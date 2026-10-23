# szalona-beata (POC)

Reads selected WhatsApp groups and produces summaries — fully local, no third-party service.

```
bridge (Go, whatsmeow)  --writes-->  SQLite (volume wa-data)  <--reads--  summarizer (Python)
   linked device, like                                                       |  dummy: stats only
   WhatsApp Web                                                              |  ollama: local LLM
        ^                                                                    |
        +------------------- POST /send (digest to a group) <---------------+
```

- **bridge** links to WhatsApp as a companion device (QR code or pairing code) and stores messages only from
  groups listed in `WATCH_GROUPS` (private chats and other groups are dropped without being saved; only group
  names are kept so `--list-groups` works). It exposes `/send` + `/health` inside the compose network
  (no published ports). Sending is restricted to groups.
- **summarizer** is a one-shot CLI: `--model dummy` (message/sender statistics, no AI) or `--model ollama`
  (default `qwen2.5:0.5b`, ~600 MB RAM — proves the pipeline, quality is poor; use a 3B+ model for real use).
- `--model openai` uses an external OpenAI-compatible API (default: Groq, `openai/gpt-oss-120b`). It refuses
  to send chats from the real database unless `--allow-real-data` is given — test it with synthetic data:
  ```sh
  docker compose run --rm --entrypoint python summarizer sample_data.py /samples/sample.db
  docker compose run --rm summarizer --db /samples/sample.db --group "Klasa 2c" --model openai
  ```
  Rate limits (HTTP 429) are retried after the server's `Retry-After` delay (up to 3 times, max 120 s);
  if the model stays unavailable, that group falls back to statistics so the digest still goes out.
- **ollama** serves the model; it's pulled automatically on first use. It runs only on demand
  (compose profile `llm`) and unloads the model right after each request (`OLLAMA_KEEP_ALIVE=0`).

> ⚠️ Unofficial clients violate WhatsApp's ToS. Reading is low-risk; sending can get the account banned.
> Prefer a secondary number added to the groups. The phone must come online at least every ~14 days
> or linked devices get logged out.

## Usage

```sh
# 0. Choose the groups to read — BEFORE linking (history is synced only once, at link time)
cp .env.example .env && $EDITOR .env    # WATCH_GROUPS=Rowery Gdynia, Klasa 3B

# 1. Start the bridge and link it (WhatsApp > Settings > Linked devices > Link a device)
docker compose up -d bridge
docker compose logs -f bridge          # scan the QR code shown here; Ctrl+C when "Device linked"
#    or, instead of QR:  PAIR_PHONE=48500100200 docker compose up -d bridge  → pairing code in logs

# 2. Wait for history sync ("History sync ...: saved N group messages"), then list groups
docker compose run --rm summarizer --list-groups

# 3. Summarize (prints only)
docker compose run --rm summarizer --group "Rowery" --group "3B" --hours 24 --model dummy
docker compose --profile llm run --rm summarizer --group "Rowery" --model ollama   # starts ollama
docker compose --profile llm stop ollama                                           # optional, frees ~20 MB

# 4. Send the digest to a group
docker compose --profile llm run --rm summarizer --group "Rowery" --model ollama   # starts ollama
docker compose --profile llm stop ollama                                           # optional, frees ~20 MB --send-to "Podsumowania"
```

### Daily email digest

Uses SMTP; defaults target [Resend](https://resend.com) (free: 3,000 emails/month, 100/day).
Create an API key in Resend, then set `EMAIL_TO`, `EMAIL_FROM` and `SMTP_PASSWORD` in `.env` (see `.env.example`).
Without a verified domain Resend only sends from `onboarding@resend.dev` to your own Resend account address.

```sh
docker compose run --rm summarizer --email-to you@example.com   # one-off test (groups from WATCH_GROUPS)
docker compose --profile digest up -d digest                     # every day at DAILY_AT (default 20:00)
# with SUMMARY_MODEL=ollama:  docker compose --profile digest --profile llm up -d digest
docker compose logs -f digest
```

Note: the digest text passes through the email provider.

Changing `WATCH_GROUPS` later: edit `.env`, then `docker compose up -d bridge`. On start the bridge deletes
stored messages of groups no longer listed; newly added groups are collected from that moment on (no history).
Note that WhatsApp delivers all messages to a linked device — the filter drops unwatched ones in memory.

`--group` in the summarizer is matched by JID, exact name or unique name fragment (case-insensitive).
All settings are listed in `.env.example`.
To use Ollama on another machine: `docker compose run --rm summarizer --ollama-url http://host:11434 ...`.

To unlink: remove the device on the phone, then `docker compose down && docker volume rm szalona-beata_wa-data`.

## Tests

```sh
./test.sh          # Go (bridge) + Python (summarizer) unit tests, run in Docker
```

Image builds also run the tests. `bridge/main.go` (whatsmeow wiring) is integration glue and is verified
by linking a real account; all logic lives in tested files (`text.go`, `convert.go`, `store.go`, `http.go`).
