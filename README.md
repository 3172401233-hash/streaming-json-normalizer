# streaming-json-normalizer

Python 流式 JSON 规范化库。第三方数据文件有时是超大 JSON 数组，直接
`json.load` 会吃满内存；本库从字节块（chunk）读取，把外层容器拆成逐条
记录输出，内存占用与单条记录大小相关，而不是整个文件大小。

## 特性

- 支持两种顶层布局：
  - **数组**：`[{"...": 1}, {"...": 2}]`，逐元素输出。
  - **连续对象（JSONL）**：一行一个对象，对象之间允许任意 JSON 空白。
  - `layout="auto"`（默认）根据第一个非空白字节自动选择。
- 字节级增量 UTF-8 解码。多字节字符、字符串转义、对象分隔符跨在两个
  chunk 之间时不会丢字或重复。
- 严格校验字符串、数字、字面量、代理项和 UTF-8 编码。
- 重复对象键直接报错（`DuplicateKeyError`），不做后值覆盖。
- 超过安全整数范围的整数保留为字符串，避免精度静默丢失。
- 限制单条记录字节数与嵌套深度。
- 每条记录成功产出后立即释放已消费字节的缓存。
- 输出稳定的键顺序和紧凑 JSON（无多余空白，非 ASCII 直接输出）。
- 任何错误都报告在整个流上的绝对字节位置（以及行、列）；出错后生成器
  终止，不会继续吐出看似正常的后续记录。

## 安装与环境

纯标准库实现，无第三方依赖，Python 3.8+。

## 快速开始

```python
from streaming_json_normalizer import iter_records, iter_compact_json

# 1) 二进制文件对象
with open("huge.json", "rb") as fh:
    for record in iter_records(fh):
        process(record)

# 2) bytes chunk 的可迭代对象（例如网络分包）
chunks = [b'[{"a":', b' 1},', b' {"b": 2}]']
records = list(iter_records(iter(chunks)))
# [{"a": 1}, {"b": 2}]

# 3) 直接拿紧凑 JSON 文本
for line in iter_compact_json(fh):
    print(line)  # {"a":1}
```

`iter_records` 接收二进制文件对象（带 `read`）或 `bytes` 块的可迭代对象；
`str` 块和空块也会被正确处理（空块跳过）。

## 公共 API

```python
iter_records(
    source,
    layout="auto",              # "auto" | "array" | "sequence"
    max_record_bytes=16 * 1024 * 1024,
    max_depth=100,
)
```

- `iter_records(...)`：逐条约出 Python 对象（`dict` / `list` / `str` /
  `int` / `float` / `bool` / `None`）。
- `iter_compact_json(...)`：逐条约出紧凑 JSON 字符串，参数与上面相同。
- `compact_json(record)`：把单条记录序列化为紧凑 JSON，保持插入键顺序。

## 数字处理规则

- **整数**：先按 JSON 语法完整校验，再用 Python 任意精度整数判断范围。
  - 落在闭区间 `[-2^53 + 1, 2^53 - 1]`（即 JavaScript
    `Number.MIN_SAFE_INTEGER` 到 `Number.MAX_SAFE_INTEGER`）内的整数输出为
    Python `int`。
  - 超出该范围的整数（含 `2^53`、`-2^53`）**保留为字符串**，字符串内容与
    输入字面量完全一致（包含负号），从而避免精度被悄悄截断。
  - 示例：`9007199254740991` -> `int`；`9007199254740992` ->
    `"9007199254740992"`。
- **小数与科学计数法**：只要包含小数点或指数（`e`/`E`）就按 JSON 数字
  校验，然后输出为 Python `float`。它们不适用整数保留规则。
- 非法数字（前导零 `01`、悬空小数点 `1.`、悬空指数 `1e` 等）抛
  `InvalidTokenError`。

## 流式与内存限制

- **内存有界**：读取器只保留“尚未消费的字节”。一条记录完整解析并产出
  后，已消费字节立即从内部缓冲区释放（`release_consumed`），因此处理数万
  条记录时缓冲不会随文件增长。
- **单条记录字节上限** `max_record_bytes`：统计当前记录从起始到已解析位置
  占用的真实字节跨度（含仍在缓冲中的部分，但不含预读的后续记录字节）。
  超限抛 `RecordTooLargeError`。默认 16 MiB。
- **嵌套深度上限** `max_depth`：按容器嵌套层数计算。数组布局下最外层数组
  只是记录容器、不计入单条记录的深度；连续对象布局下顶层值本身即记录。
  超限抛 `NestingLimitError`。默认 100。

## 错误模型

所有错误继承 `StreamParseError`，并携带整个输入流上的绝对位置：

| 异常 | 触发场景 |
| --- | --- |
| `TruncatedInputError` | 流在值、字符串、转义、UTF-8 序列或字面量中途结束 |
| `EncodingError` | 非法或截断的 UTF-8 字节序列、UTF-8 编码的代理项 |
| `InvalidTokenError` | 非法 token、非法转义、孤立高/低代理项、未转义控制字符、尾随数据 |
| `DuplicateKeyError` | 同一对象内出现重复键 |
| `RecordTooLargeError` | 单条记录超过 `max_record_bytes` |
| `NestingLimitError` | 嵌套超过 `max_depth` |

每个异常都有 `offset`（绝对字节偏移）、`line`、`column` 属性。生成器在
首次错误时抛出异常并永久结束，错误之后不会再产出任何记录。

## 运行测试

```bash
python3 -m unittest
```

测试覆盖：单条记录拆到多个 byte 块、跨字符边界、转义字符串、超大整数、
重复键、两种顶层布局、深度限制、记录大小限制、孤立代理项、编码错误、
截断输入和损坏输入。
