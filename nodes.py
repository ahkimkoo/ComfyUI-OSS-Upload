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

class OSSImageUploader:
    """ComfyUI node for uploading images to Alibaba Cloud OSS"""
    
    def __init__(self):
        pass
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "IMAGE": ("IMAGE",),  # 端口显示为IMAGE，类型为IMAGE
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
            # Convert tensor to PIL Image
            if isinstance(IMAGE, torch.Tensor):
                # Handle batched images - take first one
                if len(IMAGE.shape) == 4:
                    IMAGE = IMAGE[0]
                
                # Convert from tensor to numpy array
                if IMAGE.shape[0] == 3:  # CHW format
                    IMAGE = IMAGE.permute(1, 2, 0)
                
                # Convert to numpy and then PIL
                image_np = (IMAGE.cpu().numpy() * 255).astype(np.uint8)
                pil_image = Image.fromarray(image_np)
            else:
                pil_image = IMAGE
            
            # Generate filename
            if random_filename:
                filename = self.generate_random_filename("png")
            
            # Ensure filename has proper extension
            if not filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                filename += '.png'
            
            # Setup OSS auth
            auth = oss2.Auth(access_key, access_secret)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)
            
            # Prepare OSS path
            oss_path = os.path.join(path, filename).replace('\\', '/')
            
            # Save image to temporary file
            temp_path = os.path.join(folder_paths.get_temp_directory(), filename)
            pil_image.save(temp_path, 'PNG')
            
            # Upload to OSS
            with open(temp_path, 'rb') as f:
                bucket_obj.put_object(oss_path, f)
            
            # Clean up temp file
            os.remove(temp_path)
            
            # Construct URL
            if endpoint.startswith('https://'):
                base_url = endpoint.replace('https://', f'https://{bucket}.')
            elif endpoint.startswith('http://'):
                base_url = endpoint.replace('http://', f'http://{bucket}.')
            else:
                # Assume https if no protocol specified
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
                "VHS_FILENAMES": ("VHS_FILENAMES",),  # 端口显示为VHS_FILENAMES，类型为VHS_FILENAMES，匹配VideoCombine节点
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
            print(f"[DEBUG] upload_video called with VHS_FILENAMES: {VHS_FILENAMES}")
            print(f"[DEBUG] Type of VHS_FILENAMES: {type(VHS_FILENAMES)}")
            
            # Handle VideoHelperSuite output format: [true, ["path1.png", "path2.mp4"]]
            video_path = None
            
            if isinstance(VHS_FILENAMES, (list, tuple)) and len(VHS_FILENAMES) >= 2:
                print(f"[DEBUG] Detected list with length >= 2: {len(VHS_FILENAMES)}")
                # Check if it's the VideoHelperSuite format: [boolean, [file_paths]]
                if isinstance(VHS_FILENAMES[0], bool) and isinstance(VHS_FILENAMES[1], (list, tuple)):
                    print(f"[DEBUG] Confirmed VideoHelperSuite format: bool={VHS_FILENAMES[0]}, files={VHS_FILENAMES[1]}")
                    # Look for video files (.mp4, .avi, .mov, .webm', '.mkv) in the file list
                    video_files = []
                    for file_path in VHS_FILENAMES[1]:
                        print(f"[DEBUG] Checking file: {file_path}")
                        if file_path.lower().endswith(('.mp4', '.avi', '.mov', '.webm', '.mkv')):
                            video_files.append(file_path)
                            print(f"[DEBUG] Found video file: {file_path}")
                    
                    if video_files:
                        # 如果有多个视频文件，选择最后一个（通常是最终输出）
                        video_path = video_files[-1]
                        print(f"[DEBUG] Selected video file: {video_path}")
                    else:
                        print("[DEBUG] No video file found in VideoHelperSuite output")
                        return ("Error: No video file found in VideoHelperSuite output",)
                else:
                    print(f"[DEBUG] Not VideoHelperSuite format. First elem type: {type(VHS_FILENAMES[0])}, second elem type: {type(VHS_FILENAMES[1])}")
                    # Fallback: assume it's a simple list and try first item
                    for item in VHS_FILENAMES:
                        if isinstance(item, str) and item.lower().endswith(('.mp4', '.avi', '.mov', '.webm', '.mkv')):
                            video_path = item
                            break
                    if not video_path:
                        video_path = VHS_FILENAMES[0] if VHS_FILENAMES else None
                        print(f"[DEBUG] Fallback: using first item as video_path: {video_path}")
            elif isinstance(VHS_FILENAMES, str):
                video_path = VHS_FILENAMES
                print(f"[DEBUG] Direct string input: {video_path}")
            else:
                print(f"[DEBUG] Unexpected input type: {type(VHS_FILENAMES)}")
            
            print(f"[DEBUG] Final video_path: {video_path}")
            if not video_path:
                print(f"[DEBUG] ERROR: Could not extract video path from input: {VHS_FILENAMES}")
                return (f"Error: Could not extract video path from input. Input was: {VHS_FILENAMES}",)
            
            # 文件存在性检查改为警告，因为ComfyUI可能在临时目录工作
            if not os.path.exists(video_path):
                print(f"[WARNING] Video file may not exist yet: {video_path}")
                print(f"[DEBUG] Will attempt upload anyway, might fail later")
                # 不返回错误，继续尝试上传
            
            # Get file extension
            _, ext = os.path.splitext(video_path)
            if not ext:
                ext = '.mp4'
            
            # Generate filename
            if random_filename:
                filename = self.generate_random_filename(ext.lstrip('.'))
            else:
                # Ensure filename has proper extension
                if not filename.lower().endswith(('.mp4', '.avi', '.mov', '.webm', '.mkv')):
                    filename += ext if ext else '.mp4'
            
            # Setup OSS auth
            auth = oss2.Auth(access_key, access_secret)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)
            
            # Prepare OSS path
            oss_path = os.path.join(path, filename).replace('\\', '/')
            
            # Upload video to OSS
            with open(video_path, 'rb') as f:
                bucket_obj.put_object(oss_path, f)
            
            # Construct URL
            if endpoint.startswith('https://'):
                base_url = endpoint.replace('https://', f'https://{bucket}.')
            elif endpoint.startswith('http://'):
                base_url = endpoint.replace('http://', f'http://{bucket}.')
            else:
                # Assume https if no protocol specified
                base_url = f'https://{bucket}.{endpoint}'
            
            file_url = f"{base_url}/{oss_path}"
            
            print(f"Video uploaded successfully to: {file_url}")
            return (file_url,)
            
        except Exception as e:
            print(f"Error uploading video to OSS: {str(e)}")
            return (f"Error: {str(e)}",)

# Node mappings for ComfyUI
NODE_CLASS_MAPPINGS = {
    "OSSImageUploader": OSSImageUploader,
    "OSSVideoUploader": OSSVideoUploader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "OSSImageUploader": "OSS Image Uploader",
    "OSSVideoUploader": "OSS Video Uploader",
}