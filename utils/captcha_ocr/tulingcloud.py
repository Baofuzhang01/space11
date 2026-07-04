"""
图灵云打码平台集成模块
用于识别朝阳系选字验证码
"""

import base64
import io
import json
import logging
import re
from typing import Optional

from PIL import Image, ImageDraw


class TulingCloudOCR:
    """图灵云打码平台API调用类"""
    
    TULINGCLOUD_API_URL = "http://www.tulingcloud.com/tuling/predict"
    
    def __init__(self, username: str, password: str, model_id: str):
        """
        初始化图灵云API
        
        参数:
            username: 图灵云账户名
            password: 图灵云账户密码
            model_id: 识别模型ID (8位数字，用于选字验证码识别)
        """
        self.username = username
        self.password = password
        self.model_id = model_id

    @staticmethod
    def clamp_rotate_x(x: int, slider_max_x: int = 278) -> int:
        return max(0, min(slider_max_x, int(x)))

    @classmethod
    def rotate_angle_to_x(
        cls,
        angle: float,
        *,
        slider_max_x: int = 278,
        angle_scale: int = 500,
    ) -> int:
        return cls.clamp_rotate_x(
            round(float(angle) * slider_max_x / angle_scale),
            slider_max_x=slider_max_x,
        )

    @staticmethod
    def _circle_mask(size: tuple[int, int], inset: int = 17) -> Image.Image:
        w, h = size
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((inset, inset, w - inset, h - inset), fill=255)
        return mask

    @classmethod
    def compose_rotate_image(
        cls,
        shade_bytes: bytes,
        cutout_bytes: bytes,
        *,
        jpeg_quality: int = 95,
    ) -> bytes:
        """合成图灵云 rotate 模型需要的单张 RGB JPEG。"""
        shade = Image.open(io.BytesIO(shade_bytes)).convert("RGBA")
        cutout = Image.open(io.BytesIO(cutout_bytes)).convert("RGBA")

        if shade.size != cutout.size:
            shade = shade.resize(cutout.size, Image.Resampling.BICUBIC)

        merged = cutout.copy()
        merged.paste(shade, (0, 0), cls._circle_mask(cutout.size))

        out = io.BytesIO()
        merged.convert("RGB").save(out, format="JPEG", quality=jpeg_quality)
        return out.getvalue()
    
    def recognize_textclick(self, img_data: bytes) -> Optional[dict]:
        """
        识别选字验证码
        
        参数:
            img_data: 图片二进制数据
            
        返回:
            识别结果字典，包含 'text' 和可能的 'coordinates'，失败返回None
            示例: {"text": "朝阳系", "coordinates": [{"x": 100, "y": 200}, ...]}
        """
        try:
            import requests
            
            # 将图片编码为base64
            b64_data = base64.b64encode(img_data).decode('utf-8')
            
            # 构建请求数据
            data = {
                "username": self.username,
                "password": self.password,
                "ID": self.model_id,
                "b64": b64_data,
                "version": "3.1.1"
            }
            
            # 发送请求
            response = requests.post(
                self.TULINGCLOUD_API_URL,
                json=data,
                timeout=30
            )
            
            result = response.json()
            logging.debug(f"TulingCloud API Response: {result}")
            
            # 检查识别是否成功
            # API返回格式：
            # {
            #   "code": 1,
            #   "message": "",
            #   "data": {
            #     "顺序1": {"\u6587\u5b57": "\u5206", "X\u5750\u6807\u503c": 54, "Y\u5750\u6807\u503c": 28},
            #     "顺序2": {"\u6587\u5b57": "\u6d41", "X\u5750\u6807\u503c": 260, "Y\u5750\u6807\u503c": 50}
            #   }
            # }
            
            if result.get("code") in [0, 1]:  # code 0 or 1 both mean success
                response_data = result.get("data", {})
                
                # 处理图灵云的一牡七哨的珛c中文字段名
                if isinstance(response_data, dict):
                    coordinates = []
                    recognized_chars = []
                    
                    def _sort_key(key: str):
                        match = re.search(r"(\d+)$", str(key))
                        return int(match.group(1)) if match else 10**9

                    for key in sorted(response_data.keys(), key=_sort_key):
                        item = response_data.get(key)
                        if not isinstance(item, dict):
                            continue

                        char = item.get("文\u5b57") or item.get("text", "")
                        x = item.get("X\u5750\u6807\u503c") or item.get("x", 0)
                        y = item.get("Y\u5750\u6807\u503c") or item.get("y", 0)
                        
                        if char:
                            coordinates.append({
                                "x": int(x),
                                "y": int(y),
                                "text": str(char),
                                "source_key": str(key),
                            })
                            recognized_chars.append(str(char))
                            logging.debug(f"Parsed '{char}' at ({x}, {y}) from {key}")
                    
                    if recognized_chars and coordinates:
                        recognized_text = "".join(recognized_chars)
                        logging.debug(f"TulingCloud recognized text: {recognized_text}")
                        logging.debug(f"Coordinates: {coordinates}")
                        return {
                            "text": recognized_text,
                            "coordinates": coordinates,
                            "raw_result": result,
                        }
                    else:
                        logging.debug("TulingCloud returned empty result")
                        return None
                else:
                    logging.debug(f"Unexpected response data format: {type(response_data)}")
                    return None
            else:
                msg = result.get("message") or result.get("msg", "Unknown error")
                code = result.get("code", -1)
                logging.debug(f"TulingCloud recognition failed (code: {code}): {msg}")
                return None
                
        except ImportError:
            logging.error("requests library not installed. Install with: pip install requests")
            return None
        except json.JSONDecodeError:
            logging.debug("Failed to parse TulingCloud API response")
            return None
        except Exception as e:
            logging.debug(f"TulingCloud recognition failed: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            return None

    def recognize_iconclick(self, img_data: bytes) -> Optional[list[dict]]:
        """识别图标点选验证码，返回按点击顺序排列的坐标。

        图标点选模型返回格式示例：
        {
          "code": 1,
          "message": "",
          "data": {
            "顺序1": {"X坐标值": 165, "Y坐标值": 95},
            "顺序2": {"X坐标值": 50, "Y坐标值": 67}
          }
        }
        """
        try:
            import requests

            payload = {
                "username": self.username,
                "password": self.password,
                "ID": self.model_id,
                "b64": base64.b64encode(img_data).decode("ascii"),
                "version": "3.1.1",
            }
            response = requests.post(
                self.TULINGCLOUD_API_URL,
                json=payload,
                timeout=30,
            )
            result = response.json()
            logging.debug("TulingCloud iconclick API response: %s", result)
        except Exception as e:
            logging.warning("TulingCloud iconclick recognition request failed: %s", e)
            return None

        if result.get("code") not in {0, 1}:
            logging.debug(
                "TulingCloud iconclick recognition failed: code=%s message=%s",
                result.get("code"),
                result.get("message") or result.get("msg"),
            )
            return None

        data = result.get("data") or {}
        if not isinstance(data, dict):
            logging.debug("TulingCloud iconclick response data is not a dict: %s", result)
            return None

        def _sort_key(key: str):
            match = re.search(r"(\d+)$", str(key))
            return int(match.group(1)) if match else 10**9

        positions = []
        for key in sorted(data.keys(), key=_sort_key):
            item = data.get(key)
            if not isinstance(item, dict):
                continue
            x = item.get("X坐标值")
            y = item.get("Y坐标值")
            if x is None:
                x = item.get("X\u5750\u6807\u503c") or item.get("x")
            if y is None:
                y = item.get("Y\u5750\u6807\u503c") or item.get("y")
            try:
                positions.append({"x": int(float(x)), "y": int(float(y))})
            except (TypeError, ValueError):
                logging.debug("Skip invalid TulingCloud iconclick item %s=%s", key, item)

        if not positions:
            logging.debug("TulingCloud iconclick returned no usable coordinates")
            return None
        logging.info("TulingCloud iconclick recognition succeeded: %s", positions)
        return positions

    def recognize_rotate_angle(self, image_bytes: bytes) -> Optional[float]:
        """识别超星 rotate 验证码，返回“小圆顺时针旋转度数”。"""
        try:
            import requests

            payload = {
                "username": self.username,
                "password": self.password,
                "ID": self.model_id,
                "b64": base64.b64encode(image_bytes).decode("ascii"),
                "version": "3.1.1",
            }
            response = requests.post(
                self.TULINGCLOUD_API_URL,
                data=json.dumps(payload),
                timeout=15,
            )
            result = response.json()
            logging.debug("TulingCloud rotate API response: %s", result)
        except Exception as e:
            logging.debug("TulingCloud rotate recognition request failed: %s", e)
            return None

        if result.get("code") not in {0, 1}:
            logging.debug(
                "TulingCloud rotate recognition failed: code=%s message=%s",
                result.get("code"),
                result.get("message") or result.get("msg"),
            )
            return None

        data = result.get("data") or {}
        angle = data.get("小圆顺时针旋转度数")
        if angle is None:
            logging.debug("TulingCloud rotate response has no angle field: %s", result)
            return None

        try:
            return float(angle)
        except (TypeError, ValueError):
            logging.debug("TulingCloud rotate angle is not numeric: %r", angle)
            return None

    def solve_rotate_x(
        self,
        shade_bytes: bytes,
        cutout_bytes: bytes,
    ) -> Optional[dict]:
        """两图合成 -> 图灵云识别 -> 计算超星 rotate x。"""
        try:
            composed_image = self.compose_rotate_image(shade_bytes, cutout_bytes)
        except Exception as e:
            logging.debug("Failed to compose rotate captcha image: %s", e)
            return None

        angle = self.recognize_rotate_angle(composed_image)
        if angle is None:
            return None

        return {
            "angle": angle,
            "x": self.rotate_angle_to_x(angle),
        }
    
    @staticmethod
    def query_balance(username: str, password: str) -> Optional[float]:
        """
        查询账户余额
        
        参数:
            username: 图灵云账户名
            password: 图灵云账户密码
            
        返回:
            余额（元），失败返回None
        """
        try:
            import requests
            
            # 构建查询请求
            # 注意：此方法需要根据图灵云API文档调整
            # 这是推测的实现，需要验证
            data = {
                "username": username,
                "password": password,
                "action": "getBalance"
            }
            
            response = requests.post(
                "http://www.tulingcloud.com/tuling/user/balance",
                json=data,
                timeout=30
            )
            
            result = response.json()
            
            if result.get("code") == 0:
                balance = float(result.get("data", {}).get("balance", 0))
                logging.debug(f"TulingCloud balance query success: {balance}")
                return balance
            else:
                logging.warning(f"TulingCloud balance query failed: {result.get('msg')}")
                return None
                
        except Exception as e:
            logging.error(f"TulingCloud balance query error: {e}")
            return None
