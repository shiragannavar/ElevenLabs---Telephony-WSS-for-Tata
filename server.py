from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Optional

import httpx
import websockets
import websockets.exceptions
from dotenv import load_dotenv
from pydantic import BaseModel
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from audio_converter import AudioOutputBuffer

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("bridge")

ELEVENLABS_AGENT_ID: str = os.getenv("ELEVENLABS_AGENT_ID", "")
ELEVENLABS_AGENT_ID_INBOUND: str = os.getenv("ELEVENLABS_AGENT_ID_INBOUND", "")
ELEVENLABS_AGENT_ID_OUTBOUND: str = os.getenv("ELEVENLABS_AGENT_ID_OUTBOUND", "")
ELEVENLABS_WS_URL: str = os.getenv(
    "ELEVENLABS_WS_URL",
    "wss://api.in.residency.elevenlabs.io/v1/convai/conversation",
)
TATA_CTC_API_KEY: str = os.getenv("TATA_CTC_API_KEY", "")
TATA_CALLER_ID: str = os.getenv("TATA_CALLER_ID", "918069651024")
TATA_CTC_URL: str = os.getenv(
    "TATA_CTC_URL",
    "https://api-smartflo.tatateleservices.com/v1/click_to_call_support",
)
WSS_PUBLIC_HOST: str = os.getenv("WSS_PUBLIC_HOST", "3.109.216.10.sslip.io")
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

app = FastAPI(title="Tata–ElevenLabs WebSocket Bridge", version="1.0.0")


class ClickToCallBody(BaseModel):
    customer_number: str
    custom_params: Optional[dict] = None


class BridgeSession:
    def __init__(
        self,
        tata_ws: WebSocket,
        agent_id: str,
        query_vars: Optional[dict] = None,
        agent_id_from_url: bool = False,
    ) -> None:
        self.tata_ws = tata_ws
        self.agent_id = agent_id
        self.agent_id_from_url = agent_id_from_url
        self.query_vars: dict = query_vars or {}
        self.el_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._el_listener_task: Optional[asyncio.Task] = None
        self.stream_sid: Optional[str] = None
        self.audio_buf = AudioOutputBuffer(min_chunk=160)
        self._media_chunk: int = 0
        self._mark_counter: int = 0
        self.active: bool = True

    async def connect_elevenlabs(self, call_metadata: dict) -> None:
        url = f"{ELEVENLABS_WS_URL}?agent_id={self.agent_id}"
        logger.info("Connecting to ElevenLabs: %s", url)
        self.el_ws = await websockets.connect(url)
        await self._send_el({
            "type": "conversation_initiation_client_data",
            "dynamic_variables": call_metadata,
        })
        self._el_listener_task = asyncio.create_task(self._el_listen())
        logger.info("ElevenLabs connected for stream %s", self.stream_sid)

    async def handle_tata(self, msg: dict) -> None:
        event = msg.get("event")

        if event == "media":
            media = msg.get("media", {})
            logger.debug(
                "[Tata→] media  chunk=%s  timestamp=%s  payload_len=%d  stream=%s",
                media.get("chunk", "?"),
                media.get("timestamp", "?"),
                len(media.get("payload", "")),
                msg.get("streamSid", "?"),
            )
        else:
            logger.debug("[Tata→] raw: %s", json.dumps(msg))

        if event == "connected":
            logger.info("[Tata→] connected (handshake)")
        elif event == "start":
            await self._on_start(msg)
        elif event == "media":
            await self._on_media(msg)
        elif event == "stop":
            logger.info("[Tata→] stop  stream=%s  reason=%s",
                        msg.get("streamSid"), msg.get("stop", {}).get("reason", ""))
            await self.close()
        elif event == "dtmf":
            await self._on_dtmf(msg)
        elif event == "mark":
            name = msg.get("mark", {}).get("name", "")
            logger.info("[Tata→] mark ACK  name=%s  stream=%s",
                        name, msg.get("streamSid", "?"))
        else:
            logger.warning("[Tata→] unhandled event type: %s  full=%s", event, json.dumps(msg))

    async def _on_start(self, msg: dict) -> None:
        start = msg.get("start", {})
        self.stream_sid = msg.get("streamSid") or start.get("streamSid", "")
        custom = start.get("customParameters") or {}
        call_metadata: dict = {
            **{str(k): str(v) for k, v in self.query_vars.items()},
            "account_sid": start.get("accountSid", ""),
            "call_sid":    start.get("callSid", ""),
            "stream_sid":  self.stream_sid,
            "from_number": start.get("from", ""),
            "to_number":   start.get("to", ""),
            "direction":   start.get("direction", ""),
            **{str(k): str(v) for k, v in custom.items()},
        }
        logger.info(
            "[Tata→] start  sid=%s  from=%s  to=%s  direction=%s  encoding=%s  sampleRate=%s",
            self.stream_sid,
            call_metadata["from_number"],
            call_metadata["to_number"],
            call_metadata["direction"],
            start.get("mediaFormat", {}).get("encoding", "?"),
            start.get("mediaFormat", {}).get("sampleRate", "?"),
        )

        if not self.agent_id_from_url:
            direction = (call_metadata["direction"] or "").strip().lower()
            if direction == "inbound" and ELEVENLABS_AGENT_ID_INBOUND:
                self.agent_id = ELEVENLABS_AGENT_ID_INBOUND
            elif direction == "outbound" and ELEVENLABS_AGENT_ID_OUTBOUND:
                self.agent_id = ELEVENLABS_AGENT_ID_OUTBOUND

        if not self.agent_id:
            logger.error(
                "No agent_id resolved (direction=%s, url=%s, env defaults missing)",
                call_metadata["direction"], self.agent_id_from_url,
            )
            await self.close()
            return

        logger.info(
            "Resolved agent_id=%s  (direction=%s, from_url=%s)",
            self.agent_id, call_metadata["direction"], self.agent_id_from_url,
        )

        await self.connect_elevenlabs(call_metadata)

    async def _on_media(self, msg: dict) -> None:
        if not (self.el_ws and self.active):
            return
        payload_b64 = msg.get("media", {}).get("payload", "")
        if not payload_b64:
            return
        await self._send_el({"user_audio_chunk": payload_b64})

    async def _on_dtmf(self, msg: dict) -> None:
        digit = msg.get("dtmf", {}).get("digit", "")
        logger.info("DTMF digit: %s", digit)
        if self.el_ws and digit:
            await self._send_el({
                "type": "contextual_update",
                "text": f"The caller pressed DTMF key {digit}.",
            })

    async def _el_listen(self) -> None:
        try:
            async for raw in self.el_ws:
                if not self.active:
                    break
                await self._handle_el(raw)
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("ElevenLabs WS closed: %s %s", exc.code, exc.reason)
        except Exception:
            logger.exception("ElevenLabs listener error")
        finally:
            self.active = False

    async def _handle_el(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[EL→] Non-JSON message received: %r", raw[:200])
            return

        t = msg.get("type", "<no type>")

        if t == "audio":
            audio_b64 = msg.get("audio_event", {}).get("audio_base_64", "")
            logger.debug(
                "[EL→] audio  event_id=%s  payload_len=%d",
                msg.get("audio_event", {}).get("event_id", "?"),
                len(audio_b64),
            )
        else:
            logger.debug("[EL→] raw: %s", json.dumps(msg))

        if t == "conversation_initiation_metadata":
            ev = msg.get("conversation_initiation_metadata_event", {})
            conv_id = ev.get("conversation_id", "")
            agent_out = ev.get("agent_output_audio_format", "?")
            user_in   = ev.get("user_input_audio_format", "?")
            logger.info(
                "[EL→] conversation_initiation_metadata  conv_id=%s  agent_out=%s  user_in=%s",
                conv_id, agent_out, user_in,
            )
        elif t == "audio":
            await self._on_el_audio(msg)
        elif t == "ping":
            event_id = msg.get("ping_event", {}).get("event_id")
            ping_ms  = msg.get("ping_event", {}).get("ping_ms", "?")
            logger.debug("[EL→] ping  event_id=%s  ping_ms=%s → sending pong", event_id, ping_ms)
            if event_id is not None:
                await self._send_el({"type": "pong", "event_id": event_id})
        elif t == "interruption":
            event_id = msg.get("interruption_event", {}).get("event_id", "?")
            self.audio_buf.clear()
            logger.info("[EL→] interruption  event_id=%s → clearing Tata audio buffer", event_id)
            if self.stream_sid:
                await self._send_tata({"event": "clear", "streamSid": self.stream_sid})
        elif t == "agent_response":
            text = msg.get("agent_response_event", {}).get("agent_response", "")
            logger.info("[EL→] agent_response: %s", text)
            await self._flush_audio_buf()
        elif t == "user_transcript":
            text = msg.get("user_transcription_event", {}).get("user_transcript", "")
            logger.info("[EL→] user_transcript: %s", text)
        elif t == "agent_response_correction":
            ev = msg.get("agent_response_correction_event", {})
            logger.info(
                "[EL→] agent_response_correction  original='%s'  corrected='%s'",
                ev.get("original_agent_response", ""),
                ev.get("corrected_agent_response", ""),
            )
        elif t == "client_tool_call":
            logger.info("[EL→] client_tool_call: %s", json.dumps(msg.get("client_tool_call", {})))
            await self._handle_tool_call(msg.get("client_tool_call", {}))
        elif t == "vad_score":
            score = msg.get("vad_score_event", {}).get("vad_score", "?")
            logger.debug("[EL→] vad_score: %s", score)
        elif t == "internal_tentative_agent_response":
            text = msg.get("tentative_agent_response_internal_event", {}).get(
                "tentative_agent_response", ""
            )
            logger.debug("[EL→] tentative_agent_response: %s", text)
        else:
            logger.warning("[EL→] unhandled message type: %s  full=%s", t, json.dumps(msg))

    async def _on_el_audio(self, msg: dict) -> None:
        audio_b64 = msg.get("audio_event", {}).get("audio_base_64", "")
        if not (audio_b64 and self.stream_sid):
            return
        raw_audio = base64.b64decode(audio_b64)
        for chunk in self.audio_buf.push(raw_audio):
            await self._send_audio_chunk(chunk)

    async def _flush_audio_buf(self) -> None:
        tail = self.audio_buf.flush()
        if tail:
            await self._send_audio_chunk(tail)

    async def _send_audio_chunk(self, audio_bytes: bytes) -> None:
        self._media_chunk += 1
        self._mark_counter += 1
        mark_name = f"el_{self._mark_counter}"
        logger.debug(
            "[→Tata] media  chunk=%d  bytes=%d  stream=%s",
            self._media_chunk, len(audio_bytes), self.stream_sid,
        )
        await self._send_tata({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {
                "payload": base64.b64encode(audio_bytes).decode(),
                "chunk": self._media_chunk,
            },
        })
        await self._send_tata({
            "event": "mark",
            "streamSid": self.stream_sid,
            "mark": {"name": mark_name},
        })

    async def _handle_tool_call(self, tool_call: dict) -> None:
        tool_name = tool_call.get("tool_name", "")
        tool_call_id = tool_call.get("tool_call_id", "")
        params = tool_call.get("parameters", {})
        logger.info("Tool call: %s (id=%s)  params=%s", tool_name, tool_call_id, params)
        if self.el_ws:
            await self._send_el({
                "type": "client_tool_result",
                "tool_call_id": tool_call_id,
                "result": f"Tool '{tool_name}' is not implemented on this bridge.",
                "is_error": True,
            })

    async def _send_tata(self, payload: dict) -> None:
        try:
            await self.tata_ws.send_json(payload)
        except Exception as exc:
            logger.warning("Failed to send to Tata: %s", exc)

    async def _send_el(self, payload: dict) -> None:
        try:
            if self.el_ws:
                await self.el_ws.send(json.dumps(payload))
        except Exception as exc:
            logger.warning("Failed to send to ElevenLabs: %s", exc)

    async def close(self) -> None:
        if not self.active:
            return
        self.active = False
        if self._el_listener_task:
            self._el_listener_task.cancel()
            try:
                await self._el_listener_task
            except asyncio.CancelledError:
                pass
        if self.el_ws:
            try:
                await self.el_ws.close()
            except Exception:
                pass
        logger.info("Session closed for stream %s", self.stream_sid)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    qs = dict(websocket.query_params)
    url_agent_id = qs.pop("agent_id", "").strip()
    resolved_agent_id = url_agent_id or ELEVENLABS_AGENT_ID
    has_direction_defaults = bool(
        ELEVENLABS_AGENT_ID_INBOUND or ELEVENLABS_AGENT_ID_OUTBOUND
    )
    if not (resolved_agent_id or has_direction_defaults):
        logger.error(
            "No agent_id provided and no ELEVENLABS_AGENT_ID / "
            "ELEVENLABS_AGENT_ID_INBOUND / ELEVENLABS_AGENT_ID_OUTBOUND set"
        )
        await websocket.close(code=1008, reason="agent_id is required")
        return
    client = getattr(websocket.client, "host", "unknown")
    logger.info(
        "Tata connected from %s (agent=%s, from_url=%s)  query_vars=%s",
        client, resolved_agent_id or "<deferred>", bool(url_agent_id), qs,
    )
    session = BridgeSession(
        websocket,
        resolved_agent_id,
        query_vars=qs,
        agent_id_from_url=bool(url_agent_id),
    )
    try:
        async for text in websocket.iter_text():
            if not session.active:
                break
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON message from Tata: %r", text[:120])
                continue
            await session.handle_tata(data)
    except WebSocketDisconnect:
        logger.info("Tata WebSocket disconnected (stream=%s)", session.stream_sid)
    except Exception:
        logger.exception("Unexpected error in WebSocket handler")
    finally:
        await session.close()


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "service": "tata-elevenlabs-bridge"}


@app.get("/ws")
async def ws_probe() -> dict:
    return {"status": "ok", "info": "Connect via WebSocket (ws:// / wss://)"}


@app.post("/tata")
@app.get("/tata")
async def dynamic_voice_endpoint(request: Request) -> JSONResponse:
    if request.method == "POST":
        try:
            params = await request.json()
        except Exception:
            params = {}
    else:
        params = dict(request.query_params)

    logger.info("[Dynamic] %s /tata  params=%s", request.method, params)

    qs = {k: str(v) for k, v in params.items() if v is not None and str(v).strip()}
    wss_url = f"wss://{WSS_PUBLIC_HOST}/ws?{urlencode(qs)}" if qs else f"wss://{WSS_PUBLIC_HOST}/ws"

    logger.info("[Dynamic] Returning wss_url=%s", wss_url)
    return JSONResponse({"sucess": True, "wss_url": wss_url})


def _build_ctc_payload(num: str, custom_params: Optional[dict] = None) -> dict:
    customer_num = num
    payload: dict = {"async": 1, "api_key": TATA_CTC_API_KEY, "caller_id": TATA_CALLER_ID, "customer_number": customer_num, "customer_ring_timeout": 30, "custom_identifier": str(int(time.time()))}
    if custom_params:
        for k, v in custom_params.items():
            if k and str(k).strip():
                payload[str(k).strip()] = str(v) if v is not None else ""
    return payload



def _render_ui() -> str:
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Cache-Control" content="no-cache">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Voice AI - Click to Call</title>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'DM Sans', -apple-system, sans-serif;
      background: #0a0a0f;
      min-height: 100vh;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      padding: 24px 24px 48px;
      color: #e4e4e7;
      overflow-x: hidden;
    }
    .bg-grid {
      position: fixed;
      inset: 0;
      background-image: linear-gradient(rgba(99,102,241,0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(99,102,241,0.03) 1px, transparent 1px);
      background-size: 48px 48px;
      pointer-events: none;
    }
    .glow {
      position: fixed;
      width: 400px;
      height: 400px;
      background: radial-gradient(circle, rgba(99,102,241,0.15) 0%, transparent 70%);
      top: -100px;
      left: 50%;
      transform: translateX(-50%);
      pointer-events: none;
    }
    .card {
      position: relative;
      max-width: 520px;
      width: 100%;
      overflow-x: hidden;
      background: rgba(24,24,27,0.8);
      backdrop-filter: blur(24px);
      border: 1px solid rgba(63,63,70,0.5);
      border-radius: 24px;
      padding: 48px 40px;
      animation: fadeUp 0.6s ease-out;
    }
    @keyframes fadeUp {
      from { opacity: 0; transform: translateY(24px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .logo {
      text-align: center;
      margin-bottom: 40px;
      animation: fadeUp 0.6s ease-out 0.1s both;
    }
    .logo h1 {
      font-size: 32px;
      font-weight: 700;
      letter-spacing: -1px;
      background: linear-gradient(135deg, #fff 0%, #a1a1aa 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }
    .logo span {
      display: inline-block;
      font-size: 13px;
      color: #71717a;
      margin-top: 8px;
      font-weight: 500;
    }
    .form-wrap {
      animation: fadeUp 0.6s ease-out 0.2s both;
    }
    .params-section {
      margin-bottom: 20px;
    }
    .params-section label {
      display: block;
      font-size: 12px;
      color: #71717a;
      margin-bottom: 12px;
      font-weight: 500;
    }
    .param-row {
      display: flex;
      gap: 10px;
      margin-bottom: 10px;
      align-items: center;
      min-width: 0;
    }
    .param-row input {
      flex: 1;
      min-width: 0;
      padding: 12px 14px;
      font-size: 14px;
      background: rgba(39,39,42,0.8);
      border: 1px solid rgba(63,63,70,0.6);
      border-radius: 10px;
      color: #e4e4e7;
      transition: all 0.2s ease;
    }
    .param-row input::placeholder { color: #71717a; }
    .param-row input:focus {
      outline: none;
      border-color: #6366f1;
    }
    .param-row .btn-add {
      flex-shrink: 0;
      width: 40px;
      height: 40px;
      padding: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(99,102,241,0.2);
      border: 1px solid rgba(99,102,241,0.4);
      border-radius: 10px;
      color: #6366f1;
      font-size: 20px;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    .param-row .btn-add:hover {
      background: rgba(99,102,241,0.3);
      transform: scale(1.05);
    }
    .param-row .btn-remove {
      flex-shrink: 0;
      width: 40px;
      height: 40px;
      padding: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(239,68,68,0.15);
      border: 1px solid rgba(239,68,68,0.3);
      border-radius: 10px;
      color: #f87171;
      font-size: 18px;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    .param-row .btn-remove:hover {
      background: rgba(239,68,68,0.25);
    }
    .param-row .btn-remove:focus,
    .param-row .btn-add:focus {
      outline: none;
    }
    input[type="tel"] {
      width: 100%;
      padding: 18px 20px;
      font-size: 16px;
      background: rgba(39,39,42,0.8);
      border: 1px solid rgba(63,63,70,0.6);
      border-radius: 14px;
      margin-bottom: 16px;
      color: #e4e4e7;
      transition: all 0.3s ease;
    }
    input[type="tel"]::placeholder { color: #71717a; }
    input[type="tel"]:focus {
      outline: none;
      border-color: #6366f1;
      box-shadow: 0 0 0 3px rgba(99,102,241,0.2);
    }
    #submit-btn {
      width: 100%;
      padding: 18px;
      font-size: 16px;
      font-weight: 600;
      background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
      color: #fff;
      border: none;
      border-radius: 14px;
      cursor: pointer;
      transition: all 0.3s ease;
      position: relative;
      overflow: hidden;
    }
    #submit-btn::before {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(135deg, rgba(255,255,255,0.2) 0%, transparent 50%);
      opacity: 0;
      transition: opacity 0.3s;
    }
    #submit-btn:hover:not(:disabled) {
      transform: translateY(-2px);
      box-shadow: 0 12px 40px rgba(99,102,241,0.4);
    }
    #submit-btn:hover:not(:disabled)::before { opacity: 1; }
    button:active { transform: translateY(0); }
    #submit-btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
      box-shadow: none;
    }
    .msg {
      min-height: 24px;
      padding: 14px 18px;
      margin-top: 20px;
      font-size: 14px;
      background: rgba(34,197,94,0.15);
      color: #4ade80;
      border-radius: 12px;
      border: 1px solid rgba(34,197,94,0.3);
      animation: fadeIn 0.4s ease;
    }
    .msg.err {
      background: rgba(239,68,68,0.15);
      color: #f87171;
      border-color: rgba(239,68,68,0.3);
    }
    @keyframes fadeIn {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    .curl-section {
      margin-top: 24px;
      padding-top: 20px;
      border-top: 1px solid rgba(63,63,70,0.5);
      animation: fadeIn 0.4s ease;
    }
    .curl-section label {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: #71717a;
      margin-bottom: 8px;
      font-weight: 500;
    }
    .live-badge {
      display: inline-block;
      font-size: 9px;
      font-weight: 700;
      letter-spacing: 1px;
      padding: 2px 6px;
      border-radius: 4px;
      background: rgba(34,197,94,0.2);
      color: #4ade80;
      border: 1px solid rgba(34,197,94,0.3);
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }
    .curl-output {
      font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
      font-size: 12px;
      line-height: 1.6;
      padding: 16px;
      margin: 0;
      background: rgba(15,15,18,0.9);
      border: 1px solid rgba(63,63,70,0.5);
      border-radius: 12px;
      color: #a1a1aa;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      transition: all 0.2s ease;
    }
    .divider {
      height: 1px;
      background: rgba(63,63,70,0.5);
      margin: 32px 0;
    }
    .endpoint-section {
      animation: fadeUp 0.6s ease-out 0.3s both;
    }
    .endpoint-section h2 {
      font-size: 16px;
      font-weight: 600;
      margin-bottom: 6px;
      color: #e4e4e7;
    }
    .endpoint-section p {
      font-size: 13px;
      color: #71717a;
      margin-bottom: 16px;
      line-height: 1.5;
    }
    .endpoint-section .url-box {
      font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
      font-size: 12px;
      line-height: 1.5;
      padding: 14px 16px;
      background: rgba(15,15,18,0.9);
      border: 1px solid rgba(63,63,70,0.5);
      border-radius: 10px;
      color: #6366f1;
      word-break: break-all;
      margin-bottom: 16px;
    }
    .endpoint-section .info-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 16px;
    }
    .endpoint-section .info-item {
      padding: 10px 14px;
      background: rgba(39,39,42,0.5);
      border: 1px solid rgba(63,63,70,0.4);
      border-radius: 8px;
    }
    .info-item .il { font-size: 10px; color: #71717a; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
    .info-item .iv { font-size: 13px; color: #e4e4e7; font-family: 'SF Mono', monospace; }
    .endpoint-section .note {
      font-size: 12px;
      color: #71717a;
      padding: 12px 14px;
      background: rgba(99,102,241,0.08);
      border: 1px solid rgba(99,102,241,0.2);
      border-radius: 8px;
      line-height: 1.5;
    }
    .endpoint-section .note strong { color: #a5b4fc; }
  </style>
</head>
<body>
  <div class="bg-grid"></div>
  <div class="glow"></div>
  <div class="card">
    <div class="logo">
      <h1>Voice AI</h1>
      <span>AI Voice Assistant</span>
    </div>
    <div class="form-wrap">
      <form id="ctc-form" onsubmit="return false;">
        <input type="tel" id="phone-input" placeholder="Enter customer number" required value="">
        <div class="params-section">
          <label>Custom parameters (optional)</label>
          <div id="param-rows">
            <div class="param-row">
              <input type="text" class="param-key" placeholder="e.g. custom_identifier">
              <input type="text" class="param-value" placeholder="e.g. testing">
              <button type="button" class="btn-add" title="Add parameter">+</button>
              <button type="button" class="btn-remove" title="Remove">×</button>
            </div>
          </div>
        </div>
        <button type="button" id="submit-btn">Initiate Call</button>
      </form>
      <div id="msg" class="msg" style="display:none;"></div>
      <div class="curl-section">
        <label>Request payload <span class="live-badge">LIVE</span></label>
        <pre class="curl-output" id="live-payload">Enter a customer number to preview the payload.</pre>
      </div>
    </div>
    <div class="divider"></div>
    <div class="endpoint-section">
      <h2>Dynamic Voice Endpoint</h2>
      <p>Configure this in Tata SmartFlo Voice Streaming settings as a <strong>Dynamic</strong> endpoint (POST).</p>
      <div class="url-box">https://""" + WSS_PUBLIC_HOST + """/tata</div>
      <div class="info-grid">
        <div class="info-item"><div class="il">Method</div><div class="iv">POST</div></div>
        <div class="info-item"><div class="il">WSS Endpoint</div><div class="iv">wss://""" + WSS_PUBLIC_HOST + """/ws</div></div>
      </div>
      <label style="font-size:12px;color:#71717a;font-weight:500;margin-bottom:10px;display:block;">Predefined variables</label>
      <div class="info-grid">
        <div class="info-item"><div class="il">callId</div><div class="iv">$callId</div></div>
        <div class="info-item"><div class="il">fromNumber</div><div class="iv">$fromNumber</div></div>
        <div class="info-item"><div class="il">toNumber</div><div class="iv">$toNumber</div></div>
        <div class="info-item"><div class="il">status</div><div class="iv">$status</div></div>
      </div>
      <div class="note">
        <strong>How it works:</strong> Tata sends call data to this endpoint. We return a <code>wss://</code> URL with all variables as query params. When the WebSocket connects, these are forwarded to ElevenLabs as <code>dynamic_variables</code> in <code>conversation_initiation_client_data</code>.
      </div>
    </div>
  </div>
  <script>
    (function() {
      var container = document.getElementById('param-rows');
      var phoneInput = document.getElementById('phone-input');
      var livePayload = document.getElementById('live-payload');
      var submitBtn = document.getElementById('submit-btn');
      var msgEl = document.getElementById('msg');
      var CTC_URL = \"""" + TATA_CTC_URL + """\";
      var CALLER_ID = \"""" + TATA_CALLER_ID + """\";
      var rowCount = 1;

      function getCustomParams() {
        var params = {};
        container.querySelectorAll('.param-row').forEach(function(r) {
          var k = (r.querySelector('.param-key').value || '').trim();
          var v = (r.querySelector('.param-value').value || '').trim();
          if (k) params[k] = v;
        });
        return params;
      }

      function buildPayload() {
        var raw = (phoneInput.value || '').replace(/[\s\-\+]/g, '').replace(/\D/g, '');
        var num = raw;
        var payload = {
          "async": 1,
          "api_key": "***REDACTED***",
          "caller_id": CALLER_ID,
          "customer_number": num || "(enter number)",
          "customer_ring_timeout": 30
        };
        var cp = getCustomParams();
        for (var k in cp) payload[k] = cp[k];
        return payload;
      }

      function updatePreview() {
        var p = buildPayload();
        var body = JSON.stringify(p, null, 2);
        var lines = [
          'curl -X POST "' + CTC_URL + '"',
          '  -H "Content-Type: application/json"',
          "  -d '" + body + "'"
        ];
        var sep = ' ' + String.fromCharCode(92, 10);
        livePayload.textContent = lines.join(sep);
      }

      function showMsg(text, isErr) {
        msgEl.textContent = text;
        msgEl.className = isErr ? 'msg err' : 'msg';
        msgEl.style.display = 'block';
        msgEl.style.animation = 'none';
        msgEl.offsetHeight;
        msgEl.style.animation = 'fadeIn 0.4s ease';
      }

      function wireRow(row) {
        row.querySelector('.btn-add').onclick = addRow;
        row.querySelector('.btn-remove').onclick = function() {
          if (container.children.length > 1) {
            row.remove();
            updatePreview();
          }
        };
        row.querySelectorAll('input').forEach(function(i) {
          i.addEventListener('input', updatePreview);
        });
      }

      function addRow() {
        var row = document.createElement('div');
        row.className = 'param-row';
        row.innerHTML = '<input type="text" class="param-key" placeholder="e.g. custom_identifier">' +
          '<input type="text" class="param-value" placeholder="e.g. value">' +
          '<button type="button" class="btn-add" title="Add parameter">+</button>' +
          '<button type="button" class="btn-remove" title="Remove">&times;</button>';
        wireRow(row);
        container.appendChild(row);
        rowCount++;
        updatePreview();
      }

      wireRow(container.querySelector('.param-row'));
      phoneInput.addEventListener('input', updatePreview);

      submitBtn.addEventListener('click', function() {
        var num = (phoneInput.value || '').replace(/[\\s\\-\\+]/g, '').replace(/\\D/g, '');
        if (!num) {
          showMsg('Enter a customer number.', true);
          return;
        }
        var cp = getCustomParams();
        var hasCustom = Object.keys(cp).length > 0;

        submitBtn.disabled = true;
        submitBtn.textContent = 'Calling...';

        fetch('/api/click-to-call', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            customer_number: num,
            custom_params: hasCustom ? cp : null
          })
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          var text = data.message || 'Done.';
          if (data.ok && data.data && data.data.ref_id) {
            text += ' (ref: ' + data.data.ref_id + ')';
          }
          showMsg(text, !data.ok);
        })
        .catch(function(err) {
          showMsg('Request failed: ' + err.message, true);
        })
        .finally(function() {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Initiate Call';
        });
      });

      updatePreview();
    })();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def click_to_call_ui() -> str:
    return _render_ui()



async def _do_click_to_call(num: str, custom_params: Optional[dict] = None) -> dict:
    payload = _build_ctc_payload(num, custom_params)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(TATA_CTC_URL, json=payload, timeout=15.0)
        data = r.json() if r.content else {}
        logger.info("Tata click-to-call response: %s %s", r.status_code, data)
        if r.is_success:
            return {"ok": True, "message": "Call initiated.", "data": data}
        return {"ok": False, "message": data.get("message", data.get("error", str(data))), "data": data}
    except httpx.RequestError as e:
        return {"ok": False, "message": str(e), "data": {}}


@app.post("/api/click-to-call")
async def click_to_call(body: ClickToCallBody) -> dict:
    if not TATA_CTC_API_KEY:
        return {"ok": False, "message": "TATA_CTC_API_KEY not configured"}
    num = body.customer_number.replace("+", "").replace(" ", "").replace("-", "")
    if not num.isdigit():
        return {"ok": False, "message": "Invalid customer number"}
    return await _do_click_to_call(num, body.custom_params)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)

