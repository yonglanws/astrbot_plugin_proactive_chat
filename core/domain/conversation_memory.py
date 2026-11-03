"""开放话题、话题账本与轻量用户画像的纯逻辑。

长期事实记忆由 LivingMemory 负责；本模块只维护“最近聊到哪、有什么还没
闭环”的短期工作记忆，供主动消息生成时自然跟进，避免每次重新查户口。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable, Mapping

PLAN_PATTERN = re.compile(
    r"(明天|后天|大后天|下周[一二三四五六日天]?|周末|今晚|今天晚上|"
    r"等会|等会儿|待会|待会儿|晚点|回头|下次|一会儿|月底|下个月|"
    r"下周|开学|放假|放假后)"
)
EVENT_PATTERN = re.compile(
    r"(面试|考试|上课|开会|上班|下班|到家|回家|吃饭|睡觉|看病|医院|"
    r"生日|旅行|出差|搬家|比赛|演出|约会|开学|作业|项目|练琴|健身|"
    r"看电影|看剧|打游戏|排位|抽卡|直播|发布会)"
)
PROMISE_PATTERN = re.compile(
    r"(我(会|要|去|来|给你|帮你|答应|准备|打算)|等我|回头|晚点|"
    r"下次|到时候|说好|约好)"
)
QUESTION_PATTERN = re.compile(
    r"[?？]|吗|呢|怎么|为什么|什么|哪儿|哪|如何|是否|要不要|好不好"
)
_EMOJI_RE = re.compile(
    "[" 
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "]"
)
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_]{1,15}")
_STOPWORDS = {
    "今天",
    "明天",
    "后天",
    "现在",
    "刚才",
    "然后",
    "就是",
    "这个",
    "那个",
    "什么",
    "怎么",
    "可以",
    "还是",
    "但是",
    "因为",
    "所以",
    "如果",
    "真的",
    "有点",
    "感觉",
    "觉得",
    "我们",
    "你们",
    "他们",
    "自己",
    "一个",
    "一下",
    "已经",
    "没有",
    "不是",
    "时候",
    "知道",
    "想要",
    "可能",
    "应该",
    "有点",
    "哈哈",
    "嗯嗯",
}


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def _keywords(text: str, limit: int = 6) -> list[str]:
    seen: list[str] = []
    for token in _TOKEN_RE.findall(_clean(text)):
        if token in _STOPWORDS or token in seen:
            continue
        seen.append(token)
        if len(seen) >= limit:
            break
    return seen


def _fingerprint(kind: str, text: str) -> str:
    normalized = re.sub(r"[\s，,。.!！?？~～…、；;：:\"'“”‘’（）()]+", "", text)
    return f"{kind}:{normalized[:48]}"


def extract_loop_candidates(
    text: str,
    *,
    role: str,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """从一条消息中提取值得后续跟进的开放话题。"""
    timestamp = float(now if now is not None else time.time())
    content = _clean(text)
    if not content:
        return []

    loops: list[dict[str, Any]] = []
    due_hint = ""
    plan_match = PLAN_PATTERN.search(content)
    if plan_match:
        due_hint = plan_match.group(1)

    if role == "user":
        future_intent = bool(
            plan_match
            or re.search(r"(要|有|得|去|准备|即将|快要|马上|还没|回头|等会)", content)
        )
        if future_intent and (plan_match or EVENT_PATTERN.search(content)):
            kind = "plan" if (plan_match or PROMISE_PATTERN.search(content)) else "event"
            loops.append(
                {
                    "kind": kind,
                    "text": content[:160],
                    "keywords": _keywords(content),
                    "due_hint": due_hint,
                    "created_at": timestamp,
                    "last_mentioned": timestamp,
                    "status": "open",
                    "fingerprint": _fingerprint(kind, content),
                    "source": "user",
                }
            )

    if role == "assistant" and QUESTION_PATTERN.search(content):
        loops.append(
            {
                "kind": "question",
                "text": content[:160],
                "keywords": _keywords(content),
                "due_hint": "",
                "created_at": timestamp,
                "last_mentioned": timestamp,
                "status": "open",
                "fingerprint": _fingerprint("question", content),
                "source": "assistant",
            }
        )
    return loops


def loop_matches_text(loop: Mapping[str, Any], text: str) -> bool:
    """判断用户的新消息是否回应/延续了某个开放话题。"""
    content = _clean(text)
    if not content:
        return False
    for keyword in loop.get("keywords", []) or []:
        if isinstance(keyword, str) and len(keyword) >= 2 and keyword in content:
            return True
    due_hint = str(loop.get("due_hint") or "")
    return bool(due_hint and due_hint in content)


def close_answered_questions(
    loops: list[dict[str, Any]],
    *,
    after_ts: float,
    quality: str,
    now: float,
) -> None:
    """用户给出实质回复后，关闭更早的助手提问型开放话题。"""
    if quality not in {"substantive", "question", "media"}:
        return
    for loop in loops:
        if loop.get("kind") != "question" or loop.get("status") != "open":
            continue
        if float(loop.get("created_at", 0) or 0) <= after_ts:
            loop["status"] = "closed"
            loop["closed_at"] = now
            loop["close_reason"] = "user_replied"


def format_open_loops(loops: Iterable[Mapping[str, Any]], *, now: float) -> str:
    """生成适合注入提示词的开放话题块。"""
    lines: list[str] = []
    for loop in loops:
        if loop.get("status") != "open":
            continue
        text = _clean(str(loop.get("text") or ""))
        if not text:
            continue
        kind = str(loop.get("kind") or "topic")
        label = {
            "plan": "计划",
            "event": "事件",
            "question": "未回答的问题",
            "topic": "话题",
        }.get(kind, "话题")
        age_days = max(0.0, (now - float(loop.get("created_at", 0) or 0)) / 86400)
        if age_days >= 1:
            age = f"，约 {int(age_days)} 天前"
        else:
            age = ""
        lines.append(f"- [{label}] {text}{age}")
    if not lines:
        return ""
    return (
        "<topic_ledger>\n"
        "以下是你和对方最近还没有闭环的话题。可以自然跟进其中一条，"
        "但不要逐条追问，也不要表现得像在核对清单：\n"
        + "\n".join(lines[:5])
        + "\n</topic_ledger>"
    )


def update_profile(
    profile: dict[str, Any],
    text: str,
    *,
    quality: str,
    now: float,
) -> dict[str, Any]:
    """用指数移动平均维护轻量用户画像。"""
    content = _clean(text)
    length = len(content)
    has_question = 1.0 if QUESTION_PATTERN.search(content) else 0.0
    has_emoji = 1.0 if _EMOJI_RE.search(content) else 0.0

    count = int(profile.get("message_count", 0) or 0) + 1
    alpha = 0.2 if count <= 5 else 0.08

    def _blend(key: str, value: float) -> float:
        old = float(profile.get(key, value) or value)
        return old * (1 - alpha) + value * alpha

    profile["message_count"] = count
    profile["avg_length"] = round(_blend("avg_length", float(length)), 2)
    profile["question_ratio"] = round(_blend("question_ratio", has_question), 4)
    profile["emoji_ratio"] = round(_blend("emoji_ratio", has_emoji), 4)
    profile["last_quality"] = quality
    profile["updated_at"] = now
    return profile


def profile_summary(profile: Mapping[str, Any]) -> str:
    if not profile or not int(profile.get("message_count", 0) or 0):
        return ""
    avg_length = float(profile.get("avg_length", 0) or 0)
    style = "偏长" if avg_length >= 35 else ("偏短" if avg_length <= 10 else "中等")
    return (
        f"对方消息平均 {avg_length:.0f} 字，表达{style}；"
        f"提问比例 {float(profile.get('question_ratio', 0) or 0) * 100:.0f}%，"
        f"表情比例 {float(profile.get('emoji_ratio', 0) or 0) * 100:.0f}%。"
    )


def prune_loops(
    loops: list[dict[str, Any]],
    *,
    now: float,
    ttl_days: float,
    max_open: int,
) -> list[dict[str, Any]]:
    """关闭过期话题，并裁剪开放话题数量。"""
    ttl_seconds = max(1.0, float(ttl_days)) * 86400
    for loop in loops:
        if loop.get("status") == "open" and now - float(
            loop.get("created_at", 0) or 0
        ) > ttl_seconds:
            loop["status"] = "closed"
            loop["closed_at"] = now
            loop["close_reason"] = "ttl"
    open_loops = [loop for loop in loops if loop.get("status") == "open"]
    closed_loops = [loop for loop in loops if loop.get("status") != "open"]
    open_loops.sort(key=lambda item: float(item.get("created_at", 0) or 0), reverse=True)
    closed_loops.sort(
        key=lambda item: float(item.get("closed_at", 0) or 0), reverse=True
    )
    keep_open = max(0, int(max_open))
    return open_loops[:keep_open] + closed_loops[: max(4, keep_open)]
