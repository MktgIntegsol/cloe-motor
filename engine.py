#!/usr/bin/env python3
"""
engine.py — Motor de vídeo de Cloe (render + locución).
Reutiliza el pipeline validado en el piloto: guion -> slides -> locución (ElevenLabs)
-> montaje ffmpeg con cortinilla de entrada/salida del proyecto -> MP4.

No depende de Supabase; recibe datos ya cargados. worker.py lo orquesta.
"""
import os, subprocess, tempfile, textwrap, math, requests
from PIL import Image, ImageDraw, ImageFont

FB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
W, H = 1920, 1080

def _hex(c, default=(99,102,241)):
    try:
        c = c.lstrip("#"); return tuple(int(c[i:i+2],16) for i in (0,2,4))
    except Exception:
        return default

# ---------- 1. Locución (ElevenLabs) ----------
def tts_elevenlabs(text, voice_id, out_path, api_key,
                   model="eleven_turbo_v2_5"):
    """Genera locución y la guarda en out_path (mp3). Devuelve nº de caracteres (para coste)."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    r = requests.post(url,
        headers={"xi-api-key": api_key, "content-type": "application/json"},
        json={"text": text, "model_id": model,
              "voice_settings": {"stability": 0.5, "similarity_boost": 0.4}},
        timeout=120)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)
    return len(text)

# ---------- 2. Slides desde el guion de la lección ----------
def build_slide(title, body_lines, brand, out_path, kicker=""):
    navy = _hex(brand.get("bg", "#0B1220"))
    primary = _hex(brand.get("color", "#6366F1"))
    gold = _hex(brand.get("accent", "#F59E0B"))
    white = (248,250,252); muted = (148,163,184)
    img = Image.new("RGB", (W,H), navy); d = ImageDraw.Draw(img)
    d.rectangle([0,0,W,10], fill=primary)
    if kicker:
        d.rectangle([96,150,108,200], fill=gold)
        d.text((140,158), kicker.upper(), font=ImageFont.truetype(FB,30), fill=muted)
    d.text((96,250), title, font=ImageFont.truetype(FB,64), fill=white)
    y = 400
    for ln in body_lines:
        d.text((96,y), ln, font=ImageFont.truetype(FR,38), fill=white)
        y += 70
    d.text((96,1010), brand.get("footer","").upper(), font=ImageFont.truetype(FR,24), fill=muted)
    img.save(out_path)

def bumper_frame(text_big, sub, brand, out_path, kicker=""):
    navy = _hex(brand.get("bg", "#0B1220"))
    primary = _hex(brand.get("color", "#6366F1"))
    gold = _hex(brand.get("accent", "#F59E0B"))
    white=(248,250,252); muted=(148,163,184)
    img = Image.new("RGB",(W,H),navy); d=ImageDraw.Draw(img)
    d.rectangle([0,0,W,10],fill=primary); d.rectangle([0,H-10,W,H],fill=primary)
    def center(t,f,y,fill):
        b=d.textbbox((0,0),t,font=f); d.text(((W-(b[2]-b[0]))/2,y),t,font=f,fill=fill)
    if kicker: center(kicker.upper(), ImageFont.truetype(FB,30), 405, primary)
    center(text_big, ImageFont.truetype(FB,110), 455, white)
    d.rectangle([(W/2)-90,610,(W/2)+90,618], fill=gold)
    center(sub, ImageFont.truetype(FR,34), 650, muted)
    img.save(out_path)

def _wrap(text, width=64):
    out=[]
    for para in text.split("\n"):
        out += textwrap.wrap(para, width) or [""]
    return out

# ---------- 3. Montaje ffmpeg (segmento imagen + audio, con fundidos) ----------
def _dur(path):
    o = subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration",
        "-of","csv=p=0",path]).decode().strip()
    return float(o)

def _seg(img, audio, out, pad="0x0B1220", lead=0.35, tail=0.75):
    d = _dur(audio) + lead + tail
    vf = (f"scale=1920:1080:force_original_aspect_ratio=decrease,"
          f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={pad},fps=30,format=yuv420p,"
          f"fade=t=in:st=0:d=0.4,fade=t=out:st={d-0.4:.2f}:d=0.4")
    af = (f"[1:a]adelay={int(lead*1000)}|{int(lead*1000)},apad,atrim=0:{d:.2f},"
          f"afade=t=out:st={d-0.5:.2f}:d=0.5,aresample=48000[a]")
    subprocess.run(["ffmpeg","-y","-loop","1","-i",img,"-i",audio,
        "-filter_complex",af,"-map","0:v","-map","[a]","-t",f"{d:.2f}","-vf",vf,
        "-c:v","libx264","-preset","medium","-crf","20","-pix_fmt","yuv420p","-r","30",
        "-c:a","aac","-b:a","160k","-ar","48000","-ac","2",out], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return d

def _bumper_seg(img, out, music=None, dur=3.0, pad="0x0B1220"):
    vf = (f"scale=1920:1080:force_original_aspect_ratio=decrease,"
          f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={pad},fps=30,format=yuv420p,"
          f"fade=t=in:st=0:d=0.4,fade=t=out:st={dur-0.4}:d=0.4")
    if music and os.path.exists(music):
        ain = ["-i", music]; amap = ["-map","1:a","-shortest"]
    else:  # sting sintético suave
        ain = ["-f","lavfi","-i",f"sine=frequency=392:duration={dur}"]
        amap = ["-map","1:a"]
    subprocess.run(["ffmpeg","-y","-loop","1","-i",img]+ain+
        ["-map","0:v"]+amap+["-t",f"{dur}","-vf",vf,
        "-c:v","libx264","-preset","medium","-crf","20","-pix_fmt","yuv420p","-r","30",
        "-c:a","aac","-b:a","160k","-ar","48000","-ac","2",out], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def concat(segments, out):
    inputs=[]; fc=""
    for i,s in enumerate(segments):
        inputs += ["-i", s]; fc += f"[{i}:v][{i}:a]"
    fc += f"concat=n={len(segments)}:v=1:a=1[v][a]"
    subprocess.run(["ffmpeg","-y"]+inputs+["-filter_complex",fc,"-map","[v]","-map","[a]",
        "-c:v","libx264","-preset","medium","-crf","20","-pix_fmt","yuv420p","-r","30",
        "-c:a","aac","-b:a","160k","-ar","48000","-ac","2","-movflags","+faststart",out],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ---------- 4. Orquestación de una lección ----------
def render_lesson(lesson, project, audio_path, workdir,
                  intro_path=None, outro_path=None, music_path=None):
    """
    lesson: {titulo, guion}
    project: {name, color, voice_id, footer, cortinilla music...}
    audio_path: mp3 de la locución (ya generado)
    Devuelve la ruta del MP4 final.
    """
    os.makedirs(workdir, exist_ok=True)
    brand = {"color": project.get("color","#6366F1"), "bg":"#0B1220",
             "accent":"#F59E0B", "footer": project.get("name","")}
    # slide de contenido a partir del guion
    slide = os.path.join(workdir,"slide.png")
    build_slide(lesson.get("titulo","Lección"), _wrap(lesson.get("guion",""))[:8],
                brand, slide, kicker=project.get("name",""))
    segs=[]
    # cortinilla intro (asset del proyecto o bumper generado)
    intro_seg = os.path.join(workdir,"intro.mp4")
    if intro_path and os.path.exists(intro_path):
        # normaliza el asset a 1920x1080 h264
        subprocess.run(["ffmpeg","-y","-i",intro_path,"-t","4",
            "-vf","scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0B1220,fps=30,format=yuv420p",
            "-c:v","libx264","-preset","medium","-crf","20","-pix_fmt","yuv420p","-r","30",
            "-c:a","aac","-b:a","160k","-ar","48000","-ac","2","-shortest",intro_seg],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        b = os.path.join(workdir,"bi.png")
        bumper_frame(project.get("name","CLOE"), project.get("tagline",""), brand, b, kicker="")
        _bumper_seg(b, intro_seg, music=music_path)
    segs.append(intro_seg)
    # cuerpo
    body = os.path.join(workdir,"body.mp4")
    _seg(slide, audio_path, body)
    segs.append(body)
    # cortinilla outro
    outro_seg = os.path.join(workdir,"outro.mp4")
    if outro_path and os.path.exists(outro_path):
        subprocess.run(["ffmpeg","-y","-i",outro_path,"-t","4",
            "-vf","scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0B1220,fps=30,format=yuv420p",
            "-c:v","libx264","-preset","medium","-crf","20","-pix_fmt","yuv420p","-r","30",
            "-c:a","aac","-b:a","160k","-ar","48000","-ac","2","-shortest",outro_seg],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        b = os.path.join(workdir,"bo.png")
        bumper_frame(project.get("name","CLOE"), "Continúa con la siguiente lección", brand, b)
        _bumper_seg(b, outro_seg, music=music_path)
    segs.append(outro_seg)
    final = os.path.join(workdir,"final.mp4")
    concat(segs, final)
    return final, _dur(final)
