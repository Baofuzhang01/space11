import json
import time
import argparse
import os
import logging
import datetime
import threading
import random
from zoneinfo import ZoneInfo
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

# 统一日志时间为北京时间，方便在 GitHub Actions 日志中查看
# 精确到毫秒，格式示例：2026-01-22 19:16:59.123 [Asia/Shanghai] - INFO - ...
class BeijingFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        """始终将日志时间格式化为北京时间。"""
        dt = datetime.datetime.fromtimestamp(record.created, ZoneInfo("Asia/Shanghai"))
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


_formatter = BeijingFormatter(
    fmt="%(asctime)s.%(msecs)03d [Asia/Shanghai] - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_handler = logging.StreamHandler()
_handler.setFormatter(_formatter)
_log_dir = Path(__file__).resolve().parent / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_file_handler = TimedRotatingFileHandler(
    filename=str(_log_dir / "reserve.log"),
    when="midnight",
    interval=1,
    backupCount=1,
    encoding="utf-8",
)
_file_handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_handler, _file_handler])


def _beijing_now() -> datetime.datetime:
    """获取北京时间（带时区信息）。"""
    return datetime.datetime.now(ZoneInfo("Asia/Shanghai"))


def _wait_until(
    target_dt: datetime.datetime,
    *,
    coarse_sleep_s: float = 0.05,
    medium_sleep_s: float = 0.005,
    fine_sleep_s: float = 0.001,
    spin_window_ms: float = 2.0,
) -> None:
    """分段等待到目标时刻，尽量压低 oversleep 带来的毫秒级误差。"""
    spin_window_s = max(0.0, spin_window_ms / 1000.0)

    while True:
        remaining_s = (target_dt - _beijing_now()).total_seconds()
        if remaining_s <= 0:
            return

        if remaining_s > 0.2:
            time.sleep(min(coarse_sleep_s, remaining_s / 2))
            continue

        if remaining_s > 0.02:
            time.sleep(min(medium_sleep_s, max(0.0, remaining_s - 0.01)))
            continue

        if remaining_s > spin_window_s:
            time.sleep(min(fine_sleep_s, max(0.0, remaining_s - spin_window_s)))
            continue

        while _beijing_now() < target_dt:
            pass
        return


def _warm_connection_before_token(s, url: str, timeout_s: float) -> None:
    """Finish page pre-warm before the formal token window; always discard its page token."""
    try:
        s.warm_connection(url, timeout=timeout_s, quiet=True)
    except Exception:
        pass
    logging.info(
        "[warm] Page pre-warm finished before token window, timeout=%dms; response discarded",
        int(max(0.001, float(timeout_s)) * 1000),
    )


def _try_page_prewarm_with_full_window(
    s,
    url: str,
    request_nodes,
    *,
    not_before: datetime.datetime | None = None,
    minimum_window_s: float = 4.0,
) -> bool:
    """Run one 4-second page pre-warm only when the next pending request node is far enough."""
    now = _beijing_now()
    minimum_window_s = max(4.0, float(minimum_window_s))
    if not request_nodes:
        logging.info("[warm] Skip connection pre-warm because no pending request node was provided")
        return False

    nearest_node_name, nearest_node_dt = min(request_nodes, key=lambda item: item[1])
    planned_start_dt = max(now, not_before) if not_before is not None else now
    warm_budget_s = (nearest_node_dt - planned_start_dt).total_seconds()
    if warm_budget_s <= minimum_window_s:
        logging.info(
            "[warm] Skip connection pre-warm because its planned start leaves only %.0fms "
            "before %s request node; more than %.0fms is required",
            warm_budget_s * 1000,
            nearest_node_name,
            minimum_window_s * 1000,
        )
        return False

    if not_before is not None:
        _wait_until(not_before)

    remaining_s = (nearest_node_dt - _beijing_now()).total_seconds()
    if remaining_s <= minimum_window_s:
        logging.info(
            "[warm] Skip connection pre-warm after waiting because only %.0fms remain before "
            "%s request node; more than %.0fms is required",
            remaining_s * 1000,
            nearest_node_name,
            minimum_window_s * 1000,
        )
        return False

    logging.info(
        "[warm] Dispatch connection pre-warm with full 4000ms window before "
        f"{nearest_node_name} request node; budget {remaining_s * 1000:.0f}ms"
    )
    _warm_connection_before_token(s, url, timeout_s=4.0)
    return True


from utils import AES_Decrypt, reserve, get_user_credentials
from utils.reserve import CredentialRejectedError
from utils.time_utils import (
    infer_use_custom_day,
    normalize_day_offset,
    parse_times_range,
    resolve_request_day,
)


def _now(action: bool) -> datetime.datetime:
    """获取当前逻辑时间。

    为了在 GitHub Actions 日志中时间统一可读：
    - 本地模式(action=False): 使用本地系统时间；1111
    - GitHub Actions(action=True): 使用北京时间(Asia/Shanghai)。
    """
    if action:
        return _beijing_now()
    return datetime.datetime.now()


# 日志时间：保留 3 位毫秒，和日志头部保持一致
get_log_time = lambda action: _now(action).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
# 逻辑比较时间：只用到当天的时分秒
get_hms = lambda action: _now(action).strftime("%H:%M:%S")
get_current_dayofweek = lambda action: _now(action).strftime("%A")


def _format_seat_number(seat_num: int) -> str:
    """将座位号格式化为三位数字符串，如 1 -> '001', 43 -> '043'"""
    return f"{seat_num:03d}"


def _pick_ordered_fallback_seat(
    base_seat_num: int,
    attempt_no: int,
    used_seats: set[str] | None = None,
) -> tuple[str | None, str]:
    """按固定顺序生成补抢座位号。

    顺序为：
    第 1 轮: +1
    第 2 轮: -1
    第 3 轮: +2
    第 4 轮: -2
    ...
    第 9 轮: +5
    第 10 轮: -5

    返回三位数字符串和本轮偏移说明；如果座位号无效或已用过，则返回 (None, offset)。
    """
    distance = (attempt_no + 1) // 2
    direction = 1 if attempt_no % 2 == 1 else -1
    offset = direction * distance
    seat_num = base_seat_num + offset
    formatted_offset = f"{offset:+d}"

    if seat_num <= 0:
        return None, formatted_offset

    formatted_seat = _format_seat_number(seat_num)
    if used_seats and formatted_seat in used_seats:
        return None, formatted_offset

    return formatted_seat, formatted_offset


def _pick_next_ordered_fallback_seat(
    base_seat_num: int,
    start_attempt_no: int,
    used_seats: set[str] | None = None,
) -> tuple[str | None, str, int]:
    """从指定轮次开始，跳过无效/已用座位，返回下一个可尝试的有序补位。"""
    for attempt_no in range(
        max(1, start_attempt_no), MAX_SEAT_INCREMENT_ATTEMPTS + 1
    ):
        seat, offset = _pick_ordered_fallback_seat(
            base_seat_num,
            attempt_no,
            used_seats,
        )
        if seat:
            return seat, offset, attempt_no
    return None, "", max(1, start_attempt_no)


def _normalize_backup_slots(raw_slots) -> list[dict]:
    if isinstance(raw_slots, str):
        result = []
        for token in raw_slots.split(","):
            token = token.strip()
            if not token or "-" not in token:
                continue
            roomid, seatid = token.split("-", 1)
            roomid = roomid.strip()
            seatid = seatid.strip()
            if not roomid or not seatid:
                continue
            result.append(
                {
                    "roomid": roomid,
                    "seatid": seatid,
                    "seatPageId": roomid,
                    "fidEnc": "",
                }
            )
        return result
    if not isinstance(raw_slots, list):
        return []
    result = []
    for item in raw_slots:
        if not isinstance(item, dict):
            continue
        roomid = str(item.get("roomid") or item.get("r") or "").strip()
        seatid = str(item.get("seatid") or item.get("s") or "").strip()
        if not roomid or not seatid:
            continue
        result.append(
            {
                "roomid": roomid,
                "seatid": seatid,
                "seatPageId": str(item.get("seatPageId") or item.get("p") or roomid).strip(),
                "fidEnc": str(item.get("fidEnc") or item.get("f") or "").strip(),
            }
        )
    return result


def _available_preheated_captchas(results, consumed):
    return [
        captcha
        for shot_idx in (1, 2, 3)
        if (captcha := results.get(shot_idx, "")) and captcha not in consumed
    ]


def _store_shared_captcha(results, consumed, captcha):
    if not captcha:
        return None
    for slot_idx in (1, 2, 3):
        if results.get(slot_idx) == captcha:
            return slot_idx
    slot_idx = next(
        (
            idx
            for idx in (1, 2, 3)
            if not results.get(idx) or results.get(idx) in consumed
        ),
        None,
    )
    if slot_idx is None:
        return None
    results[slot_idx] = captcha
    consumed.discard(captcha)
    return slot_idx


def _reuse_unsubmitted_captcha(submit_sent, captcha):
    return "" if submit_sent else (captcha or "")


def _click_captcha_preheat_slots(slot_count: int):
    return (1, 2, 3) if slot_count > 1 else (1,)


def _should_wait_for_click_preheat(slot_count: int, is_alive: bool) -> bool:
    return slot_count <= 1 and is_alive


def _shared_captcha_preheat_is_serial(slot_count: int) -> bool:
    return slot_count > 1


def _should_wait_for_background_followup(submit_mode: str, shot_idx: int) -> bool:
    return submit_mode != "burst" and shot_idx > 1


def _getusedtimes_conflict_ready(handle) -> bool | None:
    if not isinstance(handle, dict):
        return None
    event = handle.get("event")
    if event is None or not event.is_set():
        return None
    conflict = handle.get("conflict")
    return conflict if isinstance(conflict, bool) else None


ENDTIME = "23:22:40"  # 根据学校的预约座位时间+40ms即可
WARM_CONNECTION_LEAD_MS = 2500  # 连接预热提前量（毫秒）
TEXTCLICK_FIRST_CAPTCHA_GUARD_MS = -1000  # 正数表示 T 前截止，负数表示允许延迟到 T 后
FIRST_TOKEN_DATE_MODE = "today"  # 首次取 token 的日期：today 或 submit_date
SKIP_FIRST_SEAT_QUERY = True  # 策略 A/C 首抢是否跳过 getusedtimes 查座
RESERVE_NEXT_DAY = True  # 预约明天而不是今天的
RESERVE_DAY_OFFSET = None  # 可选：覆盖提交参数 day 的北京时间日期偏移，2 表示后天
ENABLE_SLIDER = False  # 是否有滑块验证（调试阶段先关闭）
ENABLE_TEXTCLICK = False  # 是否有选字验证码（默认使用超级鹰打码平台）
ENABLE_ICONCLICK = False  # 是否有图标点选验证码（超级鹰 9103）
ENABLE_ROTATE = False  # 是否有旋转滑块验证码（使用图灵云 rotate 模型）
ICONCLICK_OCR_PROVIDER = "chaojiying"  # 图标点选识别平台：chaojiying / tulingcloud / jfbym
SEAT_API_MODE = "seat"  # 页面 token 接口模式：auto / seatengine / seat / seatengine_code / seat_code

FAST_PROBE_START_OFFSET_MS = 14  # 目标时间后多少毫秒开始轻探测
FAST_PROBE_INTERVAL_MS = 2  # 轻探测轮询间隔（毫秒）
FAST_PROBE_DEADLINE_MS = 1100  # 目标时间后多久强制结束轻探测并正式取 token


MAX_ATTEMPT = 1
SLEEPTIME = 0.05  # 每次抢座的间隔（减少到0.05秒以加快速度）



# 是否在每一轮主循环中都重新登录。
# True：每一轮都会重新创建会话并登录（原有行为）；
# False：每个账号只在第一次需要时登录一次，后续循环复用同一个会话。
RELOGIN_EVERY_LOOP = True
MAX_SEAT_INCREMENT_ATTEMPTS = 10


def _normalize_times(times):
    """把 times 统一成 [start, end] 结构。"""
    return parse_times_range(times)


def _split_action_credentials(usernames, passwords):
    """单账号 CX_* 凭据原样使用；只有旧版多账号变量才按逗号拆分。"""
    if (
        usernames == os.environ.get("CX_USERNAME")
        and passwords == os.environ.get("CX_PASSWORD")
    ):
        return [usernames], [passwords]
    return usernames.split(","), passwords.split(",")


def _load_runtime_config(config_path, dispatch_mode, action):
    if dispatch_mode:
        payload_raw = os.environ.get("DISPATCH_PAYLOAD")
        if not payload_raw:
            raise ValueError("DISPATCH_PAYLOAD is required when --dispatch is enabled")

        payload = json.loads(payload_raw)
        username = payload.get("username")
        password = payload.get("password")
        slots = payload.get("slots")

        # 兼容旧格式（单条 roomid/seatid/times）
        if not slots:
            roomid = payload.get("roomid")
            seatid = payload.get("seatid")
            times = payload.get("times")
            if roomid and times:
                slots = [{"roomid": roomid, "seatid": seatid, "times": times,
                          "seatPageId": payload.get("seatPageId") or "",
                          "fidEnc": payload.get("fidEnc") or "",
                          "backupSeats": payload.get("backupSeats") or "",
                          "backupSlots": payload.get("backupSlots") or [],
                          "use_custom_day": payload.get("use_custom_day", False)}]
            else:
                slots = []

        if not username or not password or not slots:
            raise ValueError("DISPATCH_PAYLOAD missing required fields")

        decrypted_password = AES_Decrypt(password)
        os.environ["CX_USERNAME"] = username
        os.environ["CX_PASSWORD"] = decrypted_password
        current_day = get_current_dayofweek(action)

        reserve_list = []
        for slot in slots:
            seatid = slot.get("seatid")
            times = _normalize_times(slot.get("times"))
            use_custom_day = infer_use_custom_day(
                times,
                slot.get("use_custom_day", payload.get("use_custom_day", False)),
            )
            reserve_list.append({
                "username": username,
                "password": decrypted_password,
                "times": times,
                "use_custom_day": use_custom_day,
                "roomid": slot.get("roomid"),
                "seatid": seatid if isinstance(seatid, list) else [seatid],
                "seatPageId": slot.get("seatPageId") or "",
                "fidEnc": slot.get("fidEnc") or "",
                "backupSeats": slot.get("backupSeats") or "",
                "backupSlots": slot.get("backupSlots") or [],
                "daysofweek": [current_day],
            })

        return {
            "reserve": reserve_list,
            "strategy": payload.get("strategy", {}),
            "endtime": payload.get("endtime", ENDTIME),
            "seat_api_mode": payload.get("seat_api_mode", SEAT_API_MODE),
            "reserve_next_day": payload.get("reserve_next_day", RESERVE_NEXT_DAY),
            "reserve_day_offset": payload.get("reserve_day_offset"),
            "enable_slider": payload.get("enable_slider", ENABLE_SLIDER),
            "enable_textclick": payload.get("enable_textclick", ENABLE_TEXTCLICK),
            "enable_iconclick": payload.get("enable_iconclick", ENABLE_ICONCLICK),
            "enable_rotate": payload.get("enable_rotate", ENABLE_ROTATE),
            "iconclick_ocr_provider": payload.get("iconclick_ocr_provider", ICONCLICK_OCR_PROVIDER),
            "relogin_every_loop": False,
        }

    with open(config_path, "r+") as data:
        return json.load(data)


def _parse_int_range(value, fallback):
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return fallback, fallback
    if isinstance(value, str) and "," in value:
        parts = value.split(",", 1)
        try:
            return int(parts[0].strip()), int(parts[1].strip())
        except (TypeError, ValueError):
            return fallback, fallback
    return fallback, fallback


def _normalize_slider_lead_range_value_ms(value: int) -> int:
    """只规范 slider_lead_seconds_range：兼容旧秒值，且至少提前 5000ms。"""
    value = int(value)
    value = value * 1000 if 0 <= value < 30 else value
    return max(5000, value)


def _apply_strategy_config(config):
    global ENDTIME
    global RELOGIN_EVERY_LOOP
    global RESERVE_NEXT_DAY
    global ENABLE_SLIDER
    global ENABLE_TEXTCLICK
    global ENABLE_ICONCLICK
    global ENABLE_ROTATE
    global ICONCLICK_OCR_PROVIDER
    global STRATEGY_LOGIN_LEAD_SECONDS
    global STRATEGY_SLIDER_LEAD_MS
    global STRATEGIC_MODE
    global PRE_FETCH_TOKEN_MS
    global FIRST_SUBMIT_OFFSET_MS
    global SUBMIT_MODE
    global BURST_OFFSETS_MS
    global TOKEN_FETCH_DELAY_MS
    global FAST_PROBE_START_OFFSET_MS
    global WARM_CONNECTION_LEAD_MS
    global TEXTCLICK_FIRST_CAPTCHA_GUARD_MS
    global FIRST_TOKEN_DATE_MODE
    global SKIP_FIRST_SEAT_QUERY
    global SEAT_API_MODE
    global RESERVE_DAY_OFFSET

    strategy_cfg = config.get("strategy", {})
    ENDTIME = config.get("endtime", ENDTIME)
    RESERVE_NEXT_DAY = bool(config.get("reserve_next_day", RESERVE_NEXT_DAY))
    RESERVE_DAY_OFFSET = normalize_day_offset(config.get("reserve_day_offset", None))
    ENABLE_SLIDER = bool(config.get("enable_slider", ENABLE_SLIDER))
    ENABLE_TEXTCLICK = bool(config.get("enable_textclick", ENABLE_TEXTCLICK))
    ENABLE_ICONCLICK = bool(config.get("enable_iconclick", ENABLE_ICONCLICK))
    ENABLE_ROTATE = bool(config.get("enable_rotate", ENABLE_ROTATE))
    iconclick_provider = str(config.get("iconclick_ocr_provider", ICONCLICK_OCR_PROVIDER)).strip().lower()
    ICONCLICK_OCR_PROVIDER = (
        iconclick_provider
        if iconclick_provider in {"chaojiying", "tulingcloud", "jfbym"}
        else "chaojiying"
    )
    seat_api_mode = str(config.get("seat_api_mode", SEAT_API_MODE)).strip().lower()
    SEAT_API_MODE = (
        seat_api_mode
        if seat_api_mode in {"auto", "seatengine", "seat", "seatengine_code", "seat_code"}
        else "auto"
    )
    os.environ["CX_SEAT_API_MODE"] = SEAT_API_MODE
    if "login_lead_seconds" in strategy_cfg:
        STRATEGY_LOGIN_LEAD_SECONDS = int(strategy_cfg.get("login_lead_seconds", 20))
    else:
        login_lead_min, login_lead_max = _parse_int_range(
            strategy_cfg.get("login_lead_seconds_range"),
            20,
        )
        STRATEGY_LOGIN_LEAD_SECONDS = random.randint(
            min(login_lead_min, login_lead_max),
            max(login_lead_min, login_lead_max),
        )
    if "slider_lead_seconds_range" in strategy_cfg:
        slider_lead_min, slider_lead_max = _parse_int_range(
            strategy_cfg.get("slider_lead_seconds_range"),
            14000,
        )
        normalized_slider_min = _normalize_slider_lead_range_value_ms(slider_lead_min)
        normalized_slider_max = _normalize_slider_lead_range_value_ms(slider_lead_max)
        STRATEGY_SLIDER_LEAD_MS = random.randint(
            min(normalized_slider_min, normalized_slider_max),
            max(normalized_slider_min, normalized_slider_max),
        )
    else:
        STRATEGY_SLIDER_LEAD_MS = int(strategy_cfg.get("slider_lead_seconds", 14)) * 1000
    if (
        (ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK)
        and STRATEGY_SLIDER_LEAD_MS > STRATEGY_LOGIN_LEAD_SECONDS * 1000
    ):
        logging.info(
            "[策略] 验证码预热提前量 %dms 超过登录提前量 %ds；"
            "将自动延后到登录完成后立即开始",
            STRATEGY_SLIDER_LEAD_MS,
            STRATEGY_LOGIN_LEAD_SECONDS,
        )
    STRATEGIC_MODE = strategy_cfg.get("mode", "B")
    PRE_FETCH_TOKEN_MS = int(strategy_cfg.get("pre_fetch_token_ms", 3000))
    FIRST_SUBMIT_OFFSET_MS = int(strategy_cfg.get("first_submit_offset_ms", 89))
    SUBMIT_MODE = strategy_cfg.get("submit_mode", "serial")
    BURST_OFFSETS_MS = strategy_cfg.get("burst_offsets_ms", [120, 420, 820])[:3]
    TOKEN_FETCH_DELAY_MS = int(strategy_cfg.get("token_fetch_delay_ms", 50))
    token_fetch_timeout_ms = max(
        1,
        int(strategy_cfg.get("token_fetch_timeout_ms", 2830)),
    )
    fast_probe_timeout_ms = max(
        1,
        int(strategy_cfg.get("fast_probe_timeout_ms", 2830)),
    )
    os.environ["CX_TOKEN_FETCH_TIMEOUT_MS"] = str(token_fetch_timeout_ms)
    os.environ["CX_FAST_PROBE_CONNECT_TIMEOUT"] = f"{fast_probe_timeout_ms / 1000.0:g}"
    os.environ["CX_FAST_PROBE_READ_TIMEOUT"] = f"{fast_probe_timeout_ms / 1000.0:g}"
    FAST_PROBE_START_OFFSET_MS = int(
        strategy_cfg.get("fast_probe_start_offset_ms", FAST_PROBE_START_OFFSET_MS)
    )
    WARM_CONNECTION_LEAD_MS = int(
        strategy_cfg.get("warm_connection_lead_ms", WARM_CONNECTION_LEAD_MS)
    )
    TEXTCLICK_FIRST_CAPTCHA_GUARD_MS = int(
        strategy_cfg.get(
            "textclick_first_captcha_guard_ms",
            TEXTCLICK_FIRST_CAPTCHA_GUARD_MS,
        )
    )
    first_token_date_mode = str(
        strategy_cfg.get("first_token_date_mode", FIRST_TOKEN_DATE_MODE)
    ).strip().lower()
    FIRST_TOKEN_DATE_MODE = (
        first_token_date_mode if first_token_date_mode in {"today", "submit_date"} else "submit_date"
    )
    if "skip_first_seat_query" in strategy_cfg:
        SKIP_FIRST_SEAT_QUERY = bool(strategy_cfg.get("skip_first_seat_query"))
    RELOGIN_EVERY_LOOP = bool(config.get("relogin_every_loop", RELOGIN_EVERY_LOOP))


def _get_first_token_day(
    warm_day: datetime.date,
    submit_day: datetime.date,
) -> datetime.date:
    """返回首次取 token 使用的日期。"""
    if FIRST_TOKEN_DATE_MODE == "today":
        return warm_day
    return submit_day


def _get_beijing_target_from_endtime() -> datetime.datetime:
    """根据 ENDTIME 计算目标时间（北京时间，当天 ENDTIME 减 40 秒）。"""
    now = _beijing_now()
    today = now.date()
    h, m, s = map(int, ENDTIME.split(":"))
    end_dt = datetime.datetime(
        year=today.year,
        month=today.month,
        day=today.day,
        hour=h,
        minute=m,
        second=s,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    if end_dt < now and now - end_dt > datetime.timedelta(hours=12):
        end_dt += datetime.timedelta(days=1)
    return end_dt - datetime.timedelta(seconds=40)
    # return end_dt - datetime.timedelta(minutes=1)  # ENDTIME 前 1 分钟（60秒）


def _get_beijing_end_dt_from_target(target_dt: datetime.datetime) -> datetime.datetime:
    """返回本轮预约窗口的结束时刻，支持 ENDTIME 跨午夜。"""
    return target_dt + datetime.timedelta(seconds=40)


def _get_strategy_login_deadline(target_dt: datetime.datetime) -> datetime.datetime:
    """战略登录的最晚补救时刻。

    目的不是无限等待登录，而是在前三枪仍然有意义的时间窗内继续补救。
    超过该时刻后交给普通主循环处理，避免阻塞后续流程。
    """
    if SUBMIT_MODE == "burst":
        max_offset_ms = max(BURST_OFFSETS_MS) if BURST_OFFSETS_MS else 0
    else:
        max_offset_ms = max(
            FIRST_SUBMIT_OFFSET_MS,
            TOKEN_FETCH_DELAY_MS,
        )
    return target_dt + datetime.timedelta(milliseconds=max_offset_ms + 500)


def _get_first_token_start_dt(target_dt: datetime.datetime) -> datetime.datetime:
    """返回战略流程中首个探测/正式 token 请求最早可能启动的时间。"""
    if SUBMIT_MODE == "burst":
        if STRATEGIC_MODE == "A":
            return target_dt - datetime.timedelta(milliseconds=PRE_FETCH_TOKEN_MS)
        if STRATEGIC_MODE == "C":
            return target_dt + datetime.timedelta(milliseconds=FAST_PROBE_START_OFFSET_MS)
        first_offset = min(BURST_OFFSETS_MS or [FIRST_SUBMIT_OFFSET_MS])
        return target_dt + datetime.timedelta(milliseconds=first_offset)

    if STRATEGIC_MODE == "A":
        return target_dt - datetime.timedelta(milliseconds=PRE_FETCH_TOKEN_MS)
    if STRATEGIC_MODE == "C":
        return target_dt + datetime.timedelta(milliseconds=FAST_PROBE_START_OFFSET_MS)
    return target_dt + datetime.timedelta(milliseconds=FIRST_SUBMIT_OFFSET_MS)


def _get_captcha_start_dt(
    target_dt: datetime.datetime,
    login_completed_at: datetime.datetime,
) -> datetime.datetime:
    configured_start_dt = target_dt - datetime.timedelta(
        milliseconds=STRATEGY_SLIDER_LEAD_MS
    )
    return max(configured_start_dt, login_completed_at)


def _get_captcha_preheat_deadline(
    target_dt: datetime.datetime,
    first_token_start_dt: datetime.datetime,
    slot_count: int,
    strategy_mode: str,
) -> datetime.datetime:
    if slot_count <= 1 or strategy_mode not in {"A", "C"}:
        return target_dt
    return min(
        target_dt,
        first_token_start_dt - datetime.timedelta(seconds=2),
    )


def _remaining_captcha_preheat_seconds(
    now: datetime.datetime,
    soft_deadline: datetime.datetime,
    retry_deadline: datetime.datetime,
    retry_when_empty: bool,
    results,
) -> float:
    if now < soft_deadline:
        return (soft_deadline - now).total_seconds()
    if retry_when_empty and not any(results.values()):
        return (retry_deadline - now).total_seconds()
    return 0.0


def _probe_then_get_page_token(
    s,
    token_url: str,
    target_dt: datetime.datetime,
    *,
    require_value: bool = True,
    formal_fetch_not_before=None,
    not_open_retry_until=None,
    not_open_retry_interval: float | None = None,
    start_log_message: str | None = None,
):
    """战略模式首枪取 token 前的轻量探测。"""
    probe_start_dt = target_dt + datetime.timedelta(milliseconds=FAST_PROBE_START_OFFSET_MS)
    probe_deadline_dt = target_dt + datetime.timedelta(milliseconds=FAST_PROBE_DEADLINE_MS)
    if _beijing_now() < probe_start_dt:
        _wait_until(probe_start_dt)

    if start_log_message:
        logging.info("%s，实际启动时间 %s", start_log_message, _beijing_now())

    probe_attempt = 0
    while True:
        probe_attempt += 1
        probe_result = s.probe_not_open_fast(
            token_url,
            log_connection_reuse=(probe_attempt == 1),
        )
        probe_checked_dt = _beijing_now()
        elapsed_ms = max(0.0, (probe_checked_dt - target_dt).total_seconds() * 1000)
        if probe_result.get("is_not_open"):
            logging.info(
                f"[strategic] 快速探测第 {probe_attempt} 次：页面仍未开放；"
                f"探测时间 {probe_checked_dt}，距目标时刻 {elapsed_ms:.1f}ms"
            )
            if probe_checked_dt >= probe_deadline_dt:
                logging.warning(
                    f"[strategic] 快速探测在第 {probe_attempt} 次达到硬截止时间；"
                    f"距目标时刻 {elapsed_ms:.1f}ms，强制切换到正式取 token"
                )
                break
            time.sleep(FAST_PROBE_INTERVAL_MS / 1000)
            continue

        probe_token = probe_result.get("token", "")
        probe_value = probe_result.get("value", "") if require_value else ""
        if probe_token:
            logging.info(
                f"[strategic] 快速探测第 {probe_attempt} 次：拿到可复用 token；"
                f"探测时间 {probe_checked_dt}，距目标时刻 {elapsed_ms:.1f}ms，"
                "跳过额外 token 抓取"
            )
            return probe_token, probe_value

        logging.info(
            f"[strategic] 快速探测第 {probe_attempt} 次：未识别到未开放提示，"
            "但本次响应未提取到 token；"
            f"探测时间 {probe_checked_dt}，距目标时刻 {elapsed_ms:.1f}ms，"
            f"诊断={probe_result.get('diagnostic', {})}，切换到正式取 token"
        )
        break

    if formal_fetch_not_before is not None and _beijing_now() < formal_fetch_not_before:
        _wait_until(formal_fetch_not_before)

    return s._get_page_token(
        token_url,
        require_value=require_value,
        not_open_retry_until=not_open_retry_until,
        not_open_retry_interval=not_open_retry_interval,
    )


def _get_page_token_until_success(
    s,
    token_url: str,
    *,
    require_value: bool = True,
    retry_until: datetime.datetime | None = None,
    retry_interval: float = 0.005,
    label: str = "token",
):
    """正式获取页面 token；空 token 视为失败并持续刷新到 retry_until。"""
    logging.info(
        f"[策略] {label}：开始从 {token_url} 正式获取 token"
        + (f"，持续重试到 {retry_until}" if retry_until else "")
    )
    token, value = s._get_page_token(
        token_url,
        require_value=require_value,
        not_open_retry_until=retry_until,
        not_open_retry_interval=retry_interval,
    )
    if token:
        logging.info(f"[策略] {label}：已从 {token_url} 获取 token：{token}")
    else:
        logging.error(f"[策略] {label}：正式获取后 token 仍为空")
    return token, value


def _burst_shot_worker(
    index, offset_ms, target_dt, s, token_url,
    times, roomid, seatid, captcha, action, results,
    token_submit_lock, submitted_captchas, use_custom_day=False, day="", fid_enc=""
):
    """定时连发（极限型）的单次提交工作线程。

    在 target_dt + offset_ms 时刻提交预约，结果写入 results[index]。
    页面 submit_enc/value 只能使用一次，且刷新页面会使之前获取的值失效。
    因此每枪都必须在锁内获取新 token/value 并立即 POST，禁止并发刷新或复用。
    """
    fire_dt = target_dt + datetime.timedelta(milliseconds=offset_ms)
    _wait_until(fire_dt)

    logging.info(
        f"[burst] 第 {index + 1} 枪在 {_beijing_now()} 触发（目标时间 + {offset_ms}ms）"
    )

    if (ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK) and not captcha:
        logging.error(
            f"[burst] 第 {index + 1} 枪验证码为空，跳过提交以避免空验证码"
        )
        results[index] = False
        return

    with token_submit_lock:
        token, value = s._get_page_token(
            token_url,
            require_value=True,
        )
        if not token:
            logging.error(f"[burst] 第 {index + 1} 枪获取页面 token 失败")
            results[index] = False
            return
        logging.info(
            f"[burst] 第 {index + 1} 枪从 {token_url} 即时获取 token：{token}"
        )
        submitted_captchas.add(captcha)
        result = s.get_submit(
            url=s.submit_url,
            times=times,
            token=token,
            roomid=roomid,
            seatid=seatid,
            captcha=captcha,
            action=action,
            value=value,
            dept_id_enc=fid_enc,
            use_custom_day=use_custom_day,
        )
    results[index] = result
    logging.info(f"[burst] Shot {index + 1} result: {result}")


def strategic_first_attempt(
    users,
    usernames: str | None,
    passwords: str | None,
    action: bool,
    target_dt: datetime.datetime,
    success_list=None,
    sessions=None,
    fallback_used_seats=None,
):
    """只在第一次调用时使用的“有策略抢座”。

    - 在目标时间前 2 分钟左右开始（由 Actions 的 cron 控制）；
    - 目标时间前 20 秒：预先获取页面 token / algorithm value；
    - 目标时间前 12 秒：预先完成滑块并拿到 validate；
    - 目标时间到达瞬间：直接调用 get_submit 提交一次；
    - 之后的重试逻辑仍交给原有 while 循环和 login_and_reserve。
    """
    if success_list is None:
        success_list = [False] * len(users)
    if fallback_used_seats is None or len(fallback_used_seats) != len(users):
        fallback_used_seats = [set() for _ in users]

    now = _beijing_now()
    # 如果已经过了目标时间，直接退回到普通逻辑由外层处理
    if now >= target_dt:
        return success_list

    # 等到“目标时间前若干秒”附近再开始策略流程，由 cron 提前少量时间启动
    thirty_before = target_dt - datetime.timedelta(seconds=STRATEGY_LOGIN_LEAD_SECONDS)
    _wait_until(thirty_before)

    usernames_list, passwords_list = None, None
    if action:
        if not usernames or not passwords:
            raise Exception("USERNAMES or PASSWORDS not configured correctly in env")
        usernames_list, passwords_list = _split_action_credentials(
            usernames, passwords
        )
        if len(usernames_list) != len(passwords_list):
            raise Exception("USERNAMES and PASSWORDS count mismatch")

    current_dayofweek = get_current_dayofweek(action)
    active_strategy_slot_count = sum(
        1
        for index, user in enumerate(users)
        if not success_list[index]
        and current_dayofweek in user.get("daysofweek", [])
    )
    preheated_captcha_results = {1: "", 2: "", 3: ""}
    consumed_preheated_captchas = set()
    warm_done = False
    shared_strategy_session = None
    shared_strategy_username = None
    shared_click_preheat_threads = []
    claimed_backup_seats = set()
    strategic_primary_seats = set()
    for candidate_user in users:
        if current_dayofweek not in candidate_user.get("daysofweek", []):
            continue
        candidate_room = str(candidate_user.get("roomid") or "").strip()
        candidate_seats = candidate_user.get("seatid")
        candidate_seat_list = (
            [candidate_seats]
            if isinstance(candidate_seats, str)
            else (candidate_seats if isinstance(candidate_seats, list) else [])
        )
        if candidate_room and candidate_seat_list:
            strategic_primary_seats.add((candidate_room, str(candidate_seat_list[0]).strip()))
    not_open_retry_until = target_dt + datetime.timedelta(milliseconds=FAST_PROBE_DEADLINE_MS)

    for index, user in enumerate(users):
        # 已经成功的配置不再参与策略尝试
        if success_list[index]:
            continue

        username = user["username"]
        password = user["password"]
        times = user["times"]
        roomid = user["roomid"]
        seatid = user["seatid"]
        seat_page_id = user.get("seatPageId")
        fid_enc = user.get("fidEnc")
        use_custom_day = bool(user.get("use_custom_day"))
        daysofweek = user["daysofweek"]

        # 今天不预约该配置，跳过
        if current_dayofweek not in daysofweek:
            logging.info("[策略] 今天不在预约星期配置内，跳过当前配置")
            continue

        # Actions 模式：根据索引或单账号覆盖用户名和密码
        if action:
            if len(usernames_list) == 1:
                username = usernames_list[0]
                password = passwords_list[0]
            elif index < len(usernames_list):
                username = usernames_list[index]
                password = passwords_list[index]
            else:
                logging.error(
                    "[策略] USERNAMES/PASSWORDS 索引越界，跳过当前配置"
                )
                continue

        # seatid 可能是字符串或列表，只在策略阶段针对第一个座位做一次精准尝试
        seat_list = [seatid] if isinstance(seatid, str) else seatid
        if not seat_list:
            logging.error("[策略] 座位列表为空，跳过当前配置")
            continue

        logging.info(
            f"[策略] 开始策略首次尝试：{username} -- {times} -- {seat_list} "
            f"-- seatPageId={seat_page_id} -- fidEnc={fid_enc} -- use_custom_day={use_custom_day}"
        )

        first_seat = seat_list[0]
        backup_slots = _normalize_backup_slots(user.get("backupSeats") or user.get("backupSlots"))
        submit_day = resolve_request_day(
            times,
            RESERVE_NEXT_DAY,
            use_custom_day=use_custom_day,
            reserve_day_offset=RESERVE_DAY_OFFSET,
        )
        warm_day = str(_beijing_now().date())
        first_token_day = submit_day
        if not use_custom_day:
            first_token_day = str(
                _get_first_token_day(
                    _beijing_now().date(),
                    datetime.date.fromisoformat(submit_day),
                )
            )
        captcha1 = captcha2 = captcha3 = ""
        live_captcha_results = preheated_captcha_results
        unavailable_preheated_captchas = set(consumed_preheated_captchas)
        textclick_preheat_thread = None
        click_captcha_type = (
            "rotate" if ENABLE_ROTATE else "iconclick" if ENABLE_ICONCLICK else "textclick"
        )
        click_captcha_name = (
            "旋转滑块" if ENABLE_ROTATE else "图标点选" if ENABLE_ICONCLICK else "文字点选"
        )

        def _resolve_textclick_with_retries(
            captcha_session,
            label: str,
            *,
            max_retries: int | None = 3,
            deadline_func=None,
        ) -> str:
            attempt = 0
            while max_retries is None or attempt < max_retries:
                attempt += 1
                if deadline_func is not None and deadline_func() <= 0:
                    logging.warning(
                        f"[策略] {click_captcha_name} {label} 已停止：达到预热截止时间，"
                        f"共处理 {attempt - 1} 轮"
                    )
                    return ""
                try:
                    captcha = captcha_session.resolve_captcha(click_captcha_type) or ""
                except Exception as e:
                    logging.debug(
                        f"[策略] {click_captcha_name} {label} 第 {attempt} 次请求异常：{e}"
                    )
                    captcha = ""
                if captcha:
                    logging.info(
                        f"[策略] {click_captcha_name} {label} 第 {attempt} 轮处理成功，"
                        f"共处理 {attempt} 轮"
                    )
                    return captcha
                if max_retries is None or attempt < max_retries:
                    if deadline_func is None:
                        time.sleep(0.2)
                    else:
                        sleep_s = min(0.2, max(0.0, deadline_func()))
                        if sleep_s > 0:
                            time.sleep(sleep_s)

            logging.warning(
                f"[策略] {click_captcha_name} {label} 请求 {max_retries} 次后仍失败"
            )
            return ""

        def _resolve_single_captcha_until_success(
            captcha_session,
            captcha_type_name: str,
            label: str,
            *,
            max_retries: int | None = 3,
            deadline_func=None,
        ) -> str:
            attempt = 0
            log_name = captcha_type_name.capitalize()
            while max_retries is None or attempt < max_retries:
                attempt += 1
                if deadline_func is not None and deadline_func() <= 0:
                    logging.warning(
                        f"[策略] {log_name} {label} 在成功前停止：已达到预热截止时间"
                    )
                    return ""
                try:
                    captcha = captcha_session.resolve_captcha(captcha_type_name) or ""
                except Exception as e:
                    logging.debug(
                        f"[策略] {log_name} {label} 第 {attempt} 次处理异常：{e}"
                    )
                    captcha = ""
                if captcha:
                    logging.info(
                        f"[策略] {log_name} {label} 第 {attempt} 次处理成功"
                    )
                    return captcha
                if max_retries is None or attempt < max_retries:
                    if deadline_func is None:
                        time.sleep(0.2)
                    else:
                        sleep_s = min(0.2, max(0.0, deadline_func()))
                        if sleep_s > 0:
                            time.sleep(sleep_s)

            logging.warning(
                f"[策略] {log_name} {label} 请求 {max_retries} 次后仍失败"
            )
            return ""

        is_primary_strategy_config = shared_strategy_session is None
        if is_primary_strategy_config:
            # 1. 只有首个配置执行登录和预热；后续配置直接复用这个登录态。
            s = reserve(
                sleep_time=SLEEPTIME,
                max_attempt=MAX_ATTEMPT,
                enable_slider=ENABLE_SLIDER,
                enable_textclick=ENABLE_TEXTCLICK,
                enable_iconclick=ENABLE_ICONCLICK,
                enable_rotate=ENABLE_ROTATE,
                iconclick_ocr_provider=ICONCLICK_OCR_PROVIDER,
                reserve_next_day=RESERVE_NEXT_DAY,
                reserve_day_offset=RESERVE_DAY_OFFSET,
            )
            login_deadline = _get_strategy_login_deadline(target_dt)
            login_ok = False
            while _beijing_now() < login_deadline:
                if s.bootstrap_login(username, password, attempts=1):
                    login_ok = True
                    break

                remaining_login_s = (login_deadline - _beijing_now()).total_seconds()
                if remaining_login_s <= 0:
                    break

                logging.warning(
                    f"[策略] {username} 登录预启动失败，"
                    f"继续在策略窗口内重试 {remaining_login_s:.2f}s"
                )
                time.sleep(min(0.2, remaining_login_s))

            if not login_ok:
                logging.warning(
                    f"[策略] 跳过 {username} 的策略首次尝试："
                    f"直到策略截止时间 {login_deadline} 仍未登录成功"
                )
                continue

            if _beijing_now() >= target_dt:
                logging.warning(
                    f"[策略] {username} 在目标时间后才恢复登录；"
                    "继续策略提交，但预热预算已减少"
                )

            s.set_captcha_context(
                roomid=roomid,
                seat_num=first_seat,
                day=submit_day,
                seat_page_id=seat_page_id,
                fid_enc=fid_enc,
            )
            shared_strategy_session = s
            shared_strategy_username = username

            captcha_start_dt = _get_captcha_start_dt(target_dt, _beijing_now())
            warm_dt = target_dt - datetime.timedelta(
                milliseconds=WARM_CONNECTION_LEAD_MS
            )
            first_token_start_dt = _get_first_token_start_dt(target_dt)
            early_warm_url = s.build_token_url(
                roomid,
                warm_day,
                seat_page_id,
                fid_enc,
                first_seat,
            )

            # 某些学校把页面预热配置在验证码之前；按配置时间顺序真正先执行预热。
            if (
                not warm_done
                and WARM_CONNECTION_LEAD_MS > 0
                and (ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK)
                and warm_dt <= captcha_start_dt
            ):
                warm_done = _try_page_prewarm_with_full_window(
                    s,
                    early_warm_url,
                    [
                        ("captcha", captcha_start_dt),
                        ("probe/token", first_token_start_dt),
                    ],
                    not_before=warm_dt,
                )

            captcha_deadline = _get_captcha_preheat_deadline(
                target_dt,
                first_token_start_dt,
                active_strategy_slot_count,
                STRATEGIC_MODE,
            )
            multi_slot_soft_deadline = (
                active_strategy_slot_count > 1 and STRATEGIC_MODE in {"A", "C"}
            )
            multi_slot_retry_when_empty = active_strategy_slot_count > 1
            if multi_slot_soft_deadline:
                logging.info(
                    "[策略] 检测到 %d 个时间段，验证码共享池必须在%s节点前 2 秒完成；"
                    "本次截止时间=%s",
                    active_strategy_slot_count,
                    "预取 token" if STRATEGIC_MODE == "A" else "轻探测",
                    captcha_deadline,
                )

            def _remaining_captcha_seconds() -> float:
                return _remaining_captcha_preheat_seconds(
                    _beijing_now(),
                    captcha_deadline,
                    _get_beijing_end_dt_from_target(target_dt),
                    multi_slot_retry_when_empty,
                    preheated_captcha_results,
                )

            # 2. 按毫秒提前量等待，统一预热滑块、选字、图标或旋转滑块验证码。
            if ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK:
                _wait_until(captcha_start_dt)

            if ENABLE_SLIDER:
                active_captcha_type = "slide"

                def _resolve_image_captcha_parallel(slot_idx: int) -> str:
                    if _remaining_captcha_seconds() <= 0:
                        logging.warning(
                            f"[策略] {active_captcha_type} captcha{slot_idx} 跳过：已达到预热截止时间"
                        )
                        return ""

                    worker = reserve(
                        sleep_time=SLEEPTIME,
                        max_attempt=MAX_ATTEMPT,
                        enable_slider=ENABLE_SLIDER,
                        enable_textclick=ENABLE_TEXTCLICK,
                        enable_iconclick=ENABLE_ICONCLICK,
                        enable_rotate=ENABLE_ROTATE,
                        iconclick_ocr_provider=ICONCLICK_OCR_PROVIDER,
                        reserve_next_day=RESERVE_NEXT_DAY,
                        reserve_day_offset=RESERVE_DAY_OFFSET,
                    )
                    worker.requests.cookies.update(s.requests.cookies)
                    worker.requests.headers.update(s.requests.headers)
                    worker.set_captcha_context(
                        roomid=roomid,
                        seat_num=first_seat,
                        day=submit_day,
                        seat_page_id=seat_page_id,
                        fid_enc=fid_enc,
                    )

                    captcha = worker.resolve_captcha(active_captcha_type)
                    if not captcha:
                        if _remaining_captcha_seconds() <= 0:
                            logging.warning(
                                f"[策略] {active_captcha_type} captcha{slot_idx} 重试跳过：已达到预热截止时间"
                            )
                            return ""
                        logging.warning(
                            f"[策略] {active_captcha_type} captcha{slot_idx} 失败或为空，立即再试一次"
                        )
                        captcha = worker.resolve_captcha(active_captcha_type)
                    return captcha

                remaining = _remaining_captcha_seconds()
                if remaining <= 0:
                    logging.warning(
                        f"[策略] {active_captcha_type} 开始前验证码预热预算已耗尽，跳过预热"
                    )
                else:
                    def _worker(slot_idx: int):
                        try:
                            if active_strategy_slot_count <= 1:
                                preheated_captcha_results[slot_idx] = (
                                    _resolve_image_captcha_parallel(slot_idx) or ""
                                )
                                return
                            while _remaining_captcha_seconds() > 0:
                                captcha = _resolve_image_captcha_parallel(slot_idx) or ""
                                if captcha and _remaining_captcha_seconds() > 0:
                                    preheated_captcha_results[slot_idx] = captcha
                                    return
                                if captcha:
                                    logging.warning(
                                        "[策略] %s captcha%d 在预热收口后返回，丢弃该结果",
                                        active_captcha_type,
                                        slot_idx,
                                    )
                                    return
                        except Exception as e:
                            logging.warning(
                                f"[策略] {active_captcha_type} captcha{slot_idx} 线程失败：{e}"
                            )
                            preheated_captcha_results[slot_idx] = ""

                    deadline_mono = time.monotonic() + remaining

                    def _start_threads(slot_ids):
                        local_threads = []
                        for slot_idx in slot_ids:
                            t = threading.Thread(
                                target=_worker,
                                args=(slot_idx,),
                                name=f"{active_captcha_type}-captcha-{slot_idx}",
                                daemon=True,
                            )
                            local_threads.append((slot_idx, t))
                            t.start()
                        return local_threads

                    def _join_threads_until_deadline(threads_to_join):
                        for _, t in threads_to_join:
                            timeout_left = deadline_mono - time.monotonic()
                            if timeout_left <= 0:
                                break
                            t.join(timeout=timeout_left)

                    if _shared_captcha_preheat_is_serial(active_strategy_slot_count):
                        logging.info(
                            "[策略] 多时间段滑块验证码按 captcha1→captcha2→captcha3 串行预热"
                        )

                        def _serial_worker():
                            while _remaining_captcha_seconds() > 0:
                                for slot_idx in (1, 2, 3):
                                    if _remaining_captcha_seconds() <= 0:
                                        return
                                    if not preheated_captcha_results[slot_idx]:
                                        _worker(slot_idx)
                                if all(preheated_captcha_results.values()):
                                    return

                        serial_thread = threading.Thread(
                            target=_serial_worker,
                            name="slide-captcha-serial-preheat",
                            daemon=True,
                        )
                        serial_thread.start()
                        serial_thread.join(
                            timeout=max(0.0, deadline_mono - time.monotonic())
                        )
                    elif remaining < 3:
                        logging.warning(
                            "[策略] 剩余验证码预热预算小于 3 秒，优先预热 slot1/slot2"
                        )
                        priority_slots = [1, 2]
                        first_two_threads = _start_threads(priority_slots)
                        _join_threads_until_deadline(first_two_threads)

                        ready_count = sum(
                            1
                            for slot_idx in priority_slots
                            if preheated_captcha_results[slot_idx]
                        )
                        if ready_count >= 1:
                            logging.warning(
                                "[策略] 预算小于 3 秒且 captcha1/2 已有结果，跳过 captcha3 预热"
                            )
                        else:
                            timeout_left = deadline_mono - time.monotonic()
                            if timeout_left > 0:
                                logging.warning(
                                    "[策略] 预算小于 3 秒且 captcha1/2 为空，尝试 captcha3 作为兜底"
                                )
                                third_threads = _start_threads([3])
                                _join_threads_until_deadline(third_threads)
                    else:
                        all_threads = _start_threads([1, 2, 3])
                        _join_threads_until_deadline(all_threads)

                captcha1 = live_captcha_results[1]
                captcha2 = live_captcha_results[2]
                captcha3 = live_captcha_results[3]
                logging.info(f"[策略] 已预处理 {active_captcha_type} captcha1：{captcha1}")
                logging.info(f"[策略] 已预处理 {active_captcha_type} captcha2：{captcha2}")
                logging.info(f"[策略] 已预处理 {active_captcha_type} captcha3：{captcha3}")
            elif ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE:
                click_preheat_slots = _click_captcha_preheat_slots(
                    active_strategy_slot_count
                )
                remaining = _remaining_captcha_seconds()
                if remaining <= 0:
                    logging.warning(
                        f"[策略] {click_captcha_name} 开始前已耗尽预热时间，跳过预热"
                    )
                else:
                    def _make_textclick_worker():
                        worker = reserve(
                            sleep_time=SLEEPTIME,
                            max_attempt=MAX_ATTEMPT,
                            enable_slider=ENABLE_SLIDER,
                            enable_textclick=ENABLE_TEXTCLICK,
                            enable_iconclick=ENABLE_ICONCLICK,
                            enable_rotate=ENABLE_ROTATE,
                            iconclick_ocr_provider=ICONCLICK_OCR_PROVIDER,
                            reserve_next_day=RESERVE_NEXT_DAY,
                            reserve_day_offset=RESERVE_DAY_OFFSET,
                        )
                        worker.requests.cookies.update(s.requests.cookies)
                        worker.requests.headers.update(s.requests.headers)
                        worker.set_captcha_context(
                            roomid=roomid,
                            seat_num=first_seat,
                            day=submit_day,
                            seat_page_id=seat_page_id,
                            fid_enc=fid_enc,
                        )
                        return worker

                    def _worker(slot_idx: int):
                        try:
                            captcha = _resolve_textclick_with_retries(
                                _make_textclick_worker(),
                                f"preheat captcha{slot_idx}",
                                max_retries=None,
                                deadline_func=_remaining_captcha_seconds,
                            ) or ""
                            if active_strategy_slot_count <= 1:
                                preheated_captcha_results[slot_idx] = captcha
                                return
                            if captcha and _remaining_captcha_seconds() > 0:
                                preheated_captcha_results[slot_idx] = captcha
                                if _beijing_now() >= captcha_deadline:
                                    shared_click_preheat_threads.clear()
                            elif captcha:
                                logging.warning(
                                    "[策略] %s captcha%d 在预热收口后返回，丢弃该结果",
                                    click_captcha_name,
                                    slot_idx,
                                )
                        except Exception as e:
                            logging.warning(
                                f"[策略] {click_captcha_name} captcha{slot_idx} 预热线程失败：{e}"
                            )
                            preheated_captcha_results[slot_idx] = ""

                    deadline_mono = time.monotonic() + remaining
                    logging.info(
                        "[策略] 开始%s预热 %d 份%s验证码%s",
                        "串行" if _shared_captcha_preheat_is_serial(active_strategy_slot_count) else "",
                        len(click_preheat_slots),
                        click_captcha_name,
                        "，未消费的结果可顺延给后续时间段"
                        if active_strategy_slot_count > 1
                        else "",
                    )
                    if _shared_captcha_preheat_is_serial(active_strategy_slot_count):
                        def _serial_worker():
                            while _remaining_captcha_seconds() > 0:
                                for slot_idx in click_preheat_slots:
                                    if _remaining_captcha_seconds() <= 0:
                                        return
                                    if not preheated_captcha_results[slot_idx]:
                                        _worker(slot_idx)
                                if all(
                                    preheated_captcha_results[slot_idx]
                                    for slot_idx in click_preheat_slots
                                ):
                                    return

                        textclick_preheat_threads = [threading.Thread(
                            target=_serial_worker,
                            name=f"{click_captcha_type}-captcha-serial-preheat",
                            daemon=True,
                        )]
                    else:
                        textclick_preheat_threads = [
                            threading.Thread(
                                target=_worker,
                                args=(slot_idx,),
                                name=f"{click_captcha_type}-captcha-{slot_idx}",
                                daemon=True,
                            )
                            for slot_idx in click_preheat_slots
                        ]
                    shared_click_preheat_threads[:] = textclick_preheat_threads
                    for thread in textclick_preheat_threads:
                        thread.start()
                    for thread in textclick_preheat_threads:
                        timeout_left = deadline_mono - time.monotonic()
                        if timeout_left <= 0:
                            break
                        thread.join(timeout=timeout_left)
                    textclick_preheat_thread = next(
                        (thread for thread in textclick_preheat_threads if thread.is_alive()),
                        None,
                    )

                ready_count = sum(bool(captcha) for captcha in live_captcha_results.values())
                if multi_slot_retry_when_empty:
                    if ready_count:
                        textclick_preheat_thread = None
                        shared_click_preheat_threads.clear()
                        logging.info(
                            "[策略] 多时间段验证码预热到软截止点已完成 %d/3 份；"
                            "已有至少一份，不再等待其余结果",
                            ready_count,
                        )
                    else:
                        logging.warning(
                            "[策略] 多时间段验证码预热到软截止点仍为 0/3；"
                            "后台继续重试，取得第一份后立即停止"
                        )

                captcha1 = live_captcha_results[1]
                captcha2 = live_captcha_results[2]
                captcha3 = live_captcha_results[3]
                logging.info(
                    f"[策略] 当前时间段{click_captcha_name}预热完成 %d/%d 份",
                    sum(bool(captcha) for captcha in (captcha1, captcha2, captcha3)),
                    len(click_preheat_slots),
                )
        else:
            s = shared_strategy_session
            s.requests.headers.update({"Host": "office.chaoxing.com"})
            s.set_captcha_context(
                roomid=roomid,
                seat_num=first_seat,
                day=submit_day,
                seat_page_id=seat_page_id,
                fid_enc=fid_enc,
            )
            logging.info(
                f"[策略] {username} 复用 {shared_strategy_username} 的已预热 session；"
                "跳过登录和验证码预热"
            )
            available_preheated_captchas = _available_preheated_captchas(
                live_captcha_results,
                consumed_preheated_captchas,
            )
            if (
                active_strategy_slot_count > 1
                and (ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK)
                and not available_preheated_captchas
            ):
                logging.warning(
                    "[策略] 多时间段验证码共享池已为 0；当前时间段现场获取一份新验证码"
                )

                def _remaining_onsite_captcha_seconds() -> float:
                    return (
                        _get_beijing_end_dt_from_target(target_dt) - _beijing_now()
                    ).total_seconds()

                if ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE:
                    onsite_captcha = _resolve_textclick_with_retries(
                        s,
                        "shared pool zero onsite",
                        max_retries=None,
                        deadline_func=_remaining_onsite_captcha_seconds,
                    )
                else:
                    onsite_captcha = _resolve_single_captcha_until_success(
                        s,
                        "slide",
                        "shared pool zero onsite",
                        max_retries=None,
                        deadline_func=_remaining_onsite_captcha_seconds,
                    )
                slot_idx = _store_shared_captcha(
                    live_captcha_results,
                    consumed_preheated_captchas,
                    onsite_captcha,
                )
                if slot_idx is not None:
                    logging.info(
                        "[策略] 共享池现场补充成功：captcha%d 已就绪",
                        slot_idx,
                    )
                    available_preheated_captchas = _available_preheated_captchas(
                        live_captcha_results,
                        consumed_preheated_captchas,
                    )
                else:
                    logging.warning(
                        "[策略] 共享池现场补充未成功；当前时间段继续按空验证码保护逻辑执行"
                    )
            captcha1, captcha2, captcha3 = (
                available_preheated_captchas + ["", "", ""]
            )[:3]
            if ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK:
                logging.info(
                    "[策略] 当前时间段领取共享预热池剩余结果：%d/3 份可用",
                    sum(bool(captcha) for captcha in (captcha1, captcha2, captcha3)),
                )

        captcha_required = bool(
            ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK
        )
        captcha_type = (
            "rotate"
            if ENABLE_ROTATE
            else "slide"
            if ENABLE_SLIDER
            else "iconclick"
            if ENABLE_ICONCLICK
            else "textclick"
        )
        raw_captchas = [captcha1, captcha2, captcha3]
        if (
            (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE)
            and captcha1
            and not captcha2
            and not captcha3
        ):
            captchas_for_submit = [captcha1, captcha1, captcha1]
            logging.info(
                f"[策略] 已准备第一个{click_captcha_name}验证码；后续每次真正提交后都会按已消费处理，失败续枪会优先换新验证码"
            )
        else:
            captchas_for_submit = [captcha for captcha in raw_captchas if captcha]
        expected_single_reused_captcha = bool(
            (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE)
            and captchas_for_submit
            and raw_captchas[0]
            and not raw_captchas[1]
            and not raw_captchas[2]
        )
        if captcha_required and captchas_for_submit != raw_captchas and not expected_single_reused_captcha:
            logging.warning(
                "[策略] 为避免空验证码提交，已整理验证码提交顺序："
                f"原始={raw_captchas}，非空数量={len(captchas_for_submit)}"
            )
        elif expected_single_reused_captcha:
            logging.info(
                f"[策略] {click_captcha_name}提交队列先占位使用已准备验证码；"
                "每次真正提交后会视为已消费并按需换新"
            )
        captchas_for_submit = (captchas_for_submit + ["", "", ""])[:3]
        captcha1, captcha2, captcha3 = captchas_for_submit
        if captcha_required:
            logging.info(
                "[策略] 验证码提交队列整理后："
                f"captcha1={captcha1}, captcha2={captcha2}, captcha3={captcha3}"
            )

        def _refresh_submit_captchas_from_live_results():
            if not captcha_required or not live_captcha_results:
                return

            live_captchas = [
                captcha
                for shot_idx in (1, 2, 3)
                if (captcha := live_captcha_results.get(shot_idx, ""))
                and captcha not in unavailable_preheated_captchas
            ]
            merged = []
            seen = set()
            for captcha in captchas_for_submit + live_captchas:
                if captcha and captcha not in seen:
                    merged.append(captcha)
                    seen.add(captcha)

            refreshed = (merged + ["", "", ""])[:3]
            if refreshed != captchas_for_submit:
                captchas_for_submit[:] = refreshed
                logging.info(
                    "[策略] 根据稍晚返回的预热结果刷新验证码提交队列："
                    f"live={live_captchas}, captcha1={captchas_for_submit[0]}, "
                    f"captcha2={captchas_for_submit[1]}, captcha3={captchas_for_submit[2]}"
                )

        def _click_preheat_is_alive() -> bool:
            return any(thread.is_alive() for thread in shared_click_preheat_threads)

        def _wait_for_first_background_captcha(shot_idx: int, list_idx: int) -> bool:
            if not _should_wait_for_background_followup(effective_submit_mode, shot_idx):
                return False
            wait_deadline = _get_beijing_end_dt_from_target(target_dt)
            logging.info(
                "[策略] 第 %d 次提交等待多时间段%s后台取得第一份验证码；"
                "A/C 首次 token 节点已经执行，不会被本次等待推迟",
                shot_idx,
                click_captcha_name,
            )
            for thread in list(shared_click_preheat_threads):
                timeout_left = (wait_deadline - _beijing_now()).total_seconds()
                if timeout_left <= 0:
                    break
                thread.join(timeout=timeout_left)
                _refresh_submit_captchas_from_live_results()
                if any(captchas_for_submit):
                    break
            if not captchas_for_submit[list_idx]:
                reusable = next(
                    (captcha for captcha in captchas_for_submit if captcha),
                    "",
                )
                if reusable:
                    captchas_for_submit[list_idx] = reusable
            return bool(captchas_for_submit[list_idx])

        def _prepare_textclick_captcha_for_submit(
            shot_idx: int,
            reason: str,
            *,
            max_retries: int | None = 3,
        ):
            if not (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE):
                return

            _refresh_submit_captchas_from_live_results()
            list_idx = shot_idx - 1
            if not (0 <= list_idx < len(captchas_for_submit)):
                return

            if (
                _click_preheat_is_alive()
                and not _should_wait_for_click_preheat(
                    active_strategy_slot_count,
                    True,
                )
            ):
                if _wait_for_first_background_captcha(shot_idx, list_idx):
                    return
                logging.info(
                    "[策略] 多时间段%s后台预热仍在重试；第 %d 次提交不等待，"
                    "避免阻塞 A/C token 节点",
                    click_captcha_name,
                    shot_idx,
                )
                return

            if (
                textclick_preheat_thread is not None
                and textclick_preheat_thread.is_alive()
            ):
                wait_s = max(
                    0.0,
                    (
                        _get_beijing_end_dt_from_target(target_dt) - _beijing_now()
                    ).total_seconds(),
                )
                logging.info(
                    "[策略] %s预热仍在进行，最多等待 %.3f 秒；"
                    "优先复用结果，不重复发起请求",
                    click_captcha_name,
                    wait_s,
                )
                textclick_preheat_thread.join(timeout=wait_s)
                _refresh_submit_captchas_from_live_results()
                if captchas_for_submit[list_idx]:
                    logging.info(
                        "[策略] 第 %d 次提交复用稍晚返回的%s预热结果",
                        shot_idx,
                        click_captcha_name,
                    )
                    return
                if _beijing_now() >= _get_beijing_end_dt_from_target(target_dt):
                    logging.warning(
                        "[策略] 已到结束时间，跳过第 %d 次提交的新%s请求",
                        shot_idx,
                        click_captcha_name,
                    )
                    return

            if _beijing_now() >= _get_beijing_end_dt_from_target(target_dt):
                logging.warning(
                    "[策略] 已到结束时间，跳过第 %d 次提交的新%s请求",
                    shot_idx,
                    click_captcha_name,
                )
                return

            logging.info(
                f"[策略] {reason}；立即为第 {shot_idx} 次提交获取新的"
                f"{click_captcha_name}验证码，并替换复用的预热验证码"
            )
            captchas_for_submit[list_idx] = ""
            effective_max_retries = (
                1 if ENABLE_ICONCLICK and max_retries is None else max_retries
            )
            captcha = _resolve_textclick_with_retries(
                s,
                f"submit shot {shot_idx}",
                max_retries=effective_max_retries,
            ) or ""
            if captcha:
                captchas_for_submit[list_idx] = captcha
                if active_strategy_slot_count > 1:
                    _store_shared_captcha(
                        live_captcha_results,
                        consumed_preheated_captchas,
                        captcha,
                    )
            else:
                logging.warning(
                    f"[策略] 为第 {shot_idx} 次提交准备{click_captcha_name}验证码失败"
                )

        def _prepare_slide_captcha_for_submit(shot_idx: int, reason: str):
            if not ENABLE_SLIDER:
                return

            _refresh_submit_captchas_from_live_results()
            list_idx = shot_idx - 1
            if not (0 <= list_idx < len(captchas_for_submit)):
                return

            logging.info(
                f"[策略] {reason}；立即为第 {shot_idx} 次提交获取新的滑块验证码，"
                "并替换已有预热验证码"
            )
            captchas_for_submit[list_idx] = ""
            captcha = s.resolve_captcha("slide") or ""
            if captcha:
                captchas_for_submit[list_idx] = captcha
                if active_strategy_slot_count > 1:
                    _store_shared_captcha(
                        live_captcha_results,
                        consumed_preheated_captchas,
                        captcha,
                    )
            else:
                logging.warning(
                    f"[策略] 为第 {shot_idx} 次提交准备滑块验证码失败"
                )

        def _prepare_fresh_captcha_for_submit(shot_idx: int, reason: str, *, max_retries=None):
            _prepare_textclick_captcha_for_submit(
                shot_idx,
                reason,
                max_retries=max_retries,
            )
            _prepare_slide_captcha_for_submit(shot_idx, reason)

        def _has_distinct_preheated_captcha(shot_idx: int) -> bool:
            list_idx = shot_idx - 1
            if not (0 <= list_idx < len(captchas_for_submit)):
                return False
            captcha = captchas_for_submit[list_idx]
            return bool(captcha) and captcha not in captchas_for_submit[:list_idx]

        def _last_submit_failure_msg() -> str:
            if not isinstance(s.last_submit_result, dict):
                return ""
            return str(s.last_submit_result.get("msg", ""))

        def _get_submit_captcha(shot_idx: int) -> str | None:
            if not captcha_required:
                return ""

            _refresh_submit_captchas_from_live_results()
            list_idx = shot_idx - 1
            captcha = (
                captchas_for_submit[list_idx]
                if 0 <= list_idx < len(captchas_for_submit)
                else ""
            )
            if captcha:
                return captcha

            if (
                (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE)
                and _click_preheat_is_alive()
                and not _should_wait_for_click_preheat(
                    active_strategy_slot_count,
                    True,
                )
            ):
                if _wait_for_first_background_captcha(shot_idx, list_idx):
                    return captchas_for_submit[list_idx]
                logging.warning(
                    "[策略] 多时间段%s后台预热仍在重试；第 %d 次提交跳过等待，"
                    "稍后结果继续进入共享池",
                    click_captcha_name,
                    shot_idx,
                )
                return None

            if (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE) and shot_idx == 1:
                logging.error(
                    f"[策略] 第 1 次提交需要{click_captcha_name}验证码时仍为空；"
                    "跳过本次策略提交，不在取 token 后临时补验证码"
                )
                return None

            logging.warning(
                f"[策略] 第 {shot_idx} 次提交验证码为空，提交前按需获取{click_captcha_name}验证码"
            )
            if ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE:
                captcha = _resolve_textclick_with_retries(
                    s,
                    f"submit shot {shot_idx} fallback",
                    max_retries=3,
                ) or ""
            else:
                captcha = s.resolve_captcha(captcha_type) or ""
            if captcha:
                if 0 <= list_idx < len(captchas_for_submit):
                    captchas_for_submit[list_idx] = captcha
                if active_strategy_slot_count > 1:
                    _store_shared_captcha(
                        live_captcha_results,
                        consumed_preheated_captchas,
                        captcha,
                    )
                logging.info(
                    f"[策略] 第 {shot_idx} 次提交按需获取到{click_captcha_name}验证码：{captcha}"
                )
                return captcha

            logging.error(
                f"[策略] 第 {shot_idx} 次提交按需获取后仍无验证码，跳过提交以避免空验证码"
            )
            return None

        def _ensure_textclick_captcha1_before_strategic_token() -> bool:
            if not (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE):
                return True

            _refresh_submit_captchas_from_live_results()
            if captchas_for_submit[0]:
                return True

            if (
                multi_slot_retry_when_empty
                and _beijing_now() >= captcha_deadline
            ):
                logging.warning(
                    "[策略] 多时间段%s在软截止点仍为 0 份；后台继续重试，"
                    "A/C token 流程按原定节点继续",
                    click_captcha_name,
                )
                return True

            guard_ms = TEXTCLICK_FIRST_CAPTCHA_GUARD_MS
            hard_deadline = target_dt - datetime.timedelta(milliseconds=guard_ms)

            if _beijing_now() < hard_deadline:
                logging.warning(
                    f"[策略] 取 token 前第一个{click_captcha_name}验证码仍为空；"
                    f"继续处理到 {hard_deadline} 后再进入 token 阶段"
                )

                if (
                    (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE)
                    and
                    textclick_preheat_thread is not None
                    and textclick_preheat_thread.is_alive()
                ):
                    wait_s = max(0.0, (hard_deadline - _beijing_now()).total_seconds())
                    logging.info(
                        f"[策略] {click_captcha_name}预热线程仍在运行，"
                        "等待现有验证码请求，不再重复发起"
                    )
                    textclick_preheat_thread.join(timeout=wait_s)
                    _refresh_submit_captchas_from_live_results()
                    if captchas_for_submit[0]:
                        logging.info(
                            f"[策略] 取策略 token 前，已从现有预热线程收到第一个{click_captcha_name}验证码"
                        )
                        return True

                def _remaining_first_captcha_seconds() -> float:
                    return (hard_deadline - _beijing_now()).total_seconds()

                if ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE:
                    captcha = _resolve_textclick_with_retries(
                        s,
                        "captcha1 pre-token guarantee",
                        max_retries=None,
                        deadline_func=_remaining_first_captcha_seconds,
                    ) or ""
                else:
                    captcha = _resolve_single_captcha_until_success(
                        s,
                        captcha_type,
                        "captcha1 pre-token guarantee",
                        max_retries=None,
                        deadline_func=_remaining_first_captcha_seconds,
                    ) or ""
                if captcha:
                    captchas_for_submit[0] = captcha

            _refresh_submit_captchas_from_live_results()
            if captchas_for_submit[0]:
                logging.info(
                    f"[策略] 取策略 token 前，第一个{click_captcha_name}验证码已就绪"
                )
                return True

            logging.error(
                f"[策略] 到硬截止点第一个{click_captcha_name}验证码仍为空；"
                "跳过第一枪策略提交，继续后续第二/第三枪"
            )
            return False

        # 将已登录的 session 存入 sessions[]，fallback 直接复用，无需重新登录
        if sessions is not None and sessions[index] is None:
            sessions[index] = s

        _first_token_url = s.build_token_url(
            roomid,
            first_token_day,
            seat_page_id,
            fid_enc,
            first_seat,
        )
        # 预热仍使用当天页面；响应内容和其中的页面 token 永远丢弃。
        _warm_url = s.build_token_url(
            roomid,
            warm_day,
            seat_page_id,
            fid_enc,
            first_seat,
        )
        _submit_token_url = s.build_token_url(
            roomid,
            submit_day,
            seat_page_id,
            fid_enc,
            first_seat,
        )

        def _maybe_switch_to_backup(handle, token, value, label: str, shot_no: int):
            if (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE) and isinstance(handle, dict):
                event = handle.get("event")
                if event is not None and not event.is_set():
                    event.wait(timeout=0.5)
            conflict = _getusedtimes_conflict_ready(handle)
            if conflict is None:
                logging.info(
                    "[策略] %s 提交前 getusedtimes 尚未就绪，保留主座位 %s/%s",
                    label,
                    roomid,
                    first_seat,
                )
                return roomid, first_seat, seat_page_id, fid_enc, token, value
            if conflict is False:
                logging.info(
                    "[策略] %s 主座位 %s/%s 未冲突，继续使用主座位",
                    label,
                    roomid,
                    first_seat,
                )
                return roomid, first_seat, seat_page_id, fid_enc, token, value

            for backup in backup_slots:
                backup_room = backup["roomid"]
                backup_seat = backup["seatid"]
                backup_key = (backup_room, backup_seat)
                if backup_key in claimed_backup_seats:
                    continue
                backup_page_id = backup.get("seatPageId") or backup_room
                backup_fid = backup.get("fidEnc") or fid_enc
                if ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE:
                    backup_conflict = s.check_getusedtimes_conflict_sync(
                        times,
                        backup_room,
                        backup_seat,
                        submit_day,
                        fid_enc=backup_fid,
                    )
                    if backup_conflict is True:
                        logging.info(
                            "[策略] %s 候补座位 %s/%s 也已冲突，继续尝试下一个候补",
                            label,
                            backup_room,
                            backup_seat,
                        )
                        continue
                claimed_backup_seats.add(backup_key)
                logging.info(
                    "[策略] %s 主座位 %s/%s 已冲突，切换到候补 %s/%s",
                    label,
                    roomid,
                    first_seat,
                    backup_room,
                    backup_seat,
                )
                if (
                    backup_room != roomid
                    or str(backup_page_id or "") != str(seat_page_id or "")
                    or str(backup_seat) != str(first_seat)
                ):
                    backup_token_url = s.build_token_url(
                        backup_room,
                        submit_day,
                        backup_page_id,
                        backup_fid,
                        backup_seat,
                    )
                    backup_token, backup_value = s._get_page_token(
                        backup_token_url,
                        require_value=True,
                    )
                    if backup_token:
                        return backup_room, backup_seat, backup_page_id, backup_fid, backup_token, backup_value
                    logging.warning(
                        "[策略] %s 候补 %s/%s 获取 token 失败，继续尝试下一个候补",
                        label,
                        backup_room,
                        backup_seat,
                    )
                    continue
                return backup_room, backup_seat, backup_page_id, backup_fid, token, value

            fallback_base_room = roomid
            fallback_base_seat = first_seat
            fallback_page_id = seat_page_id
            fallback_fid = fid_enc
            if backup_slots:
                last_backup = backup_slots[-1]
                fallback_base_room = last_backup.get("roomid") or roomid
                fallback_base_seat = last_backup.get("seatid") or first_seat
                fallback_page_id = last_backup.get("seatPageId") or fallback_base_room
                fallback_fid = last_backup.get("fidEnc") or fid_enc

            try:
                base_seat_num = int(str(fallback_base_seat).strip())
            except (TypeError, ValueError):
                base_seat_num = 0
            for attempt_no in range(max(1, shot_no), MAX_SEAT_INCREMENT_ATTEMPTS + 1):
                used_for_config = (
                    fallback_used_seats[index]
                    if index < len(fallback_used_seats)
                    else None
                )
                fallback_seat, offset = _pick_ordered_fallback_seat(
                    base_seat_num,
                    attempt_no,
                    used_for_config,
                )
                fallback_key = (fallback_base_room, fallback_seat or "")
                if not fallback_seat:
                    continue
                if fallback_key in strategic_primary_seats or fallback_key in claimed_backup_seats:
                    logging.info(
                        "[策略] %s 有序兜底座位 %s/%s 已在使用或已被占用，跳过",
                        label,
                        fallback_base_room,
                        fallback_seat,
                    )
                    continue
                if ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE:
                    fallback_conflict = s.check_getusedtimes_conflict_sync(
                        times,
                        fallback_base_room,
                        fallback_seat,
                        submit_day,
                        fid_enc=fallback_fid,
                    )
                    if fallback_conflict is True:
                        logging.info(
                            "[策略] %s 有序兜底座位 %s/%s 因 getusedtimes 冲突而跳过",
                            label,
                            fallback_base_room,
                            fallback_seat,
                        )
                    continue
                claimed_backup_seats.add(fallback_key)
                if used_for_config is not None:
                    used_for_config.add(fallback_seat)
                logging.info(
                    "[策略] %s 主座位 %s/%s 已冲突且%s，使用有序兜底 %s/%s -> %s/%s（%s）",
                    label,
                    roomid,
                    first_seat,
                    "候补座位已用尽" if backup_slots else "候补座位为空",
                    fallback_base_room,
                    fallback_base_seat,
                    fallback_base_room,
                    fallback_seat,
                    offset,
                )
                if (
                    fallback_base_room != roomid
                    or str(fallback_page_id or "") != str(seat_page_id or "")
                    or str(fallback_seat) != str(first_seat)
                ):
                    fallback_token_url = s.build_token_url(
                        fallback_base_room,
                        submit_day,
                        fallback_page_id,
                        fallback_fid,
                        fallback_seat,
                    )
                    fallback_token, fallback_value = s._get_page_token(
                        fallback_token_url,
                        require_value=True,
                    )
                    if fallback_token:
                        return fallback_base_room, fallback_seat, fallback_page_id, fallback_fid, fallback_token, fallback_value
                    logging.warning(
                        "[策略] %s 有序兜底座位 %s/%s 获取 token 失败，保留主座位",
                        label,
                        fallback_base_room,
                        fallback_seat,
                    )
                    continue
                return fallback_base_room, fallback_seat, fallback_page_id, fallback_fid, token, value

            logging.warning(
                "[策略] %s 主座位已冲突，但没有可用候补座位，保留主座位 %s/%s",
                label,
                roomid,
                first_seat,
            )
            return roomid, first_seat, seat_page_id, fid_enc, token, value

        # 连接预热：只有首个配置执行一次，后续配置直接复用已预热的连接池
        if is_primary_strategy_config and not warm_done:
            first_token_start_dt = _get_first_token_start_dt(target_dt)
            captcha_enabled = bool(
                ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK
            )
            allow_early_warm_after_captcha = (
                captcha_enabled and WARM_CONNECTION_LEAD_MS > 4500
            )
            if allow_early_warm_after_captcha:
                # 后置页面预热只会在验证码流程结束后执行；此时仅判断距离首个
                # 轻探测/正式 token 请求是否大于 4400ms。满足时立即预热，
                # 不再等待配置的页面预热时刻。只有配置早于 T-4500ms 才启用。
                warm_done = _try_page_prewarm_with_full_window(
                    s,
                    _warm_url,
                    [("probe/token", first_token_start_dt)],
                    minimum_window_s=4.4,
                )
            if not warm_done:
                # 提前预热未执行、未启用验证码，或配置不早于 T-4500ms 时，
                # 到配置时刻再进行一次标准 4 秒窗口判断。
                warm_dt = target_dt - datetime.timedelta(
                    milliseconds=WARM_CONNECTION_LEAD_MS
                )
                warm_done = _try_page_prewarm_with_full_window(
                    s,
                    _warm_url,
                    [("probe/token", first_token_start_dt)],
                    not_before=warm_dt,
                )

        skip_first_strategic_submit = not _ensure_textclick_captcha1_before_strategic_token()
        background_captcha_zero = bool(
            SUBMIT_MODE == "burst"
            and active_strategy_slot_count > 1
            and (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE)
            and not captchas_for_submit[0]
        )
        use_serial_followups = SUBMIT_MODE == "burst" and (
            skip_first_strategic_submit or background_captcha_zero
        )
        effective_submit_mode = "serial" if use_serial_followups else SUBMIT_MODE
        if use_serial_followups:
            reason = (
                "第一个验证码错过硬截止点"
                if skip_first_strategic_submit
                else "多时间段共享池在软截止点仍为 0 份"
            )
            logging.warning(
                "[策略] burst 模式下%s；跳过连发计划，改用串行续枪",
                reason,
            )

        if SUBMIT_MODE == "burst" and not use_serial_followups:
            # ── 定时连发（极限型）──
            n_shots = len(BURST_OFFSETS_MS)
            for shot_idx in range(2, n_shots + 1):
                if not _has_distinct_preheated_captcha(shot_idx):
                    _prepare_fresh_captcha_for_submit(
                        shot_idx,
                        "burst 连发每次预约 POST 都会消费一个验证码，因此每枪必须使用独立验证码",
                        max_retries=None,
                    )
            captchas_list = (
                captchas_for_submit + [""] * max(0, n_shots - len(captchas_for_submit))
            )[:n_shots]

            first_burst_conflict = s.check_getusedtimes_conflict_sync(
                times,
                roomid,
                first_seat,
                submit_day,
                fid_enc=fid_enc,
            )
            first_burst_handle = {"conflict": first_burst_conflict}
            burst_room, burst_seat, burst_page_id, burst_fid, _, _ = _maybe_switch_to_backup(
                first_burst_handle,
                "",
                "",
                "first burst submit",
                1,
            )
            burst_token_url = s.build_token_url(
                burst_room,
                submit_day,
                burst_page_id or burst_room,
                burst_fid,
                burst_seat,
            )
            token_submit_lock = threading.Lock()
            logging.info(
                "[策略] [burst] 只在第一枪前选择座位冲突结果；"
                "所有枪都在 token-submit 锁内获取全新一次性页面 token/value"
            )

            burst_results = [None] * n_shots
            burst_submitted_captchas = set()
            threads = []
            for burst_i, burst_offset_ms in enumerate(BURST_OFFSETS_MS):
                burst_cap = captchas_list[burst_i] if burst_i < len(captchas_list) else ""
                t = threading.Thread(
                    target=_burst_shot_worker,
                    args=(
                        burst_i, burst_offset_ms, target_dt, s, burst_token_url,
                        times, burst_room, burst_seat, burst_cap, action, burst_results,
                        token_submit_lock, burst_submitted_captchas,
                        use_custom_day, submit_day, burst_fid,
                    ),
                    daemon=True,
                    name=f"burst-shot-{burst_i + 1}",
                )
                threads.append(t)

            logging.info(
                f"[策略] [burst] 启动 {len(threads)} 枪，目标时间偏移为 "
                f"{BURST_OFFSETS_MS} ms"
            )
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            preheated_values = set(preheated_captcha_results.values()) - {""}
            consumed_preheated_captchas.update(
                burst_submitted_captchas & preheated_values
            )
            suc = any(r for r in burst_results if r)
            logging.info(
                f"[策略] [burst] 所有提交完成，结果：{burst_results}，整体是否成功：{suc}"
            )

        else:
            # ── 串行重试（稳健型）──
            # 每枪等到 HTTP 响应后，失败才发下一枪
            serial_submitted_shots = set()
            serial_target_room = roomid
            serial_target_seat = first_seat
            serial_target_page_id = seat_page_id
            serial_target_fid = fid_enc
            serial_target_token_url = _submit_token_url

            def _serial_get_submit(shot_idx: int, **kwargs):
                serial_submitted_shots.add(shot_idx)
                submit_captcha = str(kwargs.get("captcha") or "")
                try:
                    return s.get_submit(**kwargs)
                finally:
                    if submit_captcha in preheated_captcha_results.values():
                        consumed_preheated_captchas.add(submit_captcha)
                    if (
                        ENABLE_ROTATE
                        and submit_captcha
                        and submit_captcha == getattr(s, "_rotate_normal_reusable_captcha", "")
                    ):
                        s._rotate_normal_reusable_captcha = ""
                        logging.info(
                            "[策略] 第 %d 枪已提交此前暂存的验证码，已从普通候补复用池移除",
                            shot_idx,
                        )

            def _remember_serial_target(submit_room, submit_seat, submit_page_id, submit_fid):
                nonlocal serial_target_room, serial_target_seat
                nonlocal serial_target_page_id, serial_target_fid, serial_target_token_url
                serial_target_room = submit_room
                serial_target_seat = submit_seat
                serial_target_page_id = submit_page_id
                serial_target_fid = submit_fid
                serial_target_token_url = s.build_token_url(
                    submit_room,
                    submit_day,
                    submit_page_id or submit_room,
                    submit_fid,
                    submit_seat,
                )

            def _serial_followup_seat_is_free(shot_idx: int) -> bool:
                conflict = s.check_getusedtimes_conflict_sync(
                    times,
                    serial_target_room,
                    serial_target_seat,
                    submit_day,
                    fid_enc=serial_target_fid,
                )
                if conflict is False:
                    logging.info(
                        "[策略] 第 %d 枪提交前查座：当前目标座位 %s/%s 空闲，允许提交",
                        shot_idx,
                        serial_target_room,
                        serial_target_seat,
                    )
                    return True
                if conflict is True:
                    logging.info(
                        "[策略] 第 %d 枪提交前查座：当前目标座位 %s/%s 已冲突，跳过本枪提交，验证码不消费",
                        shot_idx,
                        serial_target_room,
                        serial_target_seat,
                    )
                    return False
                logging.info(
                    "[策略] 第 %d 枪提交前查座：当前目标座位 %s/%s 状态未知，按严格顺序跳过本枪提交，验证码不消费",
                    shot_idx,
                    serial_target_room,
                    serial_target_seat,
                )
                return False

            def _stash_unconsumed_followup_captcha(shot_idx: int, captcha: str, reason: str):
                if not captcha:
                    return
                if active_strategy_slot_count > 1:
                    slot_idx = _store_shared_captcha(
                        live_captcha_results,
                        consumed_preheated_captchas,
                        captcha,
                    )
                    if slot_idx is not None:
                        logging.info(
                            "[策略] 第 %d 枪验证码未提交消费（%s），已保留在共享池 captcha%d",
                            shot_idx,
                            reason,
                            slot_idx,
                        )
                if ENABLE_ROTATE:
                    s._rotate_normal_reusable_captcha = captcha
                    logging.info(
                        "[策略] 第 %d 枪验证码未提交消费（%s），已放入普通候补复用池",
                        shot_idx,
                        reason,
                    )

            if skip_first_strategic_submit:
                logging.warning(
                    "[策略] 第一次提交因第一个验证码错过硬截止点而跳过；"
                    "直接继续第二枪策略提交"
                )
                s.last_submit_result = None
                suc = False
            elif STRATEGIC_MODE == "C":
                # 策略 C：先从 T + FAST_PROBE_START_OFFSET_MS 开始轻探测，
                # 到 T + TOKEN_FETCH_DELAY_MS 后再正式取一次 token 并立即提交
                fetch_dt = target_dt + datetime.timedelta(milliseconds=TOKEN_FETCH_DELAY_MS)
                token1, value1 = _probe_then_get_page_token(
                    s,
                    _first_token_url,
                    target_dt,
                    require_value=True,
                    formal_fetch_not_before=fetch_dt,
                    not_open_retry_until=not_open_retry_until,
                    not_open_retry_interval=0.005,
                    start_log_message=(
                        f"[strategic] [C] 开始探测"
                        f"（从目标时刻 + {FAST_PROBE_START_OFFSET_MS}ms 开始轻探测，"
                        f"不早于目标时刻 + {TOKEN_FETCH_DELAY_MS}ms 正式取 token），"
                        f"目标链接：{_first_token_url}"
                    ),
                )
                if not token1:
                    logging.error("[策略] [C] 获取 token 失败，跳过当前配置")
                    continue
                if SKIP_FIRST_SEAT_QUERY:
                    logging.info(
                        f"[策略] [C] 已从 {_first_token_url} 获取 token：{token1}；"
                        "第一枪跳过 getusedtimes 查座，直接使用主座位"
                    )
                    submit_room = roomid
                    submit_seat = first_seat
                    submit_page_id = seat_page_id
                    submit_fid = fid_enc
                else:
                    logging.info(
                        f"[策略] [C] 已从 {_first_token_url} 获取 token：{token1}；"
                        "第一枪提交前先检查 getusedtimes"
                    )
                    used_handle1 = s.post_getusedtimes_after_token(
                        times,
                        roomid,
                        first_seat,
                        submit_day,
                        fid_enc=fid_enc,
                    )
                    submit_room, submit_seat, submit_page_id, submit_fid, token1, value1 = _maybe_switch_to_backup(
                        used_handle1,
                        token1,
                        value1,
                        "first submit",
                        1,
                    )
                _remember_serial_target(submit_room, submit_seat, submit_page_id, submit_fid)
                submit_captcha1 = _get_submit_captcha(1)
                if submit_captcha1 is None:
                    suc = False
                else:
                    suc = _serial_get_submit(
                        1,
                        url=s.submit_url,
                        times=times,
                        token=token1,
                        roomid=submit_room,
                        seatid=submit_seat,
                        captcha=submit_captcha1,
                        action=action,
                        value=value1,
                        dept_id_enc=submit_fid,
                        use_custom_day=use_custom_day,
                    )

            elif STRATEGIC_MODE == "A":
                # 策略 A：目标时间前 PRE_FETCH_TOKEN_MS 毫秒开始正式取 token；
                #         空 token 视为失败，并持续刷新到本轮 ENDTIME；
                #         目标时间后 FIRST_SUBMIT_OFFSET_MS 毫秒提交。
                pre_fetch_dt = target_dt - datetime.timedelta(milliseconds=PRE_FETCH_TOKEN_MS)
                token_retry_until = target_dt + datetime.timedelta(seconds=40)
                _wait_until(pre_fetch_dt)
                logging.info(
                    f"[策略] [A] 在 {_beijing_now()} 正式预取页面 token "
                    f"（目标时间 - {PRE_FETCH_TOKEN_MS}ms），链接：{_first_token_url}"
                )
                token1, value1 = _get_page_token_until_success(
                    s,
                    _first_token_url,
                    require_value=True,
                    retry_until=token_retry_until,
                    retry_interval=0.005,
                    label="[A] First token",
                )
                if not token1:
                    logging.error("[策略] [A] 第一枪 token 为空，跳过当前配置")
                    continue

                submit_dt1 = target_dt + datetime.timedelta(milliseconds=FIRST_SUBMIT_OFFSET_MS)
                _wait_until(submit_dt1)
                logging.info(
                    f"[策略] [A] 第一枪提交时间 {_beijing_now()}（目标时间 + {FIRST_SUBMIT_OFFSET_MS}ms）"
                )
                if SKIP_FIRST_SEAT_QUERY:
                    logging.info(
                        "[策略] [A] 第一枪跳过 getusedtimes 查座，直接使用主座位"
                    )
                    submit_room = roomid
                    submit_seat = first_seat
                    submit_page_id = seat_page_id
                    submit_fid = fid_enc
                else:
                    used_handle1 = s.post_getusedtimes_after_token(
                        times,
                        roomid,
                        first_seat,
                        submit_day,
                        fid_enc=fid_enc,
                    )
                    submit_room, submit_seat, submit_page_id, submit_fid, token1, value1 = _maybe_switch_to_backup(
                        used_handle1,
                        token1,
                        value1,
                        "first submit",
                        1,
                    )
                _remember_serial_target(submit_room, submit_seat, submit_page_id, submit_fid)
                submit_captcha1 = _get_submit_captcha(1)
                if submit_captcha1 is None:
                    suc = False
                else:
                    suc = _serial_get_submit(
                        1,
                        url=s.submit_url,
                        times=times,
                        token=token1,
                        roomid=submit_room,
                        seatid=submit_seat,
                        captcha=submit_captcha1,
                        action=action,
                        value=value1,
                        dept_id_enc=submit_fid,
                        use_custom_day=use_custom_day,
                    )

            else:
                # 策略 B：目标时间后 FIRST_SUBMIT_OFFSET_MS 毫秒获取 token 并立即提交
                token_fetch_dt1 = target_dt + datetime.timedelta(milliseconds=FIRST_SUBMIT_OFFSET_MS)
                _wait_until(token_fetch_dt1)
                logging.info(
                    f"[策略] [B] 在 {_beijing_now()} 获取页面 token（目标时间 + {FIRST_SUBMIT_OFFSET_MS}ms）"
                )
                token1, value1 = _probe_then_get_page_token(
                    s,
                    _first_token_url,
                    target_dt,
                    require_value=True,
                    not_open_retry_until=not_open_retry_until,
                    not_open_retry_interval=0.005,
                )
                if not token1:
                    logging.error("[策略] 第一枪获取页面 token 失败，跳过当前配置")
                    continue
                logging.info(
                    f"[策略] 第一枪已从 {_first_token_url} 获取页面 token：{token1}，value：{value1}"
                )
                used_handle1 = s.post_getusedtimes_after_token(
                    times,
                    roomid,
                    first_seat,
                    submit_day,
                    fid_enc=fid_enc,
                )
                logging.info("[策略] [B] 获取页面 token 后立即提交")
                submit_room, submit_seat, submit_page_id, submit_fid, token1, value1 = _maybe_switch_to_backup(
                    used_handle1,
                    token1,
                    value1,
                    "first submit",
                    1,
                )
                _remember_serial_target(submit_room, submit_seat, submit_page_id, submit_fid)
                submit_captcha1 = _get_submit_captcha(1)
                if submit_captcha1 is None:
                    suc = False
                else:
                    suc = _serial_get_submit(
                        1,
                        url=s.submit_url,
                        times=times,
                        token=token1,
                        roomid=submit_room,
                        seatid=submit_seat,
                        captcha=submit_captcha1,
                        action=action,
                        value=value1,
                        dept_id_enc=submit_fid,
                        use_custom_day=use_custom_day,
                    )

            # 如果第一次没有成功：重新获取页面 token，获取后立即提交第二枪
            if not suc:
                if not skip_first_strategic_submit and s.should_skip_followup_submit():
                    logging.info(
                        "[策略] 第一枪命中终止型失败信息，跳过第二/第三枪"
                    )
                    success_list[index] = suc
                    continue
                logging.info("[策略] 第一枪未成功，准备第二枪：先准备验证码，再查座，空闲后取新页面 token 提交")
                first_failure_msg = _last_submit_failure_msg()
                first_submit_sent = 1 in serial_submitted_shots
                logging.info(
                    "[策略] 第一枪失败原因：%s；是否已发送提交=%s",
                    (
                        "第一个验证码错过硬截止点"
                        if skip_first_strategic_submit
                        else (first_failure_msg or "<空>")
                    ),
                    first_submit_sent,
                )
                if (
                    (first_submit_sent or skip_first_strategic_submit)
                    and not _has_distinct_preheated_captcha(2)
                ):
                    _prepare_fresh_captcha_for_submit(
                        2,
                        (
                            "第一枪已发送预约 POST，因此验证码按已消费处理"
                            if first_submit_sent
                            else "第一枪因第一个验证码错过硬截止点被跳过"
                        ),
                        max_retries=3 if skip_first_strategic_submit else None,
                    )

                submit_captcha2 = _get_submit_captcha(2)
                if submit_captcha2 is None:
                    suc = False
                elif not _serial_followup_seat_is_free(2):
                    _stash_unconsumed_followup_captcha(2, submit_captcha2, "查座未确认空闲")
                    suc = False
                else:
                    logging.info(
                        "[策略] 第二枪查座确认空闲，开始获取新的页面 token 并立即提交"
                    )
                    if STRATEGIC_MODE == "A":
                        token2, value2 = _get_page_token_until_success(
                            s,
                            serial_target_token_url,
                            require_value=True,
                            retry_until=target_dt + datetime.timedelta(seconds=40),
                            retry_interval=0.005,
                            label="[A] Second token",
                        )
                    else:
                        token2, value2 = s._get_page_token(
                            serial_target_token_url,
                            require_value=True,
                        )
                    if not token2:
                        logging.error("[策略] 第二枪获取页面 token 失败，验证码未消费，跳到第三枪/普通流程")
                        _stash_unconsumed_followup_captcha(2, submit_captcha2, "获取页面 token 失败")
                        suc = False
                    else:
                        suc = _serial_get_submit(
                            2,
                            url=s.submit_url,
                            times=times,
                            token=token2,
                            roomid=serial_target_room,
                            seatid=serial_target_seat,
                            captcha=submit_captcha2,
                            action=action,
                            value=value2,
                            dept_id_enc=serial_target_fid,
                            use_custom_day=use_custom_day,
                        )

            # 如果第二次仍未成功：重新获取页面 token，获取后立即提交第三枪
            if not suc:
                if s.should_skip_followup_submit():
                    logging.info(
                        "[策略] 第二枪命中终止型失败信息，跳过第三枪"
                    )
                    success_list[index] = suc
                    continue
                logging.info("[策略] 第二枪未成功，准备第三枪：先准备验证码，再查座，空闲后取新页面 token 提交")
                second_failure_msg = _last_submit_failure_msg()
                second_submit_sent = 2 in serial_submitted_shots
                reusable_second_captcha = _reuse_unsubmitted_captcha(
                    second_submit_sent,
                    submit_captcha2,
                )
                logging.info(
                    "[策略] 第二枪失败原因：%s；是否已发送提交=%s",
                    second_failure_msg or "<空>",
                    second_submit_sent,
                )
                if second_submit_sent and not _has_distinct_preheated_captcha(3):
                    _prepare_fresh_captcha_for_submit(
                        3,
                        "第二枪已发送预约 POST，因此验证码按已消费处理",
                        max_retries=None,
                    )
                elif reusable_second_captcha:
                    logging.info(
                        "[策略] 第二枪没有发送预约 POST；第三枪复用第二枪未消费的新验证码"
                    )

                submit_captcha3 = (
                    reusable_second_captcha
                    if reusable_second_captcha
                    else _get_submit_captcha(3)
                )
                if submit_captcha3 is None:
                    suc = False
                elif not _serial_followup_seat_is_free(3):
                    _stash_unconsumed_followup_captcha(3, submit_captcha3, "查座未确认空闲")
                    suc = False
                else:
                    logging.info(
                        "[策略] 第三枪查座确认空闲，开始获取新的页面 token 并立即提交"
                    )
                    token3, value3 = s._get_page_token(
                        serial_target_token_url,
                        require_value=True,
                    )
                    if not token3:
                        logging.error("[策略] 第三枪获取页面 token 失败，验证码未消费，放入普通候补复用池")
                        _stash_unconsumed_followup_captcha(3, submit_captcha3, "获取页面 token 失败")
                        suc = False
                    else:
                        suc = _serial_get_submit(
                            3,
                            url=s.submit_url,
                            times=times,
                            token=token3,
                            roomid=serial_target_room,
                            seatid=serial_target_seat,
                            captcha=submit_captcha3,
                            action=action,
                            value=value3,
                            dept_id_enc=serial_target_fid,
                            use_custom_day=use_custom_day,
                        )

        success_list[index] = suc

    return success_list


def login_and_reserve(
    users, usernames, passwords, action, success_list=None, sessions=None
):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nENABLE_TEXTCLICK: {ENABLE_TEXTCLICK}\nENABLE_ICONCLICK: {ENABLE_ICONCLICK}\nENABLE_ROTATE: {ENABLE_ROTATE}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )

    usernames_list, passwords_list = None, None
    if action:
        if not usernames or not passwords:
            raise Exception("USERNAMES or PASSWORDS not configured correctly in env")
        usernames_list, passwords_list = _split_action_credentials(
            usernames, passwords
        )
        if len(usernames_list) != len(passwords_list):
            raise Exception("USERNAMES and PASSWORDS count mismatch")

    if success_list is None:
        success_list = [False] * len(users)

    # 如果传入了 sessions，但长度和 users 不匹配，则忽略 sessions，退回每轮重登
    if sessions is not None and len(sessions) != len(users):
        logging.error("sessions length mismatch with users, ignore sessions and relogin each loop.")
        sessions = None

    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username = user["username"]
        password = user["password"]
        times = user["times"]
        roomid = user["roomid"]
        seatid = user["seatid"]
        seat_page_id = user.get("seatPageId")
        fid_enc = user.get("fidEnc")
        backup_slots = _normalize_backup_slots(user.get("backupSeats") or user.get("backupSlots"))
        use_custom_day = bool(user.get("use_custom_day"))
        daysofweek = user["daysofweek"]

        # 如果今天不在该配置的 daysofweek 中，直接跳过
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue

        if action:
            if len(usernames_list) == 1:
                # 只有一个账号，所有配置都用这个账号
                username = usernames_list[0]
                password = passwords_list[0]
            elif index < len(usernames_list):
                username = usernames_list[index]
                password = passwords_list[index]
            else:
                logging.error(
                    "USERNAMES/PASSWORDS 索引越界，跳过当前配置"
                )
                continue

        if not success_list[index]:
            logging.info(
                f"----------- {username} -- {times} -- {seatid} try -----------"
            )

            # 根据 RELOGIN_EVERY_LOOP 决定是否复用会话
            s = None
            if sessions is not None:
                s = sessions[index]
                if s is None:
                    # 该账号第一次使用：创建会话并登录
                    s = reserve(
                        sleep_time=SLEEPTIME,
                        max_attempt=MAX_ATTEMPT,
                        enable_slider=ENABLE_SLIDER,
                        enable_textclick=ENABLE_TEXTCLICK,
                        enable_iconclick=ENABLE_ICONCLICK,
                        enable_rotate=ENABLE_ROTATE,
                        iconclick_ocr_provider=ICONCLICK_OCR_PROVIDER,
                        reserve_next_day=RESERVE_NEXT_DAY,
                        reserve_day_offset=RESERVE_DAY_OFFSET,
                    )
                    if not s.bootstrap_login(username, password):
                        logging.warning(
                            f"跳过 {username} 本轮尝试：登录预启动失败"
                        )
                        continue
                    sessions[index] = s
                else:
                    # 复用已有会话，确保 Host 头正确
                    s.requests.headers.update({"Host": "office.chaoxing.com"})
            else:
                # 维持原有行为：每一轮循环都重新创建会话并登录
                s = reserve(
                    sleep_time=SLEEPTIME,
                    max_attempt=MAX_ATTEMPT,
                    enable_slider=ENABLE_SLIDER,
                    enable_textclick=ENABLE_TEXTCLICK,
                    enable_iconclick=ENABLE_ICONCLICK,
                    enable_rotate=ENABLE_ROTATE,
                    iconclick_ocr_provider=ICONCLICK_OCR_PROVIDER,
                    reserve_next_day=RESERVE_NEXT_DAY,
                    reserve_day_offset=RESERVE_DAY_OFFSET,
                )
                if not s.bootstrap_login(username, password):
                    logging.warning(
                        f"跳过 {username} 本轮尝试：登录预启动失败"
                    )
                    continue

            # 在 GitHub Actions 中传入 ENDTIME，确保内部循环在超过结束时间后及时停止
            suc = s.submit(
                times,
                roomid,
                seatid,
                action,
                ENDTIME if action else None,
                fidEnc=fid_enc,
                seat_page_id=seat_page_id,
                use_custom_day=use_custom_day,
                backup_slots=backup_slots,
            )
            success_list[index] = suc
    return success_list


def main(users, action=False):
    global MAX_ATTEMPT
    target_dt = _get_beijing_target_from_endtime()
    end_dt = _get_beijing_end_dt_from_target(target_dt)
    logging.info(
        f"start time {get_log_time(action)}, action {'on' if action else 'off'}, target_dt {target_dt}, end_dt {end_dt}"
    )
    attempt_times = 0
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    success_list = None

    # 根据 RELOGIN_EVERY_LOOP 决定是否为每个用户维护持久会话
    sessions = None
    if not RELOGIN_EVERY_LOOP:
        sessions = [None] * len(users)

    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        1 for d in users if current_dayofweek in d.get("daysofweek")
    )

    # 本地与 GitHub Actions 都执行一次“有策略”的第一次尝试，
    # 这样两边都走同一套前三抢/预热/补位逻辑。
    strategic_done = False

    # 保存每个配置的初始座位号（优先取 seatid 第一个），用于预热失败后按 +1 递增
    original_seatids = []
    for user in users:
        sid = user.get("seatid")
        raw_sid = (
            sid
            if isinstance(sid, str)
            else (sid[0] if isinstance(sid, list) and sid else None)
        )
        try:
            original_seatids.append(int(raw_sid) if raw_sid is not None else None)
        except (TypeError, ValueError):
            logging.warning(
                f"[seat-increment] Invalid seatid {raw_sid}, skip auto-increment for this config"
            )
            original_seatids.append(None)
    seat_increment_attempts = 0
    fallback_attempt_limit = MAX_SEAT_INCREMENT_ATTEMPTS
    fallback_used_seats = [set() for _ in users]

    while True:
        current_dt = _beijing_now()
        if current_dt >= end_dt:
            logging.info(
                f"Current time {current_dt.strftime('%Y-%m-%d %H:%M:%S')} >= end_dt {end_dt.strftime('%Y-%m-%d %H:%M:%S')} (ENDTIME {ENDTIME}), stop main loop"
            )
            return

        attempt_times += 1

        if not strategic_done:
            success_list = strategic_first_attempt(
                users,
                usernames,
                passwords,
                action,
                target_dt,
                success_list,
                sessions,
                fallback_used_seats,
            )
            strategic_done = True

            # 预热三次结束后，如果仍有配置未成功，按固定顺序补位并立即继续尝试
            if success_list is not None and sum(success_list) < today_reservation_num:
                seat_increment_attempts = 1
                for i, user in enumerate(users):
                    if not success_list[i] and original_seatids[i] is not None \
                            and current_dayofweek in user.get("daysofweek", []):
                        new_seat, offset, _ = _pick_next_ordered_fallback_seat(
                            original_seatids[i],
                            seat_increment_attempts,
                            fallback_used_seats[i],
                        )
                        if not new_seat:
                            logging.info(
                            f"[seat-ordered-after-strategic] Config {i}: skip invalid/used fallback "
                            f"(base {original_seatids[i]}, offset {offset or 'none'}, "
                            f"attempt {seat_increment_attempts}/{fallback_attempt_limit})"
                            )
                            continue
                        fallback_used_seats[i].add(new_seat)
                        user["seatid"] = [new_seat]
                        logging.info(
                            f"[seat-ordered-after-strategic] Config {i}: try seat {new_seat} "
                            f"(base {original_seatids[i]}, offset {offset}, "
                            f"attempt {seat_increment_attempts}/{fallback_attempt_limit})"
                        )
                # 递增座位后立即调用 login_and_reserve（每个座位只试一次）
                MAX_ATTEMPT = 1
                if sessions is not None:
                    for s_obj in sessions:
                        if s_obj is not None:
                            s_obj.max_attempt = 1
                success_list = login_and_reserve(
                    users, usernames, passwords, action, success_list, sessions
                )
        else:
            # 预热结束后仍未成功：未成功配置继续按固定顺序补位尝试
            if success_list is not None and sum(success_list) < today_reservation_num:
                if seat_increment_attempts >= fallback_attempt_limit:
                    logging.info(
                        f"[seat-ordered] Reached max fallback attempts "
                        f"{fallback_attempt_limit}, stop fallback seat changes"
                    )
                    print(
                        f"ordered fallback stopped after {seat_increment_attempts} attempts, "
                        f"success list {success_list}"
                    )
                    return
                seat_increment_attempts += 1
                for i, user in enumerate(users):
                    if not success_list[i] and original_seatids[i] is not None \
                            and current_dayofweek in user.get("daysofweek", []):
                        new_seat, offset, _ = _pick_next_ordered_fallback_seat(
                            original_seatids[i],
                            seat_increment_attempts,
                            fallback_used_seats[i],
                        )
                        if not new_seat:
                            logging.info(
                            f"[seat-ordered] Config {i}: skip invalid/used fallback "
                            f"(base {original_seatids[i]}, offset {offset or 'none'}, "
                            f"attempt {seat_increment_attempts}/{fallback_attempt_limit})"
                            )
                            continue
                        fallback_used_seats[i].add(new_seat)
                        user["seatid"] = [new_seat]
                        logging.info(
                            f"[seat-ordered] Config {i}: try seat {new_seat} "
                            f"(base {original_seatids[i]}, offset {offset}, "
                            f"attempt {seat_increment_attempts}/{fallback_attempt_limit})"
                        )

                # 固定顺序补位模式下每个座位只提交一次，失败就下一轮切换到下一个偏移
                MAX_ATTEMPT = 1
                if sessions is not None:
                    for s_obj in sessions:
                        if s_obj is not None:
                            s_obj.max_attempt = 1
            success_list = login_and_reserve(
                users, usernames, passwords, action, success_list, sessions
            )

        print(
            f"attempt time {attempt_times}, time now {current_dt}, success list {success_list}"
        )
        if sum(success_list) == today_reservation_num:
            print(f"reserved successfully!")
            return


def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nENABLE_TEXTCLICK: {ENABLE_TEXTCLICK}\nENABLE_ICONCLICK: {ENABLE_ICONCLICK}\nENABLE_ROTATE: {ENABLE_ROTATE}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    suc = False
    logging.info(f" Debug Mode start! , action {'on' if action else 'off'}")

    usernames_list, passwords_list = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
        if not usernames or not passwords:
            logging.error("USERNAMES or PASSWORDS not configured correctly in env.")
            return
        usernames_list, passwords_list = _split_action_credentials(
            usernames, passwords
        )
        if len(usernames_list) != len(passwords_list):
            logging.error("USERNAMES and PASSWORDS count mismatch.")
            return

    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username = user["username"]
        password = user["password"]
        times = user["times"]
        roomid = user["roomid"]
        seatid = user["seatid"]
        seat_page_id = user.get("seatPageId")
        fid_enc = user.get("fidEnc")
        backup_slots = _normalize_backup_slots(user.get("backupSeats") or user.get("backupSlots"))
        use_custom_day = bool(user.get("use_custom_day"))
        daysofweek = user["daysofweek"]
        if type(seatid) == str:
            seatid = [seatid]

        # 如果今天不在该配置的 daysofweek 中，直接跳过，不处理账号
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue

        # 在 GitHub Actions 中，从环境变量获取账号密码
        if action:
            if len(usernames_list) == 1:
                # 只有一个账号时，所有配置都用这个账号
                username = usernames_list[0]
                password = passwords_list[0]
            elif index < len(usernames_list):
                username = usernames_list[index]
                password = passwords_list[index]
            else:
                logging.error(
                    "Index out of range for USERNAMES/PASSWORDS, skipping this config."
                )
                continue

        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            enable_textclick=ENABLE_TEXTCLICK,
            enable_iconclick=ENABLE_ICONCLICK,
            enable_rotate=ENABLE_ROTATE,
            iconclick_ocr_provider=ICONCLICK_OCR_PROVIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
            reserve_day_offset=RESERVE_DAY_OFFSET,
        )
        if not s.bootstrap_login(username, password):
            logging.warning(f"Skip debug reserve attempt for {username}: login bootstrap failed")
            continue
        suc = s.submit(
            times,
            roomid,
            seatid,
            action,
            None,
            fidEnc=fid_enc,
            seat_page_id=seat_page_id,
            use_custom_day=use_custom_day,
            backup_slots=backup_slots,
        )
        if suc:
            return


def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        enable_textclick=ENABLE_TEXTCLICK,
        enable_iconclick=ENABLE_ICONCLICK,
        enable_rotate=ENABLE_ROTATE,
        iconclick_ocr_provider=ICONCLICK_OCR_PROVIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
        reserve_day_offset=RESERVE_DAY_OFFSET,
    )
    if not s.bootstrap_login(username=username, password=password):
        logging.error("Failed to bootstrap login session, abort room query")
        return
    encode = input("请输入deptldEnc：")
    s.roomid(encode)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="load single-user config from DISPATCH_PAYLOAD",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    config = _load_runtime_config(args.user, args.dispatch, args.action)
    usersdata = config["reserve"]

    # 从配置读取策略参数。
    # ┌─────────────────────────────────────────────────────────────────────┐
    # │  mode (STRATEGIC_MODE) × submit_mode (SUBMIT_MODE) 四种组合         │
    # ├──────────┬────────────┬──────────────────────────────────────────────┤
    # │ mode=A   │ serial     │ T-pre_fetch_token_ms 预取token1              │
    # │          │            │ → T+first_submit_offset_ms POST，等结果       │
    # │          │            │ → 失败则现取token2并立即POST，等结果           │
    # │          │            │ → 失败则现取token3并立即POST                  │
    # ├──────────┼────────────┼──────────────────────────────────────────────┤
    # │ mode=A   │ burst ★   │ T-pre_fetch_token_ms 预取token1/2/3           │
    # │          │            │ → T+burst[0] thread-1 直接POST（零GET延迟）   │
    # │          │            │ → T+burst[1] thread-2 直接POST（零GET延迟）   │
    # │          │            │ → T+burst[2] thread-3 直接POST（零GET延迟）   │
    # ├──────────┼────────────┼──────────────────────────────────────────────┤
    # │ mode=B   │ serial     │ T+first_submit_offset_ms 取token1并POST，等结果│
    # │ (默认)   │ (默认)     │ → 失败则现取token2并立即POST，等结果           │
    # │          │            │ → 失败则现取token3并立即POST                  │
    # ├──────────┼────────────┼──────────────────────────────────────────────┤
    # │ mode=B   │ burst      │ T+burst[0] thread-1 自取token并POST           │
    # │          │            │ T+burst[1] thread-2 自取token并POST           │
    # │          │            │ T+burst[2] thread-3 自取token并POST           │
    # │          │            │ 注意：实际POST = burst[i] + GET网络延迟        │
    # └──────────┴────────────┴──────────────────────────────────────────────┘
    _apply_strategy_config(config)

    try:
        func_dict[args.method](usersdata, args.action)
    except CredentialRejectedError as e:
        logging.error(str(e))
        raise SystemExit(1) from None
