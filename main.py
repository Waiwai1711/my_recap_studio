import os
import io
import json
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

from database import SessionLocal, User, PaymentRequest, Package

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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8881875491:AAGTyx6m3-LnsOWSdRKtfuwJ8nEioIjn3Hk")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5750529400")
ADMIN_EMAIL = "waiphyo171104@gmail.com"

BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "https://my-recap-studio.onrender.com").rstrip("/")

TEMP_DIR = "temp_audios"
SLIPS_DIR = "uploaded_slips"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(SLIPS_DIR, exist_ok=True)

app.mount("/slips", StaticFiles(directory=SLIPS_DIR), name="slips")

# Telegram Bot Alert (Direct Action Link)
async def send_telegram_payment_alert(req_id: int, user_email: str, package_type: str, amount: int, payment_method: str, photo_path: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    try:
        caption = (
            f"🔔 *ငွေလွှဲပြေစာ အသစ်ရောက်ရှိပါသည်!*\n\n"
            f"🆔 Req ID: `#{req_id}`\n"
            f"👤 User: `{user_email}`\n"
            f"📦 Package: *{package_type} ပုဒ်*\n"
            f"💰 ပမာဏ: *{amount:,} Ks*\n"
            f"💳 Method: *{payment_method}*\n\n"
            f"အောက်ပါခလုတ်ကို နှိပ်၍ တိုက်ရိုက် အတည်ပြုနိုင်ပါသည် 👇"
        )
        approve_url = f"{BASE_URL}/api/quick-action?action=approve&id={req_id}&key={SECRET_KEY}"
        reject_url = f"{BASE_URL}/api/quick-action?action=reject&id={req_id}&key={SECRET_KEY}"

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve (ခွင့်ပြုမည်)", "url": approve_url},
                    {"text": "❌ Reject (ပယ်ဖျက်မည်)", "url": reject_url}
                ]
            ]
        }
        async with httpx.AsyncClient() as client:
            if photo_path and os.path.exists(photo_path):
                with open(photo_path, "rb") as f:
                    res = await client.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                        data={
                            "chat_id": TELEGRAM_CHAT_ID,
                            "caption": caption,
                            "parse_mode": "Markdown",
                            "reply_markup": json.dumps(inline_keyboard)
                        },
                        files={"photo": f}
                    )
                    if res.status_code == 200:
                        return res.json().get("result", {}).get("message_id")
    except Exception:
        pass
    return None

# Telegram Quick Action
@app.get("/api/quick-action", response_class=HTMLResponse)
async def quick_action(action: str, id: int, key: str):
    if key != SECRET_KEY:
        return HTMLResponse("<h2 style='color:red; text-align:center; margin-top:50px;'>403 Forbidden</h2>", status_code=403)

    db = SessionLocal()
    pay_req = db.query(PaymentRequest).filter(PaymentRequest.id == id).first()
    if not pay_req:
        db.close()
        return HTMLResponse("<h2 style='color:red; text-align:center; margin-top:50px;'>တောင်းဆိုမှု ရှာမတွေ့ပါ။</h2>")

    if pay_req.status != "pending":
        status_text = pay_req.status.upper()
        db.close()
        return HTMLResponse(f"<h2 style='color:#facc15; text-align:center; margin-top:50px;'>ဤပြေစာသည် {status_text} ပြုလုပ်ပြီးသား ဖြစ်ပါသည်။</h2>")

    target_user = db.query(User).filter(User.email == pay_req.user_email).first()

    if action == "approve":
        if target_user:
            target_user.package_credits += int(pay_req.package_type)
        pay_req.status = "approved"
        title = "✅ အောင်မြင်ပါပြီ!"
        desc = f"<b>{pay_req.user_email}</b> ထံသို့ Package <b>{pay_req.package_type} ပုဒ်</b> ထည့်သွင်းပေးပြီးပါပြီ။"
        color = "#10b981"
    else:
        pay_req.status = "rejected"
        title = "❌ ပယ်ဖျက်လိုက်ပါပြီ"
        desc = f"ငွေလွှဲပြေစာ <b>#{id}</b> ကို ပယ်ဖျက်လိုက်ပါပြီ။"
        color = "#ef4444"

    db.commit()
    db.close()

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Action Completed</title></head>
    <body style="background:#0b0c10; color:#f4f4f5; display:flex; align-items:center; justify-content:center; height:90vh; font-family:sans-serif; text-align:center; margin:0; padding:16px;">
      <div style="background:#14161f; padding:28px 20px; border-radius:20px; border:1px solid rgba(255,255,255,0.1); max-width:360px; width:100%;">
        <h2 style="color:{color}; margin-bottom:12px; font-size:20px;">{title}</h2>
        <p style="color:#a1a1aa; font-size:13px; line-height:1.6;">{desc}</p>
        <p style="color:#71717a; font-size:11px; margin-top:20px;">Telegram သို့ ပြန်သွားနိုင်ပါပြီ</p>
      </div>
    </body>
    </html>
    """)

@app.get("/", response_class=HTMLResponse)
async def serve_home():
    return FileResponse(os.path.join("templates", "index.html"))

@app.get("/admin", response_class=HTMLResponse)
async def serve_admin(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email or user_email.strip().lower() != ADMIN_EMAIL.lower():
        return HTMLResponse("<h2 style='color:red; text-align:center; margin-top:50px;'>403 Forbidden: Admin သာ ဝင်ရောက်ခွင့်ရှိပါသည်။</h2>", status_code=403)
    return FileResponse(os.path.join("templates", "admin.html"))

@app.get("/api/packages")
async def get_public_packages():
    db = SessionLocal()
    pkgs = db.query(Package).filter(Package.is_active == True).order_by(Package.credits.asc()).all()
    result = [{"id": p.id, "name": p.name, "credits": p.credits, "price": p.price_mmk} for p in pkgs]
    db.close()
    return result

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
            is_admin=is_admin,
            is_banned=False
        )
        db.add(user)
        db.commit()
    else:
        if is_admin and not user.is_admin:
            user.is_admin = True
            db.commit()
    
    is_banned = user.is_banned
    db.close()

    if is_banned:
        return HTMLResponse("<h2 style='color:red; text-align:center; margin-top:50px;'>ဤအကောင့်သည် ပိတ်ပင် (Ban) ခံထားရပါသည်ခင်ဗျာ။</h2>", status_code=403)

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
        return JSONResponse(status_code=401, content={"logged_in": False, "banned": getattr(user, 'is_banned', False)})

    today = date.today()
    if user.last_reset_date != today:
        user.daily_credits_left = 2
        user.last_reset_date = today
        db.commit()

    is_admin = (user.email.strip().lower() == ADMIN_EMAIL.lower()) or user.is_admin

    data = {
        "logged_in": True,
        "email": user.email,
        "name": user.name,
        "avatar": user.avatar,
        "daily_credits": user.daily_credits_left,
        "package_credits": user.package_credits,
        "is_admin": is_admin
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

    db = SessionLocal()
    user = db.query(User).filter(User.email == user_email).first()
    if not user or user.is_banned:
        db.close()
        return JSONResponse(status_code=403, content={"error": "ဤအကောင့်သည် အသုံးပြုခွင့် ပိတ်ပင်ခံထားရပါသည်"})

    pkg = db.query(Package).filter(Package.credits == int(package_type)).first()
    amount = pkg.price_mmk if pkg else (int(package_type) * 500)

    ext = os.path.splitext(slip.filename)[1] or ".jpg"
    slip_filename = f"slip_{uuid.uuid4().hex[:10]}{ext}"
    slip_path = os.path.join(SLIPS_DIR, slip_filename)
    
    with open(slip_path, "wb") as buffer:
        shutil.copyfileobj(slip.file, buffer)

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
    db.refresh(pay_req)

    t_msg_id = await send_telegram_payment_alert(pay_req.id, user_email, package_type, amount, payment_method, slip_path)
    if t_msg_id:
        pay_req.telegram_msg_id = t_msg_id
        db.commit()

    db.close()
    return {"status": "success", "message": "ငွေလွှဲပြေစာ ပေးပို့ပြီးပါပြီ။ Admin မှ မကြာမီ စစ်ဆေးပေးပါမည်။"}

# ================= ADMIN APIS =================

def verify_admin(request: Request):
    user_email = request.cookies.get("user_email")
    return user_email and (user_email.strip().lower() == ADMIN_EMAIL.lower())

@app.get("/api/admin/finance/summary")
async def get_finance_summary(request: Request):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    approved_reqs = db.query(PaymentRequest).filter(PaymentRequest.status == "approved").all()
    pending_count = db.query(PaymentRequest).filter(PaymentRequest.status == "pending").count()
    users_count = db.query(User).count()

    total_revenue = sum(r.amount for r in approved_reqs if r.amount)
    
    today = date.today()
    today_revenue = sum(r.amount for r in approved_reqs if r.created_at and r.created_at.date() == today and r.amount)

    methods = {}
    for r in approved_reqs:
        m = r.payment_method.split(" ")[0]
        methods[m] = methods.get(m, 0) + (r.amount or 0)

    db.close()
    return {
        "total_revenue": total_revenue,
        "today_revenue": today_revenue,
        "total_orders": len(approved_reqs),
        "pending_orders": pending_count,
        "total_users": users_count,
        "methods": methods
    }

@app.get("/api/admin/users")
async def get_all_users(request: Request):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    users = db.query(User).order_by(User.id.desc()).all()
    result = [{
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "avatar": u.avatar,
        "daily_credits": u.daily_credits_left,
        "package_credits": u.package_credits,
        "is_admin": u.is_admin,
        "is_banned": u.is_banned,
        "created_at": u.created_at.strftime("%Y-%m-%d") if u.created_at else ""
    } for u in users]
    db.close()
    return result

@app.post("/api/admin/users/adjust-credits")
async def adjust_credits(
    request: Request,
    email: str = Form(...),
    action_type: str = Form(...),
    amount: int = Form(...)
):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user:
        db.close()
        return JSONResponse(status_code=404, content={"error": "User ရှာမတွေ့ပါ"})

    if action_type == "add":
        user.package_credits += amount
        msg = f"{user.email} ထံသို့ {amount} ပုဒ် ထည့်ပေးပြီးပါပြီ!"
    else:
        user.package_credits = max(0, user.package_credits - amount)
        msg = f"{user.email} ထံမှ {amount} ပုဒ် ပြန်နုတ်ပြီးပါပြီ (လက်ကျန်: {user.package_credits} ပုဒ်)!"

    db.commit()
    db.close()
    return {"status": "success", "message": msg}

@app.post("/api/admin/users/toggle-ban")
async def toggle_ban_user(request: Request, email: str = Form(...)):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    clean_email = email.strip().lower()
    if clean_email == ADMIN_EMAIL.lower():
        return JSONResponse(status_code=400, content={"error": "Admin အကောင့်ကို Ban ၍ မရပါ"})

    db = SessionLocal()
    user = db.query(User).filter(User.email == clean_email).first()
    if not user:
        db.close()
        return JSONResponse(status_code=404, content={"error": "User ရှာမတွေ့ပါ"})

    user.is_banned = not user.is_banned
    status = "Ban လိုက်ပါပြီ" if user.is_banned else "Ban မှ ပြန်လည်ဖွင့်ပေးလိုက်ပါပြီ"
    db.commit()
    db.close()
    return {"status": "success", "message": f"{clean_email} အား {status}"}

@app.get("/api/admin/packages")
async def admin_get_packages(request: Request):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})
    db = SessionLocal()
    pkgs = db.query(Package).order_by(Package.credits.asc()).all()
    res = [{"id": p.id, "name": p.name, "credits": p.credits, "price": p.price_mmk, "is_active": p.is_active} for p in pkgs]
    db.close()
    return res

@app.post("/api/admin/packages/save")
async def save_package(
    request: Request,
    pkg_id: int = Form(0),
    name: str = Form(...),
    credits: int = Form(...),
    price: int = Form(...)
):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    if pkg_id > 0:
        pkg = db.query(Package).filter(Package.id == pkg_id).first()
        if pkg:
            pkg.name = name.strip()
            pkg.credits = credits
            pkg.price_mmk = price
    else:
        new_pkg = Package(name=name.strip(), credits=credits, price_mmk=price, is_active=True)
        db.add(new_pkg)

    db.commit()
    db.close()
    return {"status": "success", "message": "Package သိမ်းဆည်းပြီးပါပြီ!"}

@app.post("/api/admin/packages/delete")
async def delete_package(request: Request, pkg_id: int = Form(...)):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})
    db = SessionLocal()
    pkg = db.query(Package).filter(Package.id == pkg_id).first()
    if pkg:
        db.delete(pkg)
        db.commit()
    db.close()
    return {"status": "success", "message": "Package ဖျက်ပြီးပါပြီ"}

@app.get("/api/admin/requests")
async def get_admin_requests(request: Request):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    requests_list = db.query(PaymentRequest).order_by(PaymentRequest.id.desc()).all()
    result = [{
        "id": r.id,
        "email": r.user_email,
        "package_type": r.package_type,
        "amount": r.amount,
        "payment_method": r.payment_method,
        "slip_url": r.slip_url,
        "status": r.status,
        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M")
    } for r in requests_list]
    db.close()
    return result

@app.post("/api/admin/approve")
async def approve_request(request: Request, req_id: int = Form(...)):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    pay_req = db.query(PaymentRequest).filter(PaymentRequest.id == req_id).first()
    if not pay_req or pay_req.status != "pending":
        db.close()
        return JSONResponse(status_code=400, content={"error": "Request not found or processed"})

    target_user = db.query(User).filter(User.email == pay_req.user_email).first()
    if target_user:
        target_user.package_credits += int(pay_req.package_type)
        pay_req.status = "approved"
        db.commit()
    db.close()
    return {"status": "success", "message": "အတည်ပြုပြီး ပုဒ်ရေ ထည့်သွင်းပေးပြီးပါပြီ!"}

@app.post("/api/admin/reject")
async def reject_request(request: Request, req_id: int = Form(...)):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    pay_req = db.query(PaymentRequest).filter(PaymentRequest.id == req_id).first()
    if pay_req and pay_req.status == "pending":
        pay_req.status = "rejected"
        db.commit()
    db.close()
    return {"status": "success", "message": "ငွေလွှဲပြေစာကို ပယ်ဖျက်လိုက်ပါပြီ"}

@app.post("/api/admin/clawback")
async def clawback_request(request: Request, req_id: int = Form(...)):
    if not verify_admin(request):
        return JSONResponse(status_code=403, content={"error": "Access Denied"})

    db = SessionLocal()
    pay_req = db.query(PaymentRequest).filter(PaymentRequest.id == req_id).first()
    if not pay_req or pay_req.status != "approved":
        db.close()
        return JSONResponse(status_code=400, content={"error": "Approved ဖြစ်ထားသော ပြေစာသာ Clawback လုပ်၍ ရပါမည်"})

    target_user = db.query(User).filter(User.email == pay_req.user_email).first()
    if target_user:
        target_user.package_credits = max(0, target_user.package_credits - int(pay_req.package_type))
    
    pay_req.status = "rejected"
    db.commit()
    db.close()
    return {"status": "success", "message": f"ပြေစာ #{req_id} ကို ပယ်ဖျက်ပြီး ပုဒ်ရေ {pay_req.package_type} ခုအား အောင်မြင်စွာ ပြန်လည်နုတ်ယူပြီးပါပြီ!"}

# ================= AUDIO, TTS & CREDIT SAFE CONSUMPTION =================

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

# TTS ထုတ်ယူစဉ်တွင် Credit စစ်ဆေးရုံသာ စစ်ဆေးမည် (Credit မဖြတ်ပါ)
@app.post("/api/tts/batch-generate")
async def batch_generate_tts(request: Request, payload: BatchTTSRequest):
    user_email = request.cookies.get("user_email")
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Export ပြုလုပ်ရန် Google ဖြင့် Login အရင်ဝင်ပေးပါခင်ဗျာ။"})

    db = SessionLocal()
    user = db.query(User).filter(User.email == user_email).first()
    if not user or user.is_banned:
        db.close()
        return JSONResponse(status_code=403, content={"error": "ဤအကောင့်သည် ပိတ်ပင် (Ban) ခံထားရပါသည်"})

    is_admin_user = (user.email.strip().lower() == ADMIN_EMAIL.lower()) or user.is_admin

    if not is_admin_user:
        today = date.today()
        if user.last_reset_date != today:
            user.daily_credits_left = 2
            user.last_reset_date = today
            db.commit()

        if (user.daily_credits_left + user.package_credits) <= 0:
            db.close()
            return JSONResponse(status_code=403, content={"error": "ယနေ့အတွက် အခမဲ့ ၂ ပုဒ် ကုန်ဆုံးသွားပါပြီ။ ဆက်လက်သုံးလိုပါက Package ဝယ်ယူပေးပါခင်ဗျာ။"})

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

# ★ ဗီဒီယို Render 100% အောင်မြင်မှသာ Credit ၁ ပုဒ် အမှန်တကယ် ဖြတ်တောက်သည့် API
@app.post("/api/credits/consume")
async def consume_credit(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    db = SessionLocal()
    user = db.query(User).filter(User.email == user_email).first()
    if not user or user.is_banned:
        db.close()
        return JSONResponse(status_code=403, content={"error": "Banned or not found"})

    is_admin_user = (user.email.strip().lower() == ADMIN_EMAIL.lower()) or user.is_admin
    if not is_admin_user:
        today = date.today()
        if user.last_reset_date != today:
            user.daily_credits_left = 2
            user.last_reset_date = today

        if user.daily_credits_left > 0:
            user.daily_credits_left -= 1
        elif user.package_credits > 0:
            user.package_credits -= 1
        db.commit()

    db.close()
    return {"status": "success"}

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
