"""Speak text with Kokoro (local TTS on the GPU). Runs in .venv-voice:  .venv-voice/bin/python say.py "text" """
import subprocess, sys, tempfile
from kokoro import KPipeline
import numpy as np, soundfile as sf

text = " ".join(sys.argv[1:]) or "Hello"
pipe = KPipeline(lang_code="a")
audio = np.concatenate([a for _, _, a in pipe(text, voice="am_michael")])
with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
    sf.write(f.name, audio, 24000)
    subprocess.run(["aplay", "-q", f.name])
