#!/usr/bin/env python3
"""Render, verify and review a local video project."""
import argparse
import hashlib
import json
import math
import re
import subprocess
from fractions import Fraction
from pathlib import Path


def run(command):
    result = subprocess.run([str(x) for x in command], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
    return result


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path]).stdout)


def main_video(info):
    return next(s for s in info['streams'] if s['codec_type'] == 'video' and not s['disposition'].get('attached_pic'))


def audio_stream(info):
    return next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


class Plan:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        self.data = json.loads(self.path.read_text())
        self.fps = self.data.get('fps', 30)
        if not isinstance(self.fps, int) or self.fps <= 0:
            raise ValueError('fps must be a positive integer (constant output frame rate)')
        self.size = self.data.get('size', [3840, 2160])
        if len(self.size) != 2 or any(not isinstance(n, int) or n <= 0 or n % 2 for n in self.size):
            raise ValueError('size must contain two positive even integers')
        self.shots = self.data['shots']
        if not self.shots:
            raise ValueError('shots must not be empty')
        seen = set()
        position = 0
        for i, shot in enumerate(self.shots):
            if shot['index'] in seen:
                raise ValueError('shot indexes must be unique')
            seen.add(shot['index'])
            duration = self.frames(shot['duration'])
            overlap = self.frames(shot.get('transition_after', 0))
            if duration <= 0 or not math.isfinite(shot['in']) or shot['in'] < 0 or overlap < 0 or overlap >= duration:
                raise ValueError('invalid shot duration, source in-point or transition')
            if i == len(self.shots) - 1 and overlap:
                raise ValueError('last shot cannot have a transition_after')
            if overlap and overlap >= self.frames(self.shots[i + 1]['duration']):
                raise ValueError('transition must be shorter than both shots')
            if abs(shot.get('timeline_start', position / self.fps) - position / self.fps) > .5 / self.fps:
                raise ValueError('timeline_start does not match the shot sequence')
            shot.setdefault('timeline_start', position / self.fps)
            position += duration - overlap
        self.frames_total = position
        self.duration = position / self.fps
        if abs(self.data['duration'] - self.duration) > .5 / self.fps:
            raise ValueError('plan duration does not match shot durations minus overlaps')
        self.digest = hashlib.sha256(json.dumps(self.data, sort_keys=True).encode()).hexdigest()

    def frames(self, seconds):
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            raise ValueError('times must be finite numbers')
        value = seconds * self.fps
        if abs(value - round(value)) > .001:
            raise ValueError('durations and transitions must align to output frames')
        return round(value)

    def asset(self, value):
        path = Path(value)
        return path if path.is_absolute() else self.root / path


def verify_media(path, duration=None, frames=None, size=None, fps=None, poster=False, decode=True):
    info = probe(path)
    video = main_video(info)
    audio = audio_stream(info)
    if audio is None:
        raise ValueError('export has no audio stream')
    video_duration = float(video['duration'])
    audio_duration = float(audio['duration'])
    rate = float(Fraction(video['r_frame_rate']))
    tolerance = 1 / rate + .002
    if abs(video_duration - audio_duration) > tolerance:
        raise ValueError(f'audio/video duration mismatch: {video_duration} vs {audio_duration}')
    if duration is not None and abs(video_duration - duration) > tolerance:
        raise ValueError(f'video duration {video_duration} does not match expected {duration}')
    if frames is not None and int(video['nb_frames']) != frames:
        raise ValueError('video frame count does not match the plan')
    if size is not None and [video['width'], video['height']] != list(size):
        raise ValueError('unexpected export dimensions')
    if fps is not None and abs(rate - fps) > .0001:
        raise ValueError('unexpected output frame rate')
    has_poster = any(s['disposition'].get('attached_pic') for s in info['streams'])
    if poster and not has_poster:
        raise ValueError('export is missing its attached poster')
    peak = None
    if decode:
        result = run(['ffmpeg', '-hide_banner', '-nostats', '-v', 'info', '-xerror', '-threads', '2',
                      '-i', path, '-map', '0:v:0', '-map', '0:a:0', '-af', 'volumedetect',
                      '-threads', '2', '-f', 'null', '-'])
        match = re.search(r'max_volume: ([-\d.]+) dB', result.stderr)
        if match:
            peak = float(match.group(1))
    return {'file': str(Path(path).resolve()), 'video_duration': video_duration,
            'audio_duration': audio_duration, 'frames': int(video['nb_frames']),
            'size': [video['width'], video['height']], 'fps': rate,
            'time_base': video['time_base'], 'poster': has_poster, 'decoded': decode,
            'peak_db': peak, 'possible_clipping': peak is not None and peak >= -.1,
            'bytes': Path(path).stat().st_size}


def compatible_parts(paths):
    signatures = []
    for path in paths:
        info = probe(path)
        video = main_video(info)
        audio = audio_stream(info)
        signatures.append(tuple(video[k] for k in ['codec_name', 'width', 'height', 'pix_fmt', 'r_frame_rate', 'time_base'])
                          + tuple(audio[k] for k in ['codec_name', 'sample_rate', 'channels', 'time_base']))
    if len(set(signatures)) != 1:
        raise ValueError(f'concat parts have incompatible video/audio formats or time bases: {signatures}')


def source_info(plan, source_root):
    result = {}
    for shot in plan.shots:
        path = Path(source_root) / Path(shot['source']).name if source_root else plan.asset(shot['source'])
        path = path.resolve()
        if not path.is_file():
            raise ValueError(f'original source is missing: {path}; reconnect media or use --source-root')
        if path not in result:
            result[path] = probe(path)
        video = main_video(result[path])
        if video.get('color_transfer') in ['smpte2084', 'arib-std-b67']:
            raise ValueError('HDR sources must be converted to SDR before using this renderer')
        length = float(video.get('duration', result[path]['format']['duration']))
        if shot['in'] + shot['duration'] > length + 1 / plan.fps:
            raise ValueError(f'shot exceeds source duration: {path}')
    return result


def groups(shots):
    result, group = [], []
    for shot in shots:
        group.append(shot)
        if not shot.get('transition_after', 0):
            result.append(group)
            group = []
    return result


def fingerprint(path):
    path = Path(path).resolve()
    stat = path.stat()
    return [str(path), stat.st_size, stat.st_mtime_ns]


def build_video(plan, output, size=None, source_root=None, cache_dir=None):
    size = list(size or plan.size)
    width, height = size
    if any(n <= 0 or n % 2 for n in size):
        raise ValueError('output dimensions must be positive and even')
    sources = source_info(plan, source_root)
    output = Path(output).resolve()
    protected = set(sources)
    audio_config = plan.data.get('audio', {})
    music = plan.asset(audio_config['music']) if audio_config.get('music') else None
    cover = plan.data.get('cover', {})
    cover_path = plan.asset(cover['image']) if cover else None
    assets = [p for p in [music, cover_path] if p]
    assets += [plan.asset(s['overlay']['image']) for s in plan.shots if s.get('overlay')]
    for path in assets:
        if not path.is_file():
            raise ValueError(f'persistent project asset is missing: {path}')
    if output in protected | {p.resolve() for p in assets}:
        raise ValueError('output would overwrite a source or persistent asset')
    cache = Path(cache_dir) if cache_dir else plan.root / 'work/render-cache'
    cache.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    chunks = []
    renderer = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for number, group in enumerate(groups(plan.shots)):
        paths = [Path(source_root) / Path(s['source']).name if source_root else plan.asset(s['source']) for s in group]
        group_assets = [plan.asset(s['overlay']['image']) for s in group if s.get('overlay')]
        if number == 0 and cover_path:
            group_assets.append(cover_path)
        spec = {'renderer': renderer, 'group': group, 'fps': plan.fps, 'size': size, 'video_filter': plan.data.get('video_filter', ''),
                'voice_filter': audio_config.get('voice_filter', ''), 'cover': cover if number == 0 else {},
                'inputs': [fingerprint(p) for p in paths + group_assets]}
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:24]
        target = cache / (digest + '.mov')
        count = sum(plan.frames(s['duration']) - plan.frames(s.get('transition_after', 0)) for s in group)
        if target.exists():
            verify_media(target, count / plan.fps, count, size, plan.fps, decode=False)
            chunks.append(target)
            continue
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
        for shot, path in zip(group, paths):
            command += ['-threads', '2', '-ss', str(shot['in']), '-t', str(shot['duration']), '-i', str(path)]
        overlays = {}
        next_input = len(group)
        for j, shot in enumerate(group):
            if shot.get('overlay'):
                overlays[j] = next_input
                next_input += 1
                command += ['-loop', '1', '-framerate', str(plan.fps), '-i', str(plan.asset(shot['overlay']['image']))]
        cover_input = next_input if number == 0 and cover_path else None
        if cover_input is not None:
            command += ['-loop', '1', '-framerate', str(plan.fps), '-i', str(cover_path)]
        filters = []
        for j, (shot, path) in enumerate(zip(group, paths)):
            vf = (f'fps={plan.fps},settb=AVTB,setpts=PTS-STARTPTS,'
                  f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                  f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p')
            if plan.data.get('video_filter'):
                vf += ',' + plan.data['video_filter']
            if shot.get('fade_in'):
                vf += f",fade=t=in:st=0:d={shot['fade_in']}"
            if shot.get('fade_out'):
                vf += f",fade=t=out:st={shot['duration']-shot['fade_out']}:d={shot['fade_out']}"
            filters.append(f'[{j}:v:0]{vf}[base{j}]')
            if j in overlays:
                overlay = shot['overlay']
                filters.append(f"[{overlays[j]}:v]scale={width}:{height},format=rgba,"
                               f"fade=t=in:st={overlay['fade_in_start']}:d={overlay['fade_in_seconds']}:alpha=1,"
                               f"fade=t=out:st={overlay['fade_out_start']}:d={overlay['fade_out_seconds']}:alpha=1[title{j}]")
                filters.append(f'[base{j}][title{j}]overlay=shortest=1:format=auto,format=yuv420p[v{j}]')
            else:
                filters.append(f'[base{j}]null[v{j}]')
            af = (f"atrim=duration={shot['duration']},asetpts=PTS-STARTPTS,aresample=48000,"
                  f"aformat=sample_fmts=fltp:channel_layouts=stereo,volume={shot.get('gain_db',0)}dB")
            if audio_config.get('voice_filter'):
                af += ',' + audio_config['voice_filter']
            af += f",afade=t=in:st=0:d=0.075,afade=t=out:st={max(0,shot['duration']-.12)}:d=0.12"
            if audio_stream(sources[path.resolve()]):
                filters.append(f'[{j}:a:0]{af}[a{j}]')
            else:
                filters.append(f'anullsrc=r=48000:cl=stereo,{af}[a{j}]')
        cv, ca, elapsed = 'v0', 'a0', group[0]['duration']
        for j in range(1, len(group)):
            cross = group[j - 1]['transition_after']
            filters.append(f'[{cv}][v{j}]xfade=transition=fade:duration={cross}:offset={elapsed-cross}[vx{j}]')
            filters.append(f'[{ca}][a{j}]acrossfade=d={cross}:c1=tri:c2=tri[ax{j}]')
            cv, ca = f'vx{j}', f'ax{j}'
            elapsed += group[j]['duration'] - cross
        if cover_input is not None:
            hold = cover.get('hold_seconds', .75)
            fade = cover.get('fade_seconds', .35)
            filters.append(f'[{cover_input}:v]scale={width}:{height},format=rgba,fade=t=out:st={hold}:d={fade}:alpha=1[c]')
            filters.append(f"[{cv}][c]overlay=shortest=1:format=auto:enable='lt(t,{hold+fade})',format=yuv420p[covered]")
            cv = 'covered'
        filters.append(f'[{cv}]scale=out_range=tv,format=yuv420p,setparams=range=limited[normalized]')
        cv = 'normalized'
        temporary = target.with_suffix('.partial.mov')
        command += ['-filter_complex_threads', '2', '-filter_complex', ';'.join(filters), '-map', f'[{cv}]', '-map', f'[{ca}]',
                    '-frames:v', str(count), '-t', str(count / plan.fps), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                    '-threads', '4', '-profile:v', 'high', '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709',
                    '-color_trc', 'bt709', '-colorspace', 'bt709', '-video_track_timescale', '90000',
                    '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '2', '-map_metadata', '-1', str(temporary)]
        run(command)
        verify_media(temporary, count / plan.fps, count, size, plan.fps, decode=False)
        temporary.replace(target)
        chunks.append(target)
        print(f'rendered group {number+1}', flush=True)
    compatible_parts(chunks)
    manifest = cache / ('concat-' + plan.digest[:16] + f'-{width}x{height}.txt')
    manifest.write_text(''.join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in chunks))
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(manifest)]
    graph = []
    input_index = 1
    if music:
        command += ['-i', str(music)]
        graph.append(f'[{input_index}:a]loudnorm=I=-23.5:TP=-3:LRA=10,aresample=48000[music]')
        music_label = 'music'
        if audio_config.get('duck', True):
            expression = '1'
            for shot in plan.shots:
                if not shot.get('foreground_audio'):
                    continue
                start = shot['timeline_start']; end = start + shot['duration']
                a, b, c, d = start-.45, start+.12, end-.12, end+.45
                duck = f'if(lt(t,{a}),1,if(lt(t,{b}),1-(t-({a}))/{b-a}*0.48,if(lt(t,{c}),0.52,if(lt(t,{d}),0.52+(t-({c}))/{d-c}*0.48,1))))'
                expression = f'min({expression},{duck})'
            graph.append(f"[music]volume='{expression}':eval=frame[ducked]")
            music_label = 'ducked'
        graph.append(f'[0:a][{music_label}]amix=inputs=2:duration=first:normalize=0[mixed]')
        input_index += 1
        voice = 'mixed'
    else:
        voice = '0:a'
    fade = min(1.6, plan.duration / 2)
    graph.append(f'[{voice}]loudnorm=I=-18.5:TP=-1.5:LRA=9,aresample=48000,afade=t=out:st={plan.duration-fade}:d={fade}[audio]')
    poster = bool(cover_path and cover.get('embedded_poster', True))
    if poster:
        command += ['-i', str(cover_path)]
    command += ['-filter_complex', ';'.join(graph), '-map', '0:v:0', '-map', '[audio]']
    if poster:
        command += ['-map', f'{input_index}:v:0', '-disposition:v:1', 'attached_pic']
    temporary = output.with_name(output.stem + '.partial.mp4')
    command += ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '256k', '-t', str(plan.duration),
                '-video_track_timescale', '90000', '-movflags', '+faststart', '-map_metadata', '-1', str(temporary)]
    run(command)
    result = verify_media(temporary, plan.duration, plan.frames_total, size, plan.fps, poster)
    temporary.replace(output)
    result.update(file=str(output), plan_digest=plan.digest)
    save_json(output.with_suffix('.verify.json'), result)
    return result


def require_review(plan, path):
    review = json.loads(Path(path).read_text())
    if review['plan_digest'] != plan.digest:
        raise ValueError('review belongs to a different plan; regenerate the review package')
    if not review.get('reviewer', '').strip():
        raise ValueError('review must identify its reviewer')
    expected = {s['index'] for s in plan.shots}
    actual = [s['index'] for s in review['shots']]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError('review does not cover every shot exactly once')
    if any(s['decision'] != 'passed' for s in review['shots']):
        raise ValueError('all shots must pass framing, motion, action and dialogue review before final rendering')


def review_project(plan, folder, video=None, source_root=None, transcript=None):
    words = []
    if transcript:
        data = json.loads(Path(transcript).read_text())
        if not video or Path(data['input']).resolve() != Path(video).resolve():
            raise ValueError('transcript must belong to the same --video input')
        if data['input_fingerprint'] != fingerprint(video):
            raise ValueError('review video changed after transcription; transcribe it again')
        words = [word for segment in data['segments'] for word in segment['words']]
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    preview = folder / 'preview.mp4'
    if video:
        if Path(video).resolve() == preview:
            raise ValueError('review input and preview output must differ')
        verify_media(video, plan.duration, plan.frames_total, fps=plan.fps, decode=False)
        run(['ffmpeg', '-v', 'error', '-y', '-i', video, '-map', '0:v:0', '-map', '0:a:0',
             '-vf', 'scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '25', '-threads', '2',
             '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', preview])
    else:
        build_video(plan, preview, [640, 360], source_root)
    clips = folder / 'shots'
    clips.mkdir(exist_ok=True)
    review = {'plan_digest': plan.digest, 'plan': str(plan.path), 'preview': str(preview),
              'reviewer': '', 'shots': []}
    for number, shot in enumerate(plan.shots):
        start = max(0, shot['timeline_start'] - .5)
        end = min(plan.duration, shot['timeline_start'] + shot['duration'] + .5)
        clip = clips / f'{number:03d}.mp4'
        run(['ffmpeg', '-v', 'error', '-y', '-ss', str(start), '-i', preview, '-t', str(end-start),
             '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '25',
             '-threads', '2', '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', clip])
        review['shots'].append({'index': shot['index'], 'label': shot['label'], 'source': shot['source'],
                                'source_in': shot['in'], 'timeline_start': shot['timeline_start'],
                                'duration': shot['duration'], 'window_start': start,
                                'clip': str(clip.relative_to(folder)), 'decision': 'pending', 'notes': '',
                                'words': [w for w in words if start <= w['start'] < end]})
    save_json(folder / 'review.json', review)
    template = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>独立审片</title>
<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;background:#181818;color:#eee}article{border-top:1px solid #666;padding:24px 0}video{width:min(100%,640px)}textarea{display:block;width:90%;height:60px}button,select,input{font:inherit;margin:6px;padding:8px}.words button{padding:3px}</style>
<h1>独立审片</h1><p>逐段播放并听取原声：检查构图、完整运镜、动作与对白句尾。转写用于辅助定位，不能自动替代审片。</p>
<label>审片者 <input id="reviewer"></label><button id="save">下载审片决定 JSON</button><p>下载后用 render --review 文件路径。任何一段待审或需重剪，最终渲染都会拒绝。</p><main></main>
<script>const data=REVIEW_DATA;const reviewer=document.querySelector('#reviewer');reviewer.value=data.reviewer;
for(const shot of data.shots){const card=document.createElement('article');const h=document.createElement('h2');h.textContent=`${shot.index} · ${shot.label} · 成片 ${shot.timeline_start}s / 原片 ${shot.source_in}s`;card.append(h);const video=document.createElement('video');video.controls=true;video.preload='none';video.src=shot.clip;card.append(video);card.append(document.createElement('br'));const select=document.createElement('select');for(const [value,label] of [['pending','待审'],['passed','通过：构图、运镜、动作和对白均已检查'],['needs_edit','需要重剪']]){const o=document.createElement('option');o.value=value;o.textContent=label;select.append(o)}select.value=shot.decision;select.onchange=()=>shot.decision=select.value;card.append(select);const notes=document.createElement('textarea');notes.placeholder='问题及原片/成片时间';notes.value=shot.notes;notes.oninput=()=>shot.notes=notes.value;card.append(notes);const words=document.createElement('div');words.className='words';for(const w of shot.words){const b=document.createElement('button');b.textContent=`${w.word} (${w.start.toFixed(2)}s)`;b.onclick=()=>{video.currentTime=w.start-shot.window_start;video.play()};words.append(b)}card.append(words);document.querySelector('main').append(card)}
document.querySelector('#save').onclick=()=>{data.reviewer=reviewer.value;const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));a.download='review-decisions.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};</script></html>'''
    payload = json.dumps(review, ensure_ascii=False).replace('</', '<\\/')
    (folder / 'index.html').write_text(template.replace('REVIEW_DATA', payload))
    return {'review': str(folder / 'index.html'), 'decisions': str(folder / 'review.json'), 'shots': len(review['shots'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    render = sub.add_parser('render', help='render an approved final plan, rebuilding missing caches')
    render.add_argument('plan'); render.add_argument('--output', required=True)
    render.add_argument('--review', required=True); render.add_argument('--source-root')
    render.add_argument('--size', type=lambda s: [int(n) for n in s.split('x')])
    render.add_argument('--cache-dir')
    verify = sub.add_parser('verify', help='verify individual streams, frame counts and decoding')
    verify.add_argument('video'); verify.add_argument('--plan'); verify.add_argument('--report')
    verify.add_argument('--expect-poster', action='store_true')
    review = sub.add_parser('review', help='build full-motion review clips and a decision sheet')
    review.add_argument('plan'); review.add_argument('--output-dir', required=True)
    review.add_argument('--video'); review.add_argument('--source-root'); review.add_argument('--transcript')
    args = parser.parse_args()
    try:
        if args.command == 'render':
            plan = Plan(args.plan)
            require_review(plan, args.review)
            result = build_video(plan, args.output, args.size, args.source_root, args.cache_dir)
        elif args.command == 'verify':
            plan = Plan(args.plan) if args.plan else None
            result = verify_media(args.video, plan.duration if plan else None,
                                  plan.frames_total if plan else None, fps=plan.fps if plan else None,
                                  poster=args.expect_poster or bool(plan and plan.data.get('cover', {}).get('embedded_poster', bool(plan.data.get('cover')))))
            if args.report:
                save_json(Path(args.report), result)
        elif args.command == 'review':
            result = review_project(Plan(args.plan), args.output_dir, args.video, args.source_root, args.transcript)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, KeyError, StopIteration, ImportError, OSError) as error:
        parser.exit(1, f'error: {error}\n')


if __name__ == '__main__':
    main()
