"""配置加载工具"""
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件
_env_path = Path(__file__).parent.parent / "config" / ".env"
load_dotenv(_env_path)

_config_path = Path(__file__).parent.parent / "config" / "sync-config.yaml"

def load_config() -> dict:
    """加载 YAML 配置并替换环境变量"""
    with open(_config_path, "r", encoding="utf-8") as f:
        raw = f.read()
    # 替换 ${VAR} 格式的环境变量
    import re
    def _replace_env(match):
        var_name = match.group(1)
        value = os.getenv(var_name, "")
        if not value:
            print(f"  ⚠ 环境变量 {var_name} 未设置")
        return value
    raw = re.sub(r'\$\{(\w+)\}', _replace_env, raw)
    return yaml.safe_load(raw)

def get_dingtalk_credentials() -> tuple[str, str]:
    """返回 (app_key, app_secret)"""
    app_key = os.getenv("DINGTALK_APP_KEY", "")
    app_secret = os.getenv("DINGTALK_APP_SECRET", "")
    if not app_key or not app_secret:
        raise ValueError("DINGTALK_APP_KEY 和 DINGTALK_APP_SECRET 必须在 .env 中设置")
    return app_key, app_secret
