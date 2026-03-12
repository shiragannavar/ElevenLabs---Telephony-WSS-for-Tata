from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Optional

import websockets
import websockets.exceptions
from dotenv import load_dotenv
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from audio_converter import AudioOutputBuffer

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("bridge")

ELEVENLABS_AGENT_ID: str = os.getenv("ELEVENLABS_AGENT_ID", "")
ELEVENLABS_WS_URL: str = os.getenv(
    "ELEVENLABS_WS_URL",
    "wss://api.elevenlabs.io/v1/convai/conversation",
)
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

app = FastAPI(title="Tata–ElevenLabs WebSocket Bridge", version="1.0.0")


class BridgeSession:
    def __init__(self, tata_ws: WebSocket, agent_id: str) -> None:
        self.tata_ws = tata_ws
        self.agent_id = agent_id
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
async def websocket_endpoint(
    websocket: WebSocket,
    agent_id: str = Query(default="", description="ElevenLabs agent ID (overrides ELEVENLABS_AGENT_ID)"),
) -> None:
    await websocket.accept()
    resolved_agent_id = agent_id.strip() or ELEVENLABS_AGENT_ID
    if not resolved_agent_id:
        logger.error("No agent_id provided and ELEVENLABS_AGENT_ID is not set")
        await websocket.close(code=1008, reason="agent_id is required")
        return
    client = getattr(websocket.client, "host", "unknown")
    logger.info("Tata connected from %s (agent=%s)", client, resolved_agent_id)
    session = BridgeSession(websocket, resolved_agent_id)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=HOST, port=PORT, log_level="info")