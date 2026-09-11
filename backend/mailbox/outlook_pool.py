"""Outlook 账号池与临时邮箱渠道适配器。

同时支持 API Key 查询、Web Session 登录、CSRF 状态更新和验证码轮询。
"""

from __future__ import annotations

import random
import re
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from typing import Any, Callable, Iterable, List, Optional
from urllib.parse import urlsplit

from backend.mailbox.utilities import extract_verification_code, strip_html

HttpGet = Callable[..., Any]
SessionFactory = Callable[[], Any]
UnavailableCheck = Callable[[str], bool]

_state_lock = threading.RLock()
_account_index = 0
_session_cookie = ""
_session_cookie_key: tuple[str, str] | None = None
_reserved_emails: set[str] = set()
_reserved_accounts: dict[str, dict] = {}


def response_error_detail(resp: Any, *, url: str = "", request_body: Any = None) -> str:
    """Keep HTTP failures actionable without dumping cookies or auth headers."""
    status = int(getattr(resp, "status_code", 0) or 0)
    body = ""
    try:
        body = str(resp.text or "").strip()
    except Exception:
        try:
            body = str(resp.json())
        except Exception:
            body = ""
    if len(body) > 1000:
        body = body[:1000] + "..."
    parts = [f"HTTP {status}"]
    if url:
        parts.append(f"url={url}")
    if request_body is not None:
        parts.append(f"request_body={request_body!r}")
    if body:
        parts.append(f"response_body={body}")
    return "; ".join(parts)


def normalize_base(api_base: str) -> str:
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        raise Exception("OutlookEmail API Base 未配置")
    return base


def normalize_source(source: str) -> str:
    value = str(source or "accounts").strip().lower()
    return value if value in {"accounts", "temp"} else "accounts"


def parse_group_id(raw: Any) -> Optional[int]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def same_group_id(left: Any, right: Any) -> bool:
    parsed_left = parse_group_id(left)
    parsed_right = parse_group_id(right)
    if parsed_left is not None and parsed_right is not None:
        return parsed_left == parsed_right
    first = str(left or "").strip()
    second = str(right or "").strip()
    return bool(first) and first == second


def api_headers(api_key: str) -> dict:
    key = str(api_key or "").strip()
    if not key:
        raise Exception("OutlookEmail accounts 来源需要配置 API Key")
    return {"X-API-Key": key}


def reset_runtime_state() -> None:
    global _account_index, _session_cookie, _session_cookie_key
    with _state_lock:
        _account_index = 0
        _session_cookie = ""
        _session_cookie_key = None
        _reserved_emails.clear()
        _reserved_accounts.clear()


def release_email(email: str) -> None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return
    with _state_lock:
        _reserved_emails.discard(normalized)


def cookie_from_response(resp: Any) -> str:
    try:
        raw_cookie = str(resp.headers.get("set-cookie", "") or "")
    except Exception:
        raw_cookie = ""
    if not raw_cookie:
        return ""
    try:
        cookie = SimpleCookie()
        cookie.load(raw_cookie)
        return "; ".join(f"{key}={value.value}" for key, value in cookie.items())
    except Exception:
        return ""


def cookie_from_session(session: Any) -> str:
    try:
        items = list(session.cookies.items())
        if items:
            return "; ".join(f"{key}={value}" for key, value in items)
    except Exception:
        pass
    try:
        jar = getattr(session.cookies, "jar", None)
        if jar:
            items = [f"{item.name}={item.value}" for item in jar]
            if items:
                return "; ".join(items)
    except Exception:
        pass
    return ""


def merge_cookie_headers(*values: str) -> str:
    """合并多次响应里的 Cookie，同名项以后者为准。"""
    merged: dict[str, str] = {}
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            cookie = SimpleCookie()
            cookie.load(text)
            for key, value in cookie.items():
                merged[key] = value.value
        except Exception:
            for part in text.split(";"):
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                key = key.strip()
                if key:
                    merged[key] = value.strip()
    return "; ".join(f"{key}={value}" for key, value in merged.items())


def seed_session_cookie(session: Any, cookie_header: str, api_base: str = "") -> bool:
    """Put an existing Cookie header into the session jar.

    The OutlookEmail CSRF endpoint rotates the Flask session cookie. Keeping the
    cookie in the jar lets the following PUT use that rotated value instead of
    an explicit stale ``Cookie`` header.
    """
    text = str(cookie_header or "").strip()
    if not text:
        return False
    pairs: list[tuple[str, str]] = []
    try:
        parsed = SimpleCookie()
        parsed.load(text)
        pairs = [(key, morsel.value) for key, morsel in parsed.items()]
    except Exception:
        pairs = []
    if not pairs:
        pairs = [
            (part.split("=", 1)[0].strip(), part.split("=", 1)[1].strip())
            for part in text.split(";")
            if "=" in part and part.split("=", 1)[0].strip()
        ]
    hostname = str(urlsplit(normalize_base(api_base)).hostname or "").strip() if api_base else ""
    try:
        for key, value in pairs:
            setter = getattr(session.cookies, "set", None)
            if callable(setter):
                if hostname:
                    # A leading dot keeps the seeded cookie ahead of the
                    # host-only cookie rotated by Flask. This works for IPs,
                    # localhost and single-label Docker service names.
                    setter(key, value, domain=f".{hostname}", path="/")
                else:
                    setter(key, value)
            else:
                session.cookies[key] = value
        return bool(pairs)
    except Exception:
        return False


def login_cookie(
    session_factory: SessionFactory,
    api_base: str,
    web_password: str,
    *,
    proxies: Optional[dict] = None,
    force_refresh: bool = False,
) -> str:
    global _session_cookie, _session_cookie_key
    base = normalize_base(api_base)
    password = str(web_password or "")
    if not password:
        return ""
    cache_key = (base, password)
    with _state_lock:
        if not force_refresh and _session_cookie and _session_cookie_key == cache_key:
            return _session_cookie

    session = session_factory()
    if proxies:
        try:
            session.proxies = proxies
        except Exception:
            pass
    login_resp = session.post(
        f"{base}/api/extension/login",
        json={"password": password, "next": "/"},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    login_resp.raise_for_status()
    data = login_resp.json()
    if not isinstance(data, dict) or not data.get("success") or not data.get("launch_url"):
        raise Exception(f"OutlookEmail 网页登录失败: {str(data)[:200]}")
    launch_url = str(data.get("launch_url") or "")
    url = launch_url if launch_url.startswith(("http://", "https://")) else f"{base}/{launch_url.lstrip('/')}"
    session_resp = session.get(url, allow_redirects=True, timeout=15)
    session_resp.raise_for_status()
    cookie = merge_cookie_headers(
        cookie_from_response(login_resp),
        cookie_from_response(session_resp),
        cookie_from_session(session),
    )
    if not cookie:
        raise Exception("OutlookEmail 登录成功但未获取到 Session Cookie")
    with _state_lock:
        _session_cookie = cookie
        _session_cookie_key = cache_key
    return cookie


def account_for_email(
    http_get: HttpGet,
    api_base: str,
    api_key: str,
    email: str,
    *,
    group_id: str = "",
) -> dict:
    normalized = str(email or "").strip().lower()
    if not normalized:
        raise Exception("OutlookEmail 停用缺少邮箱地址")
    with _state_lock:
        cached = dict(_reserved_accounts.get(normalized) or {})
    if cached.get("id"):
        return cached
    for item in get_accounts(http_get, api_base, api_key, group_id=group_id):
        if item_email(item).strip().lower() == normalized:
            with _state_lock:
                _reserved_accounts[normalized] = dict(item)
            return item
    raise Exception(f"OutlookEmail 账号池中未找到邮箱: {email}")


def web_json_request(
    session_factory: SessionFactory,
    api_base: str,
    *,
    method: str,
    path: str,
    payload: dict,
    web_password: str = "",
    session_cookie: str = "",
    proxies: Optional[dict] = None,
    missing_auth_error: str,
    failure_label: str,
) -> dict:
    """登录 OutlookEmail 管理页并发送带 CSRF 的 JSON 请求。"""
    base = normalize_base(api_base)
    password = str(web_password or "")
    manual_cookie = str(session_cookie or "").strip()
    if not password and not manual_cookie:
        raise Exception(missing_auth_error)

    last_error = ""
    method_name = str(method or "post").strip().lower() or "post"
    url = f"{base}{path}"
    for attempt in range(2):
        cookie = login_cookie(
            session_factory,
            base,
            password,
            proxies=proxies,
            force_refresh=attempt > 0,
        ) if password else manual_cookie
        if not cookie:
            raise Exception("OutlookEmail 未获取到 Web Session Cookie")

        session = session_factory()
        if proxies:
            try:
                session.proxies = proxies
            except Exception:
                pass
        cookie_in_jar = seed_session_cookie(session, cookie, base)
        csrf_headers = {"Accept": "application/json"}
        if not cookie_in_jar:
            csrf_headers["Cookie"] = cookie
        csrf_resp = session.get(
            f"{base}/api/csrf-token",
            headers=csrf_headers,
            timeout=15,
        )
        if int(getattr(csrf_resp, "status_code", 0) or 0) in (401, 403):
            last_error = "Web Session 已失效"
            if password and attempt == 0:
                continue
            raise Exception(f"OutlookEmail {last_error}")
        if int(getattr(csrf_resp, "status_code", 0) or 0) >= 400:
            raise Exception(response_error_detail(csrf_resp, url=f"{base}/api/csrf-token"))
        csrf_data = csrf_resp.json()
        if not isinstance(csrf_data, dict):
            raise Exception("OutlookEmail CSRF 响应格式错误")
        csrf_disabled = bool(csrf_data.get("csrf_disabled"))
        csrf_token = str(csrf_data.get("csrf_token") or "")
        if not csrf_disabled and not csrf_token:
            raise Exception("OutlookEmail 未获取到 CSRF Token")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if not cookie_in_jar:
            # Fallback for minimal/custom session implementations without a jar.
            headers["Cookie"] = merge_cookie_headers(cookie, cookie_from_response(csrf_resp))
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        request_fn = getattr(session, method_name, None)
        if not callable(request_fn):
            raise Exception(f"OutlookEmail 会话不支持 {method_name.upper()} 请求")
        resp = request_fn(
            url,
            headers=headers,
            json=payload,
            timeout=15,
        )
        if int(getattr(resp, "status_code", 0) or 0) in (401, 403):
            last_error = "Web Session 或 CSRF 校验失效"
            if password and attempt == 0:
                continue
            raise Exception(f"OutlookEmail {last_error}")
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            detail = response_error_detail(
                resp,
                url=url,
                request_body=payload,
            )
            raise Exception(f"OutlookEmail {failure_label}请求失败: {detail}")
        data = resp.json()
        if not isinstance(data, dict) or not data.get("success"):
            error = data.get("error") if isinstance(data, dict) else ""
            message = data.get("message") if isinstance(data, dict) else ""
            raise Exception(
                f"OutlookEmail {failure_label}失败: {error or message or str(data)[:200]}"
            )
        return data
    raise Exception(f"OutlookEmail {failure_label}失败: {last_error or '未知错误'}")


def _account_id_for_email(account: dict, email: str) -> int:
    account_id = account.get("id")
    try:
        return int(account_id)
    except (TypeError, ValueError):
        raise Exception(f"OutlookEmail 账号缺少有效 ID: {email}")


# ==================== 标签模式（本地魔改） ====================
# 取号判据是「不带任何 <prefix>* 标签」；占用写「<prefix>使用中」；结束写「<prefix>成功/失败」。
# 平台侧 status 一律不改，邮箱台账保持 成功/失败 的纯净口径。
DEFAULT_TAG_PREFIX = "Grok-"
_tag_client_lock = threading.RLock()
_tag_client: Any = None


def tag_names(item: Any) -> set[str]:
    """取出账号对象上的标签名集合。"""
    if not isinstance(item, dict):
        return set()
    tags = item.get("tags")
    if not isinstance(tags, list):
        return set()
    names: set[str] = set()
    for tag in tags:
        if isinstance(tag, dict):
            name = str(tag.get("name", "") or "").strip()
        else:
            name = str(tag or "").strip()
        if name:
            names.add(name)
    return names


def item_has_no_prefixed_tag(item: Any, prefix: str = DEFAULT_TAG_PREFIX) -> bool:
    """标签模式取号判据：不带任何该前缀的标签（= 从未被使用过）。"""
    normalized_prefix = str(prefix or DEFAULT_TAG_PREFIX).strip() or DEFAULT_TAG_PREFIX
    return not any(name.startswith(normalized_prefix) for name in tag_names(item))


def _tag_session() -> Any:
    """惰性创建一个用于调用标签对外 API 的会话。"""
    global _tag_client
    with _tag_client_lock:
        if _tag_client is None:
            import requests  # 局部导入：非标签模式不依赖

            _tag_client = requests.Session()
        return _tag_client


def set_account_tags(
    api_base: str,
    email: str,
    *,
    action: str,
    api_key: str = "",
    tags: Optional[Iterable[str]] = None,
    tag_prefix: str = DEFAULT_TAG_PREFIX,
    timeout: int = 20,
) -> dict:
    """调用 OutlookEmail 对外标签 API。

    action: add | remove | set | claim | unclaim
    返回 {success, available?, tags?}；claim 返回 409 时以 available=False 表示已占用。
    """
    target = str(email or "").strip()
    if not target:
        raise Exception("标签操作缺少邮箱地址")
    payload: dict[str, Any] = {
        "email": target,
        "action": str(action or "add").strip().lower(),
        "tag_prefix": str(tag_prefix or DEFAULT_TAG_PREFIX).strip() or DEFAULT_TAG_PREFIX,
    }
    if tags is not None:
        payload["tags"] = [str(item) for item in tags]
    url = f"{normalize_base(api_base)}/api/external/accounts/tags"
    session = _tag_session()
    resp = session.post(
        url,
        json=payload,
        headers=api_headers(api_key),
        timeout=timeout,
    )
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code == 409:
        return {
            "success": True,
            "available": False,
            "tags": list((data or {}).get("tags") or []),
            "status_code": 409,
        }
    if resp.status_code >= 400 or not isinstance(data, dict) or not data.get("success"):
        detail = data.get("error") if isinstance(data, dict) else ""
        raise Exception(
            f"OutlookEmail 标签操作({payload['action']})失败: HTTP {resp.status_code} {detail or str(data)[:160]}"
        )
    if "available" not in data:
        data["available"] = True
    data["status_code"] = resp.status_code
    return data


def disable_account(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    email: str,
    *,
    api_key: str = "",
    group_id: str = "",
    web_password: str = "",
    session_cookie: str = "",
    proxies: Optional[dict] = None,
) -> dict:
    """把 accounts 来源的邮箱状态更新为 inactive。

    API Key 用于定位账号 ID；网页登录密码会自动换 Session Cookie，再自动获取
    CSRF Token。Session Cookie 仅作为没有网页登录密码时的兼容回退。
    """
    base = normalize_base(api_base)
    account = account_for_email(
        http_get,
        base,
        api_key,
        email,
        group_id=group_id,
    )
    account_id = _account_id_for_email(account, email)
    if str(account.get("status", "") or "").strip().lower() == "inactive":
        return {
            "success": True,
            "account_id": account_id,
            "already_inactive": True,
            "message": "邮箱已处于停用状态",
        }

    data = web_json_request(
        session_factory,
        base,
        method="put",
        path=f"/api/accounts/{account_id}",
        payload={"status": "inactive"},
        web_password=web_password,
        session_cookie=session_cookie,
        proxies=proxies,
        missing_auth_error="OutlookEmail 自动停用需要配置 Web 登录密码",
        failure_label="停用",
    )
    with _state_lock:
        current = dict(_reserved_accounts.get(str(email).strip().lower()) or account)
        current["status"] = "inactive"
        _reserved_accounts[str(email).strip().lower()] = current
    return {
        "success": True,
        "account_id": account_id,
        "already_inactive": False,
        "message": str(data.get("message") or "状态更新成功"),
    }


def move_account_to_group(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    email: str,
    target_group_id: Any,
    *,
    api_key: str = "",
    group_id: str = "",
    web_password: str = "",
    session_cookie: str = "",
    proxies: Optional[dict] = None,
) -> dict:
    """把 accounts 来源的邮箱移到指定分组，不改 status。

    OutlookEmail 的 PUT /api/accounts/{id} 只带 group_id 会走完整更新，
    必须改走 POST /api/accounts/batch-update-group。
    """
    target = parse_group_id(target_group_id)
    if target is None:
        raise Exception("OutlookEmail 目标分组 ID 无效")
    if not str(email or "").strip():
        raise Exception("OutlookEmail 移动分组缺少邮箱地址")

    base = normalize_base(api_base)
    lookup_groups: list[str] = []
    source = str(group_id or "").strip()
    if source:
        lookup_groups.append(source)
    lookup_groups.append(str(target))
    lookup_groups.append("")

    account = None
    last_error: Exception | None = None
    seen: set[str] = set()
    for lookup in lookup_groups:
        if lookup in seen:
            continue
        seen.add(lookup)
        try:
            account = account_for_email(
                http_get,
                base,
                api_key,
                email,
                group_id=lookup,
            )
            break
        except Exception as exc:
            last_error = exc
            if "未找到邮箱" not in str(exc):
                raise
    if account is None:
        raise last_error or Exception(f"OutlookEmail 账号池中未找到邮箱: {email}")

    account_id = _account_id_for_email(account, email)
    if same_group_id(account.get("group_id"), target):
        return {
            "success": True,
            "account_id": account_id,
            "group_id": target,
            "already_moved": True,
            "message": "邮箱已在目标分组",
        }

    payload = {"account_ids": [account_id], "group_id": target}
    data = web_json_request(
        session_factory,
        base,
        method="post",
        path="/api/accounts/batch-update-group",
        payload=payload,
        web_password=web_password,
        session_cookie=session_cookie,
        proxies=proxies,
        missing_auth_error="OutlookEmail 移动分组需要配置 Web 登录密码",
        failure_label="移动分组",
    )
    with _state_lock:
        current = dict(_reserved_accounts.get(str(email).strip().lower()) or account)
        current["group_id"] = target
        _reserved_accounts[str(email).strip().lower()] = current
    return {
        "success": True,
        "account_id": account_id,
        "group_id": target,
        "already_moved": False,
        "message": str(data.get("message") or "分组更新成功"),
    }


def session_headers(
    session_factory: SessionFactory,
    api_base: str,
    *,
    web_password: str = "",
    session_cookie: str = "",
    proxies: Optional[dict] = None,
) -> dict:
    cookie = login_cookie(
        session_factory,
        api_base,
        web_password,
        proxies=proxies,
    ) or str(session_cookie or "").strip()
    if not cookie:
        raise Exception("OutlookEmail temp 来源需要配置网页登录密码或 Session Cookie")
    return {"Cookie": cookie}


def pick_list(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("emails", "items", "results", "accounts", "temp_emails", "tempEmails", "messages"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = data.get("data")
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    if isinstance(nested, dict):
        return pick_list(nested)
    return []


def item_email(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("email", "address", "name"):
        value = str(item.get(key, "") or "").strip()
        if "@" in value:
            return value
    return ""


def item_is_active(item: Any) -> bool:
    """账号池中 status=inactive 表示已停用，不参与注册。"""
    if not isinstance(item, dict):
        return False
    return str(item.get("status", "") or "").strip().lower() != "inactive"


def parse_tag_ids(raw: str | Iterable[Any]) -> set[str]:
    if isinstance(raw, str):
        return {item.strip() for item in re.split(r"[,，\s]+", raw) if item.strip()}
    return {str(item).strip() for item in (raw or []) if str(item).strip()}


def temp_matches_tags(item: Any, tag_ids: set[str]) -> bool:
    if not tag_ids:
        return True
    tags = item.get("tags") if isinstance(item, dict) else None
    if not isinstance(tags, list):
        return False
    for tag in tags:
        if isinstance(tag, dict) and str(tag.get("id", "")).strip() in tag_ids:
            return True
        if str(tag).strip() in tag_ids:
            return True
    return False


def get_accounts(
    http_get: HttpGet,
    api_base: str,
    api_key: str,
    *,
    group_id: str = "",
) -> List[dict]:
    params: dict[str, Any] = {
        "limit": 10000,
        "offset": 0,
        "sort_by": "created_at",
        "sort_order": "asc",
    }
    group = str(group_id or "").strip()
    if group:
        params["group_id"] = group
    resp = http_get(
        f"{normalize_base(api_base)}/api/external/accounts",
        headers=api_headers(api_key),
        params=params,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("success"):
        raise Exception(f"OutlookEmail 获取账号列表失败: {str(data)[:200]}")
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        raise Exception(f"OutlookEmail accounts 格式错误: {str(data)[:200]}")
    return [item for item in accounts if isinstance(item, dict)]


def serialize_group(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    group_id = parse_group_id(item.get("id") if item.get("id") is not None else item.get("group_id"))
    if group_id is None:
        return None
    name = str(item.get("name") or item.get("group_name") or "").strip() or f"分组 {group_id}"
    try:
        account_count = int(item.get("account_count") or 0)
    except (TypeError, ValueError):
        account_count = 0
    is_system = False
    raw_system = item.get("is_system")
    try:
        is_system = bool(int(raw_system))
    except (TypeError, ValueError):
        is_system = bool(raw_system)
    if name == "临时邮箱":
        is_system = True
    return {
        "id": group_id,
        "name": name,
        "account_count": max(0, account_count),
        # 「可用数」只有走 API Key 的账号列表统计得出来；走网页分组接口时为 None。
        "available_count": None,
        "is_system": is_system,
    }


def _attach_available_counts(groups: dict[int, dict], counts: dict[int, int], prefix: str) -> None:
    """把「可用数」写进已聚合的分组对象。

    未启用标签模式（``prefix`` 为空）时保持 ``None``：前端据此回退到只显示总数。
    启用了就记 0，这样「这个分组已经取不出号了」在下拉里看得出来。
    """
    if not str(prefix or "").strip():
        return
    for group_id, group in groups.items():
        group["available_count"] = max(0, int(counts.get(group_id, 0)))


def availability_counts(accounts: List[dict], prefix: str) -> dict[int, int]:
    """按分组统计「可用」账号数。

    可用 = 不带任何 ``prefix`` 前缀标签的账号（标签模式下的取号判据）。
    前缀为空时返回空字典，调用方据此跳过统计。
    """
    normalized_prefix = str(prefix or "").strip()
    if not normalized_prefix:
        return {}
    counts: dict[int, int] = {}
    for item in accounts:
        if not isinstance(item, dict):
            continue
        if not item_has_no_prefixed_tag(item, normalized_prefix):
            continue
        group_id = parse_group_id(item.get("group_id"))
        if group_id is None:
            continue
        counts[group_id] = counts.get(group_id, 0) + 1
    return counts


def _groups_from_accounts(
    http_get: HttpGet,
    api_base: str,
    api_key: str,
    available_prefix: str = "",
) -> List[dict]:
    grouped: dict[int, dict] = {}
    accounts = get_accounts(http_get, api_base, api_key)
    for item in accounts:
        group_id = parse_group_id(item.get("group_id"))
        if group_id is None:
            continue
        current = grouped.get(group_id)
        if current is None:
            serialized = serialize_group(
                {
                    "id": group_id,
                    "name": item.get("group_name") or "",
                    "account_count": 1,
                    "is_system": 0,
                }
            )
            if serialized:
                grouped[group_id] = serialized
            continue
        current["account_count"] = int(current.get("account_count") or 0) + 1
    _attach_available_counts(grouped, availability_counts(accounts, available_prefix), available_prefix)
    return [grouped[key] for key in sorted(grouped)]


def list_groups(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    *,
    api_key: str = "",
    web_password: str = "",
    session_cookie: str = "",
    proxies: Optional[dict] = None,
    available_prefix: str = "",
) -> List[dict]:
    """读取 OutlookEmail 分组，优先走管理页 /api/groups，便于包含空分组。

    ``available_prefix`` 非空时（标签模式），额外统计每个分组里「不带该前缀
    标签」的账号数，作为下拉列表的「可用」口径。
    """
    password = str(web_password or "")
    manual_cookie = str(session_cookie or "").strip()
    if password or manual_cookie:
        return _list_groups_via_web(
            session_factory,
            api_base,
            web_password=password,
            session_cookie=manual_cookie,
            proxies=proxies,
        )
    if str(api_key or "").strip():
        return _groups_from_accounts(http_get, api_base, api_key, available_prefix=available_prefix)
    raise Exception("OutlookEmail 获取分组需要配置网页登录密码或 API Key")


def _list_groups_via_web(
    session_factory: SessionFactory,
    api_base: str,
    *,
    web_password: str = "",
    session_cookie: str = "",
    proxies: Optional[dict] = None,
) -> List[dict]:
    base = normalize_base(api_base)
    password = str(web_password or "")
    manual_cookie = str(session_cookie or "").strip()
    last_error = ""
    for attempt in range(2):
        cookie = login_cookie(
            session_factory,
            base,
            password,
            proxies=proxies,
            force_refresh=attempt > 0,
        ) if password else manual_cookie
        if not cookie:
            raise Exception("OutlookEmail 未获取到 Web Session Cookie")
        session = session_factory()
        if proxies:
            try:
                session.proxies = proxies
            except Exception:
                pass
        cookie_in_jar = seed_session_cookie(session, cookie, base)
        headers = {"Accept": "application/json"}
        if not cookie_in_jar:
            headers["Cookie"] = cookie
        resp = session.get(f"{base}/api/groups", headers=headers, timeout=15)
        status = int(getattr(resp, "status_code", 0) or 0)
        if status in (401, 403):
            last_error = "Web Session 已失效"
            if password and attempt == 0:
                continue
            raise Exception(f"OutlookEmail {last_error}")
        if status >= 400:
            raise Exception(response_error_detail(resp, url=f"{base}/api/groups"))
        data = resp.json()
        if not isinstance(data, dict) or not data.get("success"):
            error = data.get("error") if isinstance(data, dict) else ""
            raise Exception(f"OutlookEmail 获取分组失败: {error or str(data)[:200]}")
        raw_groups = data.get("groups")
        if not isinstance(raw_groups, list):
            raise Exception(f"OutlookEmail 分组列表格式错误: {str(data)[:200]}")
        groups = []
        for item in raw_groups:
            serialized = serialize_group(item)
            if serialized:
                groups.append(serialized)
        groups.sort(key=lambda item: int(item.get("id") or 0))
        return groups
    raise Exception(f"OutlookEmail 获取分组失败: {last_error or '未知错误'}")


def get_temp_emails(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    *,
    web_password: str = "",
    session_cookie: str = "",
    temp_tag_ids: str = "",
    proxies: Optional[dict] = None,
) -> List[dict]:
    resp = http_get(
        f"{normalize_base(api_base)}/api/temp-emails",
        headers=session_headers(
            session_factory,
            api_base,
            web_password=web_password,
            session_cookie=session_cookie,
            proxies=proxies,
        ),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise Exception(f"OutlookEmail 获取临时邮箱失败: {str(data)[:200]}")
    tag_ids = parse_tag_ids(temp_tag_ids)
    return [
        item
        for item in pick_list(data)
        if item_email(item) and temp_matches_tags(item, tag_ids)
    ]


def acquire_email(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    *,
    api_key: str = "",
    source: str = "accounts",
    group_id: str = "",
    web_password: str = "",
    session_cookie: str = "",
    temp_tag_ids: str = "",
    pick_mode: str = "random",
    proxies: Optional[dict] = None,
    is_unavailable: Optional[UnavailableCheck] = None,
    use_tags: bool = False,
    tag_prefix: str = DEFAULT_TAG_PREFIX,
) -> tuple[str, str]:
    global _account_index
    normalized_source = normalize_source(source)
    if normalized_source == "temp":
        accounts = get_temp_emails(
            http_get,
            session_factory,
            api_base,
            web_password=web_password,
            session_cookie=session_cookie,
            temp_tag_ids=temp_tag_ids,
            proxies=proxies,
        )
    else:
        accounts = get_accounts(http_get, api_base, api_key, group_id=group_id)

    # 标签模式：候选 = 不带任何 <prefix>* 标签的邮箱；占用靠平台 claim 原子完成。
    if use_tags and normalized_source != "temp":
        candidates_tags: List[dict] = []
        for item in accounts:
            email = item_email(item)
            if not email or not item_has_no_prefixed_tag(item, tag_prefix):
                continue
            if is_unavailable:
                try:
                    if is_unavailable(email):
                        continue
                except Exception:
                    pass
            candidates_tags.append(item)
        if not candidates_tags:
            raise Exception(
                f"OutlookEmail 没有可用的未使用邮箱（判据：不带 {tag_prefix}* 标签）"
            )
        if str(pick_mode or "random").strip().lower() == "random":
            random.shuffle(candidates_tags)
        else:
            candidates_tags = (
                candidates_tags[_account_index % len(candidates_tags):]
                + candidates_tags[:_account_index % len(candidates_tags)]
            )
        rejected: list[str] = []
        for item in candidates_tags:
            email = item_email(item)
            result = set_account_tags(
                api_base,
                email,
                action="claim",
                api_key=api_key,
                tag_prefix=tag_prefix,
            )
            if result.get("available"):
                _account_index += 1
                with _state_lock:
                    _reserved_emails.add(email.lower())
                    _reserved_accounts[email.lower()] = dict(item)
                return email, f"outlookemail:{normalized_source}:{email}"
            rejected.append(email)
        raise Exception(
            f"OutlookEmail 候选邮箱均已被占用（claim 全部失败，{len(rejected)} 个）"
        )

    total = 0
    active = 0
    unavailable_count = 0
    candidates = []
    for item in accounts:
        email = item_email(item)
        if not email:
            continue
        total += 1
        if not item_is_active(item):
            continue
        active += 1
        if is_unavailable:
            try:
                if is_unavailable(email):
                    unavailable_count += 1
                    continue
            except Exception:
                pass
        candidates.append(item)
    if not candidates:
        if total <= 0:
            raise Exception("OutlookEmail 邮箱池为空，未返回任何账号")
        if active <= 0:
            raise Exception(
                f"OutlookEmail 邮箱池中没有 active 账号（共 {total} 个，均已停用或不可用）"
            )
        if unavailable_count > 0:
            raise Exception(
                "OutlookEmail 可取邮箱均已在本地标记为已注册/已消耗"
                f"（active {active} 个，其中已消耗 {unavailable_count} 个）。"
                "请补充新邮箱，或检查已注册账号是否已正确停用。"
            )
        raise Exception("OutlookEmail 当前没有可分配的邮箱")

    mode = str(pick_mode or "random").strip().lower()
    with _state_lock:
        account = None
        if mode == "random":
            shuffled = candidates[:]
            random.shuffle(shuffled)
            for item in shuffled:
                normalized = item_email(item).lower()
                if normalized not in _reserved_emails:
                    _reserved_emails.add(normalized)
                    account = item
                    break
        else:
            for _ in range(len(candidates)):
                item = candidates[_account_index % len(candidates)]
                _account_index += 1
                normalized = item_email(item).lower()
                if normalized not in _reserved_emails:
                    _reserved_emails.add(normalized)
                    account = item
                    break
    if account is None:
        raise Exception("OutlookEmail 可取邮箱均已被当前任务占用（并发预留），不是邮箱池为空")
    email = item_email(account)
    with _state_lock:
        _reserved_accounts[email.lower()] = dict(account)
    return email, f"outlookemail:{normalized_source}:{email}"


def get_messages(
    http_get: HttpGet,
    api_base: str,
    api_key: str,
    email: str,
    *,
    folder: str = "all",
    top: int = 10,
) -> List[dict]:
    try:
        limit = max(1, min(50, int(top)))
    except Exception:
        limit = 10
    params = {
        "email": email,
        "folder": str(folder or "all").strip() or "all",
        "top": limit,
        "skip": 0,
    }
    resp = http_get(
        f"{normalize_base(api_base)}/api/external/emails",
        headers=api_headers(api_key),
        params=params,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("success"):
        raise Exception(f"OutlookEmail 获取邮件失败: {str(data)[:200]}")
    messages = data.get("emails")
    return [item for item in messages if isinstance(item, dict)] if isinstance(messages, list) else []


def get_temp_messages(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    email: str,
    *,
    web_password: str = "",
    session_cookie: str = "",
    proxies: Optional[dict] = None,
) -> List[dict]:
    resp = http_get(
        f"{normalize_base(api_base)}/api/temp-emails/{email}/messages",
        headers=session_headers(
            session_factory,
            api_base,
            web_password=web_password,
            session_cookie=session_cookie,
            proxies=proxies,
        ),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise Exception(f"OutlookEmail 获取临时邮箱邮件失败: {str(data)[:200]}")
    return pick_list(data)


def message_received_at(message: Any) -> Optional[float]:
    """Return an email's received time as a Unix timestamp when available."""
    if not isinstance(message, dict):
        return None

    for key in (
        "timestamp",
        "received_at",
        "receivedAt",
        "receivedDateTime",
        "date",
        "created_at",
        "createdAt",
    ):
        raw_value = message.get(key)
        if raw_value is None or isinstance(raw_value, bool):
            continue

        if isinstance(raw_value, (int, float)):
            timestamp = float(raw_value)
        else:
            value = str(raw_value or "").strip()
            if not value:
                continue
            try:
                timestamp = float(value)
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    try:
                        parsed = parsedate_to_datetime(value)
                    except (TypeError, ValueError, OverflowError):
                        continue
                timestamp = parsed.timestamp()

        # Some temp-mail APIs expose Unix milliseconds instead of seconds.
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        if timestamp > 0:
            return timestamp
    return None


def mail_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = []
    for key in ("body_preview", "body", "text", "content", "snippet", "intro"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
        elif isinstance(value, dict):
            content = value.get("content") or value.get("text")
            if isinstance(content, str) and content.strip():
                parts.append(content)
    html_value = message.get("html")
    if isinstance(html_value, str):
        parts.append(strip_html(html_value))
    elif isinstance(html_value, list):
        for item in html_value:
            if isinstance(item, str):
                parts.append(strip_html(item))
    return "\n".join(parts)


def sender_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    sender = message.get("from") or message.get("sender") or ""
    if isinstance(sender, str):
        return sender
    if isinstance(sender, dict):
        email_address = sender.get("emailAddress")
        if isinstance(email_address, dict):
            return str(email_address.get("address") or email_address.get("name") or "")
        return str(sender.get("address") or sender.get("email") or sender.get("name") or "")
    return str(sender or "")


def wait_for_code(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    email: str,
    *,
    api_key: str = "",
    source: str = "accounts",
    web_password: str = "",
    session_cookie: str = "",
    folder: str = "all",
    top: int = 10,
    proxies: Optional[dict] = None,
    timeout: int = 60,
    poll_interval: int = 3,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    min_received_at: Optional[float] = None,
) -> str:
    deadline = time.time() + timeout
    seen_ids: set[str] = set()
    normalized_source = normalize_source(source)
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            if normalized_source == "temp":
                messages = get_temp_messages(
                    http_get,
                    session_factory,
                    api_base,
                    email,
                    web_password=web_password,
                    session_cookie=session_cookie,
                    proxies=proxies,
                )
            else:
                messages = get_messages(
                    http_get,
                    api_base,
                    api_key,
                    email,
                    folder=folder,
                    top=top,
                )
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] OutlookEmail 拉取邮件失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] OutlookEmail 本轮邮件数量: {len(messages)}")
        for message in messages:
            subject = str(message.get("subject", "") or "")
            text = mail_text(message)
            message_id = str(
                message.get("id")
                or message.get("message_id")
                or message.get("internet_message_id")
                or f"{subject}|{text[:120]}"
            )
            if message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            if min_received_at is not None:
                received_at = message_received_at(message)
                if received_at is None:
                    if log_callback:
                        log_callback("[Debug] OutlookEmail 跳过无可用收件时间的邮件")
                    continue
                if received_at <= min_received_at:
                    if log_callback:
                        log_callback(
                            "[Debug] OutlookEmail 跳过提交邮箱前收到的邮件: "
                            f"received_at={received_at:.3f} <= submitted_at={min_received_at:.3f}"
                        )
                    continue
            if log_callback:
                log_callback(f"[Debug] OutlookEmail 收到邮件: {subject} ({sender_text(message)})")
            code = extract_verification_code(text, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] OutlookEmail 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"OutlookEmail 在 {timeout}s 内未收到验证码邮件")
