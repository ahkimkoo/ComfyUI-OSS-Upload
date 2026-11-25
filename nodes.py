import os
import random
import string
import time
import uuid
from datetime import datetime
from typing import List, Tuple, Union
import torch
import numpy as np
from PIL import Image
import oss2
import folder_paths
import comfy.utils
import scipy.io.wavfile

try:
    from .watermark import (
        AUDIO_EXTENSIONS,
        DEFAULT_CONTENT_PRODUCER,
        IMAGE_EXTENSIONS,
        VIDEO_EXTENSIONS,
        VISIBLE_WATERMARK_POSITIONS,
        build_aigc_metadata,
        build_visible_watermark,
        embed_audio_watermark,
        embed_image_file_watermark,
        embed_video_watermark,
        save_image_with_watermark,
    )
except ImportError:
    from watermark import (
        AUDIO_EXTENSIONS,
        DEFAULT_CONTENT_PRODUCER,
        IMAGE_EXTENSIONS,
        VIDEO_EXTENSIONS,
        VISIBLE_WATERMARK_POSITIONS,
        build_aigc_metadata,
        build_visible_watermark,
        embed_audio_watermark,
        embed_image_file_watermark,
        embed_video_watermark,
        save_image_with_watermark,
    )


def _invisible_watermark_optional_inputs(default_enabled: bool = True):
    """Shared optional ComfyUI inputs for GB 45438-2025 invisible watermark."""
    return {
        "enable_invisible_watermark": ("BOOLEAN", {"default": default_enabled}),
        "invisible_watermark_label": (["1", "2", "3"], {"default": "1"}),
        "invisible_watermark_producer": (
            "STRING",
            {
                "default": "",
                "multiline": False,
                "placeholder": f"平台名称/编码 (留空默认 {DEFAULT_CONTENT_PRODUCER})",
            },
        ),
        "invisible_watermark_produce_id": (
            "STRING",
            {
                "default": "",
                "multiline": False,
                "placeholder": "内容制作编号 (留空自动生成32位 CAICAI 编号)",
            },
        ),
        "invisible_watermark_reserved_code1": (
            "STRING",
            {
                "default": "",
                "multiline": False,
                "placeholder": "预留字段1/签名校验码 (可选)",
            },
        ),
        "invisible_watermark_propagator": (
            "STRING",
            {
                "default": "",
                "multiline": False,
                "placeholder": f"传播平台名称/编码 (留空同 Producer，默认 {DEFAULT_CONTENT_PRODUCER})",
            },
        ),
        "invisible_watermark_propagate_id": (
            "STRING",
            {
                "default": "",
                "multiline": False,
                "placeholder": "内容传播编号 (留空默认同 ProduceID)",
            },
        ),
        "invisible_watermark_reserved_code2": (
            "STRING",
            {
                "default": "",
                "multiline": False,
                "placeholder": "预留字段2 (可选)",
            },
        ),
    }


def _visible_watermark_optional_inputs():
    """Shared optional ComfyUI inputs for visible image-overlay watermark."""
    return {
        "enable_visible_watermark": ("BOOLEAN", {"default": False}),
        "visible_watermark_url": (
            "STRING",
            {
                "default": "",
                "multiline": False,
                "placeholder": "显性水印图片URL或本地路径 (默认不加)",
            },
        ),
        "visible_watermark_position": (
            list(VISIBLE_WATERMARK_POSITIONS),
            {"default": "bottom_right"},
        ),
        "visible_watermark_size": (
            "STRING",
            {
                "default": "15%",
                "multiline": False,
                "placeholder": "尺寸: 如 15%、200px、200x80、20%x10%",
            },
        ),
        "visible_watermark_margin": (
            "INT",
            {"default": 20, "min": 0, "max": 500, "step": 1},
        ),
        "visible_watermark_opacity": (
            "FLOAT",
            {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05},
        ),
    }


def _watermark_optional_inputs(default_invisible_enabled: bool = True):
    """Combined optional inputs exposing both invisible and visible watermark groups."""
    merged = _invisible_watermark_optional_inputs(
        default_enabled=default_invisible_enabled
    )
    merged.update(_visible_watermark_optional_inputs())
    return merged


def _resolve_watermark_configs(
    enable_invisible_watermark: bool,
    invisible_watermark_label: str,
    invisible_watermark_producer: str,
    invisible_watermark_produce_id: str,
    invisible_watermark_reserved_code1: str,
    invisible_watermark_propagator: str,
    invisible_watermark_propagate_id: str,
    invisible_watermark_reserved_code2: str,
    enable_visible_watermark: bool,
    visible_watermark_url: str,
    visible_watermark_position: str,
    visible_watermark_size: str,
    visible_watermark_margin: int,
    visible_watermark_opacity: float,
):
    """Build (AIGCMetadata | None, VisibleWatermarkConfig | None) from node inputs."""
    metadata = None
    if enable_invisible_watermark:
        metadata = build_aigc_metadata(
            label=invisible_watermark_label,
            content_producer=invisible_watermark_producer,
            produce_id=invisible_watermark_produce_id,
            reserved_code1=invisible_watermark_reserved_code1,
            content_propagator=invisible_watermark_propagator,
            propagate_id=invisible_watermark_propagate_id,
            reserved_code2=invisible_watermark_reserved_code2,
        )

    visible_cfg = None
    if enable_visible_watermark:
        visible_cfg = build_visible_watermark(
            image_url=visible_watermark_url,
            position=visible_watermark_position,
            size=visible_watermark_size,
            margin=visible_watermark_margin,
            opacity=visible_watermark_opacity,
        )
        if visible_cfg is None:
            print(
                "[WARNING] enable_visible_watermark is True but visible_watermark_url is empty; skipping visible watermark."
            )

    return metadata, visible_cfg


def _make_temp_path(filename: str) -> str:
    """Create a collision-free temporary file path inside ComfyUI's temp directory."""
    safe_name = os.path.basename(filename)
    return os.path.join(
        folder_paths.get_temp_directory(),
        f"oss_upload_{uuid.uuid4().hex[:10]}_{safe_name}",
    )


def _upload_with_retry(bucket_obj, oss_path, local_path, max_retries=20, retry_delay=3):
    """Upload a file to OSS with retry mechanism."""
    for attempt in range(max_retries):
        try:
            with open(local_path, "rb") as f:
                bucket_obj.put_object(oss_path, f)
            print(
                f"Successfully uploaded {local_path} to {oss_path} on attempt {attempt + 1}"
            )
            return
        except Exception as e:
            print(f"Upload attempt {attempt + 1}/{max_retries} failed: {str(e)}")
            if attempt + 1 < max_retries:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                print("Max retries reached. Upload failed.")
                raise e


class OSSImageUploader:
    """ComfyUI node for uploading images to Alibaba Cloud OSS"""

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "IMAGE": ("IMAGE",),
                "endpoint": (
                    "STRING",
                    {
                        "default": "oss-cn-shanghai.aliyuncs.com",
                        "multiline": False,
                        "placeholder": "OSS endpoint (e.g., oss-cn-shanghai.aliyuncs.com)",
                    },
                ),
                "bucket": (
                    "STRING",
                    {
                        "default": "cck-sh",
                        "multiline": False,
                        "placeholder": "OSS bucket name",
                    },
                ),
                "access_key": (
                    "STRING",
                    {"default": "", "multiline": False, "placeholder": "Access Key ID"},
                ),
                "access_secret": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "Access Key Secret",
                    },
                ),
                "path": (
                    "STRING",
                    {
                        "default": "aigc/up",
                        "multiline": False,
                        "placeholder": "OSS path (e.g., aigc/up)",
                    },
                ),
                "random_filename": ("BOOLEAN", {"default": True}),
                "filename": (
                    "STRING",
                    {
                        "default": "image.png",
                        "multiline": False,
                        "placeholder": "Filename (only used when random_filename is False)",
                    },
                ),
            },
            "optional": _watermark_optional_inputs(default_invisible_enabled=True),
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("url",)
    FUNCTION = "upload_image"
    CATEGORY = "OSS Upload"

    def generate_random_filename(self, extension: str = "png") -> str:
        """Generate random filename with timestamp and random string"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_str = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=8)
        )
        return f"{timestamp}_{random_str}.{extension}"

    def upload_image(
        self,
        IMAGE,
        endpoint,
        bucket,
        access_key,
        access_secret,
        path,
        random_filename,
        filename,
        enable_invisible_watermark=True,
        invisible_watermark_label="1",
        invisible_watermark_producer="",
        invisible_watermark_produce_id="",
        invisible_watermark_reserved_code1="",
        invisible_watermark_propagator="",
        invisible_watermark_propagate_id="",
        invisible_watermark_reserved_code2="",
        enable_visible_watermark=False,
        visible_watermark_url="",
        visible_watermark_position="bottom_right",
        visible_watermark_size="15%",
        visible_watermark_margin=20,
        visible_watermark_opacity=1.0,
    ):
        """Upload image to OSS and return URL"""
        temp_path = None
        try:
            if isinstance(IMAGE, torch.Tensor):
                if len(IMAGE.shape) == 4:
                    IMAGE = IMAGE[0]
                if IMAGE.shape[0] == 3:
                    IMAGE = IMAGE.permute(1, 2, 0)
                image_np = (IMAGE.cpu().numpy() * 255).astype(np.uint8)
                pil_image = Image.fromarray(image_np)
            else:
                pil_image = IMAGE

            if random_filename:
                filename = self.generate_random_filename("png")

            if not filename.lower().endswith(IMAGE_EXTENSIONS):
                filename += ".png"

            auth = oss2.Auth(access_key, access_secret)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)

            oss_path = os.path.join(path, filename).replace("\\", "/")

            metadata, visible_cfg = _resolve_watermark_configs(
                enable_invisible_watermark=enable_invisible_watermark,
                invisible_watermark_label=invisible_watermark_label,
                invisible_watermark_producer=invisible_watermark_producer,
                invisible_watermark_produce_id=invisible_watermark_produce_id,
                invisible_watermark_reserved_code1=invisible_watermark_reserved_code1,
                invisible_watermark_propagator=invisible_watermark_propagator,
                invisible_watermark_propagate_id=invisible_watermark_propagate_id,
                invisible_watermark_reserved_code2=invisible_watermark_reserved_code2,
                enable_visible_watermark=enable_visible_watermark,
                visible_watermark_url=visible_watermark_url,
                visible_watermark_position=visible_watermark_position,
                visible_watermark_size=visible_watermark_size,
                visible_watermark_margin=visible_watermark_margin,
                visible_watermark_opacity=visible_watermark_opacity,
            )

            temp_path = _make_temp_path(filename)
            save_image_with_watermark(
                pil_image,
                temp_path,
                metadata=metadata,
                visible_config=visible_cfg,
            )
            if visible_cfg:
                print(
                    f"Applied visible watermark to image (pos={visible_cfg.position}, size={visible_cfg.size})"
                )
            if metadata:
                print(
                    f"Embedded GB 45438 invisible watermark into image (Producer={metadata.content_producer}, ProduceID={metadata.produce_id})"
                )

            _upload_with_retry(bucket_obj, oss_path, temp_path)

            if endpoint.startswith("https://"):
                base_url = endpoint.replace("https://", f"https://{bucket}.")
            elif endpoint.startswith("http://"):
                base_url = endpoint.replace("http://", f"http://{bucket}.")
            else:
                base_url = f"https://{bucket}.{endpoint}"

            file_url = f"{base_url}/{oss_path}"

            print(f"Image uploaded successfully to: {file_url}")
            return (file_url,)

        except Exception as e:
            print(f"Error uploading image to OSS: {str(e)}")
            return (f"Error: {str(e)}",)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


class OSSVideoUploader:
    """ComfyUI node for uploading videos to Alibaba Cloud OSS"""

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "VHS_FILENAMES": ("VHS_FILENAMES",),
                "endpoint": (
                    "STRING",
                    {
                        "default": "oss-cn-shanghai.aliyuncs.com",
                        "multiline": False,
                        "placeholder": "OSS endpoint (e.g., oss-cn-shanghai.aliyuncs.com)",
                    },
                ),
                "bucket": (
                    "STRING",
                    {
                        "default": "cck-sh",
                        "multiline": False,
                        "placeholder": "OSS bucket name",
                    },
                ),
                "access_key": (
                    "STRING",
                    {"default": "", "multiline": False, "placeholder": "Access Key ID"},
                ),
                "access_secret": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "Access Key Secret",
                    },
                ),
                "path": (
                    "STRING",
                    {
                        "default": "aigc/up",
                        "multiline": False,
                        "placeholder": "OSS path (e.g., aigc/up)",
                    },
                ),
                "random_filename": ("BOOLEAN", {"default": True}),
                "filename": (
                    "STRING",
                    {
                        "default": "video.mp4",
                        "multiline": False,
                        "placeholder": "Filename (only used when random_filename is False)",
                    },
                ),
            },
            "optional": _watermark_optional_inputs(default_invisible_enabled=True),
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("url",)
    FUNCTION = "upload_video"
    CATEGORY = "OSS Upload"

    def generate_random_filename(self, extension: str = "mp4") -> str:
        """Generate random filename with timestamp and random string"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_str = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=8)
        )
        return f"{timestamp}_{random_str}.{extension}"

    def upload_video(
        self,
        VHS_FILENAMES,
        endpoint,
        bucket,
        access_key,
        access_secret,
        path,
        random_filename,
        filename,
        enable_invisible_watermark=True,
        invisible_watermark_label="1",
        invisible_watermark_producer="",
        invisible_watermark_produce_id="",
        invisible_watermark_reserved_code1="",
        invisible_watermark_propagator="",
        invisible_watermark_propagate_id="",
        invisible_watermark_reserved_code2="",
        enable_visible_watermark=False,
        visible_watermark_url="",
        visible_watermark_position="bottom_right",
        visible_watermark_size="15%",
        visible_watermark_margin=20,
        visible_watermark_opacity=1.0,
    ):
        """Upload video to OSS and return URL"""
        temp_video_path = None
        try:
            video_path = None
            if isinstance(VHS_FILENAMES, (list, tuple)) and len(VHS_FILENAMES) >= 2:
                if isinstance(VHS_FILENAMES[0], bool) and isinstance(
                    VHS_FILENAMES[1], (list, tuple)
                ):
                    video_files = [
                        f
                        for f in VHS_FILENAMES[1]
                        if f.lower().endswith(VIDEO_EXTENSIONS)
                    ]
                    if video_files:
                        video_path = video_files[-1]
                else:
                    for item in VHS_FILENAMES:
                        if isinstance(item, str) and item.lower().endswith(
                            VIDEO_EXTENSIONS
                        ):
                            video_path = item
                            break
                    if not video_path:
                        video_path = VHS_FILENAMES[0] if VHS_FILENAMES else None
            elif isinstance(VHS_FILENAMES, str):
                video_path = VHS_FILENAMES

            if not video_path:
                return (
                    f"Error: Could not extract video path from input. Input was: {VHS_FILENAMES}",
                )

            if not os.path.exists(video_path):
                print(f"[WARNING] Video file may not exist yet: {video_path}")

            _, ext = os.path.splitext(video_path)
            ext = ext if ext else ".mp4"

            if random_filename:
                filename = self.generate_random_filename(ext.lstrip("."))
            else:
                if not filename.lower().endswith(VIDEO_EXTENSIONS):
                    filename += ext

            auth = oss2.Auth(access_key, access_secret)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)

            oss_path = os.path.join(path, filename).replace("\\", "/")

            metadata, visible_cfg = _resolve_watermark_configs(
                enable_invisible_watermark=enable_invisible_watermark,
                invisible_watermark_label=invisible_watermark_label,
                invisible_watermark_producer=invisible_watermark_producer,
                invisible_watermark_produce_id=invisible_watermark_produce_id,
                invisible_watermark_reserved_code1=invisible_watermark_reserved_code1,
                invisible_watermark_propagator=invisible_watermark_propagator,
                invisible_watermark_propagate_id=invisible_watermark_propagate_id,
                invisible_watermark_reserved_code2=invisible_watermark_reserved_code2,
                enable_visible_watermark=enable_visible_watermark,
                visible_watermark_url=visible_watermark_url,
                visible_watermark_position=visible_watermark_position,
                visible_watermark_size=visible_watermark_size,
                visible_watermark_margin=visible_watermark_margin,
                visible_watermark_opacity=visible_watermark_opacity,
            )

            upload_source = video_path
            if metadata is not None or visible_cfg is not None:
                temp_video_path = _make_temp_path(f"watermarked{ext}")
                embed_video_watermark(
                    video_path,
                    temp_video_path,
                    metadata=metadata,
                    visible_config=visible_cfg,
                )
                upload_source = temp_video_path
                if visible_cfg:
                    print(
                        f"Applied visible watermark to video (pos={visible_cfg.position}, size={visible_cfg.size})"
                    )
                if metadata:
                    print(
                        f"Embedded GB 45438 invisible watermark into video (Producer={metadata.content_producer}, ProduceID={metadata.produce_id})"
                    )

            _upload_with_retry(bucket_obj, oss_path, upload_source)

            if endpoint.startswith("https://"):
                base_url = endpoint.replace("https://", f"https://{bucket}.")
            elif endpoint.startswith("http://"):
                base_url = endpoint.replace("http://", f"http://{bucket}.")
            else:
                base_url = f"https://{bucket}.{endpoint}"

            file_url = f"{base_url}/{oss_path}"

            print(f"Video uploaded successfully to: {file_url}")
            return (file_url,)

        except Exception as e:
            print(f"Error uploading video to OSS: {str(e)}")
            return (f"Error: {str(e)}",)
        finally:
            if temp_video_path and os.path.exists(temp_video_path):
                try:
                    os.remove(temp_video_path)
                except OSError:
                    pass


class OSSAudioUploader:
    """ComfyUI node for uploading audio to Alibaba Cloud OSS"""

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO",),
                "endpoint": (
                    "STRING",
                    {
                        "default": "oss-cn-shanghai.aliyuncs.com",
                        "multiline": False,
                        "placeholder": "OSS endpoint (e.g., oss-cn-shanghai.aliyuncs.com)",
                    },
                ),
                "bucket": (
                    "STRING",
                    {
                        "default": "cck-sh",
                        "multiline": False,
                        "placeholder": "OSS bucket name",
                    },
                ),
                "access_key": (
                    "STRING",
                    {"default": "", "multiline": False, "placeholder": "Access Key ID"},
                ),
                "access_secret": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "Access Key Secret",
                    },
                ),
                "path": (
                    "STRING",
                    {
                        "default": "aigc/up",
                        "multiline": False,
                        "placeholder": "OSS path (e.g., aigc/up)",
                    },
                ),
                "random_filename": ("BOOLEAN", {"default": True}),
                "filename": (
                    "STRING",
                    {
                        "default": "audio.wav",
                        "multiline": False,
                        "placeholder": "Filename (only used when random_filename is False)",
                    },
                ),
            },
            "optional": _invisible_watermark_optional_inputs(default_enabled=True),
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("url",)
    FUNCTION = "upload_audio"
    CATEGORY = "OSS Upload"

    def generate_random_filename(self, extension: str = "wav") -> str:
        """Generate random filename with timestamp and random string"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_str = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=8)
        )
        return f"{timestamp}_{random_str}.{extension}"

    def upload_audio(
        self,
        audio,
        endpoint,
        bucket,
        access_key,
        access_secret,
        path,
        random_filename,
        filename,
        enable_invisible_watermark=True,
        invisible_watermark_label="1",
        invisible_watermark_producer="",
        invisible_watermark_produce_id="",
        invisible_watermark_reserved_code1="",
        invisible_watermark_propagator="",
        invisible_watermark_propagate_id="",
        invisible_watermark_reserved_code2="",
    ):
        """Upload audio to OSS and return URL"""
        raw_wav_path = None
        final_audio_path = None
        try:
            # This will trigger the LazyAudioMap if that's what is passed
            waveform = audio["waveform"]
            sample_rate = audio["sample_rate"]

            # Generate filename
            if random_filename:
                filename = self.generate_random_filename("wav")

            if not filename.lower().endswith(AUDIO_EXTENSIONS):
                filename += ".wav"

            ext = os.path.splitext(filename)[1].lower()

            # Save raw waveform to temporary WAV file
            waveform_np = waveform.cpu().numpy()
            if len(waveform_np.shape) == 3:
                waveform_np = waveform_np[0]

            if (
                len(waveform_np.shape) == 2
                and waveform_np.shape[0] < waveform_np.shape[1]
            ):
                waveform_np = waveform_np.T

            raw_wav_path = _make_temp_path("raw_audio.wav")
            scipy.io.wavfile.write(raw_wav_path, sample_rate, waveform_np)

            upload_source = raw_wav_path
            metadata = None
            if enable_invisible_watermark:
                metadata = build_aigc_metadata(
                    label=invisible_watermark_label,
                    content_producer=invisible_watermark_producer,
                    produce_id=invisible_watermark_produce_id,
                    reserved_code1=invisible_watermark_reserved_code1,
                    content_propagator=invisible_watermark_propagator,
                    propagate_id=invisible_watermark_propagate_id,
                    reserved_code2=invisible_watermark_reserved_code2,
                )

            if metadata is not None or ext != ".wav":
                final_audio_path = _make_temp_path(f"audio{ext}")
                embed_audio_watermark(raw_wav_path, final_audio_path, metadata=metadata)
                upload_source = final_audio_path
                if metadata:
                    print(
                        f"Embedded GB 45438 invisible watermark into audio (Producer={metadata.content_producer}, ProduceID={metadata.produce_id})"
                    )

            # Setup OSS auth and upload
            auth = oss2.Auth(access_key, access_secret)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)
            oss_path = os.path.join(path, filename).replace("\\", "/")

            _upload_with_retry(bucket_obj, oss_path, upload_source)

            # Construct URL
            if endpoint.startswith("https://"):
                base_url = endpoint.replace("https://", f"https://{bucket}.")
            elif endpoint.startswith("http://"):
                base_url = endpoint.replace("http://", f"http://{bucket}.")
            else:
                base_url = f"https://{bucket}.{endpoint}"

            file_url = f"{base_url}/{oss_path}"

            print(f"Audio uploaded successfully to: {file_url}")
            return (file_url,)

        except Exception as e:
            print(f"Error uploading audio to OSS: {str(e)}")
            return (f"Error: {str(e)}",)
        finally:
            for p in (raw_wav_path, final_audio_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


class OSSFileUploader:
    """ComfyUI node for uploading local files to Alibaba Cloud OSS"""

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "file_path": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "Local file path to upload",
                    },
                ),
                "endpoint": (
                    "STRING",
                    {
                        "default": "oss-cn-shanghai.aliyuncs.com",
                        "multiline": False,
                        "placeholder": "OSS endpoint (e.g., oss-cn-shanghai.aliyuncs.com)",
                    },
                ),
                "bucket": (
                    "STRING",
                    {
                        "default": "cck-sh",
                        "multiline": False,
                        "placeholder": "OSS bucket name",
                    },
                ),
                "access_key": (
                    "STRING",
                    {"default": "", "multiline": False, "placeholder": "Access Key ID"},
                ),
                "access_secret": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "Access Key Secret",
                    },
                ),
                "path": (
                    "STRING",
                    {
                        "default": "aigc/up",
                        "multiline": False,
                        "placeholder": "OSS path (e.g., aigc/up)",
                    },
                ),
                "random_filename": ("BOOLEAN", {"default": True}),
                "filename": (
                    "STRING",
                    {
                        "default": "file",
                        "multiline": False,
                        "placeholder": "Filename (only used when random_filename is False)",
                    },
                ),
            },
            "optional": _watermark_optional_inputs(default_invisible_enabled=False),
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("url",)
    FUNCTION = "upload_file"
    CATEGORY = "OSS Upload"

    def generate_random_filename(self, extension: str = "dat") -> str:
        """Generate random filename with timestamp and random string"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_str = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=8)
        )
        return f"{timestamp}_{random_str}.{extension}"

    def upload_file(
        self,
        file_path,
        endpoint,
        bucket,
        access_key,
        access_secret,
        path,
        random_filename,
        filename,
        enable_invisible_watermark=False,
        invisible_watermark_label="1",
        invisible_watermark_producer="",
        invisible_watermark_produce_id="",
        invisible_watermark_reserved_code1="",
        invisible_watermark_propagator="",
        invisible_watermark_propagate_id="",
        invisible_watermark_reserved_code2="",
        enable_visible_watermark=False,
        visible_watermark_url="",
        visible_watermark_position="bottom_right",
        visible_watermark_size="15%",
        visible_watermark_margin=20,
        visible_watermark_opacity=1.0,
    ):
        """Upload local file to OSS and return URL"""
        temp_file_path = None
        try:
            # Validate file path
            if not file_path:
                return ("Error: File path is empty",)

            if not os.path.exists(file_path):
                return (f"Error: File does not exist: {file_path}",)

            if not os.path.isfile(file_path):
                return (f"Error: Path is not a file: {file_path}",)

            # Get file extension from original file
            _, ext = os.path.splitext(file_path)
            ext = ext if ext else ""

            # Generate filename
            if random_filename:
                filename = (
                    self.generate_random_filename(ext.lstrip("."))
                    if ext
                    else self.generate_random_filename("dat")
                )
            else:
                if ext and not filename.lower().endswith(ext.lower()):
                    filename += ext

            auth = oss2.Auth(access_key, access_secret)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)

            oss_path = os.path.join(path, filename).replace("\\", "/")

            metadata, visible_cfg = _resolve_watermark_configs(
                enable_invisible_watermark=enable_invisible_watermark,
                invisible_watermark_label=invisible_watermark_label,
                invisible_watermark_producer=invisible_watermark_producer,
                invisible_watermark_produce_id=invisible_watermark_produce_id,
                invisible_watermark_reserved_code1=invisible_watermark_reserved_code1,
                invisible_watermark_propagator=invisible_watermark_propagator,
                invisible_watermark_propagate_id=invisible_watermark_propagate_id,
                invisible_watermark_reserved_code2=invisible_watermark_reserved_code2,
                enable_visible_watermark=enable_visible_watermark,
                visible_watermark_url=visible_watermark_url,
                visible_watermark_position=visible_watermark_position,
                visible_watermark_size=visible_watermark_size,
                visible_watermark_margin=visible_watermark_margin,
                visible_watermark_opacity=visible_watermark_opacity,
            )

            upload_source = file_path
            if metadata is not None or visible_cfg is not None:
                ext_lower = ext.lower()
                if ext_lower in IMAGE_EXTENSIONS:
                    temp_file_path = _make_temp_path(f"watermarked{ext_lower}")
                    embed_image_file_watermark(
                        file_path,
                        temp_file_path,
                        metadata=metadata,
                        visible_config=visible_cfg,
                    )
                    upload_source = temp_file_path
                elif ext_lower in VIDEO_EXTENSIONS:
                    temp_file_path = _make_temp_path(f"watermarked{ext_lower}")
                    embed_video_watermark(
                        file_path,
                        temp_file_path,
                        metadata=metadata,
                        visible_config=visible_cfg,
                    )
                    upload_source = temp_file_path
                elif ext_lower in AUDIO_EXTENSIONS and metadata is not None:
                    temp_file_path = _make_temp_path(f"watermarked{ext_lower}")
                    embed_audio_watermark(
                        file_path,
                        temp_file_path,
                        metadata=metadata,
                    )
                    upload_source = temp_file_path
                else:
                    print(
                        f"[INFO] Watermarking skipped for unsupported file extension: {ext or '(none)'}"
                    )

            _upload_with_retry(bucket_obj, oss_path, upload_source)

            # Construct URL
            if endpoint.startswith("https://"):
                base_url = endpoint.replace("https://", f"https://{bucket}.")
            elif endpoint.startswith("http://"):
                base_url = endpoint.replace("http://", f"http://{bucket}.")
            else:
                base_url = f"https://{bucket}.{endpoint}"

            file_url = f"{base_url}/{oss_path}"

            print(f"File uploaded successfully to: {file_url}")
            return (file_url,)

        except Exception as e:
            print(f"Error uploading file to OSS: {str(e)}")
            return (f"Error: {str(e)}",)
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except OSError:
                    pass


NODE_CLASS_MAPPINGS = {
    "OSSImageUploader": OSSImageUploader,
    "OSSVideoUploader": OSSVideoUploader,
    "OSSAudioUploader": OSSAudioUploader,
    "OSSFileUploader": OSSFileUploader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "OSSImageUploader": "OSS Image Uploader",
    "OSSVideoUploader": "OSS Video Uploader",
    "OSSAudioUploader": "OSS Audio Uploader",
    "OSSFileUploader": "OSS File Uploader",
}
