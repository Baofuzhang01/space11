"""第三方验证码识别接口集中入口。"""

from .chaojiying import ChaojiyingOCR
from .config import (
    DEFAULT_ICONCLICK_OCR_PROVIDER,
    normalize_iconclick_ocr_provider,
    normalize_tulingcloud_captcha_type,
    tulingcloud_model_id,
)
from .tulingcloud import TulingCloudOCR

__all__ = [
    "ChaojiyingOCR",
    "DEFAULT_ICONCLICK_OCR_PROVIDER",
    "TulingCloudOCR",
    "normalize_iconclick_ocr_provider",
    "normalize_tulingcloud_captcha_type",
    "tulingcloud_model_id",
]
