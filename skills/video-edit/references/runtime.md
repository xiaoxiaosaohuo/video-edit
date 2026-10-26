# 可执行的重建、验收与独立审片

入口是技能目录下的 `scripts/video_edit.py`，基础依赖为 Python 3.9+、FFmpeg 和 FFprobe。下文在仓库根目录运行；`PROJECT` 替换为本地项目目录。

## 一份最终清单

`PROJECT/edit-plan.json` 是唯一渲染输入，所有相对素材和资产路径以清单目录解析。渲染器直接读取每条原片入点、时长和转场，不读取历史修订脚本或旧版 concat 清单。

在 [基础示例](../examples/edit-plan.json) 上可以增加：

```json
{
  "size": [3840, 2160],
  "video_filter": "eq=contrast=1.025:saturation=1.025",
  "audio": {
    "music": "assets/music.wav",
    "duck": true,
    "voice_filter": "highpass=f=100,lowpass=f=8000"
  },
  "cover": {
    "image": "assets/cover.jpg",
    "hold_seconds": 0.75,
    "fade_seconds": 0.35,
    "embedded_poster": true
  }
}
```

- `size`：固定输出画布，宽高均为正偶数。`fps` 是固定输出帧率，使用正整数。
- `video_filter`：所有镜头共同的 FFmpeg 调色滤镜；源素材需先准备为 SDR。
- `audio.music`：持久配乐资产。`duck: true` 根据对白镜头降低配乐；已经做过闪避的音轨设置为 `false`。
- 每个镜头可设置 `gain_db`、`foreground_audio`、`fade_in` 和 `fade_out`。
- 片名叠图可在镜头上配置 `overlay`，字段为 `image`、`fade_in_start`、`fade_in_seconds`、`fade_out_start`、`fade_out_seconds`，单位为秒。
- 配乐、片名、封面放在 `assets/`，保留原片与最终清单；可删除的缓存放在 `work/render-cache/`。

缓存键包含渲染器代码、镜头参数、画布、原片与相关资产的大小和修改时间。删除部分或全部缓存后会重新生成，未变化的其他片段继续复用。MOV 缓存统一为 H.264、yuv420p、有限色彩范围、视频时间基 1/90000 和 48kHz 立体声 PCM。

## 先审片，再渲染最终版

从原片生成低清连续审片包：

```sh
python3 skills/video-edit/scripts/video_edit.py review "PROJECT/edit-plan.json" \
  --output-dir "PROJECT/review"
```

如果已经有这份清单对应的成片，可以直接生成审片包，无须连接原片：

```sh
python3 skills/video-edit/scripts/video_edit.py review "PROJECT/edit-plan.json" \
  --video "PROJECT/output/film-4k.mp4" --output-dir "PROJECT/review"
```

产物包括完整低清预览、逐镜带原声的连续片段、`index.html` 与 `review.json`。每段包含前后各约半秒余量，便于检查衔接；不会用首中尾截图代替完整运镜。

打开 `index.html`，审片者逐段检查构图、运镜、完整动作和对白。遇到问题填入时间和原因，并选择“需要重剪”。审片者可为能实际查看和听辨素材的 Agent 或用户。转写结果不自动将镜头标为通过。

网页支持下载决定 JSON；Agent 也可以直接编辑同样的 JSON。填写 `reviewer`，每条镜头的 `decision` 为 `pending`、`passed` 或 `needs_edit`。审片文件中的 `plan_digest` 必须对应当前清单，镜头必须完整且没有重复；修改清单后需要重新审片。

所有镜头通过后渲染：

```sh
python3 skills/video-edit/scripts/video_edit.py render "PROJECT/edit-plan.json" \
  --review "PROJECT/review/review-decisions.json" \
  --output "PROJECT/output/rebuilt-4k.mp4"
```

原片目录变化时加 `--source-root "/path/to/originals"`，按原文件名查找。快速验证或导出其他画布时可加 `--size 1280x720`。修改原片或资产后也应重新审片。

渲染器缺失原片时会明确报出路径。缺失缓存只触发重建；缺失配乐、片名或封面则需要恢复持久资产。输出完成并通过验收后，才原子替换目标文件，写入同名 `.verify.json`。

## 通用媒体验收

```sh
python3 skills/video-edit/scripts/video_edit.py verify "PROJECT/output/rebuilt-4k.mp4" \
  --plan "PROJECT/edit-plan.json" --report "PROJECT/output/verification.json"
```

验收读取主视频、音频和封面轨道，核对视频时长、音视频时长差、帧数、帧率、封面存在性，并实际解码视频与音频。报告尺寸、时间基、峰值和实际字节数；可能削波的峰值会标记在报告中。

拼接前检查各缓存的视频/音频格式及时间基一致。这个检查与拼接后验收配合，可以拦截“容器总长正确，但视频轨道被错误缩短”的情况。

仅检查外部视频时省略 `--plan`；需要要求封面时使用 `--expect-poster`。

## 对白与已有转写

优先使用当前 Agent 环境已有的音频能力，不自行安装语音模型。当前工具无法听辨时明确说明，结合用户提供的台词和波形定位候选剪点，并保留带原声的连续预览供确认。不能将波形变化当成文字识别，也不能将未经听辨的对白标为已通过。

如果已经有核对过的词级转写，可以加入审片页面：

```sh
python3 skills/video-edit/scripts/video_edit.py review "PROJECT/edit-plan.json" \
  --video "PROJECT/output/film-share.mp4" --transcript "PROJECT/review/transcript.json" \
  --output-dir "PROJECT/review"
```

转写 JSON 包含 `input`（输入视频完整路径）、`input_fingerprint`（依次为完整路径、文件字节数、`stat().st_mtime_ns`）和 `segments`。每个段落的 `words` 包含 `word`、`start`、`end`，时间单位为输入视频时间轴的秒。页面可以点击词条跳到对应片段位置。转写文件必须属于同一个 `--video` 输入视频；原片时间与成片时间不能混用。转写仍可能有错字和时间误差，不能代替听辨审片。

## 回归测试

```sh
python3 -m unittest discover -s tests -v
```

测试使用临时合成素材，覆盖不同源帧率、无音轨镜头、转场、配乐闪避、片名叠图、首帧与封面轨道、删除缓存后的重建、错误时间基、截断视频以及审片未通过或清单变化时拒绝最终渲染。
