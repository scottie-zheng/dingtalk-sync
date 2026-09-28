"""
查询当前操作者的钉钉用户信息

用法：
  python -m utils.get_my_info <unionId>

用途：确认你的 unionId 对应的用户信息
注意：此 API 通过 unionId 查询，不是 userId
"""
from alibabacloud_dingtalk.contact_1_0.client import Client as ContactClient
from alibabacloud_dingtalk.contact_1_0.models import GetUserHeaders
from alibabacloud_tea_openapi.models import Config as OpenApiConfig
from alibabacloud_tea_util.models import RuntimeOptions

from utils.token_manager import TokenManager


def get_user_info(union_id: str):
    """通过 unionId 查询用户信息"""
    tm = TokenManager()
    token = tm.get_access_token()

    config = OpenApiConfig()
    config.protocol = "https"
    config.region_id = "central"
    client = ContactClient(config)

    headers = GetUserHeaders(x_acs_dingtalk_access_token=token)
    response = client.get_user_with_options(
        union_id, headers, RuntimeOptions()
    )
    user = response.body

    print("\n" + "=" * 50)
    print("👤 用户信息")
    print("=" * 50)
    print(f"  昵称 (nick):     {user.nick}")
    print(f"  unionId:         {user.union_id}")
    print(f"  openId:          {user.open_id}")
    print(f"  邮箱 (email):    {user.email}")
    print(f"  手机号 (mobile): {user.mobile}")
    print("=" * 50)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m utils.get_my_info <unionId>")
        print("\n💡 如何获取 unionId？")
        print("   方法1: 钉钉管理后台 → 通讯录 → 找到自己 → 查看 unionId")
        print("   方法2: 如果你知道 userId，可以通过下面的 API 获取：")
        print("          请先把 userId 告诉我，我来帮你查 unionId")
        sys.exit(1)

    get_user_info(sys.argv[1])
