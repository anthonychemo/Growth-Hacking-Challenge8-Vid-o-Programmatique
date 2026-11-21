#!/usr/bin/env python3
"""
ESIEA — Sujet 8 : Vidéos programmatiques — worker de rendu (étapes 3, 4 et 5).

Exécuté dans GitHub Actions (cloud, aucun clic manuel) ou en local pour tester.

Flux :
  1. get_job   : récupère le script JSON validé + l'URL d'upload Drive auprès de l'Apps Script
                 (rien de personnel ne transite par les inputs/logs du workflow).
  2. TTS       : edge-tts (voix neuronale gratuite) scène par scène + horodatage des mots.
  3. Rendu     : visuels générés en code (Pillow) à 24 i/s, sous-titres incrustés synchronisés
                 sur l'audio, piste audio assemblée, encodage H.264 via ffmpeg.
  4. Upload    : PUT sur la session d'upload résumable Drive créée par l'Apps Script
                 (le fichier appartient donc au propriétaire de la Sheet, sans OAuth côté GitHub).
  5. done/fail : callback vers l'Apps Script qui écrit l'URL, les logs et crée le brouillon Gmail.

Secrets attendus (variables d'environnement, jamais dans le code) :
  WEBHOOK_URL    URL /exec de l'application Web Apps Script
  WEBHOOK_TOKEN  jeton partagé (PropertiesService côté Apps Script)

Test local sans réseau :
  python render.py --job ../samples/sample_job.json --out /tmp/demo.mp4 --tts fake --no-upload
"""
import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

FPS = 24
LEAD_S = 0.4            # silence avant la 1re scène
GAP_S = 0.35            # respiration entre deux scènes (fondu)
TAIL_S = 0.7            # plan final après la dernière phrase
MAX_VIDEO_S = 60.0      # contrainte de l'énoncé
TARGET_MIN_S, TARGET_MAX_S = 47.0, 57.5   # fenêtre visée (marge de sécurité sous 60 s)

THEMES = {
    "A": dict(bg1=(10, 20, 38), bg2=(16, 52, 96), accent=(56, 189, 248), accent2=(129, 140, 248),
              label="Proposition d'équipe"),
    "B": dict(bg1=(26, 12, 44), bg2=(72, 24, 104), accent=(251, 146, 60), accent2=(244, 114, 182),
              label="Candidature"),
}
WHITE, MUTED = (245, 247, 250), (170, 180, 200)

FONT_CANDIDATES = {
    "bold": ["/usr/share/fonts/opentype/inter/Inter-Bold.otf", "/usr/share/fonts/truetype/inter/Inter-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "semi": ["/usr/share/fonts/opentype/inter/Inter-SemiBold.otf", "/usr/share/fonts/truetype/inter/Inter-SemiBold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "reg": ["/usr/share/fonts/opentype/inter/Inter-Regular.otf", "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}


def log(msg):
    print(f"[render] {msg}", flush=True)


# ----------------------------------------------------------------------------- webhook Apps Script

class Webhook:
    """Client minimal du webhook Apps Script (POST JSON authentifié par jeton)."""

    def __init__(self):
        self.url = os.environ.get("WEBHOOK_URL", "")
        self.token = os.environ.get("WEBHOOK_TOKEN", "")

    def call(self, action, **data):
        import requests
        if not self.url or not self.token:
            raise RuntimeError("WEBHOOK_URL / WEBHOOK_TOKEN absents de l'environnement")
        last = None
        for attempt in range(3):
            try:
                # Apps Script répond par une redirection 302 vers l'URL de résultat : requests la suit.
                r = requests.post(self.url, data=json.dumps({"token": self.token, "action": action, **data}),
                                  headers={"Content-Type": "text/plain;charset=utf-8"}, timeout=120)
                j = r.json()
                if not j.get("ok"):
                    raise RuntimeError(f"webhook {action} refusé : {j.get('error')}")
                return j
            except Exception as e:  # noqa: BLE001 - on réessaie puis on remonte
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"webhook {action} injoignable : {last}")


# ----------------------------------------------------------------------------- audio / TTS

def ffprobe_duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "default=nw=1:nk=1", str(path)], text=True)
    return float(out.strip())


def to_wav(src, dst):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", "48000", "-ac", "1", str(dst)], check=True)


async def tts_edge(text, voice, rate, out_mp3):
    """Synthèse neuronale gratuite (edge-tts). Retourne les bornes de mots [(début, fin, mot)] en secondes."""
    import edge_tts
    last = None
    for attempt in range(3):
        try:
            try:
                com = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
            except TypeError:  # anciennes versions : WordBoundary est le comportement par défaut
                com = edge_tts.Communicate(text, voice, rate=rate)
            events = []
            with open(out_mp3, "wb") as f:
                async for ch in com.stream():
                    if ch["type"] == "audio":
                        f.write(ch["data"])
                    elif ch["type"] == "WordBoundary":
                        events.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7, ch["text"]))
            if os.path.getsize(out_mp3) < 1000:
                raise RuntimeError("audio vide")
            return events
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"edge-tts essai {attempt + 1}/3 : {e}")
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"edge-tts indisponible : {last}")


def tts_fake(text, rate, out_mp3):
    """Faux TTS pour tests hors-ligne : silence de durée ≈ 2,6 mots/s (aucun appel réseau)."""
    n = len(text.split())
    factor = 1 + float(rate.replace("%", "")) / 100.0
    dur = max(1.0, n / (2.6 * factor))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                    "-t", f"{dur:.3f}", "-q:a", "9", str(out_mp3)], check=True)
    return []


def word_timings(text, events, duration):
    """Associe chaque mot du texte original (ponctuation incluse) à un intervalle [début, fin].
    Si le nombre d'événements TTS correspond, on s'appuie dessus ; sinon répartition pondérée."""
    tokens = text.split()
    if events and len(events) == len(tokens):
        return [(ev[0], ev[1], tok) for ev, tok in zip(events, tokens)]
    weights = []
    for tok in tokens:
        w = len(re.sub(r"\W", "", tok)) + 2
        if re.search(r"[,;:]$", tok):
            w += 3
        if re.search(r"[.!?…]$", tok):
            w += 5
        weights.append(w)
    total, t, out = sum(weights), 0.0, []
    for tok, w in zip(tokens, weights):
        d = duration * w / total
        out.append((t, t + d, tok))
        t += d
    return out


def make_chunks(words, max_words=8, max_chars=78):
    """Découpe en sous-titres courts : coupe sur la ponctuation ou à ~8 mots."""
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        txt = " ".join(x[2] for x in cur)
        punct = re.search(r"[,;:.!?…]$", w[2]) is not None
        if (punct and len(cur) >= 3) or len(cur) >= max_words or len(txt) >= max_chars:
            chunks.append(cur)
            cur = []
    if cur:
        if chunks and len(cur) < 3:
            chunks[-1].extend(cur)
        else:
            chunks.append(cur)
    return [(c[0][0], c[-1][1], " ".join(x[2] for x in c)) for c in chunks]


def synthesize(scenes, voice, rate, workdir, mode):
    """Synthétise toutes les scènes. Retourne [{wav, dur, words}]."""
    res = []
    for i, sc in enumerate(scenes):
        text = sc["texte_voix_off"].strip()
        mp3 = Path(workdir) / f"scene{i + 1}.mp3"
        events = tts_fake(text, rate, mp3) if mode == "fake" else asyncio.run(tts_edge(text, voice, rate, mp3))
        wav = Path(workdir) / f"scene{i + 1}.wav"
        to_wav(mp3, wav)
        dur = ffprobe_duration(wav)
        res.append(dict(wav=wav, dur=dur, words=word_timings(text, events, dur)))
    return res


def video_length(audios):
    return LEAD_S + sum(a["dur"] for a in audios) + GAP_S * (len(audios) - 1) + TAIL_S


def fit_rate(scenes, voice, workdir, mode):
    """Calibre le débit pour que la vidéo reste dans 47–57,5 s (jamais > 60 s)."""
    rate = "+0%"
    audios = synthesize(scenes, voice, rate, workdir, mode)
    total = video_length(audios)
    log(f"durée à débit normal : {total:.1f}s")
    if total > TARGET_MAX_S or total < TARGET_MIN_S:
        speech = total - (LEAD_S + GAP_S * (len(audios) - 1) + TAIL_S)
        target_speech = (TARGET_MAX_S if total > TARGET_MAX_S else TARGET_MIN_S) - (LEAD_S + GAP_S * (len(audios) - 1) + TAIL_S)
        pct = int(math.ceil((speech / target_speech - 1) * 100)) if total > TARGET_MAX_S else int(math.floor((speech / target_speech - 1) * 100))
        pct = max(-8, min(20, pct))
        rate = f"{pct:+d}%"
        log(f"recalibrage du débit TTS : {rate}")
        audios = synthesize(scenes, voice, rate, workdir, mode)
        total = video_length(audios)
        log(f"durée après recalibrage : {total:.1f}s")
    if total > MAX_VIDEO_S:
        raise RuntimeError(f"Vidéo trop longue ({total:.1f}s > 60s) même après accélération : raccourcir le script")
    return rate, audios, total


def build_audio_track(audios, total, out_wav):
    """Assemble les scènes avec les silences (module wave, sans dépendance supplémentaire)."""
    rate_hz = 48000
    with wave.open(str(out_wav), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate_hz)
        silence = lambda s: b"\x00\x00" * int(rate_hz * s)  # noqa: E731
        out.writeframes(silence(LEAD_S))
        for i, a in enumerate(audios):
            with wave.open(str(a["wav"]), "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))
            out.writeframes(silence(GAP_S if i < len(audios) - 1 else TAIL_S))


# ----------------------------------------------------------------------------- visuels

def load_font(kind, size):
    for p in FONT_CANDIDATES[kind]:
        if os.path.exists(p):
            return ImageFont.truetype(p, int(size))
    return ImageFont.load_default()


def ease(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def text_layer(text, font, fill, max_w, spacing=1.18, align="left"):
    """Rend un bloc de texte multi-lignes sur calque RGBA transparent."""
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = wrap(probe, text, font, max_w)
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * spacing)
    w = int(max(probe.textlength(l, font=font) for l in lines)) + 8
    layer = Image.new("RGBA", (w, lh * len(lines) + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i, l in enumerate(lines):
        x = 4 if align == "left" else (w - probe.textlength(l, font=font)) / 2
        d.text((x, 4 + i * lh), l, font=font, fill=fill)
    return layer


def paste(base, layer, xy, alpha=1.0):
    """Colle un calque RGBA sur une base RGB avec opacité globale."""
    if alpha <= 0.01:
        return
    a = layer.getchannel("A")
    if alpha < 0.99:
        a = a.point(lambda v: int(v * alpha))
    base.paste(layer.convert("RGB"), (int(xy[0]), int(xy[1])), a)


class Renderer:
    def __init__(self, script, option, resolution, audios):
        self.W, self.H = (1920, 1080) if resolution == "1080p" else (1280, 720)
        self.s = self.W / 1920.0
        self.th = THEMES.get(option, THEMES["B"])
        self.scenes = script["scenes"]
        self.n = len(self.scenes)
        s = self.s
        # --- chronologie
        self.t_scene, t = [], LEAD_S
        for i, a in enumerate(audios):
            self.t_scene.append((t - 0.25 if i else 0.0, t + a["dur"] + (GAP_S if i < self.n - 1 else TAIL_S)))
            t += a["dur"] + GAP_S
        self.total = video_length(audios)
        # sous-titres (horodatage absolu)
        self.subs, t = [], LEAD_S
        for a in audios:
            for (b, e, txt) in make_chunks(a["words"]):
                self.subs.append((t + b, t + e + 0.12, txt))
            t += a["dur"] + GAP_S
        # --- polices
        self.f_title = load_font("bold", 82 * s)
        self.f_chip = load_font("semi", 38 * s)
        self.f_head = load_font("semi", 28 * s)
        self.f_sub = load_font("semi", 46 * s)
        self.f_num = load_font("bold", 250 * s)
        # --- fond (dégradé + halos), 1.25× la taille de l'image pour le mouvement lent
        self.bg = self._make_bg()
        # --- calques par scène
        label = f"{self.th['label']} · {script.get('poste_cible') or script.get('titre_video', '')}".strip(" ·")
        self.head_label = text_layer(label.upper()[:90], self.f_head, MUTED + (255,), self.W * 0.7)
        self.layers = []
        for i, sc in enumerate(self.scenes):
            title = text_layer(sc["titre_ecran"][:70], self.f_title, WHITE + (255,), 1080 * s, spacing=1.12)
            chips = [self._chip(k[:30]) for k in (sc.get("mots_cles_ecran") or [])[:4]]
            num = text_layer(f"{i + 1:02d}", self.f_num, self.th["accent"] + (120,), 700 * s)
            tag = text_layer(f"SCÈNE {i + 1}/{self.n}", self.f_head, self.th["accent"] + (255,), 600 * s)
            self.layers.append(dict(title=title, chips=chips, num=num, tag=tag))
        self.sub_layers = [self._sub(txt) for (_, _, txt) in self.subs]
        self.ring = self._make_ring()

    # -- éléments précalculés
    def _make_bg(self):
        W, H = int(self.W * 1.25), int(self.H * 1.25)
        c1, c2 = self.th["bg1"], self.th["bg2"]
        big = Image.linear_gradient("L").resize((int(W * 1.6), int(W * 1.6))).rotate(35, resample=Image.BICUBIC)
        gx, gy = (big.width - W) // 2, (big.height - H) // 2
        grad = big.crop((gx, gy, gx + W, gy + H))  # recadrage central : aucun coin vide après rotation
        base = Image.composite(Image.new("RGB", (W, H), c2), Image.new("RGB", (W, H), c1), grad)
        glow = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(glow)
        for (cx, cy, r, col) in [(0.78, 0.30, 0.34, self.th["accent"]), (0.25, 0.85, 0.30, self.th["accent2"]),
                                 (0.55, 0.60, 0.22, self.th["accent"])]:
            d.ellipse([cx * W - r * H, cy * H - r * H, cx * W + r * H, cy * H + r * H], fill=tuple(int(v * 0.33) for v in col))
        glow = glow.filter(ImageFilter.GaussianBlur(120 * self.s))
        return ImageChops.add(base, glow)

    def _chip(self, text):
        s = self.s
        probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        tw = int(probe.textlength(text, font=self.f_chip))
        padx, pady = int(30 * s), int(16 * s)
        asc, desc = self.f_chip.getmetrics()
        w, h = tw + 2 * padx, asc + desc + 2 * pady
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=self.th["accent"] + (40,),
                            outline=self.th["accent"] + (255,), width=max(2, int(3 * s)))
        d.text((padx, pady), text, font=self.f_chip, fill=WHITE + (255,))
        return layer

    def _sub(self, text):
        s = self.s
        probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        lines = wrap(probe, text, self.f_sub, 1500 * s)[:2]
        asc, desc = self.f_sub.getmetrics()
        lh = int((asc + desc) * 1.15)
        w = int(max(probe.textlength(l, font=self.f_sub) for l in lines)) + int(90 * s)
        h = lh * len(lines) + int(44 * s)
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(26 * s), fill=(6, 8, 18, 190))
        for i, l in enumerate(lines):
            lw = probe.textlength(l, font=self.f_sub)
            d.text(((w - lw) / 2, int(22 * s) + i * lh), l, font=self.f_sub, fill=WHITE + (255,))
        return layer

    def _make_ring(self):
        s = self.s
        size = int(620 * s)
        layer = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))  # 2× pour l'anti-aliasing
        d = ImageDraw.Draw(layer)
        c, r = size, int(size * 0.92)
        for k in range(36):
            a0 = k * 10
            col = (self.th["accent"] if k % 3 else self.th["accent2"]) + (200,)
            d.arc([c - r, c - r, c + r, c + r], a0, a0 + 6, fill=col, width=int(14 * s))
        r2 = int(size * 0.70)
        d.ellipse([c - r2, c - r2, c + r2, c + r2], outline=self.th["accent2"] + (90,), width=int(6 * s))
        return layer.resize((size, size), Image.LANCZOS)

    # -- rendu d'une image
    def frame(self, t):
        W, H, s = self.W, self.H, self.s
        # fond animé (dérive lente)
        bw, bh = self.bg.size
        ox = int((bw - W) * (0.5 + 0.5 * math.sin(t / 9.0)))
        oy = int((bh - H) * (0.5 + 0.5 * math.cos(t / 11.0)))
        img = self.bg.crop((ox, oy, ox + W, oy + H))
        d = ImageDraw.Draw(img)
        # barre de progression
        d.rectangle([0, 0, W, int(8 * s)], fill=(255, 255, 255, 0) if False else (30, 36, 56))
        d.rectangle([0, 0, int(W * min(1.0, t / self.total)), int(8 * s)], fill=self.th["accent"])
        # scène courante
        idx = next((i for i, (a, b) in enumerate(self.t_scene) if t < b), self.n - 1)
        a0, a1 = self.t_scene[idx]
        lt = t - max(a0, 0.0)
        fade = min(ease(lt / 0.5), ease((a1 - t) / 0.3) if idx < self.n - 1 else 1.0)
        L = self.layers[idx]
        mx = int(120 * s)
        paste(img, self.head_label, (mx, int(58 * s)), min(1.0, fade * 1.0))
        paste(img, L["tag"], (W - mx - L["tag"].width, int(58 * s)), fade)
        cx, cy = W - mx - self.ring.width // 2 - int(20 * s), int(540 * s)   # centre du motif (anneau + numéro)
        rot = self.ring.rotate(-t * 22, resample=Image.BICUBIC)
        paste(img, rot, (cx - rot.width // 2, cy - rot.height // 2), fade * 0.9)
        paste(img, L["num"], (cx - L["num"].width // 2, cy - L["num"].height // 2 - int(10 * s)), fade)
        # titre : glisse du bas + fondu
        e = ease(lt / 0.55)
        ty = int(300 * s + (1 - e) * 46 * s)
        paste(img, L["title"], (mx, ty), min(fade, e))
        # mots-clés : apparition échelonnée
        x, y = mx, ty + L["title"].height + int(36 * s)
        for k, chip in enumerate(L["chips"]):
            if x + chip.width > mx + 1100 * s:
                x, y = mx, y + chip.height + int(18 * s)
            ek = ease((lt - (0.7 + 0.32 * k)) / 0.35)
            paste(img, chip, (x, y + (1 - ek) * 20 * s), min(fade, ek))
            x += chip.width + int(20 * s)
        # sous-titre synchronisé sur la voix
        for j, (b, e2, _) in enumerate(self.subs):
            if b <= t < e2:
                lay = self.sub_layers[j]
                sa = min(1.0, (t - b) / 0.10 + 0.2, (e2 - t) / 0.10 + 0.0)
                paste(img, lay, ((W - lay.width) // 2, H - lay.height - int(64 * s)), max(0.0, sa))
                break
        return img


def render_video(renderer, audio_wav, out_mp4):
    W, H = renderer.W, renderer.H
    n_frames = int(math.ceil(renderer.total * FPS))
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-i", str(audio_wav), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-t", f"{renderer.total:.3f}",
           "-movflags", "+faststart", str(out_mp4)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t0 = time.time()
    for i in range(n_frames):
        p.stdin.write(renderer.frame(i / FPS).tobytes())
        if i % (FPS * 10) == 0:
            log(f"images {i}/{n_frames} ({time.time() - t0:.0f}s)")
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError("ffmpeg a échoué")


# ----------------------------------------------------------------------------- upload Drive

def upload_resumable(session_url, path):
    """PUT du MP4 sur la session résumable créée par l'Apps Script (URI pré-autorisée, valable 7 j)."""
    import requests
    size = os.path.getsize(path)
    last = None
    for attempt in range(3):
        try:
            with open(path, "rb") as f:
                r = requests.put(session_url, data=f, timeout=600,
                                 headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
            if r.status_code in (200, 201):
                return r.json()["id"]
            last = f"HTTP {r.status_code} {r.text[:200]}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        log(f"upload essai {attempt + 1}/3 : {last}")
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"upload Drive impossible : {last}")


# ----------------------------------------------------------------------------- main

def validate_script(script):
    sc = script.get("scenes") or []
    if not 3 <= len(sc) <= 6:
        raise RuntimeError("script invalide : 3 à 6 scènes attendues")
    for s in sc:
        for k in ("scene_id", "titre_ecran", "mots_cles_ecran", "texte_voix_off"):
            if k not in s:
                raise RuntimeError(f"script invalide : champ manquant {k}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", help="job JSON local (sinon get_job via le webhook)")
    ap.add_argument("--out", default="out.mp4")
    ap.add_argument("--tts", choices=["edge", "fake"], default="edge")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--frames-preview", help="dossier où écrire 4 images de contrôle (PNG)")
    args = ap.parse_args()

    run_id = os.environ.get("RUN_ID", "LOCAL")
    hook = None if (args.job and args.no_upload) else Webhook()
    try:
        if args.job:
            job = json.load(open(args.job, encoding="utf-8"))
        else:
            run_id = os.environ["RUN_ID"]
            job = hook.call("get_job", run_id=run_id)["job"]
        script = job["script"]
        validate_script(script)
        option = job.get("option", "B")
        with tempfile.TemporaryDirectory() as tmp:
            rate, audios, total = fit_rate(script["scenes"], job.get("voice", "fr-FR-HenriNeural"), tmp, args.tts)
            wav = Path(tmp) / "track.wav"
            build_audio_track(audios, total, wav)
            r = Renderer(script, option, job.get("resolution", "1080p"), audios)
            if args.frames_preview:
                Path(args.frames_preview).mkdir(parents=True, exist_ok=True)
                for k, t in enumerate([1.2, total * 0.3, total * 0.55, total * 0.9]):
                    r.frame(t).save(Path(args.frames_preview) / f"preview{k + 1}.png")
            render_video(r, wav, args.out)
        size_mb = os.path.getsize(args.out) / 1e6
        log(f"vidéo prête : {args.out} ({size_mb:.1f} Mo, {total:.1f}s, débit {rate})")
        if args.no_upload:
            return
        file_id = upload_resumable(job["upload_url"], args.out)
        hook.call("done", run_id=run_id, file_id=file_id, duree_audio_sec=round(sum(a["dur"] for a in audios), 1),
                  duree_video_sec=round(total, 1), debit_tts=rate, taille_mo=round(size_mb, 2))
        log("callback done envoyé")
    except Exception as e:  # noqa: BLE001 — toute erreur doit remonter dans la Sheet (statut ERREUR)
        log(f"ERREUR : {e}")
        if hook and run_id != "LOCAL":
            try:
                hook.call("fail", run_id=run_id, message=str(e)[:400])
            except Exception as e2:  # noqa: BLE001
                log(f"callback fail impossible : {e2}")
        sys.exit(1)


if __name__ == "__main__":
    main()
