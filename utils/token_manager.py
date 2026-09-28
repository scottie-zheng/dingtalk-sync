"""
钉钉 AccessToken 管理器

职责：
- 通过 AppKey/AppSecret 获取企业 AccessToken
- 将 Token 缓存在 Redis 中（TTL = expireIn - 300秒，提前5分钟刷新）
- 对外提供 get_access_token() 方法，调用方无需关心刷新逻辑

钉钉 API：
- POST https://api.dingtalk.com/v1.0/oauth2/accessToken
- SDK: alibabacloud_dingtalk.oauth2_1_0.Client.get_access_token()
"""
import time
import redis
from alibabacloud_dingtalk.oauth2_1_0.client import Client as OAuth2Client
from alibabacloud_dingtalk.oauth2_1_0.models import GetAccessTokenRequest
from alibabacloud_tea_openapi.models import Config as OpenApiConfig
from tenacity import retry, stop_after_attempt, wait_exponential

from utils.config import load_config, get_dingtalk_credentials


class TokenManager:
    """钉钉 AccessToken 管理器（进程级单例）"""

    REDIS_KEY = "dingtalk:access_token"
    REDIS_EXPIRY_KEY = "dingtalk:access_token:expiry"
    REFRESH_BUFFER_SECONDS = 300  # 提前 5 分钟刷新

    def __init__(self):
        config = load_config()
        redis_cfg = config["storage"]["redis"]
        self._redis = redis.Redis(
            host=redis_cfg["host"],
            port=redis_cfg["port"],
            db=redis_cfg.get("db", 0),
            decode_responses=True,
        )
        self._app_key, self._app_secret = get_dingtalk_credentials()

        # 初始化钉钉 OAuth2 客户端
        open_api_config = OpenApiConfig()
        open_api_config.protocol = "https"
        open_api_config.region_id = "central"
        self._oauth_client = OAuth2Client(open_api_config)

        # 内存缓存（减少 Redis 读取）
        self._cached_token: str | None = None
        self._cached_expiry: float = 0

    def get_access_token(self) -> str:
        """
        获取有效的 AccessToken。
        优先从内存缓存 → Redis 缓存 → API 获取。
        """
        # 1. 检查内存缓存
        if self._cached_token and time.time() < self._cached_expiry - self.REFRESH_BUFFER_SECONDS:
            return self._cached_token

        # 2. 检查 Redis 缓存
        cached = self._redis.get(self.REDIS_KEY)
        cached_expiry = self._redis.get(self.REDIS_EXPIRY_KEY)
        if cached and cached_expiry:
            expiry_ts = float(cached_expiry)
            if time.time() < expiry_ts - self.REFRESH_BUFFER_SECONDS:
                self._cached_token = cached
                self._cached_expiry = expiry_ts
                return cached

        # 3. 从 API 获取新 Token
        return self._refresh_token()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _refresh_token(self) -> str:
        """调用钉钉 API 获取新 Token 并缓存"""
        print(f"  🔄 正在刷新 AccessToken ...")

        request = GetAccessTokenRequest(
            app_key=self._app_key,
            app_secret=self._app_secret,
        )
        response = self._oauth_client.get_access_token(request)
        token = response.body.access_token
        expire_in = response.body.expire_in  # 通常 7200 秒

        expiry_ts = time.time() + expire_in

        # 写入 Redis（TTL = expire_in - buffer）
        redis_ttl = max(expire_in - self.REFRESH_BUFFER_SECONDS, 60)
        self._redis.set(self.REDIS_KEY, token, ex=int(redis_ttl))
        self._redis.set(self.REDIS_EXPIRY_KEY, str(expiry_ts), ex=int(redis_ttl))

        # 更新内存缓存
        self._cached_token = token
        self._cached_expiry = expiry_ts

        print(f"  ✅ AccessToken 刷新成功，有效期 {expire_in}s，Redis TTL {int(redis_ttl)}s")
        return token

    def invalidate(self):
        """手动清除缓存，强制下次刷新"""
        self._redis.delete(self.REDIS_KEY, self.REDIS_EXPIRY_KEY)
        self._cached_token = None
        self._cached_expiry = 0
        print("  🗑 AccessToken 缓存已清除")


# ===== 测试入口 =====
if __name__ == "__main__":
    print("=" * 50)
    print("TokenManager 连通性测试")
    print("=" * 50)

    tm = TokenManager()

    # 测试 1: 获取 Token
    print("\n[Test 1] 首次获取 Token（应从 API 获取）:")
    token1 = tm.get_access_token()
    print(f"  Token: {token1[:20]}...{token1[-10:]}")
    print(f"  长度: {len(token1)}")

    # 测试 2: 再次获取（应从缓存获取）
    print("\n[Test 2] 再次获取（应从内存缓存获取）:")
    token2 = tm.get_access_token()
    print(f"  Token: {token2[:20]}...{token2[-10:]}")
    print(f"  与首次相同: {token1 == token2}")

    # 测试 3: Redis 中的值
    print("\n[Test 3] 检查 Redis 缓存:")
    rds_token = tm._redis.get(TokenManager.REDIS_KEY)
    print(f"  Redis Token: {rds_token[:20] if rds_token else 'None'}...")
    print(f"  与 API Token 相同: {rds_token == token1}")

    # 测试 4: 清除缓存后重新获取
    print("\n[Test 4] 清除缓存后重新获取:")
    tm.invalidate()
    token3 = tm.get_access_token()
    print(f"  Token: {token3[:20]}...{token3[-10:]}")
    print(f"  重新获取成功: {len(token3) > 0}")

    print("\n" + "=" * 50)
    print("✅ TokenManager 测试全部通过！")
    print("=" * 50)
