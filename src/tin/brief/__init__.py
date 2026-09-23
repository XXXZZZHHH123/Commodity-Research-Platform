"""L3 简报层：组装、落库、评审。

落库与评审都只能走 `store.save` / `store.review`——这跟一期把观测入库收口成
`ingest/record.record()` 是同一招：闸门只有变成唯一通路才算数，否则谁绕过去写一次
JSON，防幻觉校验就白做了。
"""
