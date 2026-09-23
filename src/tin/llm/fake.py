"""按脚本返回固定响应的 provider。所有测试走它，不依赖网络、不依赖密钥。

它同时是 provider 抽象的第一个消费者：如果 FakeProvider 和 AnthropicProvider 能
在同一份业务代码下互换，那么将来换成本地 30B 也换得动。
"""

from dataclasses import replace

from tin.llm.base import STATUS_OK, LlmError, LlmRequest, LlmResponse


def ok(data: dict, *, text: str | None = None, model: str = "fake-model",
       input_tokens: int = 1000, output_tokens: int = 200,
       cache_read_tokens: int = 900, cache_write_tokens: int = 0) -> LlmResponse:
    """造一条成功响应。默认带上缓存命中数，让落库完整性测试有东西可断言。"""
    return LlmResponse(
        status=STATUS_OK, text=text, data=data, model=model, provider="fake",
        stop_reason="end_turn", input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens,
        raw={"fake": True},
    )


class FakeProvider:
    """按预置脚本逐次返回。

    脚本元素可以是：
    - `LlmResponse`：原样返回；
    - `dict`：包装成一条成功响应，`data` 就是它；
    - `Exception` 实例：抛出去（用来测失败路径落库）。

    `requests` 记下每次收到的请求，用来断言「打回重试确实把问题带回去了」。
    """

    name = "fake"

    def __init__(self, script, *, model: str = "fake-model"):
        self.model = model
        self._script = list(script)
        self._cursor = 0
        self.requests: list[LlmRequest] = []

    @property
    def calls(self) -> int:
        return self._cursor

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        if self._cursor >= len(self._script):
            raise LlmError(
                f"FakeProvider 脚本已用尽：第 {self._cursor + 1} 次调用没有预置响应")
        item = self._script[self._cursor]
        self._cursor += 1

        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            item = ok(item, model=self.model)
        if not isinstance(item, LlmResponse):
            raise TypeError(f"FakeProvider 脚本项类型不支持：{type(item).__name__}")
        # provider / model 以实例为准，子类改了 name 就能冒充另一个 provider
        return replace(item, provider=self.name, model=item.model or self.model)
