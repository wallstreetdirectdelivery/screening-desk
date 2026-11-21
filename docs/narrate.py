#!/usr/bin/env python3
"""Voice the walkthrough video with ElevenLabs.

    python3 docs/narrate.py voices                 # speak every line in narration.json into docs/.voice/
    node docs/record_demo.cjs                      # records, pacing itself to the clips, and logs when each line starts
    python3 docs/narrate.py mix docs/demo.webm docs/demo.mp4    # lays the clips over the recording

Needs ELEVENLABS_API_KEY in the environment or in ui/.env, and ffmpeg. ELEVENLABS_VOICE and ELEVENLABS_MODEL
choose the voice and model. Only used to make the video.
"""
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
VOICE_DIR = os.path.join(HERE, ".voice")


def env(name, default=None):
    if os.environ.get(name):
        return os.environ[name]
    path = os.path.join(HERE, "..", "ui", ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or default
    return default


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


def voices():
    key = env("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("ELEVENLABS_API_KEY is not set.")
    voice = env("ELEVENLABS_VOICE", "JBFqnCBsd6RMkjVDRZzb")
    model = env("ELEVENLABS_MODEL", "eleven_v4")
    lines = json.load(open(os.path.join(HERE, "narration.json"), encoding="utf-8"))
    os.makedirs(VOICE_DIR, exist_ok=True)
    index, total = {}, 0.0
    for caption, text in lines.items():
        name = hashlib.sha256((voice + model + text).encode("utf-8")).hexdigest()[:14] + ".mp3"
        path = os.path.join(VOICE_DIR, name)
        if not os.path.exists(path):
            body = json.dumps({"text": text, "model_id": model}).encode("utf-8")
            request = urllib.request.Request(
                "https://api.elevenlabs.io/v1/text-to-speech/{}?output_format=mp3_44100_128".format(voice), data=body, method="POST",
                headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    open(path, "wb").write(response.read())
            except urllib.error.HTTPError as exc:
                sys.exit("ElevenLabs returned HTTP {}: {}".format(exc.code, exc.read().decode("utf-8", "replace")[:300]))
        index[caption] = {"file": name, "dur": round(duration(path), 2)}
        total += index[caption]["dur"]
    json.dump(index, open(os.path.join(VOICE_DIR, "voice.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("{} lines voiced, {:.0f} seconds of speech, {} characters".format(len(index), total, sum(len(t) for t in lines.values())))


def mix(video, out):
    timeline = json.load(open(os.path.join(VOICE_DIR, "timeline.json"), encoding="utf-8"))
    start = float(os.environ.get("TRIM_START", "0.5"))   # seconds cut from the opening, while the first page loads
    command = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-i", video]
    parts = []
    for i, item in enumerate(timeline, start=1):
        command += ["-i", os.path.join(VOICE_DIR, item["file"])]
        at = max(0, int(item["at"] - start * 1000))
        parts.append("[{}:a]adelay={}|{}[a{}]".format(i, at, at, i))
    graph = ";".join(parts) + ";" + "".join("[a{}]".format(i) for i in range(1, len(timeline) + 1)) + \
        "amix=inputs={}:normalize=0:dropout_transition=0,volume=1.6[aout]".format(len(timeline))
    command += ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]", "-c:v", "libx264", "-preset", "slow", "-crf", "24",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    subprocess.run(command, check=True)
    print("Wrote {} with {} narration clips, {:.0f} seconds".format(out, len(timeline), duration(out)))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "voices":
        voices()
    elif len(sys.argv) == 4 and sys.argv[1] == "mix":
        mix(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
