import os
import io
import base64
import uuid
from datetime import date
from typing import List
from urllib.parse import urlencode
from pydantic import BaseModel

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.middleware.sessions import SessionMiddleware
import httpx
import assemblyai as aai
import edge_tts
from mutagen.mp3 import MP3

from database import SessionLocal, User

app = FastAPI()

# 1. Reverse Proxy ပေါ်တွင် HTTPS Headers များ မှန်ကန်စေရန်
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# 2. Browser WASM လည်းရ၊ Tailwind CDN လည်း အပိတ်မခံရစေရန် credentialless သုံးခြင်း
@app.middleware("http")
async def add_wasm_security_headers(request: Request, call_next):
    response = await call_next(request)
    # Browser က Local assets များကို မပိတ်ပင်စေရန် သတ်မှတ်ခြင်း
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
    return response

SECRET_KEY = os.getenv("SECRET_KEY", "RECAP_STUDIO_SECRET_KEY_PROD_2026")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

TEMP_DIR = "temp_audios"
os.makedirs(TEMP_DIR, exist_ok=True)

@app.get("/", response_class=HTMLResponse)
async def serve_home():
    return FileResponse(os.path.join("templates", "index.html"))

# --- Google OAuth Login ---
@app.get("/login/google")
async def login_google(request: Request):
    if not GOOGLE_CLIENT_ID:
        return JSONResponse(status_code=500, content={"error": "GOOGLE_CLIENT_ID မရှိသေးပါ။ Environment ကို စစ်ဆေးပါ။"})
    
    # Host header မှ domain ကို တိကျစွာ ရယူခြင်း (Render Proxy အတွက် https ဖြင့် တည်ဆောက်ခြင်း)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    redirect_uri = f"https://{host}/api/auth/google/callback"
    request.session["oauth_redirect_uri"] = redirect_uri

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri,
        "access_type": "offline",
        "prompt": "select_account"
    }
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")

@app.get("/api/auth/google/callback")
async def auth_google_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return RedirectResponse(url="/?error=no_code")

    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    redirect_uri = request.session.get("oauth_redirect_uri") or f"https://{host}/api/auth/google/callback"

    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code"
            }
        )
        if token_res.status_code != 200:
            return RedirectResponse(url="/?error=token_fetch_failed")
        
        tokens = token_res.json()
        access_token = tokens.get("access_token")

        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        if user_res.status_code != 200:
            return RedirectResponse(url="/?error=user_fetch_failed")

        user_info = user_res.json()

    email = user_info["email"]
    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            email=email,
            name=user_info.get("name", "User"),
            avatar=user_info.get("picture", ""),
            daily_credits_left=2,
            package_credits=0,
            last_reset_date=date.today()
        )
        db.add(user)
        db.commit()
    db.close()

    res = RedirectResponse(url="/")
    res.set_cookie(key="user_email", value=email, httponly=True, max_age=86400 * 30, samesite="lax")
    return res

@app.get("/api/auth/logout")
async def logout():
    res = RedirectResponse(url="/")
    res.delete_cookie("user_email")
    return res

@app.get("/api/user/me")
async def get_user_data(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email:
        return JSONResponse(status_code=401, content={"logged_in": False})

    db = SessionLocal()
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        db.close()
        return JSONResponse(status_code=401, content={"logged_in": False})

    today = date.today()
    if user.last_reset_date != today:
        user.daily_credits_left = 2
        user.last_reset_date = today
        db.commit()

    data = {
        "logged_in": True,
        "email": user.email,
        "name": user.name,
        "avatar": user.avatar,
        "daily_credits": user.daily_credits_left,
        "package_credits": user.package_credits
    }
    db.close()
    return data

# --- AssemblyAI Audio Transcribe ---
@app.post("/api/transcribe-audio")
async def transcribe_audio(api_key: str = Form(...), audio: UploadFile = File(...)):
    aai.settings.api_key = api_key.strip()
    temp_audio = os.path.join(TEMP_DIR, f"trans_{uuid.uuid4().hex[:8]}.mp3")
    
    with open(temp_audio, "wb") as f:
        f.write(await audio.read())

    try:
        config = aai.TranscriptionConfig(language_detection=True)
        transcriber = aai.Transcriber()
        transcript = transcriber.transcribe(temp_audio, config=config)

        segments = []
        for idx, s in enumerate(transcript.get_sentences()):
            segments.append({
                "id": idx + 1,
                "start": s.start,
                "end": s.end,
                "duration": round((s.end - s.start) / 1000, 2),
                "text": s.text
            })
        return {"segments": segments}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if os.path.exists(temp_audio):
            os.remove(temp_audio)

# --- Batch TTS (Edge-TTS + Mutagen) ---
class BatchTTSRequest(BaseModel):
    lines: List[str]
    voice: str = "my-MM-ThihaNeural"
    pitch: int = 0
    speed: int = 10

@app.post("/api/tts/batch-generate")
async def batch_generate_tts(request: Request, payload: BatchTTSRequest):
    user_email = request.cookies.get("user_email")
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Export ပြုလုပ်ရန် Google ဖြင့် Login အရင်ဝင်ပေးပါခင်ဗျာ။"})

    db = SessionLocal()
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        db.close()
        return JSONResponse(status_code=401, content={"error": "User မရှိပါ။"})

    today = date.today()
    if user.last_reset_date != today:
        user.daily_credits_left = 2
        user.last_reset_date = today

    if user.daily_credits_left > 0:
        user.daily_credits_left -= 1
    elif user.package_credits > 0:
        user.package_credits -= 1
    else:
        db.close()
        return JSONResponse(status_code=403, content={"error": "ယနေ့အတွက် အခမဲ့ ၂ ပုဒ် ကုန်ဆုံးသွားပါပြီ။"})

    db.commit()
    db.close()

    actual_voice = "my-MM-ThihaNeural" if "Thiha" in payload.voice else "my-MM-NilarNeural"
    pitch_str = f"{payload.pitch:+d}Hz" if payload.pitch != 0 else "+0Hz"
    rate_str = f"{payload.speed:+d}%" if payload.speed != 0 else "+0%"

    audio_results = []
    for idx, text in enumerate(payload.lines):
        clean_text = text.strip()
        if not clean_text:
            audio_results.append({"index": idx, "audio_b64": "", "duration_ms": 500})
            continue

        communicate = edge_tts.Communicate(clean_text, actual_voice, pitch=pitch_str, rate=rate_str)
        audio_stream = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_stream.write(chunk["data"])
        
        audio_bytes = audio_stream.getvalue()
        
        try:
            mp3_info = MP3(io.BytesIO(audio_bytes))
            duration_ms = int(mp3_info.info.length * 1000)
        except Exception:
            duration_ms = 1000

        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        audio_results.append({
            "index": idx,
            "audio_b64": audio_b64,
            "duration_ms": duration_ms
        })

    return {"status": "success", "audios": audio_results}

@app.post("/api/preview-single-audio")
async def preview_single_audio(
    text: str = Form(...),
    voice: str = Form("my-MM-ThihaNeural"),
    pitch: int = Form(0),
    speed: int = Form(10)
):
    clean_text = text.strip()
    if not clean_text:
        return JSONResponse(status_code=400, content={"error": "Text is empty"})

    actual_voice = "my-MM-ThihaNeural" if "Thiha" in voice else "my-MM-NilarNeural"
    pitch_str = f"{pitch:+d}Hz" if pitch != 0 else "+0Hz"
    rate_str = f"{speed:+d}%" if speed != 0 else "+0%"

    communicate = edge_tts.Communicate(clean_text, actual_voice, pitch=pitch_str, rate=rate_str)
    temp_file = os.path.join(TEMP_DIR, f"prev_{uuid.uuid4().hex[:6]}.mp3")
    await communicate.save(temp_file)
    return FileResponse(temp_file, media_type="audio/mpeg")
