# Tata Telephony - ElevenLabs WebSocket Bridge

A WebSocket bridge that connects Tata Telephony voice streams to ElevenLabs Conversational AI agents. Tata sends and receives audio as G.711 mulaw at 8000 Hz. ElevenLabs is configured to use the same format, so audio passes through without any conversion.

## How It Works

Tata connects to the bridge over WebSocket and drives the following lifecycle:

1. Sends a `connected` event as a handshake
2. Sends a `start` event with call metadata (caller number, direction, stream ID)
3. Streams `media` events containing base64-encoded mulaw audio every 100ms
4. Sends a `stop` event when the call ends

The bridge opens a WebSocket to ElevenLabs on `start`, relays audio in both directions, and handles ElevenLabs control events (interruptions, pings, transcripts).

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI application, WebSocket endpoint, bridge logic |
| `audio_converter.py` | G.711 mulaw codec and audio output buffer |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for environment variables |

## Requirements

- Python 3.9 or later
- An ElevenLabs Conversational AI agent configured with `ulaw_8000` audio format

## Local Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and set ELEVENLABS_AGENT_ID
```

Start the server:

```bash
python server.py
```

The server runs on `http://0.0.0.0:8000` by default. The WebSocket endpoint is `/ws`.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ELEVENLABS_AGENT_ID` | Yes | Default ElevenLabs agent ID to route calls to |
| `ELEVENLABS_WS_URL` | No | Override the ElevenLabs WebSocket base URL (for EU/US/India residency) |
| `HOST` | No | Bind host, default `0.0.0.0` |
| `PORT` | No | Bind port, default `8000` |

The agent ID can also be passed per-connection as a query parameter:

```
wss://your-server/ws?agent_id=your_agent_id
```

## AWS EC2 Deployment

### Initial Setup

```bash
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip nginx certbot python3-certbot-nginx

mkdir ~/tata-bridge
cd ~/tata-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your ELEVENLABS_AGENT_ID
```

### Running the Server

```bash
cd ~/tata-bridge
source venv/bin/activate
nohup python server.py >> logs.txt 2>&1 &
echo $! > server.pid
```

To stop:

```bash
kill $(cat ~/tata-bridge/server.pid)
```

To restart:

```bash
fuser -k 8000/tcp
cd ~/tata-bridge && source venv/bin/activate
nohup python server.py >> logs.txt 2>&1 &
echo $! > server.pid
```

### SSL with nginx

Install a certificate using a DNS-based hostname (example uses sslip.io with the EC2 public IP):

```bash
sudo certbot --nginx -d <EC2_IP>.sslip.io
```

Sample nginx config at `/etc/nginx/sites-available/tata-bridge`:

```nginx
server {
    listen 443 ssl;
    server_name <EC2_IP>.sslip.io;

    location /ws {
        proxy_pass http://127.0.0.1:8000/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600;
    }

    location /health {
        proxy_pass http://127.0.0.1:8000/health;
    }

    location / {
        return 444;
    }
}

server {
    listen 80;
    server_name <EC2_IP>.sslip.io;
    return 301 https://$host$request_uri;
}
```

### Security Group Rules

Open the following ports in the EC2 security group:

| Port | Protocol | Source | Purpose |
|---|---|---|---|
| 22 | TCP | Your IP only | SSH access |
| 80 | TCP | 0.0.0.0/0 | HTTP (redirects to HTTPS) |
| 443 | TCP | 0.0.0.0/0 | HTTPS and WSS |

Block direct access to port 8000 from outside (nginx proxies internally).

## WebSocket Endpoint

| Path | Protocol | Description |
|---|---|---|
| `/ws` | WebSocket | Main endpoint for Tata Telephony |
| `/health` | HTTP GET | Liveness probe, returns `{"status": "ok"}` |

The bridge accepts HTTP GET requests on `/ws` for health probes from Tata's infrastructure and returns `200 OK`. Actual WebSocket connections upgrade normally.

## Monitoring Logs

```bash
tail -f ~/tata-bridge/logs.txt
```

A successful call produces log lines in this order:

```
Tata connected from <IP> (agent=<agent_id>)
[Tata] connected (handshake)
[Tata] start  sid=...  from=...  to=...  direction=inbound
Connecting to ElevenLabs: wss://...
ElevenLabs connected for stream ...
[EL] conversation_initiation_metadata  agent_out=ulaw_8000  user_in=ulaw_8000
[EL] agent_response: Hello! How can I help you today?
[Tata] mark ACK  name=el_1 ...
[EL] user_transcript: <caller speech>
...
[Tata] stop
Session closed for stream ...
```
