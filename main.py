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
        logging.warning(
            "[strategic] Captcha preheat lead %dms exceeds login lead %ds; "
            "actual captcha preheat cannot start before login completes",
            STRATEGY_SLIDER_LEAD_MS,
            STRATEGY_LOGIN_LEAD_SECONDS,
        )
    STRATEGIC_MODE = strategy_cfg.get("mode", "B")
    PRE_FETCH_TOKEN_MS = int(strategy_cfg.get("pre_fetch_token_ms", 3000))
    FIRST_SUBMIT_OFFSET_MS = int(strategy_cfg.get("first_submit_offset_ms", 89))
    SUBMIT_MODE = strategy_cfg.get("submit_mode", "serial")
    BURST_OFFSETS_MS = strategy_cfg.get("burst_offsets_ms", [120, 420, 820])
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
        f"[strategic] {label}: start formal token fetch from {token_url}"
        + (f", retry until {retry_until}" if retry_until else "")
    )
    token, value = s._get_page_token(
        token_url,
        require_value=require_value,
        not_open_retry_until=retry_until,
        not_open_retry_interval=retry_interval,
    )
    if token:
        logging.info(f"[strategic] {label}: got token from {token_url}: {token}")
    else:
        logging.error(f"[strategic] {label}: token is empty after formal fetch")
    return token, value


def _burst_shot_worker(
    index, offset_ms, target_dt, s, token_url,
    times, roomid, seatid, captcha, action, results,
    token_submit_lock, use_custom_day=False, day="", fid_enc=""
):
    """定时连发（极限型）的单次提交工作线程。

    在 target_dt + offset_ms 时刻提交预约，结果写入 results[index]。
    页面 submit_enc/value 只能使用一次，且刷新页面会使之前获取的值失效。
    因此每枪都必须在锁内获取新 token/value 并立即 POST，禁止并发刷新或复用。
    """
    fire_dt = target_dt + datetime.timedelta(milliseconds=offset_ms)
    _wait_until(fire_dt)

    logging.info(
        f"[burst] Shot {index + 1} firing at {_beijing_now()} (target_dt + {offset_ms}ms)"
    )

    if (ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK) and not captcha:
        logging.error(
            f"[burst] Shot {index + 1} has empty captcha, skip submit to avoid empty captcha"
        )
        results[index] = False
        return

    with token_submit_lock:
        token, value = s._get_page_token(
            token_url,
            require_value=True,
        )
        if not token:
            logging.error(f"[burst] Shot {index + 1} failed to get page token")
            results[index] = False
            return
        logging.info(
            f"[burst] Shot {index + 1} fetched token on-the-fly from {token_url}: {token}"
        )
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
        usernames_list = usernames.split(",")
        passwords_list = passwords.split(",")
        if len(usernames_list) != len(passwords_list):
            raise Exception("USERNAMES and PASSWORDS count mismatch")

    current_dayofweek = get_current_dayofweek(action)
    warm_done = False
    shared_strategy_session = None
    shared_strategy_username = None
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
            logging.info("[strategic] Today not set to reserve, skip this config")
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
                    "[strategic] Index out of range for USERNAMES/PASSWORDS, skipping this config."
                )
                continue

        # seatid 可能是字符串或列表，只在策略阶段针对第一个座位做一次精准尝试
        seat_list = [seatid] if isinstance(seatid, str) else seatid
        if not seat_list:
            logging.error("[strategic] Empty seat list, skip this config")
            continue

        logging.info(
            f"[strategic] Start first attempt for {username} -- {times} -- {seat_list} "
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
        live_captcha_results = None
        textclick_preheat_thread = None
        click_captcha_type = "iconclick" if ENABLE_ICONCLICK else "textclick"
        click_captcha_name = "图标点选" if ENABLE_ICONCLICK else "文字点选"

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
                        f"[策略] {click_captcha_name} {label} 已停止：达到预热截止时间"
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
                        f"[策略] {click_captcha_name} {label} 第 {attempt} 次请求成功"
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
                        f"[strategic] {log_name} {label} stopped before success: preheat deadline reached"
                    )
                    return ""
                try:
                    captcha = captcha_session.resolve_captcha(captcha_type_name) or ""
                except Exception as e:
                    logging.debug(
                        f"[strategic] {log_name} {label} attempt {attempt} raised: {e}"
                    )
                    captcha = ""
                if captcha:
                    logging.info(
                        f"[strategic] {log_name} {label} resolved on attempt {attempt}"
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
                f"[strategic] {log_name} {label} failed after {max_retries} attempts"
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
                    f"[strategic] Login bootstrap failed for {username}, "
                    f"retry within strategic window for {remaining_login_s:.2f}s more"
                )
                time.sleep(min(0.2, remaining_login_s))

            if not login_ok:
                logging.warning(
                    f"[strategic] Skip first attempt for {username}: login bootstrap failed "
                    f"until strategic deadline {login_deadline}"
                )
                continue

            if _beijing_now() >= target_dt:
                logging.warning(
                    f"[strategic] Login for {username} recovered after target time; "
                    "continue strategic submits with reduced preheat budget"
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

            captcha_start_dt = target_dt - datetime.timedelta(
                milliseconds=STRATEGY_SLIDER_LEAD_MS
            )
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

            # 验证码预热整体预算：最多占用 [T-slider_lead_seconds_range(ms), T] 这段时间。
            # 验证码不为页面预热提前让出时间；页面预热窗口不足时由页面预热自行跳过。
            captcha_deadline = target_dt

            def _remaining_captcha_seconds() -> float:
                return (captcha_deadline - _beijing_now()).total_seconds()

            # 2. 按毫秒提前量等待，统一预热滑块、选字、图标或旋转滑块验证码。
            if ENABLE_ROTATE or ENABLE_SLIDER or ENABLE_TEXTCLICK or ENABLE_ICONCLICK:
                _wait_until(captcha_start_dt)

            if ENABLE_ROTATE:
                captcha_results = {1: "", 2: "", 3: ""}
                live_captcha_results = captcha_results
                remaining = _remaining_captcha_seconds()
                if remaining <= 0:
                    logging.warning("[strategic] Captcha preheat budget exhausted before rotate starts, skip preheat")
                else:
                    def _make_rotate_worker():
                        worker = reserve(
                            sleep_time=SLEEPTIME,
                            max_attempt=MAX_ATTEMPT,
                            enable_slider=ENABLE_SLIDER,
                            enable_textclick=ENABLE_TEXTCLICK,
                            enable_iconclick=ENABLE_ICONCLICK,
                            enable_rotate=ENABLE_ROTATE,
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

                    logging.info(
                        "[strategic] Preheat one rotate captcha until it has validate "
                        "or reaches the captcha deadline"
                    )
                    captcha_results[1] = _resolve_single_captcha_until_success(
                        _make_rotate_worker(),
                        "rotate",
                        "preheat captcha1",
                        max_retries=None,
                        deadline_func=_remaining_captcha_seconds,
                    ) or ""

                captcha1 = captcha_results[1]
                captcha2 = ""
                captcha3 = ""
                logging.info(
                    "[strategic] Pre-resolved rotate captcha ready: %s",
                    bool(captcha1),
                )
            elif ENABLE_SLIDER:
                active_captcha_type = "slide"

                def _resolve_image_captcha_parallel(slot_idx: int) -> str:
                    if _remaining_captcha_seconds() <= 0:
                        logging.warning(
                            f"[strategic] {active_captcha_type} captcha{slot_idx} skipped: preheat deadline reached"
                        )
                        return ""

                    worker = reserve(
                        sleep_time=SLEEPTIME,
                        max_attempt=MAX_ATTEMPT,
                        enable_slider=ENABLE_SLIDER,
                        enable_textclick=ENABLE_TEXTCLICK,
                        enable_iconclick=ENABLE_ICONCLICK,
                        enable_rotate=ENABLE_ROTATE,
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
                                f"[strategic] {active_captcha_type} captcha{slot_idx} retry skipped: preheat deadline reached"
                            )
                            return ""
                        logging.warning(
                            f"[strategic] {active_captcha_type} captcha{slot_idx} failed or empty, retrying once more"
                        )
                        captcha = worker.resolve_captcha(active_captcha_type)
                    return captcha

                captcha_results = {1: "", 2: "", 3: ""}
                live_captcha_results = captcha_results
                remaining = _remaining_captcha_seconds()
                if remaining <= 0:
                    logging.warning(
                        f"[strategic] Captcha preheat budget exhausted before {active_captcha_type} starts, skip preheat"
                    )
                else:
                    def _worker(slot_idx: int):
                        try:
                            captcha_results[slot_idx] = _resolve_image_captcha_parallel(slot_idx) or ""
                        except Exception as e:
                            logging.warning(f"[strategic] {active_captcha_type} captcha{slot_idx} thread failed: {e}")
                            captcha_results[slot_idx] = ""

                    deadline_mono = time.monotonic() + remaining

                    def _start_threads(slot_ids: list[int]):
                        local_threads = []
                        for idx in slot_ids:
                            t = threading.Thread(
                                target=_worker,
                                args=(idx,),
                                name=f"{active_captcha_type}-captcha-{idx}",
                                daemon=True,
                            )
                            local_threads.append((idx, t))
                            t.start()
                        return local_threads

                    def _join_threads_until_deadline(threads_to_join):
                        for _, t in threads_to_join:
                            timeout_left = deadline_mono - time.monotonic()
                            if timeout_left <= 0:
                                break
                            t.join(timeout=timeout_left)

                    if remaining < 3:
                        logging.warning(
                            "[strategic] Remaining captcha preheat budget < 3s, preheat slot1/slot2 first"
                        )
                        first_two_threads = _start_threads([1, 2])
                        _join_threads_until_deadline(first_two_threads)

                        ready_count = sum(1 for i in [1, 2] if captcha_results[i])
                        if ready_count >= 1:
                            logging.warning(
                                "[strategic] Budget < 3s and captcha1/2 already ready, skip captcha3 preheat"
                            )
                        else:
                            timeout_left = deadline_mono - time.monotonic()
                            if timeout_left > 0:
                                logging.warning(
                                    "[strategic] Budget < 3s and captcha1/2 empty, try captcha3 as fallback"
                                )
                                third_threads = _start_threads([3])
                                _join_threads_until_deadline(third_threads)
                    else:
                        all_threads = _start_threads([1, 2, 3])
                        _join_threads_until_deadline(all_threads)

                captcha1 = captcha_results[1]
                captcha2 = captcha_results[2]
                captcha3 = captcha_results[3]
                logging.info(f"[strategic] Pre-resolved {active_captcha_type} captcha1: {captcha1}")
                logging.info(f"[strategic] Pre-resolved {active_captcha_type} captcha2: {captcha2}")
                logging.info(f"[strategic] Pre-resolved {active_captcha_type} captcha3: {captcha3}")
            elif ENABLE_TEXTCLICK or ENABLE_ICONCLICK:
                captcha_results = {1: "", 2: "", 3: ""}
                live_captcha_results = captcha_results
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

                    def _worker():
                        try:
                            captcha_results[1] = _resolve_textclick_with_retries(
                                _make_textclick_worker(),
                                "preheat captcha1",
                                max_retries=None,
                                deadline_func=_remaining_captcha_seconds,
                            ) or ""
                        except Exception as e:
                            logging.warning(
                                f"[策略] {click_captcha_name} 第一个验证码预热线程失败：{e}"
                            )
                            captcha_results[1] = ""

                    deadline_mono = time.monotonic() + remaining
                    logging.info(
                        f"[策略] 开始预热{click_captcha_name}第一个验证码，"
                        "持续到取得验证令牌或达到目标时间截止点"
                    )
                    textclick_preheat_thread = threading.Thread(
                        target=_worker,
                        name=f"{click_captcha_type}-captcha-1",
                        daemon=True,
                    )
                    textclick_preheat_thread.start()
                    timeout_left = deadline_mono - time.monotonic()
                    if timeout_left > 0:
                        textclick_preheat_thread.join(timeout=timeout_left)

                captcha1 = captcha_results[1]
                captcha2 = ""
                captcha3 = ""
                logging.info(
                    f"[策略] {click_captcha_name}预热结果是否就绪：%s",
                    bool(captcha1),
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
                f"[strategic] Reuse preheated session from {shared_strategy_username} for {username}; "
                "skip login and captcha preheat"
            )
            if ENABLE_ROTATE:
                logging.info(
                    "[strategic] Captcha preheat skipped for this config; resolve one rotate captcha first"
                )
                captcha1 = _resolve_single_captcha_until_success(
                    s,
                    "rotate",
                    "reuse-session captcha1",
                    max_retries=3,
                ) or ""
                captcha2 = ""
                captcha3 = ""
            elif ENABLE_SLIDER:
                active_captcha_type = "slide"
                logging.info(
                    f"[strategic] Captcha preheat skipped for this config; resolve {active_captcha_type} captchas on demand"
                )
                captcha1 = s.resolve_captcha(active_captcha_type) or ""
                captcha2 = s.resolve_captcha(active_captcha_type) or ""
                captcha3 = s.resolve_captcha(active_captcha_type) or ""
            elif ENABLE_TEXTCLICK or ENABLE_ICONCLICK:
                logging.info(
                    f"[策略] 当前配置跳过验证码预热，先获取一个{click_captcha_name}验证码"
                )
                captcha1 = _resolve_textclick_with_retries(
                    s,
                    "reuse-session captcha1",
                    max_retries=3,
                ) or ""
                captcha2 = ""
                captcha3 = ""

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
        if (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE) and captcha1:
            captchas_for_submit = [captcha1, captcha1, captcha1]
            logging.info(
                f"[strategic] Reuse one preheated {captcha_type} captcha for the first three submits"
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
                "[strategic] Normalize captcha submit order to avoid empty captcha: "
                f"raw={raw_captchas}, non_empty_count={len(captchas_for_submit)}"
            )
        elif expected_single_reused_captcha:
            logging.info(
                f"[strategic] {captcha_type} submit order uses one prepared captcha "
                "for the first three submits"
            )
        captchas_for_submit = (captchas_for_submit + ["", "", ""])[:3]
        captcha1, captcha2, captcha3 = captchas_for_submit
        if captcha_required:
            logging.info(
                "[strategic] Captcha submit order after normalization: "
                f"captcha1={captcha1}, captcha2={captcha2}, captcha3={captcha3}"
            )

        def _refresh_submit_captchas_from_live_results():
            if not captcha_required or not live_captcha_results:
                return

            if ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE:
                live_first = live_captcha_results.get(1, "")
                if live_first and not captchas_for_submit[0]:
                    captchas_for_submit[:] = [live_first, live_first, live_first]
                    logging.info(
                        f"[strategic] Refreshed reused {captcha_type} captcha from late preheat result"
                    )
                return

            live_captchas = [
                live_captcha_results.get(1, ""),
                live_captcha_results.get(2, ""),
                live_captcha_results.get(3, ""),
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
                    "[strategic] Refreshed captcha submit order from late preheat results: "
                    f"live={live_captchas}, captcha1={captchas_for_submit[0]}, "
                    f"captcha2={captchas_for_submit[1]}, captcha3={captchas_for_submit[2]}"
                )

        def _prepare_textclick_captcha_for_submit(
            shot_idx: int,
            reason: str,
            *,
            max_retries: int | None = 3,
        ):
            if not (ENABLE_TEXTCLICK or ENABLE_ICONCLICK):
                return

            _refresh_submit_captchas_from_live_results()
            list_idx = shot_idx - 1
            if not (0 <= list_idx < len(captchas_for_submit)):
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
            else:
                logging.warning(
                    f"[策略] 为第 {shot_idx} 次提交准备{click_captcha_name}验证码失败"
                )

        def _prepare_rotate_captcha_for_submit(
            shot_idx: int,
            reason: str,
            *,
            max_retries: int | None = 3,
        ):
            if not ENABLE_ROTATE:
                return

            _refresh_submit_captchas_from_live_results()
            list_idx = shot_idx - 1
            if not (0 <= list_idx < len(captchas_for_submit)):
                return

            logging.info(
                f"[strategic] {reason}; immediately resolve a fresh rotate captcha "
                f"for submit shot {shot_idx} and replace the reused preheated captcha"
            )
            captchas_for_submit[list_idx] = ""
            captcha = _resolve_single_captcha_until_success(
                s,
                "rotate",
                f"submit shot {shot_idx}",
                max_retries=max_retries,
            ) or ""
            if captcha:
                captchas_for_submit[list_idx] = captcha
            else:
                logging.warning(
                    f"[strategic] Failed to prepare rotate captcha for submit shot {shot_idx}"
                )

        def _prepare_slide_captcha_for_submit(shot_idx: int, reason: str):
            if not ENABLE_SLIDER:
                return

            _refresh_submit_captchas_from_live_results()
            list_idx = shot_idx - 1
            if not (0 <= list_idx < len(captchas_for_submit)):
                return

            logging.info(
                f"[strategic] {reason}; immediately resolve a fresh slide captcha "
                f"for submit shot {shot_idx} and replace any preheated captcha"
            )
            captchas_for_submit[list_idx] = ""
            captcha = s.resolve_captcha("slide") or ""
            if captcha:
                captchas_for_submit[list_idx] = captcha
            else:
                logging.warning(
                    f"[strategic] Failed to prepare slide captcha for submit shot {shot_idx}"
                )

        def _prepare_fresh_captcha_for_submit(shot_idx: int, reason: str, *, max_retries=None):
            _prepare_textclick_captcha_for_submit(
                shot_idx,
                reason,
                max_retries=max_retries,
            )
            _prepare_rotate_captcha_for_submit(
                shot_idx,
                reason,
                max_retries=max_retries,
            )
            _prepare_slide_captcha_for_submit(shot_idx, reason)

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

            if (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE) and shot_idx == 1:
                logging.error(
                    f"[strategic] {captcha_type} captcha1 is still empty when submit shot 1 needs it; "
                    "skip this strategic submit instead of resolving after token fetch"
                )
                return None

            logging.warning(
                f"[strategic] Captcha for submit shot {shot_idx} is empty, "
                f"resolve {captcha_type} captcha on demand before submit"
            )
            if ENABLE_TEXTCLICK or ENABLE_ICONCLICK:
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
                logging.info(
                    f"[strategic] On-demand {captcha_type} captcha for submit shot "
                    f"{shot_idx}: {captcha}"
                )
                return captcha

            logging.error(
                f"[strategic] Submit shot {shot_idx} has no captcha after on-demand "
                "resolve, skip submit to avoid empty captcha"
            )
            return None

        def _ensure_textclick_captcha1_before_strategic_token() -> bool:
            if not (ENABLE_TEXTCLICK or ENABLE_ICONCLICK or ENABLE_ROTATE):
                return True

            _refresh_submit_captchas_from_live_results()
            if captchas_for_submit[0]:
                return True

            guard_ms = TEXTCLICK_FIRST_CAPTCHA_GUARD_MS
            hard_deadline = target_dt - datetime.timedelta(milliseconds=guard_ms)

            if _beijing_now() < hard_deadline:
                logging.warning(
                    f"[strategic] {captcha_type} captcha1 empty before token stage; "
                    f"continue resolving until {hard_deadline} before fetching token"
                )

                if (
                    (ENABLE_TEXTCLICK or ENABLE_ICONCLICK)
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
                            f"[strategic] {captcha_type} captcha1 received from existing "
                            "preheat thread before strategic token fetch"
                        )
                        return True

                def _remaining_first_captcha_seconds() -> float:
                    return (hard_deadline - _beijing_now()).total_seconds()

                if ENABLE_TEXTCLICK or ENABLE_ICONCLICK:
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
                    f"[strategic] {captcha_type} captcha1 is ready before strategic token fetch"
                )
                return True

            logging.error(
                f"[strategic] {captcha_type} captcha1 is empty at the hard deadline; "
                "skip first strategic submit and continue follow-up shots"
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
            if (ENABLE_TEXTCLICK or ENABLE_ICONCLICK) and isinstance(handle, dict):
                event = handle.get("event")
                if event is not None and not event.is_set():
                    event.wait(timeout=0.5)
            conflict = _getusedtimes_conflict_ready(handle)
            if conflict is None:
                logging.info(
                    "[strategic] %s getusedtimes not ready before submit, keep primary seat %s/%s",
                    label,
                    roomid,
                    first_seat,
                )
                return roomid, first_seat, seat_page_id, fid_enc, token, value
            if conflict is False:
                logging.info(
                    "[strategic] %s primary seat %s/%s is not conflicted, keep primary",
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
                if ENABLE_TEXTCLICK or ENABLE_ICONCLICK:
                    backup_conflict = s.check_getusedtimes_conflict_sync(
                        times,
                        backup_room,
                        backup_seat,
                        submit_day,
                        fid_enc=backup_fid,
                    )
                    if backup_conflict is True:
                        logging.info(
                            "[strategic] %s backup %s/%s also conflicted, keep trying next backup",
                            label,
                            backup_room,
                            backup_seat,
                        )
                        continue
                claimed_backup_seats.add(backup_key)
                logging.info(
                    "[strategic] %s primary seat %s/%s conflicted, switch to backup %s/%s",
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
                        "[strategic] %s backup %s/%s token fetch failed, keep trying next backup",
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
                        "[strategic] %s ordered fallback %s/%s skipped because it is already active/claimed",
                        label,
                        fallback_base_room,
                        fallback_seat,
                    )
                    continue
                if ENABLE_TEXTCLICK or ENABLE_ICONCLICK:
                    fallback_conflict = s.check_getusedtimes_conflict_sync(
                        times,
                        fallback_base_room,
                        fallback_seat,
                        submit_day,
                        fid_enc=fallback_fid,
                    )
                    if fallback_conflict is True:
                        logging.info(
                            "[strategic] %s ordered fallback %s/%s skipped because getusedtimes is conflicted",
                            label,
                            fallback_base_room,
                            fallback_seat,
                        )
                    continue
                claimed_backup_seats.add(fallback_key)
                if used_for_config is not None:
                    used_for_config.add(fallback_seat)
                logging.info(
                    "[strategic] %s primary seat %s/%s conflicted and %s, use ordered fallback from %s/%s -> %s/%s (%s)",
                    label,
                    roomid,
                    first_seat,
                    "backupSeats exhausted" if backup_slots else "backupSeats empty",
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
                        "[strategic] %s ordered fallback %s/%s token fetch failed, keep primary",
                        label,
                        fallback_base_room,
                        fallback_seat,
                    )
                    continue
                return fallback_base_room, fallback_seat, fallback_page_id, fallback_fid, token, value

            logging.warning(
                "[strategic] %s primary seat conflicted but no usable backupSeats, keep primary %s/%s",
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
        use_serial_followups = skip_first_strategic_submit and SUBMIT_MODE == "burst"
        if use_serial_followups:
            logging.warning(
                "[strategic] Captcha1 missed hard deadline in burst mode; "
                "skip the expired burst schedule and continue with serial second/third shots"
            )

        if SUBMIT_MODE == "burst" and not use_serial_followups:
            # ── 定时连发（极限型）──
            n_shots = len(BURST_OFFSETS_MS)
            for shot_idx in range(2, n_shots + 1):
                _prepare_fresh_captcha_for_submit(
                    shot_idx,
                    "Burst shots must use independent captcha values because every reservation POST consumes one",
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
                "[strategic] [burst] Only the first shot performs seat conflict selection; "
                "all shots fetch a fresh one-time page token/value inside the token-submit lock"
            )

            burst_results = [None] * n_shots
            threads = []
            for burst_i, burst_offset_ms in enumerate(BURST_OFFSETS_MS):
                burst_cap = captchas_list[burst_i] if burst_i < len(captchas_list) else ""
                t = threading.Thread(
                    target=_burst_shot_worker,
                    args=(
                        burst_i, burst_offset_ms, target_dt, s, burst_token_url,
                        times, burst_room, burst_seat, burst_cap, action, burst_results,
                        token_submit_lock, use_custom_day, submit_day, burst_fid,
                    ),
                    daemon=True,
                    name=f"burst-shot-{burst_i + 1}",
                )
                threads.append(t)

            logging.info(
                f"[strategic] [burst] Launching {len(threads)} shots at offsets "
                f"{BURST_OFFSETS_MS} ms from target_dt"
            )
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            suc = any(r for r in burst_results if r)
            logging.info(
                f"[strategic] [burst] All shots done, results: {burst_results}, overall success: {suc}"
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
                return s.get_submit(**kwargs)

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

            if skip_first_strategic_submit:
                logging.warning(
                    "[strategic] First submit skipped because captcha1 missed hard deadline; "
                    "continue directly with second strategic shot"
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
                    logging.error("[strategic] [C] Token fetch failed, skip this config")
                    continue
                if SKIP_FIRST_SEAT_QUERY:
                    logging.info(
                        f"[strategic] [C] Got token from {_first_token_url}: {token1}; "
                        "first submit skips seat getusedtimes and immediately uses primary seat"
                    )
                    submit_room = roomid
                    submit_seat = first_seat
                    submit_page_id = seat_page_id
                    submit_fid = fid_enc
                else:
                    logging.info(
                        f"[strategic] [C] Got token from {_first_token_url}: {token1}; "
                        "first submit checks seat getusedtimes before submit"
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
                    f"[strategic] [A] Formal pre-fetch page token at {_beijing_now()} "
                    f"(target_dt - {PRE_FETCH_TOKEN_MS}ms) from {_first_token_url}"
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
                    logging.error("[strategic] [A] First token is empty, skip this config")
                    continue

                submit_dt1 = target_dt + datetime.timedelta(milliseconds=FIRST_SUBMIT_OFFSET_MS)
                _wait_until(submit_dt1)
                logging.info(
                    f"[strategic] [A] First submit at {_beijing_now()} (target_dt + {FIRST_SUBMIT_OFFSET_MS}ms)"
                )
                if SKIP_FIRST_SEAT_QUERY:
                    logging.info(
                        "[strategic] [A] First submit skips seat getusedtimes and immediately uses primary seat"
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
                    f"[strategic] [B] Fetch page token at {_beijing_now()} (target_dt + {FIRST_SUBMIT_OFFSET_MS}ms)"
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
                    logging.error("[strategic] Failed to get page token for first submit, skip this config")
                    continue
                logging.info(
                    f"[strategic] Got page token for first submit from {_first_token_url}: "
                    f"{token1}, value: {value1}"
                )
                used_handle1 = s.post_getusedtimes_after_token(
                    times,
                    roomid,
                    first_seat,
                    submit_day,
                    fid_enc=fid_enc,
                )
                logging.info(f"[strategic] [B] Immediately submit after fetching page token")
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
                        "[strategic] First submit hit terminal failure msg, skip second/third submit"
                    )
                    success_list[index] = suc
                    continue
                logging.info("[strategic] First submit failed, prepare second submit with NEW page token")
                first_failure_msg = _last_submit_failure_msg()
                first_submit_sent = 1 in serial_submitted_shots
                logging.info(
                    "[strategic] First submit failure reason: %s; submit_sent=%s",
                    (
                        "captcha1 missed hard deadline"
                        if skip_first_strategic_submit
                        else (first_failure_msg or "<empty>")
                    ),
                    first_submit_sent,
                )
                if first_submit_sent or skip_first_strategic_submit:
                    _prepare_fresh_captcha_for_submit(
                        2,
                        (
                            "First submit sent a reservation POST, so its captcha is consumed"
                            if first_submit_sent
                            else "First submit was skipped because captcha1 missed the hard deadline"
                        ),
                        max_retries=3 if skip_first_strategic_submit else None,
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
                    logging.error("[strategic] Failed to get page token for second submit, skip to third/normal flow")
                else:
                    logging.info(
                        "[strategic] Second submit skips seat query and immediately uses NEW captcha + page token"
                    )
                    submit_captcha2 = _get_submit_captcha(2)
                    if submit_captcha2 is None:
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
                        "[strategic] Second submit hit terminal failure msg, skip third submit"
                    )
                    success_list[index] = suc
                    continue
                logging.info("[strategic] Second submit failed, prepare third submit with NEW page token")
                second_failure_msg = _last_submit_failure_msg()
                second_submit_sent = 2 in serial_submitted_shots
                logging.info(
                    "[strategic] Second submit failure reason: %s; submit_sent=%s",
                    second_failure_msg or "<empty>",
                    second_submit_sent,
                )
                if second_submit_sent:
                    _prepare_fresh_captcha_for_submit(
                        3,
                        "Second submit sent a reservation POST, so its captcha is consumed",
                        max_retries=None,
                    )
                elif captcha_required and captchas_for_submit[1]:
                    captchas_for_submit[2] = captchas_for_submit[1]
                    logging.info(
                        "[strategic] Second submit did not send a reservation POST; "
                        "reuse its unconsumed fresh captcha for third submit"
                    )

                token3, value3 = s._get_page_token(
                    serial_target_token_url,
                    require_value=True,
                )
                if not token3:
                    logging.error("[strategic] Failed to get page token for third submit, give up strategic submits for this config")
                else:
                    logging.info(
                        "[strategic] Third submit skips seat query and immediately uses NEW captcha + page token"
                    )
                    submit_captcha3 = _get_submit_captcha(3)
                    if submit_captcha3 is None:
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
        usernames_list = usernames.split(",")
        passwords_list = passwords.split(",")
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
                    "Index out of range for USERNAMES/PASSWORDS, skipping this config."
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
                        reserve_next_day=RESERVE_NEXT_DAY,
                        reserve_day_offset=RESERVE_DAY_OFFSET,
                    )
                    if not s.bootstrap_login(username, password):
                        logging.warning(
                            f"Skip current attempt for {username}: login bootstrap failed"
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
                    reserve_next_day=RESERVE_NEXT_DAY,
                    reserve_day_offset=RESERVE_DAY_OFFSET,
                )
                if not s.bootstrap_login(username, password):
                    logging.warning(
                        f"Skip current attempt for {username}: login bootstrap failed"
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
                                f"attempt {seat_increment_attempts}/{MAX_SEAT_INCREMENT_ATTEMPTS})"
                            )
                            continue
                        fallback_used_seats[i].add(new_seat)
                        user["seatid"] = [new_seat]
                        logging.info(
                            f"[seat-ordered-after-strategic] Config {i}: try seat {new_seat} "
                            f"(base {original_seatids[i]}, offset {offset}, "
                            f"attempt {seat_increment_attempts}/{MAX_SEAT_INCREMENT_ATTEMPTS})"
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
                if seat_increment_attempts >= MAX_SEAT_INCREMENT_ATTEMPTS:
                    logging.info(
                        f"[seat-ordered] Reached max fallback attempts "
                        f"{MAX_SEAT_INCREMENT_ATTEMPTS}, stop fallback seat changes"
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
                                f"attempt {seat_increment_attempts}/{MAX_SEAT_INCREMENT_ATTEMPTS})"
                            )
                            continue
                        fallback_used_seats[i].add(new_seat)
                        user["seatid"] = [new_seat]
                        logging.info(
                            f"[seat-ordered] Config {i}: try seat {new_seat} "
                            f"(base {original_seatids[i]}, offset {offset}, "
                            f"attempt {seat_increment_attempts}/{MAX_SEAT_INCREMENT_ATTEMPTS})"
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
        usernames_list = usernames.split(",")
        passwords_list = passwords.split(",")
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
