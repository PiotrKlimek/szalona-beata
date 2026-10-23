# szalona-beata

Daily email summaries of selected WhatsApp groups (e.g. school class groups), running on your own server.
Your WhatsApp account is linked only to your own machine. No third-party service ever gets access to it.

```
                     your server (Docker Compose)
 ┌────────────────────────────────────────────────────────────────────┐
 │  bridge (Go, whatsmeow)  ──►  SQLite  ◄──  summarizer (Python)     │
 │  linked device, like          (volume)      │                      │
 │  WhatsApp Web                               ├─ dummy: statistics   │
 │                                             ├─ ollama: local model │
 │                                             └─ openai: Groq API ───┼──► LLM provider (optional)
 └─────────────────────────────────────────────┬──────────────────────┘
                                               └── SMTP ──► Resend ──► your inbox
```

The **bridge** links to WhatsApp as a companion device, the same mechanism as WhatsApp Web. It stores messages
only from the groups you list. The **summarizer** turns the last 24 h of each group into topic threads and
sends them as an HTML email. The email shows the total number of messages at the top and the count for each
group next to its name.
Before each digest the summarizer checks the bridge. If WhatsApp has logged the device out or the bridge is
down, the email starts with a red warning instead of quietly reporting "no messages".

| Part | Where data goes |
|---|---|
| WhatsApp session keys, messages | only your server (Docker volume); messages deleted after `RETENTION_DAYS` |
| Summaries with `--model dummy` / `ollama` | only your server |
| Summaries with `--model openai` | the chat text of the selected groups is sent to the LLM provider (Groq by default) |
| LLM call log (prompts, transcripts, responses) | only your server (`llm_log.db` next to the messages database); entries deleted after `RETENTION_DAYS` |
| The email | the email provider (Resend) and your mailbox |

## Before you start: read this

- **WhatsApp's Terms of Service do not allow unofficial clients.** Reading is practically undetectable.
  Sending messages (`--send-to`) can get the account banned. This project emails summaries and never posts to
  WhatsApp unless you explicitly use `--send-to`. If you can, link a secondary number that is a member of the groups.
- **Other people's messages.** Group chats contain messages from other people, for example other parents.
  Summaries are for your own use. Don't forward them outside the group, and think twice before sending real
  chats to an external LLM API (see [Groq](#5-optional-better-summaries-with-groq)).
- **Your phone must come online at least every ~14 days**, or WhatsApp logs out linked devices.
- **The bridge never sends read receipts or "online" presence.** Your chats stay unread on the phone and nobody
  sees you online. The only visible trace is a device called *szalona-beata* under *Linked devices*, and only you see it.

## Requirements

- A Linux machine that is always on (a small VPS is fine). The bridge uses ~10 MB RAM. The summarizer runs for
  seconds and exits. A local LLM needs much more, see [Local models](#local-models-ollama).
- Docker with Compose v2.20+ (`docker compose version`).
- About 15 minutes, a phone with WhatsApp, and (optional) accounts at Resend and Groq.

Everything runs in containers. You don't need Go or Python on the host.

---

## 1. Get the code and configure

```sh
git clone https://github.com/PiotrKlimek/szalona-beata.git
cd szalona-beata
cp .env.example .env
chmod 600 .env        # .env will hold API keys; .gitignore keeps it out of git
```

Leave `WATCH_GROUPS` **empty** for now (`WATCH_GROUPS=`). You will find the group IDs in the next step.

## 2. Find the IDs of your groups (discovery link)

The WhatsApp app doesn't show group IDs (JIDs, like `120363012345678901@g.us`). The easiest way to get them is
to link the bridge once with an empty `WATCH_GROUPS`. In that mode it stores **no messages** at all, only the
group list.

```sh
# Link with a pairing code: easier than scanning a QR code from server logs.
# Pass the phone number (country code, no +) on the command line only, so it isn't stored in .env.
PAIR_PHONE=48500100200 docker compose up -d bridge
docker compose logs -f bridge
#   [Bridge INFO] Pairing code: ABCD-1234 (WhatsApp > Linked devices > Link with phone number)
```

On the phone: **WhatsApp → Settings → Linked devices → Link a device → "Link with phone number instead"**, then
type the code. It expires after a few minutes. You should then see `Device linked` and `Synced N groups` in the logs.

> Prefer a QR code? Run `docker compose up -d bridge` without `PAIR_PHONE` and scan the code printed by
> `docker compose logs -f bridge`. It refreshes every ~20 s.

List your groups:

```sh
docker compose run --rm summarizer --list-groups
#   120363012345678901@g.us  Class 3A parents  (0 messages)
#   48500100200-1602752543@g.us  Old group  (0 messages)       ← pre-2020 group, works the same
```

Archived or muted groups work normally.

## 3. Link for real, with your groups

WhatsApp sends message **history only once, at the moment a device is linked**. So now set the groups and link
again, which makes the history of the chosen groups arrive.

```sh
# 1. Remove the discovery session (it contains only session keys + group names)
docker compose rm -sf bridge
docker volume rm szalona-beata_wa-data     # name = <project dir>_wa-data, check with: docker volume ls

# 2. Put the chosen JIDs (or exact group names) into .env
#    WATCH_GROUPS=120363012345678901@g.us, 120363098765432109@g.us

# 3. Link again (new pairing code)
PAIR_PHONE=48500100200 docker compose up -d bridge
docker compose logs -f bridge
#   History sync (RECENT): saved 434 group messages

# 4. Recreate the container without PAIR_PHONE, so the number doesn't stay in its config
docker compose up -d bridge
```

On the phone, delete the old *szalona-beata* entry from the discovery link under **Linked devices**.

JIDs are better than names because they survive renames. Messages from other groups and from private chats are
dropped in memory and never written to disk. WhatsApp delivers everything to a linked device, so the filter runs
in the bridge, not on WhatsApp's side.

Check that it works:

```sh
docker compose run --rm -e EMAIL_TO= summarizer --hours 168 --model dummy
#   *Class 3A parents*
#   60 messages from 10 people between 16.10 15:27 and 22.10 14:46. Most active: ...
```

## 4. Email delivery with Resend

[Resend](https://resend.com) sends email through SMTP. The free plan has 3,000 emails/month, 100/day and
1 domain. That was true at the time of writing (October 2026); check their pricing page.

### 4.1 Create an account and an API key

1. Sign up at **https://resend.com/signup** (email, Google or GitHub) and confirm your email address.
   Remember this address. Until you add your own domain, Resend delivers **only to the account's own address**.
2. In the dashboard open **API Keys → Create API Key**:
   - Name: `szalona-beata`
   - Permission: **Sending access** (full access is not needed)
   - Domain: **All domains**
3. Click **Add** and copy the key (`re_...`). **It is shown only once.**

### 4.2 Store the key without it appearing on screen or in shell history

```sh
printf 'Resend API key: '; read -rs KEY; echo
printf 'SMTP_PASSWORD=%s\n' "$KEY" >> .env; unset KEY
```

Then set in `.env`:

```sh
EMAIL_TO=the-address-you-signed-up-with@example.com
EMAIL_FROM=Szalona Beata <onboarding@resend.dev>     # Resend's test sender, works without a domain
```

The SMTP host, port and user default to Resend (`smtp.resend.com:465`, user `resend`, password = API key).
Any other SMTP server works through `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER` and `SMTP_SECURITY` (`ssl|starttls|none`).

### 4.3 Send a test email

```sh
docker compose run --rm summarizer --hours 24
#   Emailed to the-address-you-signed-up-with@example.com.
```

Emails from `resend.dev` often land in **Spam** or **Promotions**, so check there. The Resend dashboard
(**Emails**) shows the delivery status.

> **Every manual run emails** while `EMAIL_TO` is set in `.env`. To only print, use `-e EMAIL_TO=`
> (`docker compose run --rm -e EMAIL_TO= summarizer ...`). To preview the HTML without sending:
> `docker compose run --rm -e EMAIL_TO= -u "$(id -u):$(id -g)" -v "$PWD":/out summarizer --html-out /out/preview.html`
> (the file contains real chat content: don't commit it).

### 4.4 Optional: your own domain, to email other people

1. Resend → **Domains → Add Domain**. Use a subdomain (e.g. `digest.example.com`) so your main domain's
   mail setup is left alone.
2. Add the DNS records Resend shows at your DNS provider: a DKIM `TXT` record, plus `MX` and SPF `TXT` records
   for the `send` subdomain (DMARC is optional).
3. Click **Verify DNS Records**. It takes minutes to hours. Check with `dig TXT resend._domainkey.digest.example.com`.
4. Change `.env`: `EMAIL_FROM=Szalona Beata <digest@digest.example.com>` and list recipients:
   `EMAIL_TO=me@example.com, partner@example.com`.

Emails from a verified domain also land in spam less often.

### 4.5 Customize the email

```sh
EMAIL_SUBJECT=Class groups summary, last {hours}h {date}     # placeholders: {date} {hours} {groups}
EMAIL_TITLE=Class groups summary                              # heading at the top of the email
```

## 5. Optional: better summaries with Groq

`--model dummy` only counts messages. For real summaries ("trip on Thursday 8:00, 65 PLN by Monday; open
question: ...") you need a language model. Tiny local models (0.5B) make things up and 3B ones are only acceptable, see [Local models](#local-models-ollama).
[Groq](https://groq.com) has a free API with large open models that handle Polish well.

### 5.1 Create an account and an API key

1. Sign up at **https://console.groq.com** (email, Google or GitHub). No credit card is needed for the free plan.
2. Open **API Keys → Create API Key**, name it `szalona-beata`, and copy the key (`gsk_...`). **It is shown only once.**
3. Store it the same way as the Resend key:

```sh
printf 'Groq API key: '; read -rs KEY; echo
printf 'LLM_API_KEY=%s\n' "$KEY" >> .env; unset KEY
```

The defaults are Groq's OpenAI-compatible endpoint and `openai/gpt-oss-120b`. Any OpenAI-compatible API
works (Mistral, OpenRouter, Ollama's `/v1`) through `LLM_BASE_URL` and `LLM_MODEL`.

### 5.2 Try it on made-up data first

`--model openai` **refuses to send chats from the real database** unless you pass `--allow-real-data`.
Start with a synthetic database of invented groups and people:

```sh
docker compose run --rm --entrypoint python summarizer sample_data.py /samples/sample.db   # once
docker compose run --rm -e EMAIL_TO= summarizer --db /samples/sample.db \
  --group "Klasa 2c" --group "Rodzice 4a" --model openai
```

```
*Klasa 2c (test)*
▸ Wycieczka do Centrum Nauki
- Wyjazd: czwartek 6.11, zbiórka 8:00 pod szkołą, powrót ok. 14:30
- Koszt 65 zł, wpłata do poniedziałku
▸ Zgubiona niebieska bluza
- Znaleziono, problem rozwiązany
Tokens: 1223 in + 1777 out = 3000 total
```

Message times are relative to when the sample was created. To refresh them, delete the database first
(`docker compose run --rm --entrypoint rm summarizer /samples/sample.db`), because the generator never
overwrites an existing file.

Interleaved topics come out as separate threads (shown as separate boxes in the email). Unanswered questions
stay questions ("Pytanie: ...") instead of turning into decisions.

### 5.3 Use it on your groups

Decide whether you're comfortable sending your groups' messages to the provider. Free plans may use data to
improve their services, so check the provider's terms.

```sh
docker compose run --rm summarizer --hours 24 --model openai --allow-real-data
```

### 5.4 Limits and token usage

Measured on a free Groq account in October 2026 (`openai/gpt-oss-120b`, from the API's `x-ratelimit-*` headers):

| Limit | Value | One digest of 3 groups |
|---|---|---|
| Requests per day | 1,000 | 3 (one per group) |
| Tokens per minute | 8,000 | ~1.6k–3.4k |

A daily digest fits easily. On a very busy day a burst over 8k tokens/minute gets HTTP 429. The summarizer
waits for `Retry-After` and retries (3×, at most 120 s each). If the model still fails, that group falls back
to statistics, so the email always goes out. Your current limits are at `console.groq.com/settings/limits`.

`gpt-oss` is a reasoning model, so hidden reasoning counts as output tokens. `LLM_REASONING_EFFORT` trades
quality for tokens:

| `LLM_REASONING_EFFORT` | Output tokens (same day, 3 groups) | Style |
|---|---|---|
| `low` (default) | ~540 | more "who said what" |
| `medium` | ~1,200 | synthesized decisions and open questions (recommended) |
| `off` | n/a | omit the parameter, for models without reasoning |

### 5.5 Call log for prompt tuning

Every `--model openai` call is stored in SQLite: the system prompt, the transcript sent, the raw response,
whether it parsed as threads JSON, tokens, duration and errors. The log is `llm_log.db` in the same directory
as `--db`, so real runs log to `/data/llm_log.db` and sample runs to `/samples/llm_log.db` (rows also carry a
`synthetic` flag). It contains chat content, so it stays on your server like the messages do.
Set `LLM_LOG_DB=off` to disable it, or a path to move it. `--model ollama` is not logged.

```sh
docker compose run --rm --entrypoint python summarizer -c "
import sqlite3
db = sqlite3.connect('/data/llm_log.db')
for row in db.execute('SELECT datetime(ts, \'unixepoch\'), group_name, model, reasoning_effort, parsed, total_tokens, error FROM llm_calls ORDER BY id DESC LIMIT 10'):
    print(row)"
```

To compare prompt versions, take stored transcripts (`SELECT transcript FROM llm_calls WHERE id = ?`), run
them with the changed prompt, and compare the outputs with the stored `response`.

## 6. Daily schedule

The `digest` service keeps running and sends one digest every day at `DAILY_AT` (default `20:00`, in `TZ`,
DST-safe). It summarizes `DIGEST_GROUPS` (default: `WATCH_GROUPS`) over the last `DIGEST_HOURS`.

```sh
# .env: SUMMARY_MODEL=dummy | ollama | openai, DAILY_AT=20:00
docker compose --profile digest up -d digest      # restarts automatically after reboots
docker compose logs -f digest        # "Scheduled daily at 20:00 (Europe/Warsaw)", later "Emailed to ..."
docker compose --profile digest rm -sf digest     # stop the schedule
```

With `SUMMARY_MODEL=openai` the scheduled run needs the same explicit consent as a manual one. Add it in a
`docker-compose.override.yml`, which Compose loads automatically (it's in `.gitignore`, the consent stays local):

```yaml
services:
  digest:
    command: ["--daily-at", "${DAILY_AT:-20:00}", "--hours", "${DIGEST_HOURS:-24}", "--allow-real-data"]
```

The same override can give the schedule **different recipients** than manual runs, e.g. to send the daily
digest to the whole family while your tests go only to you (`EMAIL_TO` in `.env`):

```yaml
services:
  digest:
    environment:
      EMAIL_TO: me@example.com, partner@example.com
```

## Local models (Ollama)

`--model ollama` keeps everything on your server. Ollama starts only on demand (profile `llm`) and unloads the
model right after each request.

```sh
docker compose --profile llm run --rm summarizer --model ollama --ollama-model qwen2.5:7b
docker compose --profile llm stop ollama
```

The default model is `qwen2.5:3b` (it used to be `qwen2.5:0.5b`). The first run downloads it (~1.9 GB); to keep
the old tiny model on a very small machine, set `OLLAMA_MODEL=qwen2.5:0.5b`. You can pre-pull a model with
`docker compose --profile llm up -d ollama && docker compose exec ollama ollama pull qwen2.5:3b`.

Rough RAM needs at Q4 quantization with an ~8k-token context:

| Model | RAM for the model | Machine | Polish summaries |
|---|---|---|---|
| `qwen2.5:0.5b` | ~0.6 GB | any | unusable, makes things up; only proves the pipeline |
| 3–4B (`qwen2.5:3b` = default, Gemma 3, Llama 3.2) | 2.5–4.5 GB | 4 GB + swap / 8 GB | acceptable |
| 7–8B (Qwen 2.5/3, Llama 3.1) | 5.5–6.5 GB | 8 GB | good |
| Bielik 11B (Polish), Gemma 3 12B | 8–10 GB | 12–16 GB | very good |

On 2 vCPUs a 7–8B model needs a few minutes per group. That's fine once a day. **An Apple Silicon Mac (M1+)** is
a good model host. Run Ollama natively (not in Docker, which has no GPU access on macOS), connect it over
[Tailscale](https://tailscale.com) (never expose Ollama to the internet, it has no authentication), and point the
summarizer at it with `--ollama-url http://<mac>:11434`. The Mac must be awake at digest time.

## Changing the groups later

Edit `WATCH_GROUPS` in `.env`, then run `docker compose up -d bridge`. On start the bridge **deletes stored
messages of groups no longer listed**. Newly added groups are collected from that moment on, with no history.
To get history for a new group, unlink and link again (step 3).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Sender shown as a long number | The group hides phone numbers (LID) and the name wasn't known yet. The bridge refreshes names a few seconds after each history batch, reconnect, address-book change or WhatsApp name change. |
| Which name is shown for a sender | The name from your address book if you have one, otherwise the name they set in WhatsApp (the newest one). Stored messages are renamed when the name changes, so one person appears under one name, also in older messages. |
| Red "bridge is logged out" warning in the email, `Logged out` in bridge logs | Device removed on the phone or phone offline for ~14 days: remove the `wa-data` volume and link again (step 3). |
| Red "bridge is not responding" warning | The bridge container is down: `docker compose up -d bridge`, then check `docker compose logs bridge`. |
| No history after linking | `WATCH_GROUPS` was empty or wrong at link time; history comes only once. Unlink and relink (step 3). |
| Groq `403 Forbidden`, `error code: 1010` | Cloudflare blocks the default Python User-Agent. Fixed in the code; if you fork the HTTP client, keep a custom `User-Agent`. |
| "Long chat: … left out of the summary" under a group | That day's chat exceeded `MAX_TRANSCRIPT_CHARS`, so the oldest messages weren't sent to the model. Raise the limit if your model's context and rate limits allow it. |
| Groq `429` | Tokens-per-minute limit; retried automatically, see [Limits](#54-limits-and-token-usage). |
| `refusing to send real chats to an external API` | Intended. Use `--allow-real-data` (or the override for the schedule). |
| Email not arriving | Check Spam/Promotions and Resend → Emails. Without a domain, `EMAIL_TO` must be the Resend account address. |

## Security notes

- `.env` holds the Resend and Groq keys: keep it `chmod 600`. `.gitignore` excludes it, along with `*.db`.
- The `wa-data` volume holds the **WhatsApp session keys**. Anyone with that file can read your WhatsApp as a
  linked device. Anyone with root or `docker` group access on the host can reach it. Revoke access anytime by
  deleting *szalona-beata* under **Linked devices** on the phone.
- The bridge's HTTP API (`/send`, `/health`) has no published ports and is reachable only inside the Compose network.
- **Retention:** with `RETENTION_DAYS` set, the bridge deletes older messages at start and then daily, and doesn't
  store expired messages that arrive again (e.g. history after a re-link). The summarizer deletes older LLM call log
  entries at start, before each new entry, and before each scheduled run in `dummy`/`ollama` mode (if a log
  exists). Group names are kept. A failing purge or log write (e.g. the log is locked) only prints a warning:
  the digest still goes out.

Unlink completely:

```sh
docker compose --profile digest --profile llm down
docker volume rm szalona-beata_wa-data
# and delete the device on the phone
```

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `WATCH_GROUPS` | (empty: store nothing) | Groups whose messages the bridge stores (JIDs or exact names, comma-separated) |
| `PAIR_PHONE` | | Link with a pairing code instead of QR (pass once on the command line) |
| `RETENTION_DAYS` | `0` (keep forever) | Delete messages and LLM call log entries older than N days, max 36500 (`.env.example`: 720) |
| `SUMMARY_MODEL` | `dummy` | `dummy`, `ollama` or `openai` |
| `DIGEST_GROUPS` | `WATCH_GROUPS` | Groups in the digest |
| `DAILY_AT`, `DIGEST_HOURS` | `20:00`, `24` | Schedule of the `digest` service |
| `EMAIL_TO`, `EMAIL_FROM` | | Recipients (comma-separated) and sender |
| `EMAIL_SUBJECT`, `EMAIL_TITLE` | `WhatsApp digest {date}: {groups}`, `WhatsApp digest` | Subject template and email heading |
| `SMTP_PASSWORD` | | Resend API key (or your SMTP password) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_SECURITY` | `smtp.resend.com`, `465`, `resend`, `ssl` | SMTP server |
| `LLM_API_KEY` | | Groq (or other OpenAI-compatible) API key |
| `LLM_BASE_URL`, `LLM_MODEL` | Groq, `openai/gpt-oss-120b` | OpenAI-compatible endpoint and model |
| `LLM_REASONING_EFFORT` | `low` | `low`, `medium`, `high`, or `off` |
| `MAX_TRANSCRIPT_CHARS` | `12000` | Max chat length sent to the model per group; on busier days the oldest messages are left out and the email says so under the group |
| `LLM_LOG_DB` | `llm_log.db` next to `--db` | Log of LLM prompts and responses; `off` disables it |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Local model for `--model ollama` (needs ~2.5 GB free RAM) |
| `SUMMARY_LANGUAGE` | `Polish` | Language of the summaries and of the email labels (`Polish` or `English`; other languages get English labels) |
| `TZ` | `Europe/Warsaw` | Time zone for times, dates and the schedule |

Most summarizer settings can also be overridden per run with flags: `docker compose run --rm summarizer --help`.

## Status and roadmap

Working end to end: linking, group allowlist, history sync, sender names (incl. LID), dummy / Ollama / Groq
summaries with topic threads, HTML email via Resend, daily schedule, retries and fallback. Tested on real
school class groups since October 2026.

Next steps:

- [ ] **Prompt quality.** The model occasionally adds details that aren't in the chat (e.g. a year nobody
      wrote) or labels a plan as a "decision". Tighten the prompt and compare on the same transcripts
      using the [call log](#55-call-log-for-prompt-tuning). A `--replay` command for this would help.
- [ ] **License.** Not chosen yet: without one, the code is visible but all rights are reserved.

## Development

```
bridge/        Go: whatsmeow wiring (main.go) + tested logic (text, convert, filter, names, store, http)
summarizer/    Python stdlib only: summarize.py, sample_data.py, test_summarize.py
test.sh        runs both test suites in Docker (no local toolchains needed)
```

```sh
./test.sh
# after changing code, rebuild ALL images: one-off runs (summarizer) and the schedule (digest) use separate images
docker compose --profile tools --profile digest build
docker compose --profile digest up -d --force-recreate bridge digest
```

The code was written test-first, and image builds run the tests too. `bridge/main.go` is integration glue,
verified by linking a real account. Everything else has unit tests that run against real SQLite, local HTTP and
SMTP test servers.

**When you change code, update this README (and `.env.example`) in the same commit.**
