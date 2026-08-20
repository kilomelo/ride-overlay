# ride-overlay

一个将 FIT/GPX 运动数据转换为骑行仪表盘视频的开源 Python 工具。既可以通过命令行生成透明或绿幕覆层素材，也可以在跨平台图形编辑器中直接预览骑行视频、调整仪表盘并导出带原视频音频的完整成片。

当前版本为 MVP，已支持：

- FIT 和 GPX 输入；
- 速度、里程、时长、当前时间、海拔、温度、气压、踏频、心率和功率；
- 坡度、累计爬升、消耗热量及速度/心率/踏频/功率的运行平均值；
- 完整路线、已完成路线和随运动方向旋转的当前位置轨迹仪表盘；
- 按实时心率平滑改变透明度的心跳动画仪表盘；
- 时间范围截取、平滑、线性插值和独立刷新间隔；
- 自定义字体、字号、九宫格对齐、颜色和描边；
- 静态图片预览；
- H.264 绿幕 MP4；
- 带 alpha 通道的 ProRes 4444 MOV。
- Windows、macOS、Linux 图形编辑器；
- 多段骑行视频的统一播放、定位和连接点显示；
- 自动识别相邻片段的重复画面，并在预览和导出中去除前一段末尾的重复帧；
- 编辑器预览播放源视频原声；
- 鼠标调整仪表盘位置、字号或宽度，并实时同步 `config.json`；
- 手动逐帧调整视频与运动数据的时间偏移；
- 将仪表盘直接合成到完整骑行视频，并保留源视频音频。

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
python -m pip install -e '.[dev,gui]'
```

确认安装：

```bash
ride-overlay --help
ride-overlay-editor --help
ffmpeg -version
```

## 使用

每次运行都需要传入一个项目目录：

```bash
ride-overlay /path/to/my-ride --preview
ride-overlay /path/to/my-ride
ride-overlay /path/to/my-ride --editor
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
├── new-heart.png             可选，工程自定义心脏图案
├── 20260817072500_001.mp4    图形编辑器使用的第一段视频
├── 20260817073000_002.mp4    可选的后续视频片段
└── export/                   自动创建，存放所有生成结果
```

当运动文件或字体没有在配置中明确填写时，工具会按文件名排序选择目录中第一个匹配文件；存在多个候选时会打印警告。背景图片留空表示不叠加图片。

可以从 [config.example.json](config.example.json) 开始创建配置：

```bash
cp config.example.json /path/to/my-ride/config.json
```

## 配置

配置顶层包含以下内容：

- `opacity`：所有仪表盘和仪表盘背景图的全局透明度；
- `inputs`：运动文件、字体、仪表盘背景 PNG 和可选视频列表；
- `clip`：相对于运动数据起点的开始、结束秒数；
- `timeline`：图形编辑器的视频/运动数据时间对齐信息；
- `output`：分辨率、帧率、背景模式和输出文件；
- `dashboards`：需要显示的指标及其格式和位置。

运动文件和字体可以使用 `null` 或空字符串表示自动选择。`inputs.video_files` 留空时，编辑器首次打开项目会扫描项目目录第一层，按包含数字的自然文件名排序，并把确定的视频列表写回 `config.json`；之后以配置中的顺序为准，也可以手工增删或调整：

```json
"video_files": [
  "20260817072500_001.mp4",
  "20260817073000_002.mp4"
]
```

`opacity` 的范围是 `0` 到 `1`，默认为 `1.0`。它在各仪表盘完成内部合成后统一乘到最终 alpha 上，因此同时影响数字、轨迹、心跳动画和 `inputs.background_image_file`，但不会改变它们之间的内部混色结果。

`clip.start_seconds`、`clip.end_seconds` 同时留空表示完整运动范围。`clip` 只影响命令行的静态预览和覆层素材输出，图形编辑器始终使用完整运动数据和完整视频。命令行正式输出要求：

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
  "margin": 0.02,
  "anchor": {"x": 0.95, "y": 0.05},
  "align": "top_right",
  "update_interval_ms": 200,
  "line_width": 8,
  "remaining_color": "#FFFFFF66",
  "completed_color": "#00E676CC",
  "overlap_blend_mode": "uniform",
  "background_color": null,
  "background_corner_radius": 24,
  "marker_image_file": null,
  "marker_scale": 2.0
}
```

`width` 是整个轨迹仪表盘外接矩形宽度占输出画面宽度的比例，范围为 `(0, 1]`。轨迹中心线缩放前会从矩形内部扣除线宽和当前位置图片旋转后的最大半径。`margin` 是相对于仪表盘外接矩形宽高的留白比例，可设为 `-0.25` 到 `0.25`，默认 `0.02`：正值使轨迹和箭头向矩形内收，负值则允许它们超出矩形。负边距造成的视觉溢出不会被提前裁掉；编辑器紫色虚线框与可选背景仍使用原始仪表盘矩形。高度根据等距投影后的轨迹宽高比、视觉半径及边距自动计算。更改输出分辨率时轨迹占画面宽度的比例不变。如果计算出的高度或锚点位置超出画面，工具会在终端和 `result.log` 中警告，但不会擅自缩小仪表盘。`width` 小到无法容纳线宽、箭头和正边距时会明确报错。

`background_color` 设置可选的圆角矩形背景，使用 `#RRGGBB` 或 `#RRGGBBAA`；设为 `null` 或留空时不绘制背景。`background_corner_radius` 是输出像素单位的圆角半径，背景尺寸正好等于上述仪表盘外接矩形。

轨迹固定为上北下南。当前位置图片的默认资源为项目仓库中的 `assets/images/arrow.png`，图片默认朝上；渲染时会旋转到同一连续轨迹段中的下一个不同位置点。`marker_scale` 是相对于图片原始像素尺寸的缩放倍数。需要覆盖默认图案时，将 PNG 放在当前工程目录中，并将 `marker_image_file` 写成相对于工程目录的路径，例如 `"new-arrow.png"`；绝对路径及指向工程目录外的 `..` 路径会被拒绝。

位置记录超过 5 秒的空洞、GPX track segment 切换和明显不可信的位置跳跃都不会被直线连接。空洞期间，箭头停留在最后一个可靠位置并保持最后的可靠朝向，位置恢复后再跳到恢复点。已完成轨迹始终从整段运动的起点计算，不受视频截取起点影响。

用于绘图的中心线会以 `line_width` 的约 10%（最低 0.5 像素）为容差移除肉眼不可分辨的 GPS 微小抖动，同时强制保留折返点；原始轨迹数据、当前位置和统计计算不受影响。轨迹和背景蒙版最多以 4 倍分辨率绘制后缩小，从而让粗线边缘保持平滑。

两种轨迹颜色都支持 alpha。为兼容已有配置，完整轨迹底色仍使用 `remaining_color` 这个字段名，但它现在表示“完整轨迹颜色”：完整轨迹始终绘制，并始终位于已完成轨迹下方。这样已完成部分不会替换或移除底层，轨迹的整体轮廓和基础透明度在整个视频中保持稳定。

`overlap_blend_mode` 有两种模式：

- `"uniform"`（默认）为完整轨迹和已完成轨迹分别生成统一覆盖蒙版，每种颜色只应用一次；圆角、端点和自重叠不会局部变深。
- `"accumulate"` 在最终渲染像素域内统计独立经过次数。移动轨迹的带线宽区域连续覆盖同一像素时只算一次；离开后再次覆盖才增加一次，并将 `completed_color` 再混合一层。因此折返路线在第二、第三次完成后会逐次加深，同时相邻线段接头不会产生色斑。

`accumulate` 反映的是当前缩放比例和 `line_width` 下可见线条的重叠，不是地图道路拓扑：空间上接近到足以覆盖同一像素的平行道路也会被视为重叠，高架与地面道路交叉也无法仅凭二维位置区分。该模式会在首次渲染轨迹时进行一次像素覆盖预计算，复杂轨迹的启动时间和内存占用会高于 `uniform`。

### 心跳动画仪表盘

心跳动画仪表盘使用当前心率控制一张心脏图片的透明度循环：每次循环按照平滑的余弦曲线完成 `1 → 0 → 1`，循环时长为 `60 ÷ 当前心率` 秒。例如 120 bpm 对应 0.5 秒完成一次完整透明度循环。

```json
{
  "type": "heartbeat",
  "id": "heartbeat",
  "width": 0.08,
  "anchor": {"x": 0.1, "y": 0.5},
  "align": "center",
  "heart_image_file": null
}
```

`width` 是图片宽度占输出画面宽度的比例，范围为 `(0, 1]`；图片高度根据原图宽高比自动计算，不需要配置缩放比例。`anchor` 和 `align` 的含义与其他仪表盘一致。默认图片为 `assets/images/heart.png`，也可以将自定义 PNG 放入当前工程，并在 `heart_image_file` 中填写相对于工程目录的路径。绝对路径和指向工程外的路径会被拒绝。

为了避免瞬时心率大幅变化造成动画跳动，当前循环期间不会改变速度；只有当透明度重新回到 1、一个完整循环结束时，才读取该时刻的心率并设置下一循环速度。因此循环边界上的透明度和变化方向保持连续。心率数据短暂缺失时沿用上一个有效循环频率；动画开始后尚未获得有效心率时，图片保持完全不透明。

心跳动画每一视频帧都会更新，不使用 `update_interval_ms`。在 `--preview` 模式下图片始终以原图完整透明度渲染，即使预览时刻刚好处于心率数据空洞中，也能准确检查其大小和位置。

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
5. 输出 `export/preview.png`。

没有视频时，使用截取运动范围的中点数据和输出背景。运动文件中不可用的指标会产生警告并被跳过，不会阻止其他指标预览。

## 图形编辑器

```bash
ride-overlay my-ride --editor
# 或
ride-overlay-editor my-ride
```

编辑器将项目目录中的多段视频视为一条连续时间轴。视频按输出分辨率等比缩放，比例不一致时使用黑边填充，不会拉伸。蓝色轨道表示视频范围，黄色圆点表示片段连接点，绿色轨道表示运动数据映射到视频后的范围；在两条轨道所在区域点击或拖动都可以定位视频，且不会改变播放/暂停状态。

当前时间和视频总时长使用 `HH:MM:SS:FF` 格式显示，帧号按 `output.fps` 计算。除界面上的播放按钮和时间轴定位外，还支持以下键盘操作：

- `Space`：播放或暂停；
- `←`：暂停视频，并回退一个输出帧；
- `→`：暂停视频，并前进一个输出帧。

`timeline.activity_start_offset_frames` 表示运动数据起点位于视频时间轴中的帧位置，正数表示运动数据比视频晚开始，负数表示运动数据更早开始。左右对齐按钮短按调整一帧，长按连续调整。偏移使用 `output.fps` 作为帧率基准：

```json
"timeline": {
  "activity_start_offset_frames": 4979,
  "video_joins": [
    {
      "previous_file": "20260817072500_001.mp4",
      "next_file": "20260817073000_002.mp4",
      "overlap_frames": 15
    }
  ]
}
```

编辑器首次发现某个相邻片段连接尚未配置时，会读取前一段结尾和后一段开头最多约 3 秒的缩小灰度画面，先查找完全相同的帧序列，再进行保守的近似匹配。匹配只使用视频内容，不使用音频波形、文件时间或相机元数据。每个结果连同置信度、画面特征和候选信息写入 `export/video-analysis.log`；解码结果摘要缓存在 `export/.video-analysis-cache.json`，素材没有变化时不会重复分析。

`timeline.video_joins[].overlap_frames` 表示从该连接处前一个视频末尾忽略的帧数。该值会同时影响预览时间轴和最终导出；音频也在相同时间边界裁切，但不参与重复帧判断。配置中的值始终优先，用户可以手工修正，编辑器检测到有效的外部修改后会立即重建时间轴。自动 fallback 只会修正“明显偏离稳定主簇、置信度又低、且非零”的结果；不会用其他连接处的平均值填补可靠的 0 帧结果。

暂停时点击仪表盘可以选中它；开始播放会清除选择，播放过程中点击仪表盘则先暂停再选中。选中态显示紫色虚线边框和位于锚点的移动控件：

- 拖动移动控件修改 `anchor.x/y`；
- 拖动仪表盘矩形的其他位置修改数字仪表盘的 `font_size`，或图形仪表盘的 `width`；
- 锚点和宽度写入配置时最多保留三位小数；
- 配置采用约 80ms 防抖和原子替换写入；
- 外部编辑器保存有效的 `config.json` 后，图形界面会自动更新；暂时无效的 JSON 不会覆盖最后一次有效预览，也不会被 GUI 反向覆盖。

“导出完整视频”使用 `output` 中的文件名、分辨率、帧率、码率设置，将每个源视频按 `video_joins` 独立裁切画面和音频、等比适配后再拼接，最后直接合成仪表盘。这样不会依赖关键帧位置来决定接缝，也不会把已删除的尾部声音带到下一片段。成片使用 H.264 视频和 AAC 音频，包含源视频音轨。`output.background` 只对下面的命令行覆层输出生效，图形编辑器不会把绿幕或透明底色叠加进成片。当前版本要求所有视频都包含音轨或都不包含音轨；混合情况会明确报错。

图形编辑器播放视频时会同时播放源视频原声。暂停状态跨片段定位时，播放器仅在内部短暂静音解码目标画面，完成定位后会恢复声音，不会改变用户的播放/暂停状态。

## 命令行覆层输出模式

`output.background.mode` 决定编码方式：

| 模式 | 默认文件 | 视频编码 | 用途 |
| --- | --- | --- | --- |
| `chroma_key` | `overlay.mp4` | H.264/yuv420p | 文件较小，在剪辑软件中抠除绿色 |
| `transparent` | `overlay.mov` | ProRes 4444/yuva444p10le | 直接保留 alpha，文件可能很大 |

普通 H.264 MP4 不保存透明通道，因此透明模式只接受 `.mov`，绿幕模式只接受 `.mp4`。显式配置不匹配的扩展名会报错，而不会静默丢失 alpha。

视频先写入 `export/` 中的临时文件，编码成功后再原子替换最终输出，避免失败时留下半成品或破坏上一次成功结果。`output.filename` 现在相对于 `export/` 解析；旧版本留在项目根目录中的生成文件不会被自动移动或删除。

## 工作结果日志

每次命令行执行和图形编辑器成片导出都会生成一份 UTF-8 编码的 `export/result.log`。即使任务在输入路径解析前失败，也会尽量在 `export/` 中留下报告。日志采用临时文件加原子替换的方式写入，每次运行覆盖上一份报告，使内容始终对应当前输出结果。

视频拼接分析另行生成 `export/video-analysis.log`，在打开图形编辑器、完成项目载入时即可阅读，不必等到导出。它记录视频扫描顺序、每段元数据、每个连接的配置值和自动分析值、置信度、匹配指标、警告、fallback 原因、总裁切时长及有效时间轴时长。

报告包括：

- 唯一运行 ID、程序版本、模式、命令、运行环境、最终状态和退出码；
- 每个处理阶段的开始/结束时间、耗时和成功/失败状态；
- 完整配置快照、已解析的输入/输出文件及文件大小；
- 运动记录数量、运动起止时间、总时长，以及每项指标的数据来源、样本数量、缺失数量、覆盖率、数值范围和覆盖范围；
- 每个仪表盘的启用或跳过状态、平滑与刷新设置、数据空洞的起止时刻、长度和处理策略；轨迹还会记录有效/缺失/过滤点数、分段与断点、投影外接矩形、缩放、实际矩形、越界方向及箭头资源；心跳动画会记录图片尺寸、实际渲染矩形、动画曲线、相位起点、循环速度更新策略和心率空洞；
- 预览源视频与抽帧时刻，或输出视频的编码器、帧数、分辨率、FPS 和文件大小；
- 本次任务的全部 INFO、WARNING、ERROR 以及详细 DEBUG 事件。

终端是否显示 DEBUG 信息仍由 `--verbose` 控制，但 `result.log` 始终记录详细事件。报告可能包含本地路径、运动时间和素材文件名，分享前请检查隐私信息。

## 开发

```bash
conda activate ride-overlay
python -m pytest
ruff check .
```

核心代码按职责拆分为以下模块：

- [ride_overlay.py](ride_overlay.py)：配置加载、工程文件解析、任务报告、预览/视频编码和 CLI 编排；
- [ride_overlay_data.py](ride_overlay_data.py)：FIT/GPX 读取、统一数据模型、位置清洗与投影、插值、统计及派生指标；
- [ride_overlay_dashboard.py](ride_overlay_dashboard.py)：数字/轨迹/心跳动画仪表盘配置、空洞策略、数值格式化、采样和 Pillow 绘制。
- [ride_overlay_video.py](ride_overlay_video.py)：视频扫描、FFprobe 元数据和多片段虚拟时间轴；
- [ride_overlay_video_analysis.py](ride_overlay_video_analysis.py)：视频重复帧分析、缓存、配置补全和独立报告；
- [ride_overlay_export.py](ride_overlay_export.py)：完整视频、透明仪表盘层及源音轨的 FFmpeg 合成；
- `ride_overlay_gui/`：Qt 播放器、画布交互、双层时间轴、配置同步和后台导出。

`ride_overlay.py` 会继续转发原有的公共类型和函数，因此现有的 Python 导入方式保持兼容。这一边界也为后续增加轨迹、曲线等图形仪表盘提供独立扩展位置。

真实 FIT/GPX 可能包含精确时间和 GPS 轨迹，原始骑行视频也很大。仓库默认忽略 `test_proj/`；请只提交合成或完成脱敏的测试数据。

## License

GNU General Public License v3.0，详见 [LICENSE](LICENSE)。
