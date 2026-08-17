# ride-overlay

一个将 FIT/GPX 运动数据转换为仪表盘叠加视频的开源 Python 命令行工具。生成的视频可导入剪辑软件，叠加到骑行录像上显示实时速度、里程等数据。

当前版本为 MVP，已支持：

- FIT 和 GPX 输入；
- 速度、里程、时长、当前时间、海拔、温度、气压、踏频、心率和功率；
- 坡度、累计爬升、消耗热量及速度/心率/踏频/功率的运行平均值；
- 完整路线、已完成路线和随运动方向旋转的当前位置轨迹仪表盘；
- 时间范围截取、平滑、线性插值和独立刷新间隔；
- 自定义字体、字号、九宫格对齐、颜色和描边；
- 静态图片预览；
- H.264 绿幕 MP4；
- 带 alpha 通道的 ProRes 4444 MOV。

## 安装

推荐使用 Conda。项目需要 Python 3.11–3.13 和 FFmpeg：

```bash
conda env create -f environment.yml
conda activate ride-overlay
```

如果已经手动创建环境：

```bash
conda create --name ride-overlay python=3.13 pip ffmpeg -c conda-forge
conda activate ride-overlay
python -m pip install -e '.[dev]'
```

确认安装：

```bash
ride-overlay --help
ffmpeg -version
```

## 使用

每次运行都需要传入一个项目目录：

```bash
ride-overlay /path/to/my-ride --preview
ride-overlay /path/to/my-ride
```

也可以直接运行脚本：

```bash
python ride_overlay.py /path/to/my-ride --preview
```

项目目录应包含：

```text
my-ride/
├── config.json              必需，固定名称
├── activity.fit             FIT 或 GPX，必需
├── dashboard-font.otf       OTF、TTF 或 TTC，必需
├── dashboard-background.png 可选
├── new-arrow.png             可选，工程自定义当前位置图案
└── camera-video.mp4          可选，仅供静态预览抽帧
```

当运动文件或字体没有在配置中明确填写时，工具会按文件名排序选择目录中第一个匹配文件；存在多个候选时会打印警告。背景图片留空表示不叠加图片。

可以从 [config.example.json](config.example.json) 开始创建配置：

```bash
cp config.example.json /path/to/my-ride/config.json
```

## 配置

配置顶层包含四部分：

- `inputs`：运动文件、字体和仪表盘背景 PNG；
- `clip`：相对于运动数据起点的开始、结束秒数；
- `output`：分辨率、帧率、背景模式和输出文件；
- `dashboards`：需要显示的指标及其格式和位置。

文件名可以使用 `null` 或空字符串表示自动选择。`clip.start_seconds`、`clip.end_seconds` 同时留空表示完整运动范围。正式输出要求：

```text
0 <= start_seconds < end_seconds <= 运动总时长
```

锚点以画面左上角为 `(0, 0)`、右下角为 `(1, 1)`。`align` 支持：

```text
top_left       top_center       top_right
middle_left    center           middle_right
bottom_left    bottom_center    bottom_right
```

颜色使用 `#RRGGBB` 或 `#RRGGBBAA`。字体大小和描边宽度以输出视频像素为单位。
传统数字仪表盘的 `type` 可以省略，省略时等同于 `"type": "numeric"`；图形仪表盘需要明确填写其类型。

### 指标和单位

| `source` | 含义 | 可用单位 | 默认单位 |
| --- | --- | --- | --- |
| `speed` | 当前速度 | `km/h`、`m/s`、`mph` | `km/h` |
| `distance` | 当前累计里程 | `km`、`m`、`mi` | `km` |
| `elapsed_time` | 从运动数据起点经过的时长 | `hms` | `hms` |
| `current_time` | 当前运动记录对应的本地时钟时间 | `hms` | `hms` |
| `altitude` | 当前海拔 | `m`、`ft` | `m` |
| `temperature` | 当前温度 | `C`、`F` | `C` |
| `pressure` | 当前气压 | `Pa`、`hPa`、`kPa`、`mmHg` | `hPa` |
| `cadence` | 当前踏频 | `rpm` | `rpm` |
| `heart_rate` | 当前心率 | `bpm` | `bpm` |
| `power` | 当前功率 | `W` | `W` |
| `grade` | 当前坡度 | `%` | `%` |
| `total_ascent` | 截至当前的累计爬升 | `m`、`ft` | `m` |
| `calories` | 截至当前的消耗热量 | `kcal` | `kcal` |
| `average_speed` | 截至当前的平均速度 | `km/h`、`m/s`、`mph` | `km/h` |
| `average_heart_rate` | 截至当前的平均心率 | `bpm` | `bpm` |
| `average_cadence` | 截至当前的平均踏频 | `rpm` | `rpm` |
| `average_power` | 截至当前的平均功率 | `W` | `W` |

`elapsed_time` 和 `current_time` 固定输出 `HH:MM:SS`，其 `precision` 和 `pad_zeros` 配置不会生效。`current_time` 使用运动记录的时间戳，并转换到运行工具的计算机本地时区；跨过午夜后会从 `00:00:00` 继续。其余指标只绘制数值，单位文字可放在仪表盘背景图片中。

### 数据来源和派生规则

`speed` 支持 `km/h`、`m/s` 和 `mph`。FIT 优先读取 `enhanced_speed`、其次读取 `speed`；缺少速度字段时尝试根据带时间戳的 GPS 点推导。GPX 通常采用 GPS 点推导速度。

瞬时速度可设置时间窗平滑：

```json
"smoothing": {
  "method": "moving_average",
  "window_seconds": 1.0
}
```

当前支持 `none` 和居中时间窗 `moving_average`。

`distance` 支持 `km`、`m` 和 `mi`。优先使用运动文件中的累计距离，否则根据 GPS 点累计；GPX 的不同 track segment 之间不会计算跳跃距离。

距离是累计型指标，不允许配置平滑。默认从整段活动起点累计；如需在截取起点归零：

```json
"cumulative_origin": "clip_start"
```

FIT 的海拔优先使用 `enhanced_altitude`。坡度优先读取运动文件的原始坡度；缺失时，工具先按 GPX segment 对海拔做 5 秒居中平滑，再在约 20 米距离窗口中按“高度变化 ÷ 水平距离 × 100”计算。移动距离不足 5 米时不生成坡度，避免停车状态下产生极端数值。

累计爬升不是终点与起点的海拔差。工具会对每个连续 segment 分别处理，将全部有效上升逐段加入累计值；默认使用 5 秒海拔平滑和 1 米高程消抖阈值，下降不会抵消已经获得的爬升。若运动文件直接提供了连续的累计爬升数据，则优先使用原始数据。

热量单位固定为千卡 `kcal`。优先使用逐点累计热量，其次使用 lap/session 汇总；某些 FIT 只保存全程总热量，这种情况下工具会在相邻汇总时刻间线性估算当前累计热量。

`average_speed`、`average_heart_rate`、`average_cadence` 和 `average_power` 是从运动起点到当前时刻的时间加权运行平均值。超过 5 秒的缺失区间不会加入平均值计算；空洞期间保持最后一个已经计算出的平均值，因此平均仪表盘不会消失。

GPX 标准本身通常没有心率、踏频、功率、温度和气压。工具会识别常见 Garmin TrackPointExtension 以及按本地标签命名的 `hr`、`cadence`、`power`、`atemp`、`pressure`、`grade` 等扩展；文件未提供的指标会警告并跳过。

### 轨迹仪表盘

轨迹仪表盘使用运动文件中的经纬度显示完整路线、截至当前时刻的已完成路线和当前位置。示例：

```json
{
  "type": "trajectory",
  "id": "route",
  "width": 0.3,
  "anchor": {"x": 0.95, "y": 0.05},
  "align": "top_right",
  "update_interval_ms": 200,
  "line_width": 8,
  "remaining_color": "#FFFFFF66",
  "completed_color": "#00E676CC",
  "marker_image_file": null,
  "marker_scale": 2.0
}
```

`width` 是轨迹外接矩形宽度占输出画面宽度的比例，范围为 `(0, 1]`。高度根据等距投影后的轨迹外接矩形宽高比自动计算；更改输出分辨率时轨迹占画面宽度的比例不变。如果按该比例计算出的高度或锚点位置超出画面，工具会在终端和 `result.log` 中警告，但不会擅自缩小轨迹。

轨迹固定为上北下南。当前位置图片的默认资源为项目仓库中的 `assets/images/arrow.png`，图片默认朝上；渲染时会旋转到同一连续轨迹段中的下一个不同位置点。`marker_scale` 是相对于图片原始像素尺寸的缩放倍数。需要覆盖默认图案时，将 PNG 放在当前工程目录中，并将 `marker_image_file` 写成相对于工程目录的路径，例如 `"new-arrow.png"`；绝对路径及指向工程目录外的 `..` 路径会被拒绝。

位置记录超过 5 秒的空洞、GPX track segment 切换和明显不可信的位置跳跃都不会被直线连接。空洞期间，箭头停留在最后一个可靠位置并保持最后的可靠朝向，位置恢复后再跳到恢复点。已完成轨迹始终从整段运动的起点计算，不受视频截取起点影响。

两种轨迹颜色都支持 alpha。绘制时先生成每种状态各自的覆盖蒙版，再只应用一次颜色，因此同一折线的圆角、端点或自重叠不会因重复涂色而局部变深；已完成和未完成路线真正重叠时，两种状态仍会按透明度混合。

### 刷新和插值

`update_interval_ms` 控制数字多久变化一次，与视频 FPS 相互独立。刷新时刻落在相邻原始采样点之间且间隔不超过 5 秒时使用线性插值，两个刷新时刻之间保持上一个显示值。

数据空洞按指标类型处理：

- 速度、踏频、心率、功率等瞬时指标在超过 5 秒的空洞中显示 `-`，避免用过时数据冒充实时值；
- 平均指标忽略缺失区间，并保持最后一个已经计算出的平均值；
- 里程、累计爬升、热量等累计指标不会因空洞消失：连续采样中断时保持已有结果；只有汇总数据时按前述热量等指标的估算规则更新。

## 预览模式

```bash
ride-overlay my-ride --preview
```

预览模式会：

1. 检查配置、输入文件和各仪表盘是否可用；
2. 在目录中按文件名选择第一个视频；
3. 提取视频中间帧作为底图；
4. 使用相对于截取起点相同秒数的运动数据；
5. 输出 `preview.png`。

没有视频时，使用截取运动范围的中点数据和输出背景。运动文件中不可用的指标会产生警告并被跳过，不会阻止其他指标预览。

## 输出模式

`output.background.mode` 决定编码方式：

| 模式 | 默认文件 | 视频编码 | 用途 |
| --- | --- | --- | --- |
| `chroma_key` | `overlay.mp4` | H.264/yuv420p | 文件较小，在剪辑软件中抠除绿色 |
| `transparent` | `overlay.mov` | ProRes 4444/yuva444p10le | 直接保留 alpha，文件可能很大 |

普通 H.264 MP4 不保存透明通道，因此透明模式只接受 `.mov`，绿幕模式只接受 `.mp4`。显式配置不匹配的扩展名会报错，而不会静默丢失 alpha。

视频先写入项目目录中的临时文件，编码成功后再原子替换最终输出，避免失败时留下半成品或破坏上一次成功结果。

## 工作结果日志

每次执行都会生成一份 UTF-8 编码的 `result.log`。正式渲染时，它与输出视频位于同一目录；预览模式也会生成同样的报告。如果任务在输出路径解析前失败，则尽量将报告写到项目目录。日志采用临时文件加原子替换的方式写入，每次运行覆盖上一份报告，使内容始终对应当前输出结果。

报告包括：

- 唯一运行 ID、程序版本、模式、命令、运行环境、最终状态和退出码；
- 每个处理阶段的开始/结束时间、耗时和成功/失败状态；
- 完整配置快照、已解析的输入/输出文件及文件大小；
- 运动记录数量、运动起止时间、总时长，以及每项指标的数据来源、样本数量、缺失数量、覆盖率、数值范围和覆盖范围；
- 每个仪表盘的启用或跳过状态、平滑与刷新设置、数据空洞的起止时刻、长度和处理策略；轨迹还会记录有效/缺失/过滤点数、分段与断点、投影外接矩形、缩放、实际矩形、越界方向及箭头资源；
- 预览源视频与抽帧时刻，或输出视频的编码器、帧数、分辨率、FPS 和文件大小；
- 本次任务的全部 INFO、WARNING、ERROR 以及详细 DEBUG 事件。

终端是否显示 DEBUG 信息仍由 `--verbose` 控制，但 `result.log` 始终记录详细事件。报告可能包含本地路径、运动时间和素材文件名，分享前请检查隐私信息。

## 开发

```bash
conda activate ride-overlay
python -m pytest
ruff check .
```

核心代码按职责拆分为三个模块：

- [ride_overlay.py](ride_overlay.py)：配置加载、工程文件解析、任务报告、预览/视频编码和 CLI 编排；
- [ride_overlay_data.py](ride_overlay_data.py)：FIT/GPX 读取、统一数据模型、位置清洗与投影、插值、统计及派生指标；
- [ride_overlay_dashboard.py](ride_overlay_dashboard.py)：数字/轨迹仪表盘配置、空洞策略、数值格式化、采样和 Pillow 绘制。

`ride_overlay.py` 会继续转发原有的公共类型和函数，因此现有的 Python 导入方式保持兼容。这一边界也为后续增加轨迹、曲线等图形仪表盘提供独立扩展位置。

真实 FIT/GPX 可能包含精确时间和 GPS 轨迹，原始骑行视频也很大。仓库默认忽略 `test_proj/`；请只提交合成或完成脱敏的测试数据。

## License

GNU General Public License v3.0，详见 [LICENSE](LICENSE)。
