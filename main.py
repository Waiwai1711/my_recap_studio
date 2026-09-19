import os
import sys
import json
import asyncio
import shutil
import uuid
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse

try:
    import audioop
except ModuleNotFoundError:
    try:
        import audioop_lts as audioop
        sys.modules['audioop'] = audioop
    except ModuleNotFoundError:
        pass

import assemblyai as aai
import edge_tts
from pydub import AudioSegment

app = FastAPI()

BASE_TEMP = "temp_process"
os.makedirs(BASE_TEMP, exist_ok=True)

export_tasks = {}

def get_user_dir(session_id: str):
    user_dir = os.path.join(BASE_TEMP, session_id)
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

def adjust_speed_and_fit(audio_path, target_duration_ms, output_path):
    try:
        audio = AudioSegment.from_file(audio_path)
    except Exception:
        audio = AudioSegment.silent(duration=max(500, target_duration_ms))

    current_ms = len(audio)
    if target_duration_ms <= 0:
        return audio

    if current_ms > target_duration_ms:
        speed = current_ms / target_duration_ms
        speed = min(max(speed, 0.5), 2.5)
        cmd = f'ffmpeg -y -i "{audio_path}" -filter:a "atempo={speed}" "{output_path}" -loglevel quiet'
        os.system(cmd)
        if os.path.exists(output_path):
            return AudioSegment.from_file(output_path)[:target_duration_ms]
        return audio[:target_duration_ms]
    else:
        return audio + AudioSegment.silent(duration=(target_duration_ms - current_ms))

async def generate_speech(text, voice, pitch_hz, rate_pct, output_file, ref_audio_path=None):
    clean_text = text.strip()
    if not clean_text:
        AudioSegment.silent(duration=500).export(output_file, format="mp3")
        return

    actual_voice = "my-MM-ThihaNeural" if "Thiha" in voice else "my-MM-NilarNeural"
    pitch_str = f"{pitch_hz:+d}Hz" if pitch_hz != 0 else "+0Hz"
    rate_str = f"{rate_pct:+d}%" if rate_pct != 0 else "+0%"
    try:
        communicate = edge_tts.Communicate(clean_text, actual_voice, pitch=pitch_str, rate=rate_str)
        await communicate.save(output_file)
    except Exception:
        AudioSegment.silent(duration=500).export(output_file, format="mp3")

@app.get("/", response_class=HTMLResponse)
async def serve_home():
    html_file = os.path.join("templates", "index.html")
    if not os.path.exists(html_file):
        return HTMLResponse("<h3>templates/index.html မတွေ့ရှိပါ။</h3>", status_code=404)
    return FileResponse(html_file)

@app.post("/api/upload-voice-sample")
async def upload_voice_sample(voice_file: UploadFile = File(...)):
    ext = os.path.splitext(voice_file.filename)[1] or ".wav"
    sample_id = f"sample_{int(asyncio.get_event_loop().time() * 1000)}{ext}"
    sample_path = os.path.join(BASE_TEMP, sample_id)

    with open(sample_path, "wb") as buffer:
        shutil.copyfileobj(voice_file.file, buffer)

    return {"status": "success", "voice_path": sample_path, "sample_id": sample_id}

@app.post("/api/transcribe")
async def transcribe_video(
    session_id: str = Form(...),
    api_key: str = Form(...), 
    video: UploadFile = File(...)
):
    api_key = api_key.strip()
    if not api_key:
        return JSONResponse(status_code=400, content={"error": "AssemblyAI API Key ထည့်သွင်းပေးပါခင်ဗျာ။"})

    user_dir = get_user_dir(session_id)
    video_path = os.path.join(user_dir, f"input_{video.filename}")
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(video.file, buffer)

    aai.settings.api_key = api_key
    try:
        config = aai.TranscriptionConfig(language_detection=True)
        transcriber = aai.Transcriber()
        transcript = transcriber.transcribe(video_path, config=config)

        if transcript.status == aai.TranscriptStatus.error:
            return JSONResponse(status_code=400, content={"error": transcript.error})

        segments = []
        sentences = transcript.get_sentences()
        for idx, s in enumerate(sentences):
            segments.append({
                "id": idx + 1,
                "start": s.start,
                "end": s.end,
                "duration": round((s.end - s.start) / 1000, 2),
                "text": s.text
            })

        return {"video_filename": f"input_{video.filename}", "segments": segments}
    except Exception as e:
        err_msg = str(e)
        if "11001" in err_msg or "getaddrinfo" in err_msg:
            return JSONResponse(status_code=500, content={"error": "အင်တာနက်လိုင်း ချိတ်ဆက်၍မရပါ (DNS/Connection Failed)။"})
        return JSONResponse(status_code=500, content={"error": err_msg})

@app.post("/api/preview-audio")
async def preview_audio(
    session_id: str = Form(...),
    text: str = Form(...),
    voice: str = Form(...),
    pitch: int = Form(0),
    speed: int = Form(0),
    ref_voice_path: str = Form(None)
):
    user_dir = get_user_dir(session_id)
    out_file = os.path.join(user_dir, f"preview_{uuid.uuid4().hex[:6]}.mp3")
    await generate_speech(text, voice, pitch, speed, out_file, ref_audio_path=ref_voice_path)
    return FileResponse(out_file, media_type="audio/mpeg")

@app.get("/api/progress/{task_id}")
async def progress_stream(task_id: str):
    async def event_generator():
        while True:
            data = export_tasks.get(task_id, {"percent": 0, "status": "စတင်လုပ်ဆောင်နေပါသည်...", "done": False})
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            if data.get("done") or data.get("error"):
                break
            await asyncio.sleep(0.4)
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/start-export")
async def start_export(
    task_id: str = Form(...),
    session_id: str = Form(...),
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
    user_dir = get_user_dir(session_id)

    async def _process():
        try:
            segments = json.loads(segments_json)
            translations = json.loads(translations_json)
            total = len(segments)
            video_path = os.path.join(user_dir, video_filename)

            export_tasks[task_id] = {"percent": 5, "status": "လုပ်ဆောင်မှု စတင်ပြင်ဆင်နေပါသည်...", "done": False}
            await asyncio.sleep(0.2)

            final_audio = AudioSegment.empty()
            srt_lines = []
            cur_timeline_ms = 0
            video_clips_paths = []

            for idx, (seg, mm_text) in enumerate(zip(segments, translations)):
                start_ms = seg["start"]
                end_ms = seg["end"]
                orig_dur_ms = end_ms - start_ms

                raw_file = os.path.join(user_dir, f"tts_{idx}.mp3")
                fit_file = os.path.join(user_dir, f"fit_{idx}.mp3")
                await generate_speech(mm_text, voice, pitch, speed, raw_file, ref_audio_path=ref_voice_path)

                # TTS အသံ ကြာချိန်ကို စစ်ဆေးခြင်း
                try:
                    gen_audio = AudioSegment.from_file(raw_file)
                    actual_audio_ms = len(gen_audio)
                except Exception:
                    actual_audio_ms = max(500, orig_dur_ms)
                    gen_audio = AudioSegment.silent(duration=actual_audio_ms)

                # Auto-Speed စနစ် (Narrator Video Speed Adjuster)
                if auto_speed_video:
                    clip_dur_ms = actual_audio_ms
                    final_audio += gen_audio

                    start_sec = start_ms / 1000.0
                    end_sec = end_ms / 1000.0
                    target_sec = max(0.4, actual_audio_ms / 1000.0)
                    orig_sec = max(0.4, orig_dur_ms / 1000.0)

                    clip_out = os.path.join(user_dir, f"clip_{idx}.mp4")

                    # မူရင်းပြကွက်က အသံထက် ပိုကြာနေပါက Video ကို Speed မြှင့်ခြင်း
                    if orig_sec > target_sec:
                        speed_factor = min(orig_sec / target_sec, 3.0)
                        pts_val = round(1.0 / speed_factor, 4)
                        vf = f"trim=start={start_sec}:end={end_sec},setpts={pts_val}*(PTS-STARTPTS)"
                    else:
                        vf = f"trim=start={start_sec}:end={end_sec},setpts=PTS-STARTPTS"

                    cmd_clip = f'ffmpeg -y -ss {start_sec} -to {end_sec} -i "{video_path}" -filter:v "{vf}" -an -c:v libx264 -preset ultrafast "{clip_out}" -loglevel quiet'
                    await asyncio.to_thread(os.system, cmd_clip)
                    video_clips_paths.append(clip_out)

                    # Subtitle SRT Timeline
                    sub_start = ms_to_srt_time(cur_timeline_ms)
                    cur_timeline_ms += clip_dur_ms
                    sub_end = ms_to_srt_time(cur_timeline_ms)
                    srt_lines.append(f"{idx+1}\n{sub_start} --> {sub_end}\n{mm_text}\n")
                else:
                    synced = adjust_speed_and_fit(raw_file, orig_dur_ms, fit_file)
                    final_audio += synced
                    sub_start = ms_to_srt_time(start_ms)
                    sub_end = ms_to_srt_time(end_ms)
                    srt_lines.append(f"{idx+1}\n{sub_start} --> {sub_end}\n{mm_text}\n")

                cur_pct = 10 + int(((idx + 1) / total) * 65)
                export_tasks[task_id] = {
                    "percent": cur_pct,
                    "status": f"ဗီဒီယို Speed ညှိပြီး အသံထုတ်နေသည် ({idx+1}/{total}) ကြောင်း...",
                    "done": False
                }

            export_tasks[task_id] = {"percent": 78, "status": "Subtitle ဖိုင် ဖန်တီးနေပါသည်...", "done": False}
            audio_out = os.path.join(user_dir, f"final_dub_{task_id}.mp3")
            final_audio.export(audio_out, format="mp3")

            out_srt = os.path.join(user_dir, f"output_{task_id}.srt")
            with open(out_srt, "w", encoding="utf-8") as f:
                f.write("\n".join(srt_lines))

            # Video Source ဆုံးဖြတ်ခြင်း (Auto-Speed Clip များကို ဆက်စပ်ခြင်း)
            if auto_speed_video and video_clips_paths:
                export_tasks[task_id] = {"percent": 82, "status": "Auto-Speed ဗီဒီယို အပိုင်းများကို ပေါင်းစပ်နေပါသည်...", "done": False}
                concat_txt = os.path.join(user_dir, f"concat_{task_id}.txt")
                with open(concat_txt, "w", encoding="utf-8") as f:
                    for cp in video_clips_paths:
                        c_name = os.path.basename(cp)
                        f.write(f"file '{c_name}'\n")
                
                merged_video = os.path.join(user_dir, f"merged_{task_id}.mp4")
                cmd_concat = f'ffmpeg -y -f concat -safe 0 -i "{concat_txt}" -c copy "{merged_video}" -loglevel quiet'
                await asyncio.to_thread(os.system, cmd_concat)
                input_v_src = merged_video
            else:
                input_v_src = video_path

            export_tasks[task_id] = {"percent": 88, "status": "FFmpeg Subtitle & Video Merge ပြုလုပ်နေပါသည်...", "done": False}

            out_video = os.path.join(user_dir, f"output_{task_id}.mp4")
            escaped_srt = out_srt.replace("\\", "/").replace(":", "\\:")
            sub_filter = f"subtitles='{escaped_srt}':force_style='FontName={font_name},FontSize={font_size},PrimaryColour=&H00FFFF,OutlineColour=&H000000,BorderStyle=1,Outline=2'"

            v_filters = []
            if blur_sub:
                h_pct = blur_h / 100.0
                v_filters.append(f"split[v1][v2];[v2]crop=iw:ih*{h_pct}:0:ih*(1-{h_pct}),boxblur=20[blurred];[v1][blurred]overlay=0:H-h[v_blurred]")
                prev_v = "[v_blurred]"
            else:
                prev_v = "[0:v]"

            if burn_sub:
                v_filters.append(f"{prev_v}{sub_filter}[vout]")
                final_v = "[vout]"
            else:
                final_v = prev_v

            filter_parts = []
            if v_filters:
                filter_parts.append(";".join(v_filters))

            vol_ratio = bgm_vol / 100.0
            if vol_ratio > 0:
                filter_parts.append(f"[0:a]volume={vol_ratio}[bgm];[bgm][1:a]amix=inputs=2:duration=first:dropout_transition=2[aout]")
                final_a = "[aout]"
            else:
                final_a = "1:a:0"

            full_filter = ";".join(filter_parts)
            cmd = f'ffmpeg -y -i "{input_v_src}" -i "{audio_out}" -filter_complex "{full_filter}" -map "{final_v}" -map "{final_a}" -c:v libx264 -preset fast -c:a aac -shortest "{out_video}" -loglevel quiet'
            await asyncio.to_thread(os.system, cmd)

            export_tasks[task_id] = {
                "percent": 100,
                "status": "🎉 အားလုံး ပြီးပြည့်စုံစွာ ပြီးစီးပါပြီ!",
                "done": True,
                "download_url": f"/api/download/{session_id}/{task_id}"
            }
        except Exception as e:
            export_tasks[task_id] = {"percent": 0, "status": f"Error: {str(e)}", "error": True, "done": True}

    asyncio.create_task(_process())
    return {"status": "started", "task_id": task_id}

@app.get("/api/download/{session_id}/{task_id}")
async def download_video(session_id: str, task_id: str):
    user_dir = get_user_dir(session_id)
    out_video = os.path.join(user_dir, f"output_{task_id}.mp4")
    if os.path.exists(out_video):
        return FileResponse(out_video, media_type="video/mp4", filename="recap_dubbed_output.mp4")
    return JSONResponse(status_code=404, content={"error": "File not found"})
