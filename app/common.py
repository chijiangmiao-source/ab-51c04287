"""共享常量与校验逻辑。"""
import re

# operationId：ASCII，1..64 字符，字母数字及 - _
OP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# magnetId：ASCII，1..64 字符，字母数字及 - _ : .
MAGNET_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")
# 毫安目标值范围（低温磁体安全包络）
MIN_MA = -100000
MAX_MA = 100000


def as_ascii(value) -> bool:
    return isinstance(value, str) and len(value) > 0 and value.isascii()


def validate_ramp_payload(body):
    """返回 (clean, errors)。errors 为列表，空列表表示通过。

    每个 error 形如 {"field": <json指针>, "message": ...}，便于定位。
    """
    errors = []
    if not isinstance(body, dict):
        return None, [{"field": "", "message": "请求体必须是 JSON 对象"}]

    op_id = body.get("operationId")
    if not as_ascii(op_id):
        errors.append({"field": "/operationId",
                       "message": "必须是非空 ASCII 字符串"})
    elif not OP_ID_RE.match(op_id):
        errors.append({"field": "/operationId",
                       "message": "仅允许 1-64 个字母数字或 - _ 字符"})

    magnet_id = body.get("magnetId")
    if not as_ascii(magnet_id):
        errors.append({"field": "/magnetId",
                       "message": "必须是非空 ASCII 字符串"})
    elif not MAGNET_RE.match(magnet_id):
        errors.append({"field": "/magnetId",
                       "message": "仅允许 1-64 个字母数字或 - _ : . 字符"})

    target_ma = body.get("targetMilliamps")
    if isinstance(target_ma, bool) or not isinstance(target_ma, int):
        errors.append({"field": "/targetMilliamps",
                       "message": "必须是整数（毫安）"})
    elif not (MIN_MA <= target_ma <= MAX_MA):
        errors.append({"field": "/targetMilliamps",
                       "message": f"必须位于 [{MIN_MA}, {MAX_MA}] 毫安"})

    if errors:
        return None, errors
    return {"operationId": op_id, "magnetId": magnet_id,
            "targetMilliamps": target_ma}, []
