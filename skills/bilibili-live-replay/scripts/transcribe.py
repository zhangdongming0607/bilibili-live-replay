#!/usr/bin/env python3
"""Offline MLX Whisper chunk/window transcription with resumable, media-bound caches."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import wave
from media_check import binding, write_json


def make_windows(duration, chunk, overlap):
    if chunk <= 0 or not 0 <= overlap < chunk:
        raise ValueError('Require chunk > 0 and 0 <= overlap < chunk')
    result, start = [], 0.0
    while start < duration:
        end = min(duration, start + chunk)
        result.append([start, end])
        if end == duration:
            break
        start = end - overlap
    return result


def absolute_segments(segments, start, end, window_index):
    result = []
    for item in segments:
        relative_start, relative_end = float(item['start']), float(item['end'])
        if not all(math.isfinite(t) for t in (relative_start, relative_end)) or relative_end < relative_start:
            raise ValueError('Invalid ASR timestamp')
        # Retain raw relative coordinates, constrain model overshoot to the actual window.
        a, b = max(start, start + relative_start), min(end, start + relative_end)
        if b <= a:
            continue
        result.append({'start': a, 'end': b, 'relative_start': relative_start,
                       'relative_end': relative_end, 'window_index': window_index,
                       'window_start': start, 'text': item.get('text', '').strip(),
                       'avg_logprob': item.get('avg_logprob'), 'no_speech_prob': item.get('no_speech_prob')})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', required=True, help='Mono 16 kHz PCM16 WAV extracted from final media')
    parser.add_argument('--model', required=True, help='Existing local MLX Whisper model directory; no downloads')
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--language', default='zh', help='Whisper language code, or auto')
    parser.add_argument('--chunk-seconds', type=float, default=600)
    parser.add_argument('--overlap-seconds', type=float, default=10)
    parser.add_argument('--windows', help='JSON list of [start,end] seconds; overrides chunking')
    parser.add_argument('--plan-only', action='store_true', help='Validate inputs and write plan without loading MLX')
    args = parser.parse_args()
    model = Path(args.model).expanduser().resolve(strict=True)
    if not model.is_dir():
        raise ValueError('Model must be an existing local directory')
    source = binding(args.audio)
    with wave.open(source['path'], 'rb') as audio:
        if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth(), audio.getcomptype()) != (1, 16000, 2, 'NONE'):
            raise ValueError('Require mono 16 kHz PCM16 WAV')
        duration = audio.getnframes() / 16000
    windows = json.loads(Path(args.windows).read_text()) if args.windows else make_windows(duration, args.chunk_seconds, args.overlap_seconds)
    if not windows:
        raise ValueError('No audio windows')
    for window in windows:
        if not isinstance(window, list) or len(window) != 2 or not all(type(t) in (int, float) and math.isfinite(t) for t in window) or not 0 <= window[0] < window[1] <= duration:
            raise ValueError('Each window must satisfy 0 <= start < end <= audio duration')
    model_files = {str(p.relative_to(model)): [p.stat().st_size, p.stat().st_mtime_ns]
                   for p in model.rglob('*') if p.is_file() and '.cache' not in p.relative_to(model).parts}
    if not model_files:
        raise ValueError('Model directory is empty')
    manifest = {'schema_version': 1, 'audio': source, 'model': str(model), 'model_files': model_files,
                'language': args.language, 'windows': windows, 'temperature': 0,
                'condition_on_previous_text': False, 'timestamp_basis': 'audio_zero',
                'notice': 'ASR evidence only; overlapping windows can contain duplicate text.'}
    out = Path(args.out_dir).expanduser().resolve()
    targets = [out / name for name in ('manifest.json', 'status.json', 'transcript.json', 'transcript.txt')]
    targets.extend(out / f'window-{i:04d}.json' for i in range(len(windows)))
    if Path(source['path']) in targets:
        raise ValueError('Output path collides with source audio; choose another out-dir')
    if out == model or model in out.parents:
        raise ValueError('Transcripts must be written outside the model directory')
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / 'manifest.json'
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        raise ValueError('Cache does not match audio/model/windows; choose a fresh out-dir')
    write_json(manifest_path, manifest)
    if args.plan_only:
        print(json.dumps({'windows': len(windows), 'manifest': str(manifest_path), 'transcription_performed': False}))
        return
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    try:
        import numpy as np
        import mlx_whisper
    except ImportError as error:
        raise ValueError('Install/use a Python environment with numpy and mlx_whisper for Apple Silicon') from error
    combined = []
    for index, (start, end) in enumerate(windows):
        if binding(source['path']) != source:
            raise ValueError('Source audio changed while transcribing')
        cache = out / f'window-{index:04d}.json'
        if cache.exists():
            result = json.loads(cache.read_text())
        else:
            with wave.open(source['path'], 'rb') as audio:
                audio.setpos(int(start * 16000))
                samples = np.frombuffer(audio.readframes(int((end-start)*16000)), dtype='<i2').astype(np.float32) / 32768
            raw = mlx_whisper.transcribe(samples, path_or_hf_repo=str(model),
                                         language=None if args.language == 'auto' else args.language,
                                         temperature=0, condition_on_previous_text=False, word_timestamps=False)
            result = {'window_index': index, 'start': start, 'end': end,
                      'segments': absolute_segments(raw.get('segments', []), start, end, index)}
            write_json(cache, result)
        combined.extend(result['segments'])
        write_json(out / 'status.json', {'completed_windows': index+1, 'total_windows': len(windows),
                                        'transcription_complete': index+1 == len(windows)})
        print(f'{index+1}/{len(windows)} windows', flush=True)
    combined.sort(key=lambda item: (item['start'], item['window_index']))
    write_json(out / 'transcript.json', {'manifest': manifest, 'segments': combined})
    (out / 'transcript.txt').write_text('\n'.join(f"{s['start']:.3f}\t{s['end']:.3f}\t{s['text']}" for s in combined) + '\n')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        sys.exit(str(error))
