import os
import io
import base64
import uuid
import shutil
from datetime import date, datetime
from typing import List
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

from database import SessionLocal, User, PaymentRequest

app = FastAPI()

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# WASM နှင့် Web Worker အလုပ်လုပ်စေရန် Security Headers
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Admin Gmail
ADMIN_EMAIL = "waiphyo171104@gmail.com"

TEMP_DIR = "temp_audios"
SLIPS_DIR = "uploaded_slips"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(SLIPS_DIR, exist_ok=True)

app.mount("/slips", StaticFiles(directory=SLIPS_DIR), name="slips")

async def send_telegram_alert(text: str, photo_path: str = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient() as client:
            if photo_path and os.path.exists(photo_path):
                with open(photo_path, "rb") as f:
                    await client.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                        data={"chat_id": TELEGRAM_CHAT_ID, "caption": text},
                        files={"photo": f}
                    )
            else:
                await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    data={"chat_id": TELEGRAM_CHAT_ID, "text": text}
                )
    except Exception:
        pass

@app.get("/", response_class=HTMLResponse)
async def serve_home():
    return FileResponse(os.path.join("templates", "index.html"))

# Admin Panel: waiphyo171104@gmail.com ဖြင့် ဝင်ထားမှသာ ဖွင့်ခွင့်ပေးခြင်း
@app.get("/admin", response_class=HTMLResponse)
async def serve_admin(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email or user_email.strip().lower() != ADMIN_EMAIL.lower():
        return HTMLResponse("<h2 style='color:red; text-align:center; margin-top:50px;'>403 Forbidden: Admin သာ ဝင်ရောက်ခွင့်ရှိပါသည်။</h2>", status_code=403)
    
    admin_html_path = os.path.join("templates", "admin.html")
    if not os.path.exists(admin_html_path):
        return HTMLResponse("<h2>Admin template ဖိုင် မရှိသေးပါ။</h2>", status_code=500)
    return FileResponse(admin_html_path)

@app.get("/login/google")
async def login_google(request: Request):
    if not GOOGLE_CLIENT_ID:
        return JSONResponse(status_code=500, content={"error": "GOOGLE_CLIENT_ID မရှိသေးပါ။"})
    
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
        user_info = user_res.json()

    email = user_info["email"]
    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()
    is_admin = (email.strip().lower() == ADMIN_EMAIL.lower())
    
    if not user:
        user = User(
            email=email,
            name=user_info.get("name", "User"),
            avatar=user_info.get("picture", ""),
            daily_credits_left=2,
            package_credits=0,
            last_reset_date=date.today(),
            is_admin=is_admin
        )
        db.add(user)
        db.commit()
    else:
        if is_admin and not user.is_admin:
            user.is_admin = True
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
        "package_credits": user.package_credits,
        "is_admin": (user.email.strip().lower() == ADMIN_EMAIL.lower())
    }
    db.close()
    return data

@app.post("/api/payment/submit")
async def submit_payment(
    request: Request,
    package_type: str = Form(...),
    payment_method: str = Form(...),
    slip: UploadFile = File(...)
):
    user_email = request.cookies.get("user_email")
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Login အရင်ဝင်ပေးပါ"})

    amounts = {"10": 5000, "20": 10000, "30": 15000}
    amount = amounts.get(package_type, 5000)

    ext = os.path.splitext(slip.filename)[1] or ".jpg"
    slip_filename = f"slip_{uuid.uuid4().hex[:10]}{ext}"
    slip_path = os.path.join(SLIPS_DIR, slip_filename)
    
    with open(slip_path, "wb") as buffer:
        shutil.copyfileobj(slip.file, buffer)

    db = SessionLocal()
    pay_req = PaymentRequest(
        user_email=user_email,
        package_type=package_type,
        amount=amount,
        payment_method=payment_method,
        slip_url=f"/slips/{slip_filename}",
        status="pending"
    )
    db.add(pay_req)
    db.commit()
    db.close()

    alert_msg = f"🔔 ငွေလွှဲပြေစာ အသစ်ရောက်ရှိပါသည်!\n\n👤 User: {user_email}\n📦 Package: {package_type} ပုဒ်\n💰 ပမာဏ: {amount:,} Ks\n💳 Payment: {payment_method}\n\nApprove လုပ်ရန် /admin သို့ ဝင်ရောက်ပေးပါ။"
    await send_telegram_alert(alert_msg, slip_path)

    return {"status": "success", "message": "ငွေလွှဲပြေစာ ပေးပို့ပြီးပါပြီ။ Admin မှ စစ်ဆေးပြီးပါက Credits တိုးပေးပါမည်။"}

@app.get("/api/admin/requests")
async def get_admin_requests(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email or user_email.strip().lower() != ADMIN_EMAIL.lower():
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    requests_list = db.query(PaymentRequest).order_by(PaymentRequest.id.desc()).all()
    result = []
    for r in requests_list:
        result.append({
            "id": r.id,
            "email": r.user_email,
            "package_type": r.package_type,
            "amount": r.amount,
            "payment_method": r.payment_method,
            "slip_url": r.slip_url,
            "status": r.status,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M")
        })
    db.close()
    return result

@app.post("/api/admin/approve")
async def approve_request(request: Request, req_id: int = Form(...)):
    user_email = request.cookies.get("user_email")
    if not user_email or user_email.strip().lower() != ADMIN_EMAIL.lower():
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    pay_req = db.query(PaymentRequest).filter(PaymentRequest.id == req_id).first()
    if not pay_req or pay_req.status != "pending":
        db.close()
        return JSONResponse(status_code=400, content={"error": "Request not found or already processed"})

    target_user = db.query(User).filter(User.email == pay_req.user_email).first()
    if target_user:
        target_user.package_credits += int(pay_req.package_type)
        pay_req.status = "approved"
        db.commit()
    db.close()
    return {"status": "success", "message": "အတည်ပြုပြီး ပုဒ်ရေ ထည့်သွင်းပေးပြီးပါပြီ!"}

@app.post("/api/admin/reject")
async def reject_request(request: Request, req_id: int = Form(...)):
    user_email = request.cookies.get("user_email")
    if not user_email or user_email.strip().lower() != ADMIN_EMAIL.lower():
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    pay_req = db.query(PaymentRequest).filter(PaymentRequest.id == req_id).first()
    if pay_req and pay_req.status == "pending":
        pay_req.status = "rejected"
        db.commit()
    db.close()
    return {"status": "success", "message": "ငွေလွှဲပြေစာကို ပယ်ဖျက်လိုက်ပါပြီ"}

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
        return JSONResponse(status_code=403, content={"error": "ယနေ့အတွက် အခမဲ့ ၂ ပုဒ် ကုန်ဆုံးသွားပါပြီ။ ဆက်လက်သုံးလိုပါက Package ဝယ်ယူပေးပါခင်ဗျာ။"})

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
