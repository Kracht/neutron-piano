"""Build web sample sets for Neutron Piano: mono MP3, onset-trimmed, faded tails,
one gain per instrument so velocity layers and registers keep their relative level."""
import json, os, re, struct, subprocess, sys, tempfile
import numpy as np
from concurrent.futures import ThreadPoolExecutor
SR = 44100
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'samples')
# folder with pianos/<SF2 banks>, VCSL/ ; override with SAMPLE_SOURCES
SRC = os.environ.get('SAMPLE_SOURCES', os.path.expanduser('~/projects/Elysium/sources'))
VCSL = f'{SRC}/VCSL/Idiophones/Struck Idiophones'
NAMES = {'C':0,'C#':1,'D':2,'D#':3,'E':4,'F':5,'F#':6,'G':7,'G#':8,'A':9,'A#':10,'B':11}

def load(p):
    raw = subprocess.run(['ffmpeg','-v','error','-i',p,'-ac','1','-ar',str(SR),'-f','f32le','-'],capture_output=True,check=True).stdout
    return np.frombuffer(raw, np.float32).copy()

def midi_file(note, vel, hold):
    tpq = 480; ticks = int(hold * 2 * tpq)            # 120 bpm: 1 beat = 0.5 s
    def vlq(n):
        b = [n & 0x7f]; n >>= 7
        while n: b.insert(0, (n & 0x7f) | 0x80); n >>= 7
        return bytes(b)
    trk = vlq(0) + bytes([0x90, note, vel]) + vlq(ticks) + bytes([0x80, note, 0]) + vlq(0) + b'\xff\x2f\x00'
    return b'MThd' + struct.pack('>IHHH', 6, 0, 1, tpq) + b'MTrk' + struct.pack('>I', len(trk)) + trk

def render_sf2(sf2, note, vel, hold):
    with tempfile.TemporaryDirectory() as d:
        open(f'{d}/n.mid','wb').write(midi_file(note, vel, hold))
        subprocess.run(['fluidsynth','-ni','-R','0','-C','0','-g','1','-r',str(SR),'-O','float','-T','wav',
                        '-F',f'{d}/n.wav',sf2,f'{d}/n.mid'],capture_output=True,check=True)
        return load(f'{d}/n.wav')

def shape(x, length, fade):
    a = np.abs(x); thr = 0.02 * a.max()
    i = max(0, int(np.argmax(a > thr)) - int(0.002 * SR))  # onset minus 2 ms
    x = x[i:i + int(length * SR)].copy()
    x[:16] *= np.linspace(0, 1, 16)
    n = min(len(x), int(fade * SR)); x[-n:] *= np.linspace(1, 0, n) ** 2
    return x

def encode(x, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pcm = np.clip(x, -1, 1).astype(np.float32).tobytes()
    subprocess.run(['ffmpeg','-v','error','-y','-f','f32le','-ar',str(SR),'-ac','1','-i','-',
                    '-c:a','libmp3lame','-q:a','5',path],input=pcm,check=True)

def finish(name, items, meta):
    """items: list of (midi, layer, samples, tune_cents). Normalise instrument peak to -1 dBFS."""
    # each layer normalised on its own: the soft layer keeps its darker timbre,
    # but sits only 6 dB below the hard one; the page scales loudness by velocity
    target = {'a': -7, 'b': -1}
    g = {l: 10 ** (target[l] / 20) / max(np.abs(s).max() for _, ll, s, _ in items if ll == l) for l in target}
    def job(it):
        m, layer, s, tune = it
        f = f'{name}/{m}{layer}.mp3'; encode(s * g[layer], f'{OUT}/{f}')
        return {'n': m, 'l': layer, 't': round(tune, 1)}
    with ThreadPoolExecutor(8) as ex: zones = list(ex.map(job, items))
    zones.sort(key=lambda z: (z['n'], z['l']))
    meta.update(zones=zones); return meta

def piano(name, sf2, vels):
    notes = list(range(21, 109, 3))
    def one(a):
        m, layer, v = a
        L = float(np.clip(10 - (m - 21) * 0.085, 2.5, 10))
        return (m, layer, shape(render_sf2(sf2, m, v, L), L, min(2.0, L * 0.35)), 0.0)
    with ThreadPoolExecutor(4) as ex:
        items = list(ex.map(one, [(m, l, v) for m in notes for l, v in vels.items()]))
    return finish(name, items, {})

def vcsl(name, pattern, layers, length, offset=12):
    items = []
    for f in sorted(os.listdir(pattern)):
        for layer, tag in layers.items():
            mt = re.search(tag, f)
            if not mt: continue
            m = (int(mt.group(2)) + 1) * 12 + NAMES[mt.group(1)] + offset
            x = load(f'{pattern}/{f}')
            items.append((m, layer, shape(x, length, length * 0.4), 0.0))
    return finish(name, items, {})

which = sys.argv[1:] or ['grand', 'upright', 'glock', 'vibes']
man = {}
if 'grand' in which:
    man['grand'] = piano('grand', f'{SRC}/pianos/SalamanderGrandPiano-SF2-V3+20200602/SalamanderGrandPiano-V3+20200602.sf2', {'a': 52, 'b': 104})
if 'upright' in which:
    man['upright'] = piano('upright', f'{SRC}/pianos/UprightPianoKW-SF2-20220221/UprightPianoKW-20220221.sf2', {'a': 52, 'b': 104})
if 'glock' in which:
    man['glock'] = vcsl('glock', f'{VCSL}/Glockenspiel', {'a': r'glock_medium_([A-G]#?)(\d)_', 'b': r'glock_loud_([A-G]#?)(\d)_'}, 4.0)
if 'vibes' in which:
    man['vibes'] = vcsl('vibes', f'{VCSL}/Vibraphone/Soft Mallets', {'a': r'Vibes_soft_([A-G]#?)(\d)_v1_', 'b': r'Vibes_soft_([A-G]#?)(\d)_v2_'}, 6.0)
p = f'{OUT}/manifest.json'
old = json.load(open(p)) if os.path.exists(p) else {}
old.update(man); json.dump(old, open(p, 'w'), separators=(',', ':'))
print({k: len(v['zones']) for k, v in old.items()})
