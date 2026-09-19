import os
import sys
import json
import asyncio
import shutil
import uuid
from datetime import date
from fastapi import FastAPI, UploadFile, File, Form, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse, RedirectResponse
from authlib.integrations.starlette_integration import OAuth
from starlette.middleware.sessions import SessionMiddleware
from pydub import AudioSegment
import assemblyai as aai
import edge_tts

from database import SessionLocal, User

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="RECAP_STUDIO_SECRET_KEY_1711")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "YOUR_GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "YOUR_GOOGLE_CLIENT_SECRET")

oauth = OAuth()
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

BASE_TEMP = "temp_process"
os.makedirs(BASE_TEMP, exist_ok=True)
export_tasks = {}

def get_user_dir(user_id: str):
    user_dir = os.path.join(BASE_TEMP, f"user_{user_id}")
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def ms_to_srt_time(ms):
    hours = ms // (3600 * 1000)
    ms %= (3600 * 1000)
    minutes = ms // (60 * 1000)
    ms %= (60 * 1000)
    seconds = ms // 1000
    milli = ms % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milli:03d}"

async def generate_speech_with_clone(text, voice, pitch_hz, rate_pct, output_file, ref_voice_path=None):
    clean_text = text.strip()
    if not clean_text:
        AudioSegment.silent(duration=500).export(output_file, format="mp3")
        return

    if voice != "local_clone" or not ref_voice_path or not os.path.exists(ref_voice_path):
        actual_voice = "my-MM-ThihaNeural" if "Thiha" in voice else "my-MM-NilarNeural"
        pitch_str = f"{pitch_hz:+d}Hz" if pitch_hz != 0 else "+0Hz"
        rate_str = f"{rate_pct:+d}%" if rate_pct != 0 else "+0%"
        communicate = edge_tts.Communicate(clean_text, actual_voice, pitch=pitch_str, rate=rate_str)
        await communicate.save(output_file)
        return

    temp_tts = output_file + "_base.mp3"
    communicate = edge_tts.Communicate(clean_text, "my-MM-ThihaNeural", rate=f"{rate_pct:+d}%")
    await communicate.save(temp_tts)

    try:
        cmd_filter = (
            f'ffmpeg -y -i "{temp_tts}" -i "{ref_voice_path}" '
            f'-filter_complex "[0:a]asetrate=44100*1.02,aresample=44100,equalizer=f=300:t=q:w=1.2:g=3,bass=g=2[out]" '
            f'-map "[out]" -c:a mp3 "{output_file}" -loglevel quiet'
        )
        await asyncio.to_thread(os.system, cmd_filter)
        if not os.path.exists(output_file):
            shutil.copy(temp_tts, output_file)
    except Exception:
        shutil.copy(temp_tts, output_file)
    finally:
        if os.path.exists(temp_tts):
            os.remove(temp_tts)

@app.get("/", response_class=HTMLResponse)
async def serve_home():
    return FileResponse(os.path.join("templates", "index.html"))

@app.get("/login/google")
async def login_google(request: Request):
    redirect_uri = request.url_for('auth_google_callback')
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/api/auth/google/callback")
async def auth_google_callback(request: Request, response: Response):
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get('userinfo')
        if not user_info:
            return RedirectResponse(url="/?error=auth_failed")

        email = user_info['email']
        db = SessionLocal()
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                email=email,
                name=user_info.get('name', 'User'),
                avatar=user_info.get('picture', ''),
                daily_credits_left=2,
                package_credits=0,
                last_reset_date=date.today()
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        db.close()

        res = RedirectResponse(url="/")
        res.set_cookie(key="user_email", value=email, httponly=True, max_age=86400 * 30)
        return res
    except Exception:
        return RedirectResponse(url="/")

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

    info = {
        "logged_in": True,
        "email": user.email,
        "name": user.name,
        "avatar": user.avatar,
        "daily_credits": user.daily_credits_left,
        "package_credits": user.package_credits
    }
    db.close()
    return info

@app.post("/api/upload-voice-sample")
async def upload_voice_sample(voice_file: UploadFile = File(...)):
    ext = os.path.splitext(voice_file.filename)[1] or ".wav"
    sample_name = f"ref_{uuid.uuid4().hex[:8]}{ext}"
    sample_path = os.path.join(BASE_TEMP, sample_name)
    with open(sample_path, "wb") as buffer:
        shutil.copyfileobj(voice_file.file, buffer)
    return {"status": "success", "voice_path": sample_path}

@app.post("/api/transcribe")
async def transcribe(request: Request, api_key: str = Form(...), video: UploadFile = File(...)):
    user_email = request.cookies.get("user_email") or "guest"
    user_dir = get_user_dir(user_email.replace("@", "_").replace(".", "_"))

    video_path = os.path.join(user_dir, f"input_{video.filename}")
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(video.file, buffer)

    aai.settings.api_key = api_key.strip()
    try:
        config = aai.TranscriptionConfig(language_detection=True)
        transcriber = aai.Transcriber()
        transcript = transcriber.transcribe(video_path, config=config)

        segments = []
        for idx, s in enumerate(transcript.get_sentences()):
            segments.append({
                "id": idx + 1,
                "start": s.start,
                "end": s.end,
                "duration": round((s.end - s.start) / 1000, 2),
                "text": s.text
            })
        return {"video_filename": f"input_{video.filename}", "segments": segments}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/preview-audio")
async def preview_audio(
    text: str = Form(...),
    voice: str = Form(...),
    pitch: int = Form(0),
    speed: int = Form(0),
    ref_voice_path: str = Form(None)
):
    out_file = os.path.join(BASE_TEMP, f"preview_{uuid.uuid4().hex[:6]}.mp3")
    await generate_speech_with_clone(text, voice, pitch, speed, out_file, ref_voice_path)
    return FileResponse(out_file, media_type="audio/mpeg")

@app.get("/api/progress/{task_id}")
async def get_progress(task_id: str):
    async def event_generator():
        while True:
            data = export_tasks.get(task_id, {"percent": 0, "status": "စတင်နေပါသည်...", "done": False})
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            if data.get("done") or data.get("error"):
                break
            await asyncio.sleep(0.4)
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/start-export")
async def start_export(
    request: Request,
    task_id: str = Form(...),
    video_filename: str = Form(...),
    segments_json: str = Form(...),
    translations_json: str = Form(...),
    voice: str = Form("my-MM-ThihaNeural"),
    pitch: int = Form(0),
    speed: int = Form(10),
    bgm_vol: int = Form(15),
    burn_sub: bool = Form(True),
    blur_sub: bool = Form(True),
    blur_h: int = Form(12),
    font_size: int = Form(22),
    font_name: str = Form("Pyidaungsu"),
    auto_speed_video: bool = Form(True),
    ref_voice_path: str = Form(None)
):
    user_email = request.cookies.get("user_email")
    if not user_email:
        return JSONResponse(status_code=401, content={"error": "ဗီဒီယို Export ပြုလုပ်ရန် Gmail ဖြင့် အရင် Login ဝင်ပေးပါခင်ဗျာ။"})

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
        return JSONResponse(status_code=403, content={"error": "ယနေ့အတွက် အခမဲ့ ၂ ပုဒ် ကုန်သွားပါပြီ။ ဆက်လက်သုံးလိုပါက Package ဝယ်ယူပါခင်ဗျာ။"})

    db.commit()
    db.close()

    user_dir = get_user_dir(user_email.replace("@", "_").replace(".", "_"))

    async def _process():
        try:
            segments = json.loads(segments_json)
            translations = json.loads(translations_json)
            total = len(segments)
            video_path = os.path.join(user_dir, video_filename)

            export_tasks[task_id] = {"percent": 5, "status": "Export စတင်ပြင်ဆင်နေပါသည်...", "done": False}
            final_audio = AudioSegment.empty()
            srt_lines = []
            cur_ms = 0
            video_clips = []

            for idx, (seg, text) in enumerate(zip(segments, translations)):
                raw_mp3 = os.path.join(user_dir, f"raw_{idx}.mp3")
                await generate_speech_with_clone(text, voice, pitch, speed, raw_mp3, ref_voice_path)

                try:
                    aud = AudioSegment.from_file(raw_mp3)
                    dur_ms = len(aud)
                except Exception:
                    dur_ms = max(500, seg["end"] - seg["start"])
                    aud = AudioSegment.silent(duration=dur_ms)

                final_audio += aud

                if auto_speed_video:
                    orig_sec = max(0.4, (seg["end"] - seg["start"]) / 1000.0)
                    target_sec = max(0.4, dur_ms / 1000.0)
                    clip_out = os.path.join(user_dir, f"clip_{idx}.mp4")

                    if orig_sec > target_sec:
                        factor = min(orig_sec / target_sec, 3.0)
                        vf = f"trim=start={seg['start']/1000}:end={seg['end']/1000},setpts={round(1.0/factor, 4)}*(PTS-STARTPTS)"
                    else:
                        vf = f"trim=start={seg['start']/1000}:end={seg['end']/1000},setpts=PTS-STARTPTS"

                    cmd_clip = f'ffmpeg -y -ss {seg["start"]/1000} -to {seg["end"]/1000} -i "{video_path}" -filter:v "{vf}" -an -c:v libx264 -preset ultrafast "{clip_out}" -loglevel quiet'
                    await asyncio.to_thread(os.system, cmd_clip)
                    video_clips.append(clip_out)

                sub_start = ms_to_srt_time(cur_ms)
                cur_ms += dur_ms
                sub_end = ms_to_srt_time(cur_ms)
                srt_lines.append(f"{idx+1}\n{sub_start} --> {sub_end}\n{text}\n")

                export_tasks[task_id] = {
                    "percent": 10 + int(((idx + 1) / total) * 65),
                    "status": f"အသံထွက်ထုတ်ယူပြီး Video Speed ညှိနေသည် ({idx+1}/{total})...",
                    "done": False
                }

            audio_out = os.path.join(user_dir, f"final_dub_{task_id}.mp3")
            final_audio.export(audio_out, format="mp3")

            srt_out = os.path.join(user_dir, f"sub_{task_id}.srt")
            with open(srt_out, "w", encoding="utf-8") as f:
                f.write("\n".join(srt_lines))

            if auto_speed_video and video_clips:
                concat_txt = os.path.join(user_dir, f"concat_{task_id}.txt")
                with open(concat_txt, "w", encoding="utf-8") as f:
                    for c in video_clips:
                        f.write(f"file '{os.path.basename(c)}'\n")
                merged_v = os.path.join(user_dir, f"merged_{task_id}.mp4")
                cmd_merge = f'ffmpeg -y -f concat -safe 0 -i "{concat_txt}" -c copy "{merged_v}" -loglevel quiet'
                await asyncio.to_thread(os.system, cmd_merge)
                in_v = merged_v
            else:
                in_v = video_path

            out_final = os.path.join(user_dir, f"final_{task_id}.mp4")
            esc_srt = srt_out.replace("\\", "/").replace(":", "\\:")
            sub_filter = f"subtitles='{esc_srt}':force_style='FontName={font_name},FontSize={font_size},PrimaryColour=&H00FFFF,OutlineColour=&H000000,BorderStyle=1,Outline=2'"

            vf_list = []
            if blur_sub:
                h_pct = blur_h / 100.0
                vf_list.append(f"split[v1][v2];[v2]crop=iw:ih*{h_pct}:0:ih*(1-{h_pct}),boxblur=20[bl];[v1][bl]overlay=0:H-h[v_bl]")
                cur_v = "[v_bl]"
            else:
                cur_v = "[0:v]"

            if burn_sub:
                vf_list.append(f"{cur_v}{sub_filter}[vout]")
                final_v = "[vout]"
            else:
                final_v = cur_v

            f_complex = ";".join(vf_list) if vf_list else ""
            if f_complex:
                filter_str = f'-filter_complex "{f_complex}" -map "{final_v}"'
            else:
                filter_str = '-map 0:v'

            cmd_final = f'ffmpeg -y -i "{in_v}" -i "{audio_out}" {filter_str} -map 1:a -c:v libx264 -preset fast -c:a aac -shortest "{out_final}" -loglevel quiet'
            await asyncio.to_thread(os.system, cmd_final)

            export_tasks[task_id] = {
                "percent": 100,
                "status": "ဗီဒီယို အောင်မြင်စွာ ဖန်တီးပြီးပါပြီ!",
                "done": True,
                "download_url": f"/api/download/{task_id}"
            }
        except Exception as e:
            export_tasks[task_id] = {"percent": 0, "status": f"Error: {str(e)}", "error": True, "done": True}

    asyncio.create_task(_process())
    return {"status": "started", "task_id": task_id}

@app.get("/api/download/{task_id}")
async def download_file(request: Request, task_id: str):
    user_email = request.cookies.get("user_email") or "guest"
    user_dir = get_user_dir(user_email.replace("@", "_").replace(".", "_"))
    path = os.path.join(user_dir, f"final_{task_id}.mp4")
    if os.path.exists(path):
        return FileResponse(path, media_type="video/mp4", filename="recap_video.mp4")
    return JSONResponse(status_code=404, content={"error": "File not found"})
