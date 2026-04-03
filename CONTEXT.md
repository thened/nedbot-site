# nedbot project — context for AI sessions

## Who I am
I'm a Kick streamer running a virtual horse racing betting stream (NAR / Banei tracks, Japanese racing).
I have a chat bot that lets viewers place virtual bets using a `!bet` command in Kick chat.

---

## The stack

### nedbot.site
- Public website at **https://nedbot.site**
- Hosted on a DigitalOcean droplet at `165.22.185.74`
- nginx + Let's Encrypt SSL (auto-renewing)
- DNS via Cloudflare (proxied, Full SSL mode)
- GitHub repo: **thened/nedbot-site** (branch: master)
- Local copy: `C:\OBS things\nedbot-site\`
- SSH key: `C:\OBS things\nedbot-site\digitalocean` (ed25519)
- Deploy: push to GitHub → SSH into droplet → run `deploy-nedbot`

### Bet Ticket Builder — https://bet.nedbot.site
- Single-page HTML tool that helps viewers build `!bet` chat commands
- Supports URL params `?t=<track>&r=<race>` — the bot posts a pre-filled link in chat
  e.g. `https://bet.nedbot.site/?t=mizusawa&r=3`
- Source: `C:\OBS things\bet ticket builder\index.html` (local dev)
- Deployed file: `C:\OBS things\nedbot-site\bet\index.html`
- Tracks: monbetsu, morioka, mizusawa, urawa, funabashi, oi, kawasaki,
  kanazawa, kasamatsu, nagoya, sonoda, himeji, kochi, saga, obihiro
- Bet types: win, place, quinella, qp (quinella place), exacta, wide, trio, trifecta
- Max bet: ¥1000

### Gateway — https://gateway.nedbot.site
- A local HTTP server running on **port 8888** on my Windows machine
- Exposed publicly via a **Cloudflare Tunnel** (tunnel name: `nedbot-gateway`)
- Cloudflare tunnel ID: `0fcdcbcd-f898-4c1e-aa2c-c350c332e7a5`
- Config: `C:\Users\thene\.cloudflared\config.yml`
- Started by double-clicking: `C:\OBS things\nedbot-site\gateway.bat`
  (runs `gateway.ps1` which starts both the server and the tunnel)
- API:
  - `GET  /health` → `{"status":"ok"}`
  - `POST /command` → `{"command":"<name>", ...params}` → JSON response
- Commands are defined in `gateway.ps1` in the `$Commands` block
- CORS is locked to `https://bet.nedbot.site`

---

## Credentials (local only — never commit)
Stored in `C:\OBS things\nedbot-site\.env` (gitignored):
- `CF_API_TOKEN` — Cloudflare API token (DNS edit scope for nedbot.site)
- `CF_ZONE_ID`   — Cloudflare zone ID for nedbot.site
- `DROPLET_IP`   — 165.22.185.74
- `SSH_KEY`      — ./digitalocean

---

## Future plans
- NedBotsSister — a second chatbot project (in progress)
- More subdomains under nedbot.site (stats, leaderboard, etc.)
- Gateway will grow to support more commands as the stream tools expand
