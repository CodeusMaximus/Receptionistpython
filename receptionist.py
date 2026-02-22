import asyncio
import json
import os
import re
import requests
import websockets
from dotenv import load_dotenv
from datetime import datetime, timedelta
import dateparser

load_dotenv(override=True)

# ============================================================
# ENV
# ============================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NEXTJS_BASE_URL = os.getenv("NEXTJS_BASE_URL")
NEXTJS_LOOKUP_URL = os.getenv("NEXTJS_LOOKUP_URL")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")
NEXTJS_BOOK_URL = os.getenv("NEXTJS_BOOK_URL")

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not NEXTJS_LOOKUP_URL:
    raise RuntimeError("Missing NEXTJS_LOOKUP_URL")

CONFIG_URL = f"{NEXTJS_BASE_URL}/api/receptionist/config"
BOOKING_URL = f"{NEXTJS_BOOK_URL}/api/calendar/book"

print(f"🔗 BOOKING: {BOOKING_URL}\n")

OPENAI_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
OPENAI_VOICE = "verse"

# ============================================================
# PARSING
# ============================================================
NUMBER_WORDS = {
    "zero": "0", "oh": "0",
    "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9",
}

def parse_phone(text: str) -> tuple[str | None, str | None]:
    t = text.lower()
    for w, d in NUMBER_WORDS.items():
        t = re.sub(rf"\b{w}\b", d, t)
    digits = re.sub(r"\D", "", t)
    if len(digits) == 10:
        return f"+1{digits}", digits
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}", digits[1:]
    return None, None

def parse_time_from_text(text: str) -> str | None:
    try:
        text_lower = text.lower()
        time_indicators = ['am', 'a.m.', 'pm', 'p.m.', 'oclock', 'o\'clock', 'noon', 'midnight']
        has_time = any(ind in text_lower for ind in time_indicators) or re.search(r'\d+:\d+', text_lower)
        if not has_time:
            return None
        if "noon" in text_lower:
            return "12:00"
        if "midnight" in text_lower:
            return "00:00"
        for word, digit in NUMBER_WORDS.items():
            text_lower = re.sub(rf"\b{word}\b", digit, text_lower)
        parsed = dateparser.parse(text_lower, settings={'TIMEZONE': 'America/New_York'})
        if parsed:
            result = parsed.strftime('%H:%M')
            print(f"  ✅ Time: '{text}' → {result}")
            return result
        return None
    except:
        return None

def parse_date_from_text(text: str, existing_date: str | None = None) -> str | None:
    try:
        text_lower = text.lower()
        if any(x in text_lower for x in ['am', 'pm', ':']):
            return None
        day_match = re.search(r'\b(\d{1,2})(st|nd|rd|th)?\b', text_lower)
        if day_match and existing_date:
            base = datetime.strptime(existing_date, "%Y-%m-%d")
            new_date = base.replace(day=int(day_match.group(1)))
            result = new_date.strftime("%Y-%m-%d")
            print(f"  ✅ Date (merged): '{text}' → {result}")
            return result
        parsed = dateparser.parse(
            text,
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": datetime.now(),
                "TIMEZONE": "America/New_York",
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )
        if parsed:
            now = datetime.now()
            if parsed.year > now.year + 1:
                parsed = parsed.replace(year=now.year)
            result = parsed.strftime("%Y-%m-%d")
            print(f"  ✅ Date: '{text}' → {result}")
            return result
        return None
    except:
        return None

def extract_service(text: str, services: list) -> str | None:
    text_lower = text.lower()
    for service in services:
        if service.lower() in text_lower:
            print(f"  ✅ Service: '{service}'")
            return service
    return None

# ============================================================
# HELPERS
# ============================================================
async def http_post(url, payload, timeout=10):
    headers = {"Content-Type": "application/json"}
    if INTERNAL_API_KEY:
        headers["x-api-key"] = INTERNAL_API_KEY
    print(f"\n📤 POST {url}")
    response = await asyncio.to_thread(
        requests.post, url, json=payload, headers=headers, timeout=timeout
    )
    print(f"✅ {response.status_code}")
    return response

async def ai_say(ws, text):
    await ws.send(json.dumps({
        "type": "response.create",
        "response": {"modalities": ["audio"], "instructions": f"Say: {text}"}
    }))

async def book_appointment(business_id, business_name, customer_info, details):
    start = datetime.fromisoformat(f"{details['date']}T{details['time']}:00")
    end = start + timedelta(hours=1)
    payload = {
        "businessId": business_id,
        "businessName": business_name,
        "customerId": customer_info.get("_id", ""),
        "customerName": customer_info["name"],
        "phone": customer_info["phone"],
        "attendeeEmail": customer_info["email"],
        "service": details["service"],
        "startISO": start.isoformat(),
        "endISO": end.isoformat(),
        "meetingType": "appointment",
    }
    response = await http_post(BOOKING_URL, payload)
    return response.json()

async def update_ai_with_customer(ws, customer, business_name, services_text):
    first = customer['name'].split()[0]
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "instructions": f"You're a receptionist for {business_name}. Customer: {first}. Services: {services_text}. Answer questions naturally."
        }
    }))

# ============================================================
# MAIN LOOP
# ============================================================
async def openai_loop(openai_ws, twilio_ws, stream_sid, business_id, business_name, services, services_text):
    state = {
        "phone_norm": None,
        "customer_info": None,
        "ready": False,
        "booking": False,
        "service": None,
        "date": None,
        "time": None,
    }

    async for msg in openai_ws:
        evt = json.loads(msg)
        etype = evt.get("type")

        if etype == "response.audio.delta":
            await twilio_ws.send(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": evt["delta"]}
            }))

        elif etype == "conversation.item.input_audio_transcription.completed":
            text = evt.get("transcript", "").strip()
            if not text:
                continue

            print(f"\n👤 {text}")
            print(f"📊 service={state['service']}, date={state['date']}, time={state['time']}")

            # 🔧 SLOT MERGE (DO NOT REMOVE)
            if state["booking"]:
                s = extract_service(text, services)
                d = parse_date_from_text(text, state["date"])
                t = parse_time_from_text(text)
                if s:
                    state["service"] = s
                if d:
                    state["date"] = d
                if t:
                    state["time"] = t
            # 🔧 END PATCH

            text_lower = text.lower()

            if not state["phone_norm"]:
                phone, _ = parse_phone(text)
                if phone:
                    state["phone_norm"] = phone
                    lookup = (await http_post(NEXTJS_LOOKUP_URL, {"businessId": business_id, "phone": phone})).json()
                    if lookup.get("found"):
                        state["customer_info"] = lookup["customer"]
                        state["ready"] = True
                        await update_ai_with_customer(openai_ws, state["customer_info"], business_name, services_text)
                        await ai_say(openai_ws, f"Hi {state['customer_info']['name'].split()[0]}! How can I help?")
                    continue

            if state["ready"] and not state["booking"] and any(x in text_lower for x in ["book", "schedule", "appointment"]):
                print("🎯 Booking mode")
                state["booking"] = True

            if state["booking"]:
                if not state["service"]:
                    await ai_say(openai_ws, "What service?")
                elif not state["date"]:
                    await ai_say(openai_ws, "What date?")
                elif not state["time"]:
                    await ai_say(openai_ws, "What time?")
                else:
                    result = await book_appointment(
                        business_id,
                        business_name,
                        state["customer_info"],
                        state
                    )
                    if result.get("success"):
                        await ai_say(openai_ws, "You're all set! A confirmation email has been sent.")
                        state.update({"booking": False, "service": None, "date": None, "time": None})
                    else:
                        await ai_say(openai_ws, "That time is unavailable. Another time?")
                        state["time"] = None

async def handle_twilio_stream(ws):
    async for msg in ws:
        data = json.loads(msg)
        if data.get("event") == "start":
            business_id = data["start"]["customParameters"].get("businessId")
            cfg = (await http_post(CONFIG_URL, {"businessId": business_id})).json()
            business_name = cfg.get("businessName", "our business")
            services = [s['name'] for s in cfg.get("services", [])]
            services_text = ", ".join(services)
            openai_ws = await websockets.connect(
                OPENAI_URL,
                additional_headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Beta": "realtime=v1",
                },
            )
            await openai_ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "voice": OPENAI_VOICE,
                    "instructions": f"You're a receptionist for {business_name}. Ask if they've been here before. If yes, get phone number.",
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "input_audio_transcription": {"model": "whisper-1"},
                },
            }))
            asyncio.create_task(
                openai_loop(openai_ws, ws, data["start"]["streamSid"], business_id, business_name, services, services_text)
            )
            await ai_say(openai_ws, f"Thank you for calling {business_name}. Have you been here before?")
        elif data.get("event") == "media":
            await openai_ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": data["media"]["payload"],
            }))
        elif data.get("event") == "stop":
            print("\n📞 Call ended\n")
            break

async def main():
    print("🚀 AI RECEPTIONIST\n")
    async with websockets.serve(handle_twilio_stream, "0.0.0.0", 5001):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
