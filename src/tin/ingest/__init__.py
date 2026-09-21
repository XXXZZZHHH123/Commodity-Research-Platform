"""采集层。

`NotPublished` 放在这里而不是 `runner` 里：原件留证（`raw`）在 GitHub Actions 上运行，
那条路径不应牵连任何数据库模块，而 `runner` 会 import `tin.models`。
"""


class NotPublished(Exception):
    """目标日数据尚未发布或当日非交易日。不是故障，不重试、不告警。"""
