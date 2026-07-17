#!/usr/bin/env python3
"""
worker.py — Motor de Cloe Courses Creator.
Consume la tabla `jobs` de Supabase y produce locución + vídeo con cortinillas,
sube el MP4 a Supabase Storage, actualiza estados y registra costes en `usage`.

Corre en el VPS (Coolify). Desde el VPS SÍ se alcanza ElevenLabs/HeyGen/etc.
Todos los secretos entran por variables de entorno (Coolify), nunca en el código.
"""
import os, time, tempfile, traceback, requests
import engine

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY  = os.environ["SUPABASE_SERVICE_KEY"]
ELEVEN_KEY   = os.environ.get("ELEVENLABS_API_KEY", "")
BUCKET       = os.environ.get("STORAGE_BUCKET", "media")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))

# Coste orientativo (ajústalo a tu tarifa real)
EUR_PER_1K_CHARS = float(os.environ.get("EUR_PER_1K_CHARS", "0.30"))
EUR_PER_RENDER_MIN = float(os.environ.get("EUR_PER_RENDER_MIN", "0.00"))

H = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
     "Content-Type": "application/json"}
REST = f"{SUPABASE_URL}/rest/v1"

def rest_get(path, params):
    r = requests.get(f"{REST}/{path}", headers=H, params=params, timeout=30)
    r.raise_for_status(); return r.json()

def rest_patch(path, params, body):
    r = requests.patch(f"{REST}/{path}", headers={**H, "Prefer":"return=minimal"},
                       params=params, json=body, timeout=30)
    r.raise_for_status()

def rest_insert(path, body):
    r = requests.post(f"{REST}/{path}", headers={**H, "Prefer":"return=minimal"},
                      json=body, timeout=30)
    r.raise_for_status()

def storage_upload(local_path, dest_path, content_type):
    with open(local_path, "rb") as f:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{dest_path}",
            headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
                     "Content-Type": content_type, "x-upsert": "true"},
            data=f, timeout=300)
    r.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{dest_path}"

def log_usage(api, unidad, cantidad, coste, ids):
    try:
        rest_insert("usage", {"api":api,"unidad":unidad,"cantidad":cantidad,"coste":coste,
                              "project_id":ids.get("project_id"),"course_id":ids.get("course_id"),
                              "video_id":ids.get("video_id")})
    except Exception as e:
        print("  ! usage log falló:", e)

def load_context(video_id):
    v = rest_get("videos", {"id":f"eq.{video_id}","select":"*"})[0]
    lesson = rest_get("lessons", {"id":f"eq.{v['lesson_id']}","select":"*"})[0]
    module = rest_get("modules", {"id":f"eq.{lesson['module_id']}","select":"*"})[0]
    course = rest_get("courses", {"id":f"eq.{module['course_id']}","select":"*"})[0]
    project = rest_get("projects", {"id":f"eq.{course['project_id']}","select":"*"})[0]
    return v, lesson, module, course, project

def do_locucion(v, lesson, project, ids, wd):
    if not ELEVEN_KEY: raise RuntimeError("Falta ELEVENLABS_API_KEY")
    audio = os.path.join(wd, "loc.mp3")
    chars = engine.tts_elevenlabs(lesson.get("guion","") or lesson.get("titulo",""),
                                  v.get("voz_id") or project.get("voice_id"),
                                  audio, ELEVEN_KEY)
    dest = f"audio/{v['id']}.mp3"
    url = storage_upload(audio, dest, "audio/mpeg")
    rest_patch("videos", {"id":f"eq.{v['id']}"}, {"audio_ref":url})
    log_usage("elevenlabs","caracteres",chars, round(chars/1000*EUR_PER_1K_CHARS,4), ids)
    # encola el render
    rest_insert("jobs", {"video_id":v["id"], "tipo":"render", "estado":"pendiente"})
    return url

def do_render(v, lesson, project, ids, wd):
    audio_ref = v.get("audio_ref")
    if not audio_ref: raise RuntimeError("Sin audio_ref; la locución debe ir antes")
    audio = os.path.join(wd, "in.mp3")
    with open(audio,"wb") as f: f.write(requests.get(audio_ref, timeout=120).content)
    intro = _fetch(project.get("intro_asset_url"), wd, "intro")
    outro = _fetch(project.get("outro_asset_url"), wd, "outro")
    music = _fetch(project.get("intro_music_url"), wd, "music.mp3")
    final, dur = engine.render_lesson(lesson, project, audio, wd,
                                      intro_path=intro, outro_path=outro, music_path=music)
    dest = f"video/{v['id']}.mp4"
    url = storage_upload(final, dest, "video/mp4")
    rest_patch("videos", {"id":f"eq.{v['id']}"}, {"render_url":url, "estado":"listo"})
    log_usage("render","minutos", round(dur/60,3), round(dur/60*EUR_PER_RENDER_MIN,4), ids)
    return url

def _fetch(url, wd, name):
    if not url: return None
    try:
        p = os.path.join(wd, name if "." in name else name+os.path.splitext(url)[1] or name)
        with open(p,"wb") as f: f.write(requests.get(url, timeout=120).content)
        return p
    except Exception:
        return None

HANDLERS = {"locucion": do_locucion, "render": do_render}

def process(job):
    jid, vid, tipo = job["id"], job["video_id"], job["tipo"]
    print(f"→ job {jid[:8]} tipo={tipo} video={str(vid)[:8]}")
    rest_patch("jobs", {"id":f"eq.{jid}"}, {"estado":"procesando"})
    try:
        v, lesson, module, course, project = load_context(vid)
        ids = {"project_id":project["id"], "course_id":course["id"], "video_id":v["id"]}
        rest_patch("videos", {"id":f"eq.{vid}"},
                   {"estado": "renderizando" if tipo=="render" else "en_cola"})
        with tempfile.TemporaryDirectory() as wd:
            HANDLERS.get(tipo, lambda *a: (_ for _ in ()).throw(
                RuntimeError(f"tipo no soportado: {tipo}")))(v, lesson, project, ids, wd)
        rest_patch("jobs", {"id":f"eq.{jid}"}, {"estado":"completado"})
        print(f"  ✓ ok")
    except Exception as e:
        att = int(job.get("intentos",0)) + 1
        estado = "fallido" if att >= MAX_ATTEMPTS else "pendiente"
        rest_patch("jobs", {"id":f"eq.{jid}"},
                   {"estado":estado, "intentos":att, "log":str(e)[:2000]})
        if estado == "fallido":
            try: rest_patch("videos", {"id":f"eq.{vid}"}, {"estado":"fallido"})
            except Exception: pass
        print("  ✗ error:", e); traceback.print_exc()

def main():
    print(f"Cloe motor arrancado. Supabase={SUPABASE_URL} bucket={BUCKET}")
    while True:
        try:
            jobs = rest_get("jobs", {"estado":"eq.pendiente","order":"created_at.asc","limit":"1"})
            if jobs:
                process(jobs[0])
            else:
                time.sleep(POLL_SECONDS)
        except Exception as e:
            print("loop error:", e); time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
