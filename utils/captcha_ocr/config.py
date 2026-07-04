"""验证码第三方识别平台与模型配置。

业务层只选择验证码类型/识别平台；具体模型 ID 集中放这里。
"""

DEFAULT_ICONCLICK_OCR_PROVIDER = "chaojiying"

TULINGCLOUD_MODEL_IDS = {
    "rotate": "68421777",
    "iconclick": "25998073",
}


def normalize_iconclick_ocr_provider(provider: str | None) -> str:
    value = str(provider or "").strip().lower()
    aliases = {
        "cj": "chaojiying",
        "chaojiying": "chaojiying",
        "超级鹰": "chaojiying",
        "tuling": "tulingcloud",
        "tulingcloud": "tulingcloud",
        "图灵": "tulingcloud",
        "图灵云": "tulingcloud",
    }
    return aliases.get(value, DEFAULT_ICONCLICK_OCR_PROVIDER)


def normalize_tulingcloud_captcha_type(captcha_type: str | None) -> str:
    value = str(captcha_type or "").strip().lower()
    aliases = {
        "spin": "rotate",
        "rotate": "rotate",
        "旋转": "rotate",
        "旋转滑块": "rotate",
        "icon": "iconclick",
        "iconclick": "iconclick",
        "图标": "iconclick",
        "图标点选": "iconclick",
    }
    return aliases.get(value, "rotate")


def tulingcloud_model_id(captcha_type: str | None) -> str:
    normalized = normalize_tulingcloud_captcha_type(captcha_type)
    return TULINGCLOUD_MODEL_IDS.get(normalized, "")
