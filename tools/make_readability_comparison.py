"""Build a local, synthetic ASS readability comparison; never reads movie files.

Requires FFmpeg with libass, Pillow and Windows Microsoft YaHei fonts.
Only generated samples/fonts are packaged locally; no font or video is committed.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nas_danmaku as d

WIDTH, HEIGHT, LENGTH = 1920, 1080, 36
SAMPLE_CYCLE = [
    d.Comment(0, '这一条短弹幕，你能轻松看清吗'),
    d.Comment(2, '注意文字经过明亮背景时，边缘是否还能分辨', 0x66CCFF),
    d.Comment(4, '这是一条比较长的测试弹幕，用来对比长句移动速度、字号大小和停顿之后是否更容易完整读完'),
    d.Comment(6, '看到这里，请记下你觉得最清楚的方案编号', 0xFFD166),
    d.Comment(8, '不要追着每条看，试试自然观看时能否读清'),
    d.Comment(10, '底部同时保留电影台词，比较视线切换是否轻松'),
]
SAMPLES = [d.Comment(comment.time+cycle*12,comment.text,comment.color) for cycle in range(3) for comment in SAMPLE_CYCLE]
CASES = [
    dict(name='原效果', desc='字号32 · 不透明度80% · 12秒 · 同屏最多6条'),
    dict(name='只放大字号', desc='字号40；其余沿用原效果', size=40),
    dict(name='只提高不透明度', desc='不透明度100%；其余沿用原效果', opacity=100),
    dict(name='慢速75%', desc='16秒走完全程；其余沿用原效果', duration=16),
    dict(name='慢速60%', desc='20秒走完全程；其余沿用原效果', duration=20),
    dict(name='慢速50%', desc='24秒走完全程；其余沿用原效果', duration=24),
    dict(name='减少同屏数量', desc='最多同屏3条；其余沿用原效果', density=3),
    dict(name='加粗与加强描边', desc='字号32 · 加粗 · 黑色描边3像素；其余沿用原效果', bold=True, outline=3),
    dict(name='白字与实心描边', desc='统一白字 · 字面80%不透明 · 黑色描边100%不透明', white=True, opaque_outline=True),
    dict(name='长短句保持等速', desc='统一每秒约185像素；长句获得更多时间', motion='constant'),
    dict(name='滑入停2秒再滑出', desc='到画面中间停2秒；移动部分仍约12秒', motion='pause'),
    dict(name='中间阅读区减速', desc='两侧每秒260像素 · 中间阅读区每秒70像素', motion='slowzone'),
    dict(name='全部固定显示', desc='全部弹幕顶部静止8秒；不滚动', motion='fixed'),
    dict(name='半透明黑色底框', desc='随文字移动的黑底框 · 字面80%不透明', box=True),
    dict(name='长句两行固定阅读', desc='长句分两行静止10秒；短句滚动 · 占用顶部约34%', motion='wrap'),
    dict(name='过滤超长弹幕', desc='超过35字的评论不显示；其余沿用原效果', max_chars=35),
    dict(name='组合方案：清晰慢速', desc='字号40 · 100%不透明 · 加粗描边 · 白字 · 16秒 · 最多4条',
         size=40, opacity=100, bold=True, outline=3, white=True, duration=16, density=4),
    dict(name='组合方案：清晰停顿', desc='字号40 · 100%不透明 · 加粗描边 · 白字 · 中间停2秒 · 最多4条',
         size=40, opacity=100, bold=True, outline=3, white=True, motion='pause', density=4),
]


def run(args, cwd, log):
    with log.open('ab') as stream:
        process = subprocess.run([str(x) for x in args], cwd=cwd, stdout=stream, stderr=stream)
    if process.returncode:
        raise RuntimeError(f'Command failed ({process.returncode}); see {log}')


def text_style(name, size, alignment='7'):
    result = d.style(name, size)
    result.update(Alignment=alignment, Outline='2', MarginL='0', MarginR='0', MarginV='0')
    return result


def append_event(doc, start, end, text, style='Scroll', layer=0):
    doc.events.append(d.event(round(start*100), round(end*100), text, style, layer))


def motion_rows(case):
    """Reserve whole rows for segmented paths; never let a follower hit a pause."""
    from PIL import ImageFont
    size, duration, density = case.get('size', 32), case.get('duration', 12), case.get('density', 6)
    font = ImageFont.truetype(str(FONT_BOLD if case.get('bold') else FONT_REGULAR), size)
    doc = d.Ass(styles={'Scroll': d.style('Scroll', size)})
    doc.styles['Scroll'].update(Alignment='7', Outline=str(round(max(1,size/22),2)))
    doc.info['WrapStyle'] = '2'
    row_height = size*1.45
    lane_count = max(1, int((HEIGHT*.25 - HEIGHT*.025)/row_height))
    if case['motion']=='wrap':
        lane_count = 5  # The middle lane reserves two text rows for long text.
    reservations = [0.0]*lane_count
    count = 0
    metadata = []
    for comment in SAMPLES:
        at = comment.time
        if sum(end > at for end in reservations) >= density:
            continue
        allowed = [2] if case['motion']=='wrap' and len(comment.text)>35 else [0,1,3,4] if case['motion']=='wrap' else range(lane_count)
        free = next((i for i in allowed if reservations[i] <= at), None)
        if free is None:
            continue
        text = comment.text
        length = float(font.getlength(text)) + size*.2
        y = HEIGHT*.025 + free*row_height
        x0, x1 = WIDTH+10, -length-10
        center = (WIDTH-length)/2
        bgr = f'{comment.color & 255:02X}{comment.color >> 8 & 255:02X}{comment.color >> 16 & 255:02X}'
        tags = r'\an7\q2' + f'\\c&H{bgr}&'
        segments = []
        mode = case['motion']
        if mode == 'fixed':
            segments = [(at,at+8,center,center)]
        elif mode == 'constant':
            segments = [(at,at+(x0-x1)/185,x0,x1)]
        elif mode == 'pause':
            segments = [(at,at+duration/2,x0,center),
                        (at+duration/2,at+duration/2+2,center,center),
                        (at+duration/2+2,at+duration+2,center,x1)]
        elif mode == 'slowzone':
            a,b = WIDTH*2/3-length/2, WIDTH/3-length/2
            t1 = at+(x0-a)/260
            t2 = t1+(a-b)/70
            t3 = t2+(b-x1)/260
            segments = [(at,t1,x0,a),(t1,t2,a,b),(t2,t3,b,x1)]
        elif mode == 'wrap':
            # Give the long sentence a dedicated two-line region; other rows
            # do not cross it. Positions are shared across the whole case.
            if len(text)>35:
                half = (len(text)+1)//2
                text = text[:half] + r'\N' + text[half:]
                length = max(font.getlength(part) for part in text.split(r'\N')) + size*.2
                center = (WIDTH-length)/2
                y = 142
                segments = [(at,at+10,center,center)]
            else:
                y = [27,78,142,260,311][free]
                segments = [(at,at+12,x0,x1)]
        assert segments, case['name']
        for start,end,a,b in segments:
            position = f'\\pos({a:.2f},{y:.2f})' if a==b else f'\\move({a:.2f},{y:.2f},{b:.2f},{y:.2f})'
            append_event(doc,start,end,'{'+tags+position+'}'+text)
        reservations[free] = segments[-1][1]
        metadata.append(dict(text=comment.text,start=at,end=segments[-1][1],lane=free,segments=len(segments)))
        count += 1
    return doc,count,metadata


def make_case(case, number):
    size, opacity = case.get('size',32), case.get('opacity',80)
    if 'motion' in case:
        doc,count,motions = motion_rows(case)
    else:
        samples = [c for c in SAMPLES if len(c.text) <= case.get('max_chars',999)]
        doc,_ = d.render_comments(samples,(WIDTH,HEIGHT),font_size=size,duration=case.get('duration',12),
            opacity=opacity,density=case.get('density',6),area=25,block_noise=False,filter_rules=[],deduplicate=False)
        count,motions = len(doc.events),[]
    alpha = round(255*(1-opacity/100))
    row = doc.styles['Scroll']
    row['Bold'] = '-1' if case.get('bold') else '0'
    row['Shadow'] = '0'
    if 'outline' in case:
        row['Outline'] = str(case['outline'])
    if case.get('box'):
        row.update(BorderStyle='3',Outline='6')
    for key in ('PrimaryColour','SecondaryColour','OutlineColour','BackColour'):
        row[key] = f'&H{alpha:02X}' + row[key][-6:]
    for event in doc.events:
        text = re.sub(r'\\alpha&H[0-9A-Fa-f]{2}&','',event['Text'])
        text = re.sub(r'\\(?:[1-4])a&H[0-9A-Fa-f]{2}&','',text)
        if case.get('white'):
            text = re.sub(r'\\c&H[0-9A-Fa-f]{6}&',r'\\c&HFFFFFF&',text)
        outline_alpha = 0x88 if case.get('box') else 0 if case.get('opaque_outline') else alpha
        tags = f'\\1a&H{alpha:02X}&\\2a&H{alpha:02X}&\\3a&H{outline_alpha:02X}&\\4a&H{outline_alpha:02X}&'
        event['Text'] = text.replace('{','{'+tags,1)
    # Clip at case boundaries without accelerating a partially visible move.
    clipped = []
    for event in doc.events:
        start,end = d.stamp(event['Start']),d.stamp(event['End'])
        if start>=LENGTH*100:
            continue
        if end>LENGTH*100:
            event['Text'] = re.sub(r'\\move\(([^)]+)\)',
                lambda match: r'\move('+match[1]+f',0,{(end-start)*10})',event['Text'])
            event['End'] = d.format_stamp(LENGTH*100)
        clipped.append(event)
    doc.events = clipped
    assert all(d.stamp(e['End']) <= LENGTH*100 for e in doc.events)
    for at in (200,1400,2600):
        assert any(d.stamp(e['Start']) <= at < d.stamp(e['End']) for e in doc.events), (case['name'],at)
    for i,item in enumerate(motions):
        for previous in motions[:i]:
            if previous['lane']==item['lane']:
                assert previous['end']<=item['start'], (case['name'],previous,item)
    doc.styles.update(Heading=text_style('Heading',44),Details=text_style('Details',29),
                      Small=text_style('Small',26),Dialogue=text_style('Dialogue',42,'2'))
    append_event(doc,0,LENGTH,rf'{{\pos(70,716)}}方案 {number:02}/{len(CASES):02}  ·  '+case['name'],'Heading',5)
    append_event(doc,0,LENGTH,rf'{{\pos(70,789)}}'+case['desc'],'Details',5)
    append_event(doc,0,LENGTH,rf'{{\pos(70,838)}}相同测试弹幕 · 每段36秒 · 可用章节跳转 · 本段显示{count}条','Small',5)
    for start,end,label in [(0,12,'深色背景'),(12,24,'浅色背景'),(24,36,'纹理与移动背景')]:
        append_event(doc,start,end,rf'{{\pos(70,628)}}'+label,'Small',5)
    append_event(doc,0,12,r'{\pos(960,995)}这是模拟的电影台词，测试弹幕会在上方出现。','Dialogue',4)
    append_event(doc,12,24,r'{\pos(960,995)}读完台词再看上方，看看哪一种更容易辨认。','Dialogue',4)
    append_event(doc,24,36,r'{\pos(960,995)}请记下清晰度、舒适度，以及遮挡画面的程度。','Dialogue',4)
    return doc,dict(number=number,name=case['name'],description=case['desc'],comments=count,motions=motions)


def validate_ass(doc):
    parsed = d.parse_ass(doc.dumps())
    assert parsed.resolution == (WIDTH,HEIGHT)
    for event in parsed.events:
        assert 0 <= d.stamp(event['Start']) < d.stamp(event['End'])
        assert event['Style'] in parsed.styles
        assert event['Text'].count('{') == event['Text'].count('}')
        assert len(re.findall(r'\\(?:move|pos)\(',event['Text'])) == 1
    return len(parsed.events)


def attach_fonts(args):
    for i,path in enumerate((FONT_REGULAR,FONT_BOLD)):
        args += ['-attach',path,'-metadata:s:t:'+str(i),'mimetype=application/x-truetype-font',
                 '-metadata:s:t:'+str(i),'filename='+path.name]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output',type=Path)
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--reuse-background',action='store_true')
    args = parser.parse_args()
    output,work = args.output.resolve(),args.work.resolve()
    output.mkdir(parents=True,exist_ok=False)
    work.mkdir(parents=True,exist_ok=True)
    subs = output/'各方案字幕'
    subs.mkdir()
    ffmpeg,ffprobe = shutil.which('ffmpeg'),shutil.which('ffprobe')
    assert ffmpeg and ffprobe and FONT_REGULAR.is_file() and FONT_BOLD.is_file()
    logs = work/'ffmpeg.log'
    documents,paths,manifest = [],[],[]
    for number,case in enumerate(CASES,1):
        doc,info = make_case(case,number)
        info['ass_events'] = validate_ass(doc)
        documents.append(doc)
        path = subs/f'{number:02}_{case["name"].replace("%","百分比")}.ass'
        path.write_text(doc.dumps(),encoding='utf-8')
        paths.append(path)
        manifest.append(info)
    sequence = d.Ass()
    chapters = [';FFMETADATA1']
    for i,doc in enumerate(documents):
        doc = copy.deepcopy(doc)
        d.rename_styles(doc,f'Case{i+1}_')
        d.shift_events(doc,i*LENGTH)
        sequence.styles.update(doc.styles)
        sequence.events.extend(doc.events)
        chapters += ['[CHAPTER]','TIMEBASE=1/1000',f'START={i*LENGTH*1000}',
                     f'END={(i+1)*LENGTH*1000}',f'title={i+1:02} {CASES[i]["name"]}']
    validate_ass(sequence)
    sequence_path = output/'全部方案_顺序对比.ass'
    sequence_path.write_text(sequence.dumps(),encoding='utf-8')
    (work/'chapters.txt').write_text('\n'.join(chapters)+'\n',encoding='utf-8')
    guide = d.Ass(styles={'Guide':text_style('Guide',24)})
    append_event(guide,0,LENGTH,r'{\pos(70,1040)}弹幕清晰度测试片 · 请开启一条字幕轨 · 不要同时加载内封与外挂字幕','Guide')
    (work/'guide.ass').write_text(guide.dumps(),encoding='utf-8')
    background = ("[0:v]drawbox=x=0:y=0:w=1920:h=680:color=0xdcded9:t=fill:enable='between(t,12,24)',"
                  "drawgrid=w=96:h=56:t=2:c=0x8ea29b@0.65:enable='gte(t,24)',"
                  "drawbox=x=0:y=680:w=1920:h=400:color=0x111a22:t=fill[bg];"
                  "[bg][1:v]overlay=x='mod(t*180,2180)-260':y=0:enable='gte(t,24)':shortest=1,ass=guide.ass[v]")
    base = work/'background60.mp4'
    if not args.reuse_background or not base.is_file():
        run([ffmpeg,'-hide_banner','-y','-f','lavfi','-i',f'color=c=0x18242d:s=1920x1080:r=60:d={LENGTH}',
             '-f','lavfi','-i',f'color=c=0x91a3aa@0.4:s=260x680:r=60:d={LENGTH},format=rgba',
             '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-filter_complex',background,'-map','[v]','-map','2:a:0',
             '-t',LENGTH,'-c:v','libx264','-threads','4','-preset','veryfast','-crf','18','-pix_fmt','yuv420p',
             '-c:a','aac','-b:a','64k','-movflags','+faststart',base],work,logs)
    base24 = work/'background24.mp4'
    if not args.reuse_background or not base24.is_file():
        run([ffmpeg,'-hide_banner','-y','-i',base,'-vf','fps=24','-c:v','libx264','-threads','4','-preset','veryfast',
             '-crf','18','-pix_fmt','yuv420p','-c:a','copy',base24],work,logs)
    print('Background clips complete',flush=True)
    video_paths = []
    for fps,source in [(60,base),(24,base24)]:
        result = output/f'{"02" if fps==60 else "03"}_切换字幕对比_{fps}帧.mkv'
        command = [ffmpeg,'-hide_banner','-y','-i',source]
        for path in paths:
            command += ['-i',path]
        command += ['-map','0:v:0','-map','0:a:0']
        for i in range(len(paths)):
            command += ['-map',f'{i+1}:s:0','-metadata:s:s:'+str(i),f'title={i+1:02} {CASES[i]["name"]}',
                        '-metadata:s:s:'+str(i),'language=zho','-disposition:s:'+str(i),'default' if i==0 else '0']
        command += ['-c','copy','-t',LENGTH]
        attach_fonts(command)
        run(command+[result],work,logs)
        video_paths.append(result)
    sequential = output/'01_先看这个_全部方案顺序对比.mkv'
    command = [ffmpeg,'-hide_banner','-y','-stream_loop',str(len(CASES)-1),'-i',base,'-i',sequence_path,
               '-i',work/'chapters.txt','-map','0:v:0','-map','0:a:0','-map','1:s:0','-map_chapters','2',
               '-metadata:s:s:0','title=按方案顺序对比','-metadata:s:s:0','language=zho','-disposition:s:0','default',
               '-c','copy','-t',len(CASES)*LENGTH]
    attach_fonts(command)
    run(command+[sequential],work,logs)
    video_paths.insert(0,sequential)
    # This control lets the user distinguish ASS playback issues from text
    # that remains hard to read even when rendered directly into the video.
    (work/'control.ass').write_text(documents[0].dumps(),encoding='utf-8')
    baked = output/'04_播放器排查_原效果已压制.mp4'
    run([ffmpeg,'-hide_banner','-y','-i',base,'-vf','ass=control.ass','-c:v','libx264','-threads','4',
         '-preset','veryfast','-crf','18','-pix_fmt','yuv420p','-c:a','copy','-movflags','+faststart',baked],work,logs)
    video_paths.append(baked)
    # Render every distinct variant with libass, without reading or saving images.
    for i,doc in enumerate(documents,1):
        (work/'verify.ass').write_text(doc.dumps(),encoding='utf-8')
        run([ffmpeg,'-hide_banner','-y','-i',base,'-vf','ass=verify.ass,fps=2','-an','-f','null','-'],work,logs)
    probes = []
    for i,path in enumerate(video_paths):
        raw = subprocess.check_output([ffprobe,'-v','error','-show_streams','-show_chapters','-show_format','-of','json',str(path)])
        info = json.loads(raw)
        streams = info['streams']
        video = next(s for s in streams if s['codec_type']=='video')
        subtitles = [s for s in streams if s['codec_type']=='subtitle']
        expected_duration = LENGTH*len(CASES) if i==0 else LENGTH
        assert abs(float(info['format']['duration'])-expected_duration)<.25
        assert (video['width'],video['height'])==(WIDTH,HEIGHT)
        assert video['avg_frame_rate'] == ('24/1' if i==2 else '60/1')
        assert len(subtitles) == (1 if i==0 else len(CASES) if i in (1,2) else 0)
        if i==0:
            assert len(info['chapters'])==len(CASES)
        probes.append(dict(file=path.name,seconds=info['format']['duration'],frame_rate=video['avg_frame_rate'],
                           subtitle_tracks=len(subtitles),chapters=len(info['chapters']),bytes=path.stat().st_size))
    table = '\n'.join(f'{i+1:02}  {c["name"]}：{c["desc"]}' for i,c in enumerate(CASES))
    instructions = f'''弹幕清晰度对比测试

先播放：01_先看这个_全部方案顺序对比.mkv
共 {len(CASES)} 个方案，每段 {LENGTH} 秒，总长 {len(CASES)*LENGTH//60} 分 {len(CASES)*LENGTH%60} 秒。
无需手动换字幕，按顺序展示；支持 MKV 章节的播放器可直接跳到某一方案。
编号与参数在画面下方。所有方案使用同一组短句、彩色句和长句，输入时刻一致，每12秒重复一组。
原效果使用软件的真实排版函数和当前保存参数（32号、80%、12秒、最多6条）。
慢速和密度限制会减少最终显示条数，这是实际取舍；分段停顿采用独占轨道避免追尾。

快速来回比较：打开 02_切换字幕对比_60帧.mkv，在播放器字幕菜单切换 01～18。
每次切换后拖回开头比较。不同方案进出时间不一致，中途换轨可能暂时没有字。
03_切换字幕对比_24帧.mkv 使用相同背景和18条字幕，用于比较低帧率片源中的滚动效果。
字幕动画实际刷新率还受播放器影响，24/60帧文件不保证每台电视都有差异。
04_播放器排查_原效果已压制.mp4 已把原效果文字画进视频，不用开启任何字幕。
若04明显更清楚，而02的01号轨道模糊或跳动，建议重点检查播放器的ASS渲染；这只是定位线索。

用自己的电影测：从“各方案字幕”选择对应ASS，作为电影的外挂字幕，播放前36秒。
也可以加载“全部方案_顺序对比.ass”，在电影前{len(CASES)*LENGTH//60}分{len(CASES)*LENGTH%60}秒依次看全部方案。
这些是合成的测试句，不是真实电影弹幕，底部台词也是示例。保持观看距离、电视模式和播放器一致。
每个方案的背景均为：0～12秒深色，12～24秒浅色，24～36秒纹理与移动色块。
背景是人工测试图案，选好候选后仍应加载到实际电影验证。

MKV 中附带本机微软雅黑字体供本地测试。部分电视忽略内嵌字体或ASS底框、分段移动，实际效果以设备为准。
本测试没有改动软件设置，也没有查看截图。字体、视频和字幕只保存在本机测试包内。

方案表
{table}

请记录：最清楚的编号 / 最舒服的编号 / 最挡画面的编号。
只需把喜欢的编号告诉我，再把对应方案接进正式软件。
'''
    (output/'使用说明.txt').write_text(instructions,encoding='utf-8')
    (output/'验证记录.json').write_text(json.dumps(dict(cases=manifest,media=probes,validation='ASS parsed; every case rendered to null; ffprobe verified',screenshots=False),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(probes,ensure_ascii=False,indent=2),flush=True)
    print('Created '+str(output),flush=True)


FONT_REGULAR = Path('C:/Windows/Fonts/msyh.ttc')
FONT_BOLD = Path('C:/Windows/Fonts/msyhbd.ttc')

if __name__=='__main__':
    main()
