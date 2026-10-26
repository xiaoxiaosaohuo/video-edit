# 封面、首帧与时间基

## 制作封面图片

先从成片里找情绪明确、人物自然、能放标题的真实画面。对比几个相邻帧，检查闭眼、手部遮挡、人物裁切和背景。

```sh
mkdir -p "work"
ffmpeg -ss 12.8 -i "output/film-4k.mp4" -map 0:v:0 \
  -frames:v 1 "work/cover-source.png"
```

使用当前环境允许的图像编辑工具排版，先读取相应技能。以截帧为编辑目标，要求保持真实人物的五官、表情、姿态、服装和场景不变；在留白处放标题与拍摄日期。生成后对照原帧检查人物、手指和文字。

可使用的提示词骨架：

> 在这张真实视频截帧上只添加标题。保持人物身份、五官、表情、服装、姿态与背景不变。主标题为“影片标题”，小字为实际拍摄日期。文字放在不遮挡人物的一侧，使用清晰的中文宋体风格、暖白色，必要时仅在文字后添加柔和暗色渐变。保持原画幅与自然色彩。

单独交付 `output/cover.jpg`。横屏片可以采用 1920×1080；实际画幅不同则按成片画幅设计。

## 两个位置都要检查

MP4 可以携带 `attached_pic` 封面轨道，播放器也可能直接用视频首帧做预览。部分发布平台还需要单独上传封面图片，因此交付 JPG，而不是承诺所有平台都会自动识别。

只嵌入封面、不改视频画面的例子：

```sh
ffmpeg -i "output/film-share.mp4" -i "output/cover.jpg" \
  -map 0:v:0 -map 0:a:0 -map 1:v:0 -c copy \
  -disposition:v:1 attached_pic -metadata:s:v:1 title=Cover \
  -movflags +faststart "output/film-share-covered.mp4"
```

需要处理首帧时，可覆盖开头约 0.75 秒，再用约 0.35 秒淡入原来的画面，总时长不变。此时原来的开头 1.1 秒会被遮挡；如果这些内容必须保留，改为增加封面时长，并重算后续时间轴。

一种直接做法是先重编码开头覆盖了封面的完整视频，再运行上面的封面轨道命令。核心滤镜：

```text
[1:v]scale=1920:1080,setsar=1,format=rgba,
fade=t=out:st=0.75:d=0.35:alpha=1[c];
[0:v]fps=30,setpts=PTS-STARTPTS[b];
[b][c]overlay=shortest=1:format=auto:enable='lt(t,1.1)',format=yuv420p[v]
```

这里输入 0 是视频，输入 1 是用 `-loop 1 -framerate 30 -i cover.jpg` 读取的图片。画布、帧率和色彩按成片调整；保留原音频并核对输出时长。

## 只重编码开头的优化

仅在已规范为固定 30fps、H.264，且能保持编码兼容、尺寸和像素格式一致时使用。其他情况直接重编码更容易核对。

1. 用 FFprobe 找到封面淡出结束之后的首个关键帧时间 `K`。
2. 30fps 成片可用 `N = round(K × 30)` 算该关键帧之前的帧数，并用实际帧数检查确认。
3. 重编码最初 N 帧，覆盖封面；其余视频从关键帧 K 开始复制。
4. 两段统一 `video_track_timescale`，拼接视频，再复制原音频并附加封面。
5. 核对 N + 尾部帧数 = 原视频帧数，输出主视频时长与音频时长正确，拼接位置可正常解码。

查关键帧：

```sh
ffprobe -v error -select_streams v:0 -skip_frame nokey \
  -read_intervals '0%15' -show_entries frame=pts_time -of json \
  "output/film-share.mp4"
```

开头使用 `-frames:v N` 控制帧数，避免小数时长向上取整多出一帧。重编码开头和复制尾部时都设置：

```text
-video_track_timescale 90000
```

复制尾部的命令骨架：

```sh
ffmpeg -ss K -i "output/film-share.mp4" -map 0:v:0 -an \
  -c:v copy -video_track_timescale 90000 "work/tail.mp4"
```

`K`、`N` 是说明中的占位值，执行前替换为实际数字。两段用 concat demuxer 拼接；原音频从完整源视频映射。需要嵌入 JPG 时，把它作为额外输入并设置 `attached_pic`。

## 已遇到的时间基问题

本次在开头片段使用 `1/90000`、尾部仍使用 `1/16000` 时，直接 concat 得到了约 24 秒的视频轨道和约 134 秒的音频轨道。容器总时长仍接近 134 秒，帧数也没变，单看这两个数字会漏掉问题。

将两段都设为 `-video_track_timescale 90000` 后，视频轨道、音频轨道和预期帧数恢复一致。这个问题不是通过强行修改容器时长解决的。

## 交付检查

- 主视频、音频和 `attached_pic` 是独立轨道，探测时分别识别。
- 主视频帧数符合预期，视频与音频时长及起始时间匹配。
- 开头 0 秒确实显示封面，淡入正常，后续关键帧拼接处无闪帧或解码错误。
- 核对标题、拍摄日期、真实人物和手机预览下的可读性。
- 只复制尾部时，可比较尾部非关键帧的包哈希与全部音频包哈希，确认正文和原声未被重编码。
- 三个导出版本均更新并检查后，再清理替换前的版本与临时片段。
