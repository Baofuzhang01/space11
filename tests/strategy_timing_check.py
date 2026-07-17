import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


with patch.dict(os.environ, {"CX_USERNAME": "13800138000", "CX_PASSWORD": "a,b"}, clear=False):
    assert main._split_action_credentials("13800138000", "a,b") == (["13800138000"], ["a,b"])

target = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=55)
login_completed = datetime.datetime.now(datetime.timezone.utc)
with patch.object(main, "STRATEGY_SLIDER_LEAD_MS", 60_738, create=True):
    assert main._get_captcha_start_dt(target, login_completed) == login_completed
