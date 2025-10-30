import os
import random
import string
import time
from datetime import datetime
from typing import List, Tuple, Union
import torch
import numpy as np
from PIL import Image
import oss2
import folder_paths
import comfy.utils
import scipy.io.wavfile

def _upload_with_retry(bucket_obj, oss_path, local_path, max_retries=20, retry_delay=3):
    """Upload a file to OSS with retry mechanism."""
    for attempt in range(max_retries):
        try:
            with open(local_path, 'rb') as f:
                bucket_obj.put_object(oss_path, f)
            print(f"Successfully uploaded {local_path} to {oss_path} on attempt {attempt + 1}")
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
                "endpoint": ("STRING", {
                    "default": "oss-cn-shanghai.aliyuncs.com",
                    "multiline": False,
                    "placeholder": "OSS endpoint (e.g., oss-cn-shanghai.aliyuncs.com)"
                }),
                "bucket": ("STRING", {
                    "default": "cck-sh",
                    "multiline": False,
                    "placeholder": "OSS bucket name"
                }),
                "access_key": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Access Key ID"
                }),
                "access_secret": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Access Key Secret"
                }),
                "path": ("STRING", {
                    "default": "aigc/up",
                    "multiline": False,
                    "placeholder": "OSS path (e.g., aigc/up)"
                }),
                "random_filename": ("BOOLEAN", {
                    "default": True
                }),
                "filename": ("STRING", {
                    "default": "image.png",
                    "multiline": False,
                    "placeholder": "Filename (only used when random_filename is False)"
                }),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("url",)
    FUNCTION = "upload_image"
    CATEGORY = "OSS Upload"
    
    def generate_random_filename(self, extension: str = "png") -> str:
        """Generate random filename with timestamp and random string"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"{timestamp}_{random_str}.{extension}"
    
    def upload_image(self, IMAGE, endpoint, bucket, access_key, access_secret, path, 
                     random_filename, filename):
        """Upload image to OSS and return URL"""
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
            
            if not filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                filename += '.png'
            
            auth = oss2.Auth(access_key, access_secret)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)
            
            oss_path = os.path.join(path, filename).replace('\\', '/')
            
            temp_path = os.path.join(folder_paths.get_temp_directory(), filename)
            pil_image.save(temp_path, 'PNG')
            
            _upload_with_retry(bucket_obj, oss_path, temp_path)
            
            os.remove(temp_path)
            
            if endpoint.startswith('https://'):
                base_url = endpoint.replace('https://', f'https://{bucket}.')
            elif endpoint.startswith('http://'):
                base_url = endpoint.replace('http://', f'http://{bucket}.')
            else:
                base_url = f'https://{bucket}.{endpoint}'
            
            file_url = f"{base_url}/{oss_path}"
            
            print(f"Image uploaded successfully to: {file_url}")
            return (file_url,)
            
        except Exception as e:
            print(f"Error uploading image to OSS: {str(e)}")
            return (f"Error: {str(e)}",)

class OSSVideoUploader:
    """ComfyUI node for uploading videos to Alibaba Cloud OSS"""
    
    def __init__(self):
        pass
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "VHS_FILENAMES": ("VHS_FILENAMES",),
                "endpoint": ("STRING", {
                    "default": "oss-cn-shanghai.aliyuncs.com",
                    "multiline": False,
                    "placeholder": "OSS endpoint (e.g., oss-cn-shanghai.aliyuncs.com)"
                }),
                "bucket": ("STRING", {
                    "default": "cck-sh",
                    "multiline": False,
                    "placeholder": "OSS bucket name"
                }),
                "access_key": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Access Key ID"
                }),
                "access_secret": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Access Key Secret"
                }),
                "path": ("STRING", {
                    "default": "aigc/up",
                    "multiline": False,
                    "placeholder": "OSS path (e.g., aigc/up)"
                }),
                "random_filename": ("BOOLEAN", {
                    "default": True
                }),
                "filename": ("STRING", {
                    "default": "video.mp4",
                    "multiline": False,
                    "placeholder": "Filename (only used when random_filename is False)"
                }),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("url",)
    FUNCTION = "upload_video"
    CATEGORY = "OSS Upload"
    
    def generate_random_filename(self, extension: str = "mp4") -> str:
        """Generate random filename with timestamp and random string"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"{timestamp}_{random_str}.{extension}"
    
    def upload_video(self, VHS_FILENAMES, endpoint, bucket, access_key, access_secret, path,
                     random_filename, filename):
        """Upload video to OSS and return URL"""
        try:
            video_path = None
            if isinstance(VHS_FILENAMES, (list, tuple)) and len(VHS_FILENAMES) >= 2:
                if isinstance(VHS_FILENAMES[0], bool) and isinstance(VHS_FILENAMES[1], (list, tuple)):
                    video_files = [f for f in VHS_FILENAMES[1] if f.lower().endswith(('.mp4', '.avi', '.mov', '.webm', '.mkv'))]
                    if video_files:
                        video_path = video_files[-1]
                else:
                    for item in VHS_FILENAMES:
                        if isinstance(item, str) and item.lower().endswith(('.mp4', '.avi', '.mov', '.webm', '.mkv')):
                            video_path = item
                            break
                    if not video_path:
                        video_path = VHS_FILENAMES[0] if VHS_FILENAMES else None
            elif isinstance(VHS_FILENAMES, str):
                video_path = VHS_FILENAMES
            
            if not video_path:
                return (f"Error: Could not extract video path from input. Input was: {VHS_FILENAMES}",)
            
            if not os.path.exists(video_path):
                print(f"[WARNING] Video file may not exist yet: {video_path}")

            _, ext = os.path.splitext(video_path)
            ext = ext if ext else '.mp4'
            
            if random_filename:
                filename = self.generate_random_filename(ext.lstrip('.'))
            else:
                if not filename.lower().endswith(('.mp4', '.avi', '.mov', '.webm', '.mkv')):
                    filename += ext

            auth = oss2.Auth(access_key, access_secret)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)
            
            oss_path = os.path.join(path, filename).replace('\\', '/')
            
            _upload_with_retry(bucket_obj, oss_path, video_path)
            
            if endpoint.startswith('https://'):
                base_url = endpoint.replace('https://', f'https://{bucket}.')
            elif endpoint.startswith('http://'):
                base_url = endpoint.replace('http://', f'http://{bucket}.')
            else:
                base_url = f'https://{bucket}.{endpoint}'
            
            file_url = f"{base_url}/{oss_path}"
            
            print(f"Video uploaded successfully to: {file_url}")
            return (file_url,)
            
        except Exception as e:
            print(f"Error uploading video to OSS: {str(e)}")
            return (f"Error: {str(e)}",)

class OSSAudioUploader:
    """ComfyUI node for uploading audio to Alibaba Cloud OSS"""
    
    def __init__(self):
        pass
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO",),
                "endpoint": ("STRING", {
                    "default": "oss-cn-shanghai.aliyuncs.com",
                    "multiline": False,
                    "placeholder": "OSS endpoint (e.g., oss-cn-shanghai.aliyuncs.com)"
                }),
                "bucket": ("STRING", {
                    "default": "cck-sh",
                    "multiline": False,
                    "placeholder": "OSS bucket name"
                }),
                "access_key": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Access Key ID"
                }),
                "access_secret": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Access Key Secret"
                }),
                "path": ("STRING", {
                    "default": "aigc/up",
                    "multiline": False,
                    "placeholder": "OSS path (e.g., aigc/up)"
                }),
                "random_filename": ("BOOLEAN", {
                    "default": True
                }),
                "filename": ("STRING", {
                    "default": "audio.wav",
                    "multiline": False,
                    "placeholder": "Filename (only used when random_filename is False)"
                }),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("url",)
    FUNCTION = "upload_audio"
    CATEGORY = "OSS Upload"
    
    def generate_random_filename(self, extension: str = "wav") -> str:
        """Generate random filename with timestamp and random string"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"{timestamp}_{random_str}.{extension}"
    
    def upload_audio(self, audio, endpoint, bucket, access_key, access_secret, path,
                     random_filename, filename):
        """Upload audio to OSS and return URL"""
        try:
            # This will trigger the LazyAudioMap if that's what is passed
            waveform = audio["waveform"]
            sample_rate = audio["sample_rate"]

            # Generate filename
            if random_filename:
                filename = self.generate_random_filename("wav")
            
            if not filename.lower().endswith(('.wav', '.mp3', '.flac')):
                filename += '.wav'

            # Prepare temp file path
            temp_path = os.path.join(folder_paths.get_temp_directory(), filename)

            # Save audio to temporary file
            waveform_np = waveform.cpu().numpy()
            if len(waveform_np.shape) == 3:
                waveform_np = waveform_np[0]
            
            if len(waveform_np.shape) == 2 and waveform_np.shape[0] < waveform_np.shape[1]:
                 waveform_np = waveform_np.T

            scipy.io.wavfile.write(temp_path, sample_rate, waveform_np)

            # Setup OSS auth and upload
            auth = oss2.Auth(access_key, access_secret)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)
            oss_path = os.path.join(path, filename).replace('\\', '/')
            
            _upload_with_retry(bucket_obj, oss_path, temp_path)
            
            os.remove(temp_path)
            
            # Construct URL
            if endpoint.startswith('https://'):
                base_url = endpoint.replace('https://', f'https://{bucket}.')
            elif endpoint.startswith('http://'):
                base_url = endpoint.replace('http://', f'http://{bucket}.')
            else:
                base_url = f'https://{bucket}.{endpoint}'
            
            file_url = f"{base_url}/{oss_path}"
            
            print(f"Audio uploaded successfully to: {file_url}")
            return (file_url,)
            
        except Exception as e:
            print(f"Error uploading audio to OSS: {str(e)}")
            return (f"Error: {str(e)}",)

NODE_CLASS_MAPPINGS = {
    "OSSImageUploader": OSSImageUploader,
    "OSSVideoUploader": OSSVideoUploader,
    "OSSAudioUploader": OSSAudioUploader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "OSSImageUploader": "OSS Image Uploader",
    "OSSVideoUploader": "OSS Video Uploader",
    "OSSAudioUploader": "OSS Audio Uploader",
}
