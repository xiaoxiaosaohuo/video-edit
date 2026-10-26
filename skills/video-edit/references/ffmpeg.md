# FFmpeg 执行参考

以下是本次 SDR / Rec.709 素材剪辑中使用过的参数起点。先读取本机 `ffmpeg -h`、`ffprobe -h` 与素材元数据；尺寸、色彩和音轨按实际素材调整。命令中的路径均需替换并保留引号。

## 探测与代理浏览

```sh
ffprobe -v error -show_streams -show_format -of json "media/clip-001.mp4"

mkdir -p "work/contact"
ffmpeg -i "media/clip-001.LRF" -vf "fps=1/2,scale=384:-2" \
  -q:v 3 "work/contact/%03d.jpg"
```

10-bit HEVC 不等于 HDR。检查 `color_transfer`、`color_primaries`、`color_space`，再决定色彩处理。已经是 SDR / Rec.709 的素材可以转成常见的 H.264 / yuv420p；HDR 素材需要对应的色彩转换方案。

代理只用于看图与找片段。正式画面来自原片；使用代理辅助定位时确认代理与原片的时间一致。

## 统一渲染片段

这是有音轨、4K、30fps 目标的例子；没有音轨时移除 `-af` 和对应音频编码参数。

```sh
mkdir -p "work"
ffmpeg -ss 12 -t 6 -i "media/clip-001.mp4" \
  -vf "fps=30,settb=AVTB,setpts=PTS-STARTPTS,setsar=1,format=yuv420p" \
  -af "atrim=duration=6,asetpts=PTS-STARTPTS,aresample=48000" \
  -c:v libx264 -preset veryfast -crf 20 -maxrate 45M -bufsize 90M \
  -threads 4 -profile:v high -pix_fmt yuv420p \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -c:a pcm_s16le -ar 48000 -ac 2 -map_metadata -1 "work/chunk-001.mkv"
```

所有片段使用相同尺寸、帧率、像素格式和音频采样率。混合分辨率时先缩放/留边至同一画布，按旋转元数据确认朝向。仅设置 `-r` 或色彩标签不能替代实际转换。

自然纪实可从轻微调色开始，例如 `eq=brightness=0.008:contrast=1.025:saturation=1.025:gamma=1.01`。先检查肤色与稻田高光，再决定是否使用。

4K HEVC 处理内存较高。可以从同时 2 个渲染任务、每个编码器 4 线程开始；并发度由实测速度和内存决定。先做短片段检查硬件编码可用性，失败后使用软件编码，避免反复试同一不可用路径。

## 转场

输入经过相同的帧率、时间基、尺寸、像素格式与起始时间处理后，再使用 `xfade`。音频使用对应的 `acrossfade`。

```text
[v0][v1]xfade=transition=fade:duration=0.4:offset=3.6[v01]
[a0][a1]acrossfade=d=0.4:c1=tri:c2=tri[a01]
```

这里第一段 4 秒，第二段从 3.6 秒开始。继续添加第三段时，offset 取此前合成时长减去下一次转场长度。硬切片段按顺序拼接；只将真实有重叠的相邻片段传给 `xfade`。

`concat.txt` 的相对路径以清单所在目录为基准。生成清单时使用正确转义的绝对路径，可避免目录重复解析：

```text
file '/absolute/project/work/chunk-001.mkv'
file '/absolute/project/work/chunk-002.mkv'
```

## 原声与配乐

查看片段响度起点：

```sh
ffmpeg -hide_banner -ss 12 -t 6 -i "media/clip-001.mp4" \
  -vn -af volumedetect -f null -
```

优先让对白清晰、各镜头原声音量接近。可从较小增益开始；本次实践将增益上限设为 +4dB，并用峰值余量约束增益。`mean_volume` 不是感知响度，不能替代听辨。

对白清理可按需使用 `highpass=f=100,lowpass=f=8000` 和轻度压缩 `acompressor=threshold=0.16:ratio=2.2:attack=12:release=180`。保留自然环境声时按实际需要调整频宽。

音频边缘可用约 75ms 淡入和 120ms 淡出，淡出起点为使用时长减 0.12 秒。句尾余量不足时先修正剪点，不直接淡掉最后一个字。

配乐单独保存，根据镜头清单压低对白范围的配乐。可在对白前约 0.45 秒渐降、对白后约 0.45 秒恢复。本次钢琴与尼龙吉他伴奏采用约 80 BPM；结尾和弦、尾音和淡出需要随剪辑时长调整。

本次混音参考：配乐先规范到约 `I=-23.5:TP=-3:LRA=10`；与原声混合后约 `I=-18.5:TP=-1.5:LRA=9`。这些是起点，按原声内容听辨调整，检查实际峰值。

## 高清与分享导出

从统一编码的片段拼接时可复制视频；最终把 PCM 音频编码为 AAC：

```sh
mkdir -p "output"
ffmpeg -f concat -safe 0 -i "work/concat.txt" \
  -map 0:v:0 -map 0:a:0 -c:v copy -c:a aac -b:a 256k \
  -movflags +faststart "output/film-4k.mp4"

ffmpeg -i "output/film-4k.mp4" -map 0:v:0 -map 0:a:0 \
  -vf "scale=1920:1080:flags=lanczos" \
  -c:v libx264 -preset fast -crf 22 -maxrate 6M -bufsize 12M \
  -threads 4 -pix_fmt yuv420p -c:a copy \
  -movflags +faststart "output/film-1080p.mp4"
```

`+faststart` 便于边下载边开始播放。1080p 预览版仍可能较大；需要转发时另外压缩分享版。

```text
视频码率 kb/s = (目标字节数 - 封面与封装预留字节数) × 8 / 时长秒数 / 1000 - 音频码率 kb/s
```

本次约 134 秒视频，以 720p、视频 1300kb/s、AAC 128kb/s 两遍编码得到约 24MB，附加封面后约 25MB。文件大小以导出后实际字节数为准；若有硬性大小上限，需要增加余量。

```sh
ffmpeg -i "output/film-1080p.mp4" -map 0:v:0 \
  -vf "scale=1280:720:flags=lanczos" \
  -c:v libx264 -preset fast -b:v 1300k -maxrate 2200k -bufsize 4400k \
  -threads 4 -pix_fmt yuv420p -pass 1 -passlogfile "work/share-pass" \
  -an -f null /dev/null

ffmpeg -i "output/film-1080p.mp4" -map 0:v:0 -map 0:a:0 \
  -vf "scale=1280:720:flags=lanczos" \
  -c:v libx264 -preset fast -b:v 1300k -maxrate 2200k -bufsize 4400k \
  -threads 4 -pix_fmt yuv420p -pass 2 -passlogfile "work/share-pass" \
  -c:a aac -b:a 128k -movflags +faststart "output/film-share.mp4"
```

两遍编码使用同一套视频参数。`/dev/null` 是 macOS / Linux 示例，其他系统使用对应的空设备。

## 检查与清理

```sh
ffprobe -v error -show_streams -show_format -of json "output/film-share.mp4"
ffmpeg -v error -i "output/film-share.mp4" -map 0:v:0 -map 0:a:0 -f null -
ffmpeg -hide_banner -i "output/film-share.mp4" -map 0:a:0 \
  -af volumedetect -f null -
```

核对主视频轨道和音频轨道的时长、帧数、尺寸和起始时间。固定 30fps 时，主视频帧数应与时间轴预期一致。音视频时长差一般应控制在一帧附近；存在片尾尾音时以设计为准。

检查每个修改处、首尾和转场；错误日志为空不能证明镜头选得正确。重编码或拼接出错时扩大解码检查范围。

用户要求清理后，以明确的保留清单保护原片、最终版本和剪辑清单，再删除旧输出与可重建的渲染缓存。只清理本项目，报告实际删除数量与大小。
