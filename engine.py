"""Local draft discovery, exact script matching, PCM editing and continuous SRT."""
from __future__ import annotations
import array
import dataclasses
import datetime as dt
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import unicodedata
import wave

RATE = 48000
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class UserError(Exception):
    pass


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


def read_json(path):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 80_000_000:
            return None
        return json.loads(raw.decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        return None


@dataclasses.dataclass
class Clip:
    text: str
    path: Path
    material_id: str
    original_start: float


@dataclasses.dataclass
class Candidate:
    source: Path
    location: str
    clips: list[Clip]
    fingerprint: str
    warnings: list[str]

    @property
    def label(self):
        preview = ' / '.join(c.text for c in self.clips[:2])
        return f'{len(self.clips)} 段 · {preview[:35]} · {self.location}'


def resolve_audio(root, raw_path):
    raw = str(raw_path or '').replace('\\', '/')
    p = Path(raw)
    # Prefer a same-named file in the selected draft after folder renaming.
    for candidate in (root / 'textReading' / p.name, root / p, p):
        if candidate.is_file():
            return candidate.resolve()
    return root / 'textReading' / p.name


def extract_candidate(root, data, source, location):
    mats = data.get('materials') or {}
    audio_materials = {m['id']: m for m in mats.get('audios', []) if m.get('id')}
    text_materials = {}
    for m in mats.get('texts', []):
        content = m.get('content', {})
        try:
            if isinstance(content, str):
                content = json.loads(content)
            text_materials[m['id']] = content.get('text', '')
        except (ValueError, TypeError):
            continue
    text_segments, audio_segments = [], []
    for track in data.get('tracks', []):
        if track.get('type') == 'text':
            text_segments.extend(track.get('segments', []))
        if track.get('type') == 'audio':
            for seg in track.get('segments', []):
                m = audio_materials.get(seg.get('material_id'), {})
                if m.get('type') == 'text_to_audio':
                    audio_segments.append(seg)
    if not audio_segments:
        return None
    audio_segments.sort(key=lambda s: s.get('target_timerange', {}).get('start', 0))
    text_segments.sort(key=lambda s: s.get('target_timerange', {}).get('start', 0))
    clip_by_id, clips, warnings = {}, [], []
    for seg in audio_segments:
        m = audio_materials[seg['material_id']]
        name = str(m.get('name') or '').strip()
        is_short = name.endswith(('...', '…'))
        if is_short:
            prefix = name.rstrip('.…')
            possible = {v for v in text_materials.values() if normalized(v).startswith(normalized(prefix))}
            if len(possible) == 1:
                name = possible.pop()
            else:
                warnings.append(f'配音名称被截断，无法唯一还原文案：{name}')
        if not name:
            warnings.append('有一段朗读音频没有文字名称。')
        c = Clip(name, resolve_audio(root, m.get('path')), m['id'],
                 seg.get('target_timerange', {}).get('start', 0) / 1e6)
        clips.append(c)
        clip_by_id.setdefault(normalized(name), []).append(c)
    # When subtitles and audio have a unique exact text match, subtitle order wins.
    used, ordered = set(), []
    for seg in text_segments:
        text = text_materials.get(seg.get('material_id'), '')
        matches = clip_by_id.get(normalized(text), [])
        if len(matches) == 1 and id(matches[0]) not in used:
            ordered.append(matches[0])
            used.add(id(matches[0]))
    if len(ordered) == len(clips):
        clips = ordered
    else:
        warnings.append('部分字幕与音频未能逐条唯一匹配，当前顺序按配音在时间线的位置排列，请核对文案。')
    for c in clips:
        if not c.path.is_file():
            warnings.append(f'缺少音频：{c.text}（{c.path.name}）')
    fingerprint = hashlib.sha256(json.dumps(
        [(c.material_id, c.text, c.path.name) for c in clips], ensure_ascii=False).encode()).hexdigest()
    return Candidate(source, location, clips, fingerprint, warnings)


def discover(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise UserError('请选择一个剪映草稿文件夹。')
    sources = []
    primary = root / 'draft_content.json'
    if primary.is_file():
        sources.append(primary)
    for dirname in ('Timelines', 'subdraft'):
        base = root / dirname
        if base.is_dir():
            sources.extend(sorted(base.glob('*/draft_content.json')))
    if not sources:
        raise UserError('这个文件夹里没有找到剪映草稿。请选择包含 draft_content.json 的那一层文件夹。')
    candidates, seen, unreadable = [], set(), []

    def walk(data, source, location, depth=0):
        if not isinstance(data, dict) or depth > 12:
            return
        candidate = extract_candidate(root, data, source, location)
        if candidate and candidate.fingerprint not in seen:
            seen.add(candidate.fingerprint)
            candidates.append(candidate)
        for i, m in enumerate((data.get('materials') or {}).get('drafts', []), 1):
            nested = m.get('draft')
            if isinstance(nested, str):
                try:
                    nested = json.loads(nested)
                except ValueError:
                    continue
            walk(nested, source, f'{location} / 复合片段{i}', depth + 1)

    for source in sources:
        data = read_json(source)
        if data is None:
            unreadable.append(str(source.relative_to(root)))
        else:
            walk(data, source, str(source.relative_to(root)))
    if not candidates:
        raise UserError('没有找到可读取的独立朗读配音。\n主草稿可能已加密，或只有混合后的音轨。\n请在剪映中将本次字幕和独立配音组成复合片段，保存后再读取。\n工具不会改写草稿，也不会使用旧备份冒充当前版本。')
    return candidates, unreadable


def locate_ffmpeg():
    hit = shutil.which('ffmpeg')
    if hit:
        return hit
    paths = []
    # Known installation roots plus registry-installed Jianying versions.
    bases = [
             Path(os.environ.get('LOCALAPPDATA', '')) / 'JianyingPro' / 'Apps',
             Path(os.environ.get('LOCALAPPDATA', '')) / 'CapCut' / 'Apps']
    try:
        import winreg
        for branch in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for keyname in (r'Software\Microsoft\Windows\CurrentVersion\Uninstall\JianyingPro',
                            r'Software\Microsoft\Windows\CurrentVersion\App Paths\JianyingPro.exe'):
                try:
                    with winreg.OpenKey(branch, keyname) as k:
                        for value in ('InstallLocation', ''):
                            try:
                                val, _ = winreg.QueryValueEx(k, value)
                                p = Path(val.strip('"'))
                                bases.append(p.parent if p.suffix.lower() == '.exe' else p)
                            except OSError:
                                pass
                except OSError:
                    pass
    except ImportError:
        pass
    for base in bases:
        if base.is_dir():
            paths.extend(base.glob('*/ffmpeg.exe'))
            if (base / 'ffmpeg.exe').is_file():
                paths.append(base / 'ffmpeg.exe')
    paths.sort(key=lambda p: tuple(int(n) for n in re.findall(r'\d+', p.parent.name)), reverse=True)
    return str(paths[0]) if paths else ''


@dataclasses.dataclass
class PlanRow:
    text: str
    pieces: list[int]
    pause_ms: int = 120
    speed: float = 1.0


def natural_pause(text):
    """Conservative speech rhythm; user can override every row."""
    t = normalized(text)
    if len(t) <= 3:
        return 80
    if t.endswith(('而至', '没劲', '趁早', '搭配', '营养', '磨损', '呵护', '基础', '免疫力', '保健食品', '42粒', '轻松')):
        return 220
    return 120


def make_plan(candidate, script):
    lines = [s.strip() for s in script.splitlines() if s.strip()]
    if not lines:
        raise UserError('请粘贴最终文案，每行对应一条字幕。')
    for n, line in enumerate(lines, 1):
        if any(unicodedata.category(c).startswith('P') for c in line):
            raise UserError(f'第 {n} 行含有标点。请点“去掉标点”，核对后再匹配。\n{line}')
        if not normalized(line):
            raise UserError(f'第 {n} 行没有可匹配的文字。')
    originals = [normalized(c.text) for c in candidate.clips]
    expected, actual = ''.join(originals), ''.join(normalized(s) for s in lines)
    if expected != actual:
        pos = next((i for i, (x, y) in enumerate(zip(expected, actual)) if x != y), min(len(expected), len(actual)))
        raise UserError(f'文案与配音文字不一致，尚未生成。\n第 {pos + 1} 个字符附近：\n配音文案：{expected[max(0,pos-8):pos+18]}\n你的文案：{actual[max(0,pos-8):pos+18]}\n\n可以改换行，但文字和顺序需一致。数字读法差异也请先核对。')
    ends, cursor = {}, 0
    for i, text in enumerate(originals):
        cursor += len(text)
        ends[cursor] = i + 1
    plan, char_pos, clip_pos = [], 0, 0
    for n, line in enumerate(lines, 1):
        char_pos += len(normalized(line))
        if char_pos not in ends:
            raise UserError(f'第 {n} 行的末尾落在同一段配音内部：\n{line}\n\n当前版本支持自动合并完整的朗读片段。\n要从一段音频中间拆句，请先在剪映中按新分行生成独立朗读，再读取草稿。工具不会按字数猜时间或切掉声音。')
        stop = ends[char_pos]
        plan.append(PlanRow(line, list(range(clip_pos, stop)), natural_pause(line)))
        clip_pos = stop
    return plan


def remove_punctuation(script):
    return ''.join(c for c in script if not unicodedata.category(c).startswith('P'))


def run_ffmpeg(exe, args, data=None):
    try:
        p = subprocess.run([exe, '-hide_banner', '-loglevel', 'error', *args], input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UserError(f'音频工具无法运行：{e}') from e
    if p.returncode:
        raise UserError('音频处理失败：\n' + p.stderr.decode('utf-8', errors='replace')[-1500:])
    return p.stdout


def decode(exe, path):
    raw = run_ffmpeg(exe, ['-i', str(path), '-map', '0:a:0', '-ac', '1', '-ar', str(RATE),
                              '-f', 's16le', '-acodec', 'pcm_s16le', 'pipe:1'])
    if len(raw) < 960:
        raise UserError(f'音频为空或太短：{path.name}')
    return raw


def trim_silence(raw, padding_ms=60):
    samples = array.array('h', raw)
    frame = RATE // 100
    rms = []
    for i in range(0, len(samples), frame):
        block = samples[i:i+frame]
        rms.append(math.sqrt(sum(x*x for x in block)/len(block))/32768)
    threshold = max(10 ** (-48/20), max(rms) * .008)
    active = [i for i, value in enumerate(rms) if value > threshold]
    if not active:
        raise UserError('有一段音频未检测到有效声音，请先试听原音频。')
    pad = round(padding_ms * RATE / 1000)
    left, right = max(0, active[0]*frame-pad), min(len(samples), (active[-1]+1)*frame+pad)
    clipped = samples[left:right]
    fade = min(round(.003*RATE), pad//2, len(clipped)//2)
    for i in range(fade):
        clipped[i] = round(clipped[i] * i/max(1,fade-1))
        clipped[-i-1] = round(clipped[-i-1] * i/max(1,fade-1))
    return clipped.tobytes(), left/RATE, (len(samples)-right)/RATE


def tempo(exe, raw, speed):
    if not .75 <= speed <= 1.5:
        raise UserError('语速请设为 0.75～1.50 倍，默认 1.00 倍。')
    if abs(speed - 1) < .0001:
        return raw
    return run_ffmpeg(exe, ['-f', 's16le', '-ar', str(RATE), '-ac', '1', '-i', 'pipe:0',
                            '-af', f'atempo={speed:.4f}', '-f', 's16le', '-acodec', 'pcm_s16le', 'pipe:1'], raw)


def write_wav(path, raw):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(raw)


def srt_time(sample):
    ms = round(sample * 1000 / RATE)
    return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'


def srt_text(plan, bounds):
    result = []
    for i, row in enumerate(plan):
        end = bounds[i+1][0] if i+1 < len(bounds) else bounds[i][1]
        result.append(f'{i+1}\n{srt_time(bounds[i][0])} --> {srt_time(end)}\n{row.text}')
    return '\n\n'.join(result)+'\n'


@dataclasses.dataclass
class Result:
    raw: bytes
    lines: list[bytes]
    bounds: list[tuple[int, int]]
    plan: list[PlanRow]
    report: dict


class Renderer:
    def __init__(self):
        self.cache = {}

    def render(self, candidate, plan, ffmpeg, inner_gap_ms=80, progress=None, cancel=None):
        if not Path(ffmpeg).is_file():
            raise UserError('没有找到 ffmpeg.exe，请在“音频工具”中选择剪映安装目录里的 ffmpeg.exe。')
        if not 0 <= inner_gap_ms <= 1000:
            raise UserError('行内短接缝应为 0～1000 毫秒。')
        buffers, lines, bounds, trims, cursor = [], [], [], [], 0
        for i, row in enumerate(plan):
            if cancel and cancel.is_set():
                raise UserError('已停止生成。')
            if not 0 <= row.pause_ms <= 3000:
                raise UserError(f'第 {i+1} 行的停顿应为 0～3000 毫秒。')
            pieces = []
            for k, index in enumerate(row.pieces):
                clip = candidate.clips[index]
                if not clip.path.is_file():
                    raise UserError(f'找不到这句的配音：{clip.text}\n{clip.path}')
                stat = clip.path.stat()
                key = (str(clip.path), stat.st_mtime_ns, stat.st_size, ffmpeg)
                if key not in self.cache:
                    decoded = decode(ffmpeg, clip.path)
                    trimmed, head, tail = trim_silence(decoded)
                    self.cache[key] = (trimmed, head, tail, len(decoded)//2, hashlib.sha256(clip.path.read_bytes()).hexdigest())
                trimmed, head, tail, original_samples, digest = self.cache[key]
                if k:
                    pieces.append(b'\0\0' * round(inner_gap_ms * RATE/1000))
                pieces.append(trimmed)
                trims.append({'text': clip.text, 'path': str(clip.path), 'sha256': digest,
                              'trim_head_seconds': head, 'trim_tail_seconds': tail,
                              'original_seconds': original_samples/RATE})
            audio = tempo(ffmpeg, b''.join(pieces), row.speed)
            size = len(audio)//2
            if size == 0:
                raise UserError(f'第 {i+1} 行处理后为空。')
            bounds.append((cursor, cursor+size))
            lines.append(audio)
            buffers.append(audio)
            cursor += size
            if i+1 < len(plan):
                gap = round(row.pause_ms * RATE/1000)
                buffers.append(b'\0\0'*gap)
                cursor += gap
            if progress:
                progress(i+1, len(plan), row.text)
        raw = b''.join(buffers)
        report = {'version': '1.0', 'candidate_source': str(candidate.source),
                  'candidate_location': candidate.location, 'candidate_fingerprint': candidate.fingerprint,
                  'duration_seconds': len(raw)/2/RATE, 'sample_rate': RATE,
                  'subtitle_count': len(plan), 'inner_gap_ms': inner_gap_ms,
                  'rows': [dict(dataclasses.asdict(row), start_seconds=b[0]/RATE, speech_end_seconds=b[1]/RATE,
                                subtitle_end_seconds=(bounds[i+1][0] if i+1<len(bounds) else b[1])/RATE)
                           for i,(row,b) in enumerate(zip(plan,bounds))],
                  'source_clips': trims}
        return Result(raw, lines, bounds, [dataclasses.replace(r, pieces=list(r.pieces)) for r in plan], report)


def export_result(result, folder):
    base = Path(folder).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    name = '配音字幕_' + dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    out = base / name
    suffix = 2
    while out.exists():
        out = base / f'{name}_{suffix}'
        suffix += 1
    out.mkdir()
    write_wav(out/'配音.wav', result.raw)
    (out/'字幕-连续显示.srt').write_text(srt_text(result.plan, result.bounds), encoding='utf-8-sig')
    (out/'逐行文案.txt').write_text('\n'.join(r.text for r in result.plan)+'\n', encoding='utf-8-sig')
    (out/'处理记录.json').write_text(json.dumps(result.report, ensure_ascii=False, indent=2), encoding='utf-8')
    return out
