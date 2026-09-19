import os
import io
import json
import base64
import uuid
import shutil
import re
import secrets
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import urlencode
from pydantic import BaseModel

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.middleware.sessions import SessionMiddleware
import httpx
import assemblyai as aai
import edge_tts
from mutagen.mp3 import MP3

from database import SessionLocal, User, PaymentRequest, Package, ProjectHistory, LogoPreset

app = FastAPI()
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

@app.middleware("http")
async def add_wasm_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
    return response

SECRET_KEY = os.getenv("SECRET_KEY", "RECAP_STUDIO_SECRET_KEY_PROD_2026")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8881875491:AAGTyx6m3-LnsOWSdRKtfuwJ8nEioIjn3Hk")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5750529400")
ADMIN_EMAIL = "waiphyo171104@gmail.com"

BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "https://my-recap-studio.onrender.com").rstrip("/")

TEMP_DIR = "temp_audios"
SLIPS_DIR = "uploaded_slips"
LOGOS_DIR = "user_logos"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(SLIPS_DIR, exist_ok=True)
os.makedirs(LOGOS_DIR, exist_ok=True)

app.mount("/slips", StaticFiles(directory=SLIPS_DIR), name="slips")
app.mount("/logos", StaticFiles(directory=LOGOS_DIR), name="logos")

@app.get("/", response_class=HTMLResponse)
async def serve_home():
    return FileResponse(os.path.join("templates", "index.html"))

@app.get("/login/google")
async def login_google(request: Request, ref: Optional[str] = None):
    if not GOOGLE_CLIENT_ID:
        return JSONResponse(status_code=500, content={"error": "GOOGLE_CLIENT_ID မရှိသေးပါ။"})
    
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    redirect_uri = f"https://{host}/api/auth/google/callback"
    request.session["oauth_redirect_uri"] = redirect_uri
    if ref:
        request.session["referral_code"] = ref

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
        user_info = user_res.json()

    email = user_info["email"]
    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()
    is_admin = (email.strip().lower() == ADMIN_EMAIL.lower())
    
    if not user:
        ref_code = request.session.get("referral_code")
        new_ref_id = secrets.token_hex(4).upper()
        bonus_credits = 0

        # Referral စစ်ဆေးခြင်း
        if ref_code:
            referrer = db.query(User).filter(User.referral_code == ref_code).first()
            if referrer and referrer.email != email:
                referrer.package_credits += 1  # ဖိတ်ခေါ်သူအား ၁ ပုဒ် ဘောနပ်စ်ပေးခြင်း
                bonus_credits = 1              # အသစ်ဝင်သူအား ၁ ပုဒ် ဘောနပ်စ်ပေးခြင်း

        user = User(
            email=email,
            name=user_info.get("name", "User"),
            avatar=user_info.get("picture", ""),
            daily_credits_left=2,
            package_credits=bonus_credits,
            last_reset_date=date.today(),
            is_admin=is_admin,
            is_banned=False,
            referral_code=new_ref_id,
            referred_by=ref_code
        )
        db.add(user)
        db.commit()
    else:
        if is_admin and not user.is_admin:
            user.is_admin = True
            db.commit()
        if not user.referral_code:
            user.referral_code = secrets.token_hex(4).upper()
            db.commit()
    
    is_banned = user.is_banned
    db.close()

    if is_banned:
        return HTMLResponse("<h2 style='color:red; text-align:center; margin-top:50px;'>အကောင့်ပိတ်ပင်ခံထားရပါသည်</h2>", status_code=403)

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
    if not user or user.is_banned:
        db.close()
        return JSONResponse(status_code=401, content={"logged_in": False})

    today = date.today()
    if user.last_reset_date.date() if isinstance(user.last_reset_date, datetime) else user.last_reset_date != today:
        user.daily_credits_left = 2
        user.last_reset_date = datetime.utcnow()
        db.commit()

    is_admin = (user.email.strip().lower() == ADMIN_EMAIL.lower()) or user.is_admin
    is_premium = is_admin or (user.package_credits > 0)

    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    ref_link = f"https://{host}/login/google?ref={user.referral_code}"

    data = {
        "logged_in": True,
        "email": user.email,
        "name": user.name,
        "avatar": user.avatar,
        "daily_credits": user.daily_credits_left,
        "package_credits": user.package_credits,
        "is_admin": is_admin,
        "is_premium": is_premium,
        "referral_code": user.referral_code,
        "referral_link": ref_link
    }
    db.close()
    return data

# History APIs
@app.get("/api/history")
async def get_history(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    db = SessionLocal()
    items = db.query(ProjectHistory).filter(ProjectHistory.user_email == user_email).order_by(ProjectHistory.id.desc()).limit(15).all()
    res = [{
        "id": it.id,
        "title": it.title,
        "duration": it.duration_sec,
        "created_at": it.created_at.strftime("%Y-%m-%d %H:%M")
    } for it in items]
    db.close()
    return res

@app.post("/api/history/record")
async def record_history(request: Request, title: str = Form("Recap Project"), duration: float = Form(0.0)):
    user_email = request.cookies.get("user_email")
    if user_email:
        db = SessionLocal()
        h = ProjectHistory(user_email=user_email, title=title, duration_sec=duration)
        db.add(h)
        db.commit()
        db.close()
    return {"status": "ok"}

# Logo Presets APIs
@app.get("/api/logos")
async def get_user_logos(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email: return []
    db = SessionLocal()
    logos = db.query(LogoPreset).filter(LogoPreset.user_email == user_email).all()
    res = [{"id": l.id, "name": l.name, "url": l.file_path} for l in logos]
    db.close()
    return res

@app.post("/api/logos/upload")
async def upload_logo_preset(request: Request, name: str = Form(...), file: UploadFile = File(...)):
    user_email = request.cookies.get("user_email")
    if not user_email: return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    
    ext = os.path.splitext(file.filename)[1] or ".png"
    fname = f"logo_{uuid.uuid4().hex[:8]}{ext}"
    fpath = os.path.join(LOGOS_DIR, fname)
    with open(fpath, "wb") as b:
        shutil.copyfileobj(file.file, b)

    db = SessionLocal()
    lp = LogoPreset(user_email=user_email, name=name, file_path=f"/logos/{fname}")
    db.add(lp)
    db.commit()
    db.close()
    return {"status": "success", "url": f"/logos/{fname}", "name": name}

# Telegram Direct Video Sender
@app.post("/api/telegram/send-video")
async def send_video_to_telegram(request: Request, video: UploadFile = File(...), caption: str = Form("Recap Video Export")):
    user_email = request.cookies.get("user_email")
    if not user_email: return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return JSONResponse(status_code=500, content={"error": "Telegram Bot Config မရှိပါ"})

    try:
        content = await video.read()
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": f"🎬 {caption}\n👤 By: {user_email}"},
                files={"video": (video.filename or "recap.mp4", content, "video/mp4")}
            )
        return {"status": "success", "message": "Telegram ဆီသို့ တိုက်ရိုက်ပေးပို့ပြီးပါပြီ!"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# Audio, Direct Gemini & Edge-TTS Batch
@app.post("/api/transcribe-audio")
async def transcribe_audio(api_key: str = Form(...), audio: UploadFile = File(...)):
    aai.settings.api_key = api_key.strip()
    temp_audio = os.path.join(TEMP_DIR, f"trans_{uuid.uuid4().hex[:8]}.mp3")
    with open(temp_audio, "wb") as f:
        f.write(await audio.read())

    try:
        config = aai.TranscriptionConfig(language_detection=True, speaker_labels=True)
        transcriber = aai.Transcriber()
        transcript = transcriber.transcribe(temp_audio, config=config)

        segments = []
        if transcript.utterances:
            for idx, u in enumerate(transcript.utterances):
                segments.append({
                    "id": idx + 1,
                    "start": u.start,
                    "end": u.end,
                    "duration": round((u.end - u.start) / 1000, 2),
                    "speaker": u.speaker,
                    "text": u.text
                })
        else:
            for idx, s in enumerate(transcript.get_sentences()):
                segments.append({
                    "id": idx + 1,
                    "start": s.start,
                    "end": s.end,
                    "duration": round((s.end - s.start) / 1000, 2),
                    "speaker": "A",
                    "text": s.text
                })
        return {"segments": segments}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if os.path.exists(temp_audio): os.remove(temp_audio)

class GeminiDirectRequest(BaseModel):
    segments: List[dict]

@app.post("/api/translate/gemini-direct")
async def translate_gemini_direct(request: Request, payload: GeminiDirectRequest):
    user_email = request.cookies.get("user_email")
    if not user_email: return JSONResponse(status_code=401, content={"error": "Login ဝင်ပေးပါ"})

    db = SessionLocal()
    user = db.query(User).filter(User.email == user_email).first()
    is_premium = user and ((user.email.strip().lower() == ADMIN_EMAIL.lower()) or user.is_admin or user.package_credits > 0)
    db.close()

    if not is_premium:
        return JSONResponse(status_code=403, content={"error": "💎 Direct Gemini Feature သည် Premium User များသာ သီးသန့် အသုံးပြုနိုင်ပါသည်ခင်ဗျာ။"})

    if not GEMINI_API_KEY:
        return JSONResponse(status_code=500, content={"error": "GEMINI_API_KEY မသတ်မှတ်ရသေးပါ"})

    prompt_text = """You are an expert Burmese Movie Recap Narrator.
Translate the following segmented lines into punchy, exciting Burmese movie recap style.
STRICT RULES:
1. EXTREMELY SHORT: 4 to 8 words per line.
2. DURATION LIMIT: Fit into the given duration (max 3-4 syllables per second).
3. STRICT JSON FORMAT ONLY: {"translations": ["...", "..."]}

INPUTS:
"""
    for seg in payload.segments:
        prompt_text += f"[{seg.get('id')}] [Duration: {seg.get('duration')}s] \"{seg.get('text')}\"\n"

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, json={"contents": [{"parts": [{"text": prompt_text}]}]})
            data = resp.json()
            raw_content = data["candidates"][0]["content"]["parts"][0]["text"]
            cleaned = re.sub(r"```json|```", "", raw_content).strip()
            return json.loads(cleaned)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

class BatchTTSItem(BaseModel):
    text: str
    voice: Optional[str] = "my-MM-ThihaNeural"

class BatchTTSRequest(BaseModel):
    items: List[BatchTTSItem]
    pitch: int = 0
    speed: int = 10

BAD_WORDS = ["လိုး", "လီး", "မအေလိုး", "ဖာသယ်မ", "စောက်ဖုတ်", "လိုးမသား"]
def censor_text(t: str) -> str:
    res = t
    for w in BAD_WORDS: res = re.sub(w, "***", res)
    return res

@app.post("/api/tts/batch-generate")
async def batch_generate_tts(request: Request, payload: BatchTTSRequest):
    user_email = request.cookies.get("user_email")
    if not user_email: return JSONResponse(status_code=401, content={"error": "Login ဝင်ပေးပါ"})

    db = SessionLocal()
    user = db.query(User).filter(User.email == user_email).first()
    if not user or user.is_banned:
        db.close()
        return JSONResponse(status_code=403, content={"error": "အကောင့်ပိတ်ပင်ခံထားရပါသည်"})

    is_admin = (user.email.strip().lower() == ADMIN_EMAIL.lower()) or user.is_admin
    if not is_admin and (user.daily_credits_left + user.package_credits) <= 0:
        db.close()
        return JSONResponse(status_code=403, content={"error": "Credits ကုန်ဆုံးသွားပါပြီ"})
    db.close()

    pitch_str = f"{payload.pitch:+d}Hz" if payload.pitch != 0 else "+0Hz"
    rate_str = f"{payload.speed:+d}%" if payload.speed != 0 else "+0%"

    audio_results = []
    for idx, item in enumerate(payload.items):
        clean_text = censor_text(item.text.strip())
        if not clean_text:
            audio_results.append({"index": idx, "audio_b64": "", "duration_ms": 500})
            continue

        selected_voice = "my-MM-NilarNeural" if "Nilar" in (item.voice or "") else "my-MM-ThihaNeural"
        communicate = edge_tts.Communicate(clean_text, selected_voice, pitch=pitch_str, rate=rate_str)
        audio_stream = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio": audio_stream.write(chunk["data"])
        
        audio_bytes = audio_stream.getvalue()
        try:
            mp3_info = MP3(io.BytesIO(audio_bytes))
            duration_ms = int(mp3_info.info.length * 1000)
        except Exception: duration_ms = 1000

        audio_results.append({
            "index": idx,
            "audio_b64": base64.b64encode(audio_bytes).decode("utf-8"),
            "duration_ms": duration_ms
        })
    return {"status": "success", "audios": audio_results}

@app.post("/api/credits/consume")
async def consume_credit(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email: return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    db = SessionLocal()
    user = db.query(User).filter(User.email == user_email).first()
    if user and not ((user.email.strip().lower() == ADMIN_EMAIL.lower()) or user.is_admin):
        if user.daily_credits_left > 0: user.daily_credits_left -= 1
        elif user.package_credits > 0: user.package_credits -= 1
        db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/preview-single-audio")
async def preview_single_audio(text: str = Form(...), voice: str = Form("my-MM-ThihaNeural"), pitch: int = Form(0), speed: int = Form(10)):
    clean_text = censor_text(text.strip())
    actual_voice = "my-MM-NilarNeural" if "Nilar" in voice else "my-MM-ThihaNeural"
    pitch_str = f"{pitch:+d}Hz" if pitch != 0 else "+0Hz"
    rate_str = f"{speed:+d}%" if speed != 0 else "+0%"

    communicate = edge_tts.Communicate(clean_text, actual_voice, pitch=pitch_str, rate=rate_str)
    temp_file = os.path.join(TEMP_DIR, f"prev_{uuid.uuid4().hex[:6]}.mp3")
    await communicate.save(temp_file)
    return FileResponse(temp_file, media_type="audio/mpeg")

@app.get("/api/packages")
async def get_public_packages():
    db = SessionLocal()
    pkgs = db.query(Package).filter(Package.is_active == True).order_by(Package.credits.asc()).all()
    res = [{"id": p.id, "name": p.name, "credits": p.credits, "price": p.price_mmk} for p in pkgs]
    db.close()
    return res

@app.post("/api/payment/submit")
async def submit_payment(request: Request, package_type: str = Form(...), payment_method: str = Form(...), slip: UploadFile = File(...)):
    user_email = request.cookies.get("user_email")
    if not user_email: return JSONResponse(status_code=401, content={"error": "Login ဝင်ပေးပါ"})

    db = SessionLocal()
    pkg = db.query(Package).filter(Package.credits == int(package_type)).first()
    amount = pkg.price_mmk if pkg else (int(package_type) * 500)

    ext = os.path.splitext(slip.filename)[1] or ".jpg"
    slip_filename = f"slip_{uuid.uuid4().hex[:10]}{ext}"
    slip_path = os.path.join(SLIPS_DIR, slip_filename)
    with open(slip_path, "wb") as buffer:
        shutil.copyfileobj(slip.file, buffer)

    pay_req = PaymentRequest(user_email=user_email, package_type=package_type, amount=amount, payment_method=payment_method, slip_url=f"/slips/{slip_filename}", status="pending")
    db.add(pay_req)
    db.commit()
    db.close()
    return {"status": "success", "message": "ငွေလွှဲပြေစာ ပေးပို့ပြီးပါပြီ။"}
