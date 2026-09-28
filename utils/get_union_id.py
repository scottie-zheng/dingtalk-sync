"""
通过 userId 查询 unionId（企业内部 API）

用法：
  python -m utils.get_union_id <userId>

说明：
  - 这个 API 使用企业内部应用的 Token + userId 来查询
  - userId 可以在钉钉管理后台 → 通讯录 → 成员详情中找到
  - 查到的 unionId 填入 .env 的 DINGTALK_OPERATOR_USER_ID
"""
import requests
from utils.token_manager import TokenManager


def get_union_id(user_id: str):
    """通过企业内部 API 获取 unionId"""
    tm = TokenManager()
    token = tm.get_access_token()

    # 钉钉企业内部 API: 根据 userId 获取用户详情
    url = f"https://oapi.dingtalk.com/topapi/v2/user/get?access_token={token}"
    payload = {"userid": user_id}

    response = requests.post(url, json=payload)
    data = response.json()

    if data.get("errcode") != 0:
        print(f"\n❌ API 调用失败: {data.get('errmsg', '未知错误')}")
        print(f"   errcode: {data.get('errcode')}")
        return

    user = data["result"]

    print("\n" + "=" * 50)
    print("👤 用户信息")
    print("=" * 50)
    print(f"  姓名 (name):     {user.get('name')}")
    print(f"  userId:          {user.get('userid')}")
    print(f"  unionId:         {user.get('unionid')}")
    print(f"  邮箱 (email):    {user.get('email')}")
    print(f"  手机号 (mobile): {user.get('mobile')}")
    print(f"  部门 (dept):     {user.get('dept_id_list')}")
    print("=" * 50)

    union_id = user.get("unionid")
    print(f"\n💡 请将 unionId 填入 config/.env:")
    print(f"   DINGTALK_OPERATOR_USER_ID={union_id}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m utils.get_union_id <userId>")
        print("\n💡 userId 可在钉钉管理后台 → 通讯录 → 成员详情中找到")
        sys.exit(1)

    get_union_id(sys.argv[1])
