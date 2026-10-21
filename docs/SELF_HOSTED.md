# Easypanel deployment from this fork

Use the Git source `https://github.com/lemilleur/marketplaces-mcp-ru`, the selected
branch and root Dockerfile. It installs the local sources, not the PyPI release.
Telegram delivery is registered by the combined server and survives redeployment.

Set the following runtime environment variables in Easypanel:

```dotenv
MCP_TRANSPORT=http
MCP_HTTP_HOST=0.0.0.0
MCP_HTTP_PORT=8000
MCP_HTTP_ALLOWED_HOSTS=your-mcp-domain.example
MARKETPLACE_MCP_HOME=/data
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHATS=
TELEGRAM_PROXY_URL=
```

Keep marketplace credentials in Easypanel too; see `.env.example`. Do not commit
real credentials. Telegram recipients use aliases, e.g. `me:123,team:-100456`.
The user must start the bot in a private chat, or add it to the destination group
with permission to send messages. `telegram_send` sends plain text, not files.
The `confirm_send` argument is an accidental-send gate, not role-based access.

Optional Telegram-only proxy: `http://username:password@host:port` (HTTP CONNECT)
or `https://host:port`. Percent-encode special characters in username/password.
SOCKS and Telegram MTProto proxies are not supported. With an empty setting,
Telegram connects directly. Global HTTP_PROXY/HTTPS_PROXY variables are ignored
by the Telegram client. Marketplace clients are unaffected. Proxy failures never
fall back to a direct Telegram connection.

Attach HTTPS and BasicAuth middleware to every public domain. Do not publish
container port 8000 directly. Forward requests internally to port 8000.

The Dockerfile runs as UID 1000 and stores cabinets in `/data`. Mount a persistent
volume there with matching write permissions. An existing deployment storing
cabinets under `/root/.marketplace-mcp` needs a deliberate backup/migration before
switching; do not silently discard or share its cabinet volume.

After deployment verify unauthorized requests return 401 and an authenticated MCP
initialize request succeeds. Recheck Docker log rotation after redeployment.
