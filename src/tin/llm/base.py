"""provider 抽象：请求 / 响应的统一形状。

这一层存在的唯一理由是 SPEC §4.1 那句话——「从外部 API 切到本地 30B 不改一行业务代码」。
所以请求形状只描述所有 provider 都表达得出的东西：稳定的 system 前缀、对话、输出 schema、
缓存断点位置。任何 Anthropic 专有参数（thinking / betas / fallbacks）由各 provider 自己
翻译，不许上浮到这里，否则抽象就漏了。

关于缓存断点：prompt caching 是这个项目的主要成本杠杆（prefix 稳定时缓存读只要 0.1× 价格），
而缓存是「前缀逐字节匹配」——前缀里任何一个字节变了，后面全部作废。因此 `LlmRequest`
在构造时就拒绝把日期 / 时刻 / UUID 写进 system，把易变内容逼到 messages 里去。
`LlmCall.cache_read_tokens` 长期挂 0 就是这条防线破了。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from tin.config import settings

# 一次调用的三种结局，与 LlmCall.status 同一套词表。
# refusal 单列是因为它既不是成功也不是故障：模型能答但拒绝答，重试同一段 prompt 没有意义。
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_REFUSAL = "refusal"
STATUSES = (STATUS_OK, STATUS_ERROR, STATUS_REFUSAL)

# 缓存前缀杀手。命中即拒绝构造请求——与其上线后对着一张全是 0 的 cache_read_tokens
# 排查「今天是 X 年 X 月 X 日」写在哪，不如在这里就不让它进去。
_VOLATILE = (
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "日期"),
    (re.compile(r"\d{1,2}:\d{2}:\d{2}"), "时刻"),
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"), "UUID"),
)
# Anthropic 侧一次请求最多 4 个断点；本地端点没有这个概念，取小的那个当统一上限。
MAX_CACHE_BREAKPOINTS = 4


class LlmError(RuntimeError):
    """调用失败。落库之后才抛，所以拿到这个异常时 llm_calls 里一定已经有一行 error。"""


class LlmRefused(LlmError):
    """模型拒答。不是故障，重试同一段 prompt 也没用，交由上层降级处理。"""


class ProviderUnavailable(LlmError):
    """provider 的依赖没装好或没配置。与「调用失败」区分开，因为它压根没发出请求。"""


@dataclass(frozen=True)
class SystemBlock:
    """system 前缀的一段。`cache_breakpoint=True` 表示「缓存到这一段结束为止」。

    断点要打在「稳定内容与易变内容的交界」上：越靠前的内容越稳定，断点之后的东西
    每次变都不影响前面已缓存的部分。
    """

    text: str
    cache_breakpoint: bool = False

    def dump(self) -> dict:
        return {"text": self.text, "cache_breakpoint": self.cache_breakpoint}


@dataclass(frozen=True)
class Message:
    role: str  # user / assistant
    content: str

    def __post_init__(self):
        if self.role not in ("user", "assistant"):
            raise ValueError(f"消息角色只能是 user/assistant，收到 {self.role!r}")

    def dump(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class LlmRequest:
    """一次调用的全部输入。原样进 `LlmCall.request`，是「为什么今天这么说」的前半句。"""

    purpose: str                       # compose_brief / distill / extract_playbook / map_columns
    messages: tuple[Message, ...]
    system: tuple[SystemBlock, ...] = ()
    # 输出 JSON Schema。绝不解析自由文本——没有 schema 的调用只用于纯文本场景。
    output_schema: dict | None = None
    schema_name: str = "output"
    # 思考与正文共用这个上限，给小了会在半句话处截断；默认跟着配置走
    max_tokens: int = field(default_factory=lambda: settings.llm_max_tokens)
    brief_id: int | None = None
    # 逃生口：确实需要把易变内容放进 system 时显式声明，代价是这次调用不会命中缓存。
    allow_volatile_system: bool = False

    def __post_init__(self):
        if not self.messages:
            raise ValueError("请求至少要有一条消息")
        if self.messages[0].role != "user":
            raise ValueError("第一条消息必须是 user")
        breakpoints = sum(1 for b in self.system if b.cache_breakpoint)
        if breakpoints > MAX_CACHE_BREAKPOINTS:
            raise ValueError(f"缓存断点最多 {MAX_CACHE_BREAKPOINTS} 个，收到 {breakpoints} 个")
        if not self.allow_volatile_system:
            for block in self.system:
                for pattern, what in _VOLATILE:
                    if pattern.search(block.text):
                        raise ValueError(
                            f"system 前缀里出现了{what}，会让 prompt 缓存每次失效；"
                            f"把它挪到 messages 里去，或显式传 allow_volatile_system=True"
                        )

    def followup(self, assistant_text: str, user_text: str) -> "LlmRequest":
        """在同一段前缀上追加一轮对话。打回重试走这条路，前缀不变所以缓存照样命中。"""
        extra = (Message("assistant", assistant_text), Message("user", user_text))
        return LlmRequest(
            purpose=self.purpose, messages=self.messages + extra, system=self.system,
            output_schema=self.output_schema, schema_name=self.schema_name,
            max_tokens=self.max_tokens, brief_id=self.brief_id,
            allow_volatile_system=self.allow_volatile_system,
        )

    def dump(self) -> dict:
        return {
            "purpose": self.purpose,
            "system": [b.dump() for b in self.system],
            "messages": [m.dump() for m in self.messages],
            "output_schema": self.output_schema,
            "schema_name": self.schema_name,
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True)
class LlmResponse:
    """一次调用的全部输出。原样进 `LlmCall.response`，是「为什么今天这么说」的后半句。"""

    status: str
    text: str | None = None
    data: dict | None = None           # output_schema 约束下解析出来的结构化结果
    model: str = ""
    provider: str = ""
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # 这两列是成本的主要变量，provider 拿不到就留 None，不要填 0 冒充「没命中」。
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)
    call_id: int | None = None         # 落库之后由 audit 层回填

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"调用状态只能是 {'/'.join(STATUSES)}，收到 {self.status!r}")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def dump(self) -> dict:
        return {
            "status": self.status, "text": self.text, "data": self.data,
            "model": self.model, "provider": self.provider, "stop_reason": self.stop_reason,
            "error": self.error, "raw": self.raw,
        }


@runtime_checkable
class LlmProvider(Protocol):
    """业务代码只依赖这三样东西。换 provider = 换一个实现了它的对象，别的都不动。"""

    name: str   # anthropic / openai_compatible / fake，与 LlmCall.provider 对齐
    model: str

    def complete(self, request: LlmRequest) -> LlmResponse:
        """发起一次调用。

        约定：传输层失败可以抛异常（audit 层会落库成 error 再抛）；
        拿到了响应但内容不可用（schema 解析失败等）应返回 status=error 的响应，
        这样原始响应体还能进库——排查「模型到底吐了什么」全靠它。
        """
        ...


def usage_of(response: LlmResponse) -> dict:
    """抽出四个计数，供落库与成本统计复用。"""
    return {
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cache_read_tokens": response.cache_read_tokens,
        "cache_write_tokens": response.cache_write_tokens,
    }


def dump_object(obj: Any) -> dict:
    """把 SDK 的响应对象转成可入库的 dict，转不了就退化成字符串，绝不让落库失败。"""
    for attr in ("to_dict", "model_dump"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn(mode="json") if attr == "model_dump" else fn()
            except TypeError:
                try:
                    return fn()
                except Exception:  # noqa: BLE001 — 落库比精确序列化重要
                    break
            except Exception:  # noqa: BLE001
                break
    if isinstance(obj, dict):
        return obj
    return {"repr": repr(obj)}
