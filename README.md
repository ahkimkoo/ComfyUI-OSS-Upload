# ComfyUI OSS Upload Plugin

A ComfyUI plugin for uploading generated images and videos to Alibaba Cloud OSS (Object Storage Service).

## Features

- **Image Upload Node**: Upload ComfyUI generated images to OSS
- **Video Upload Node**: Upload videos (compatible with VideoHelperSuite) to OSS
- **Audio Upload Node**: Upload audio files to OSS
- **File Upload Node**: Upload arbitrary local files to OSS
- **Dual Watermarking (Invisible + Visible)**:
  - **Invisible Watermark (GB 45438-2025)**: Enabled by default for images (PNG/JPEG/WebP) and videos (MP4/MOV/MKV/WebM/AVI/GIF) with default platform producer `CAICAI-菜菜AI`, lossless metadata writing, and extensible blind-watermark hooks
  - **Visible Watermark**: Disabled by default; supports watermark image URL (with caching) or local path, 9-grid position, flexible sizing, margin, and opacity for both images and videos
- **Automatic Retry**: Automatically retries uploads up to 20 times on failure
- **Random Filename Generation**: Option to generate random filenames with timestamp
- **Custom Filename**: Option to specify custom filenames
- **Configurable OSS Settings**: Support for custom endpoint, bucket, access keys, and paths

## Installation

1. Copy the `ComfyUI-OSS-Upload` folder to your ComfyUI `custom_nodes` directory
2. Install required dependencies:
   ```bash
   pip install oss2
   ```

## Usage

### Image Upload Node (OSS Image Uploader)

**Inputs:**
- `image`: IMAGE type from ComfyUI
- `endpoint`: OSS endpoint (e.g., https://oss-cn-hangzhou.aliyuncs.com)
- `bucket`: OSS bucket name
- `access_key`: Access Key ID
- `access_secret`: Access Key Secret
- `path`: OSS storage path (e.g., comfyui/images)
- `random_filename`: Boolean to enable/disable random filename generation
- `filename`: Custom filename (used when random_filename is False)

**Output:**
- `url`: Complete URL of the uploaded file on OSS

### Audio Upload Node (OSS Audio Uploader)

**Inputs:**
- `audio`: AUDIO type from ComfyUI
- `endpoint`: OSS endpoint (e.g., https://oss-cn-hangzhou.aliyuncs.com)
- `bucket`: OSS bucket name
- `access_key`: Access Key ID
- `access_secret`: Access Key Secret
- `path`: OSS storage path (e.g., comfyui/audio)
- `random_filename`: Boolean to enable/disable random filename generation
- `filename`: Custom filename (used when random_filename is False)

**Output:**
- `url`: Complete URL of the uploaded file on OSS

### Video Upload Node (OSS Video Uploader)

**Inputs:**
- `video`: VHS_FILENAMES type from VideoHelperSuite VideoCombine node
- `endpoint`: OSS endpoint (e.g., https://oss-cn-hangzhou.aliyuncs.com)
- `bucket`: OSS bucket name
- `access_key`: Access Key ID
- `access_secret`: Access Key Secret
- `path`: OSS storage path (e.g., comfyui/videos)
- `random_filename`: Boolean to enable/disable random filename generation
- `filename`: Custom filename (used when random_filename is False)

**Output:**
- `url`: Complete URL of the uploaded file on OSS

## Random Filename Format

When random filename is enabled, files are named with the format:
```
{timestamp}_{random_string}.{extension}
```
Example: `20241210_143052_a3k9m2p7.png`

## Example Workflow

1. Generate an image using ComfyUI nodes
2. Connect the IMAGE output to the OSS Image Uploader node
3. Configure your OSS credentials and settings
4. Run the workflow
5. The node will output the complete OSS URL of the uploaded file

## Dual Watermarking (Invisible + Visible)

`OSSImageUploader` and `OSSVideoUploader` support two independent optional watermark groups (fully backward-compatible with existing workflows; `OSSFileUploader` also exposes both groups for image/video files):

### 1. Invisible Watermark Group (隐形水印 — GB 45438-2025)
Enabled by default on image & video nodes. When not specified by the caller, the system default is applied with platform producer **`CAICAI-菜菜AI`**.

| Node Input | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `enable_invisible_watermark` | BOOLEAN | `True` (`False` on File node) | Enable/disable GB 45438-2025 implicit metadata |
| `invisible_watermark_label` | COMBO | `"1"` | `"1"` (AI-generated), `"2"` (possibly), `"3"` (suspected) |
| `invisible_watermark_producer` | STRING | `""` | Platform name / 27-digit code; falls back to env `AIGC_CONTENT_PRODUCER` → `"CAICAI-菜菜AI"` |
| `invisible_watermark_produce_id` | STRING | `""` | Unique task/content ID; auto-generated (`PROD_<timestamp>_<uuid>`) if empty |
| `invisible_watermark_reserved_code1` | STRING | `""` | Optional reserved field / signature |
| `invisible_watermark_propagator` | STRING | `""` | Optional propagator platform name/code (or env `AIGC_CONTENT_PROPAGATOR`) |
| `invisible_watermark_propagate_id` | STRING | `""` | Optional propagation ID |
| `invisible_watermark_reserved_code2` | STRING | `""` | Optional reserved field 2 |

### 2. Visible Watermark Group (显性水印 — 图片叠加)
Disabled by default (no visible watermark is added unless explicitly enabled).

| Node Input | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `enable_visible_watermark` | BOOLEAN | `False` | Enable/disable visible image watermark |
| `visible_watermark_url` | STRING | `""` | HTTP/HTTPS image URL (with in-memory TTL cache) or local image path (PNG with alpha recommended) |
| `visible_watermark_position` | COMBO | `"bottom_right"` | `bottom_right`, `bottom_left`, `top_right`, `top_left`, `center`, `top_center`, `bottom_center`, `center_left`, `center_right` |
| `visible_watermark_size` | STRING | `"15%"` | Proportional width (`"15%"`, `"0.15"`), fixed pixel width (`"200"`, `"200px"`), or explicit WxH (`"200x80"`, `"20%x10%"`) |
| `visible_watermark_margin` | INT | `20` | Distance from edges in pixels (`0`–`500`) |
| `visible_watermark_opacity` | FLOAT | `1.0` | Watermark opacity (`0.0`–`1.0`) |

### 3. GB 45438-2025 JSON Payload & API Example

All 7 GB 45438-2025 implicit metadata fields are always written (empty optional fields are stored as `""`):

```json
{
  "Label": "1",
  "ContentProducer": "CAICAI-菜菜AI",
  "ProduceID": "PROD_20261125151629_7964cd51a2be",
  "ReservedCode1": "",
  "ContentPropagator": "",
  "PropagateID": "",
  "ReservedCode2": ""
}
```

Example ComfyUI workflow node inputs (for AI platform task submission):

```json
{
  "enable_invisible_watermark": true,
  "invisible_watermark_producer": "CAICAI-菜菜AI",
  "invisible_watermark_produce_id": "task_20261125_001",
  "enable_visible_watermark": true,
  "visible_watermark_url": "https://cdn.example.com/watermark.png",
  "visible_watermark_position": "bottom_right",
  "visible_watermark_size": "15%",
  "visible_watermark_margin": 20,
  "visible_watermark_opacity": 0.8
}
```

### Storage & Encoding Behavior
- **PNG**: `tEXt`/`iTXt` chunk (`AIGC`) + XMP (`TC260:AIGC`)
- **JPEG / WebP**: EXIF (`UserComment` + `ImageDescription`) + XMP (`TC260:AIGC`)
- **MP4 / MOV / M4V / MKV / WebM / AVI**:
  - Invisible watermark only: fast lossless stream copy (`ffmpeg -c copy`)
  - Visible watermark enabled: single-pass FFmpeg YUV420 overlay using NVIDIA `h264_nvenc` when available (auto-fallback to CPU `libx264 -preset veryfast`, configurable via `AIGC_VIDEO_PRESET` / `AIGC_DISABLE_NVENC`), `-c:a copy` preserves audio + invisible metadata written in the same pass
- **GIF**: Composites visible watermark onto every frame while preserving animation timing + writes GB 45438 `comment` block

### Future Blind Watermark Extension
`watermark.py` exposes `register_image_watermark_hook` and `register_video_watermark_hook` so pixel/frame-level blind watermarking (e.g., TrustMark, Video Seal, or cloud MPS) can be plugged in later without changing node interfaces.

### Quick Verification
```bash
# Verify video metadata
ffprobe -v quiet -print_format json -show_format downloaded.mp4

# Verify image or video via bundled helper
python3 -c "import watermark; print(watermark.read_image_watermark('downloaded.png'))"
```

## Security Notes

- Keep your OSS access keys secure
- Consider using STS tokens for production use
- Set appropriate bucket permissions
- Use HTTPS endpoints for secure uploads

## Compatibility

- Tested with ComfyUI
- Compatible with VideoHelperSuite for video uploads
- Supports common image formats: PNG, JPG, JPEG, WebP
- Supports common video formats: MP4, AVI, MOV, WebM, MKV, GIF
- Supports common audio formats: WAV, MP3, FLAC
