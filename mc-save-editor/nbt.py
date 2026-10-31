"""Minecraft NBT (Named Binary Tag) 读写与 SNBT 解析。

对应 Minecraft 1.21.1 源码:
  mc_src/net/minecraft/nbt/NbtIo.java
  mc_src/net/minecraft/nbt/TagTypes.java
  mc_src/net/minecraft/nbt/*Tag.java

格式要点:
  - 大端序; 根 tag 带名字 (通常为 "")
  - 字符串使用 Java DataInput.readUTF 的 "modified UTF-8" (NUL -> C0 80,
    增补字符(BMP 外)编码为代理对, 每个代理 3 字节)
  - .dat / level.dat 为 gzip 压缩; region 区块为 zlib
  - TAG_List: [类型 u8][长度 i32][元素 payload...]
"""

from __future__ import annotations

import gzip
import io
import re
import struct
import zlib
from typing import Any, Iterable, Optional

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

TAG_NAMES = {
    0: "TAG_End",
    1: "TAG_Byte",
    2: "TAG_Short",
    3: "TAG_Int",
    4: "TAG_Long",
    5: "TAG_Float",
    6: "TAG_Double",
    7: "TAG_Byte_Array",
    8: "TAG_String",
    9: "TAG_List",
    10: "TAG_Compound",
    11: "TAG_Int_Array",
    12: "TAG_Long_Array",
}

MASK64 = 0xFFFFFFFFFFFFFFFF
MAX_DEPTH = 512


class NbtError(Exception):
    """NBT 数据格式错误。"""


class Byte(int):
    """TAG_Byte (1 字节有符号)。"""

    tag_id = TAG_BYTE


class Short(int):
    """TAG_Short (2 字节有符号)。"""

    tag_id = TAG_SHORT


class Int(int):
    """TAG_Int (4 字节有符号)。"""

    tag_id = TAG_INT


class Long(int):
    """TAG_Long (8 字节有符号)。"""

    tag_id = TAG_LONG


class Float(float):
    """TAG_Float (4 字节 IEEE754)。"""

    tag_id = TAG_FLOAT


class Double(float):
    """TAG_Double (8 字节 IEEE754)。"""

    tag_id = TAG_DOUBLE


class ByteArray:
    tag_id = TAG_BYTE_ARRAY

    def __init__(self, data: bytes | bytearray | Iterable[int] = b""):
        if isinstance(data, (bytes, bytearray)):
            self.data = bytes(data)
        else:
            self.data = bytes(int(v) & 0xFF for v in data)

    def __len__(self) -> int:
        return len(self.data)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ByteArray) and self.data == other.data

    def __hash__(self) -> int:
        return hash(self.data)

    def __repr__(self) -> str:
        return f"ByteArray({list(self.data)})"

    def to_list(self) -> list[int]:
        """把字节按有符号整数返回 (Java byte 语义)。"""
        return [v - 256 if v >= 128 else v for v in self.data]


class IntArray(list):
    tag_id = TAG_INT_ARRAY


class LongArray(list):
    tag_id = TAG_LONG_ARRAY


class ListTag(list):
    """TAG_List。保留元素类型 (空列表时需要显式类型, 否则写 TAG_End)。"""

    tag_id = TAG_LIST

    def __init__(self, items: Iterable[Any] = (), elem_type: Optional[int] = None):
        super().__init__(items)
        self.elem_type = elem_type

    # Java 风格的取值辅助 (越界返回默认值, 与 CompoundTag.getList 类似)
    def _get(self, index: int, default: Any) -> Any:
        try:
            return self[index]
        except IndexError:
            return default

    def get_compound(self, index: int) -> "CompoundTag":
        v = self._get(index, None)
        if isinstance(v, CompoundTag):
            return v
        return CompoundTag()

    def get_list(self, index: int) -> "ListTag":
        v = self._get(index, None)
        if isinstance(v, ListTag):
            return v
        return ListTag()

    def get_string(self, index: int) -> str:
        v = self._get(index, "")
        return v if isinstance(v, str) else ""

    def get_int(self, index: int) -> int:
        v = self._get(index, 0)
        return int(v) if isinstance(v, (int, float)) else 0

    def get_short(self, index: int) -> int:
        return self.get_int(index)

    def copy(self) -> "ListTag":
        return ListTag([copy_tag(v) for v in self], self.elem_type)

    def inferred_elem_type(self) -> int:
        if self.elem_type:
            return self.elem_type
        if not self:
            return TAG_END
        return tag_id_of(self[0])

    def __repr__(self) -> str:
        return f"ListTag({list.__repr__(self)}, elem_type={self.elem_type})"


class CompoundTag(dict):
    """TAG_Compound，保持插入顺序 (与写入顺序一致)。"""

    tag_id = TAG_COMPOUND

    def put(self, key: str, value: Any) -> Any:
        self[key] = value
        return value

    # ---- Java 风格 getter (容忍类型转换, 缺失返回默认值) ----
    def _get(self, key: str, default: Any) -> Any:
        return self.get(key, default)

    def get_compound(self, key: str) -> "CompoundTag":
        v = self.get(key)
        return v if isinstance(v, CompoundTag) else CompoundTag()

    def get_list(self, key: str) -> "ListTag":
        v = self.get(key)
        return v if isinstance(v, ListTag) else ListTag()

    def get_string(self, key: str, default: str = "") -> str:
        v = self.get(key)
        return v if isinstance(v, str) else default

    def get_byte(self, key: str, default: int = 0) -> int:
        v = self.get(key)
        return int(v) if isinstance(v, int) else default

    def get_short(self, key: str, default: int = 0) -> int:
        return self.get_byte(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.get(key)
        return int(v) if isinstance(v, int) else default

    def get_long(self, key: str, default: int = 0) -> int:
        return self.get_int(key, default)

    def get_float(self, key: str, default: float = 0.0) -> float:
        v = self.get(key)
        return float(v) if isinstance(v, (int, float)) else default

    def get_double(self, key: str, default: float = 0.0) -> float:
        return self.get_float(key, default)

    def get_boolean(self, key: str, default: bool = False) -> bool:
        v = self.get(key)
        return bool(v) if isinstance(v, int) else default

    def get_byte_array(self, key: str) -> ByteArray:
        v = self.get(key)
        return v if isinstance(v, ByteArray) else ByteArray()

    def get_int_array(self, key: str) -> IntArray:
        v = self.get(key)
        return v if isinstance(v, IntArray) else IntArray()

    def get_long_array(self, key: str) -> LongArray:
        v = self.get(key)
        return v if isinstance(v, LongArray) else LongArray()

    def contains(self, key: str, tag_type: Optional[int] = None) -> bool:
        if key not in self:
            return False
        return tag_type is None or tag_id_of(self[key]) == tag_type

    def copy(self) -> "CompoundTag":
        out = CompoundTag()
        for k, v in self.items():
            out[k] = copy_tag(v)
        return out

    def __repr__(self) -> str:
        return f"CompoundTag({dict.__repr__(self)})"


# --------------------------------------------------------------------------- #
# 类型推断与拷贝
# --------------------------------------------------------------------------- #

def tag_id_of(value: Any) -> int:
    if value is None:
        return TAG_END
    if isinstance(value, bool):
        return TAG_BYTE
    if isinstance(value, Byte):
        return TAG_BYTE
    if isinstance(value, Short):
        return TAG_SHORT
    if isinstance(value, Int):
        return TAG_INT
    if isinstance(value, Long):
        return TAG_LONG
    if isinstance(value, Float):
        return TAG_FLOAT
    if isinstance(value, Double):
        return TAG_DOUBLE
    if isinstance(value, ByteArray):
        return TAG_BYTE_ARRAY
    if isinstance(value, str):
        return TAG_STRING
    if isinstance(value, ListTag):
        return TAG_LIST
    if isinstance(value, CompoundTag):
        return TAG_COMPOUND
    if isinstance(value, IntArray):
        return TAG_INT_ARRAY
    if isinstance(value, LongArray):
        return TAG_LONG_ARRAY
    # 便捷推断
    if isinstance(value, int):
        return TAG_INT if -(2 ** 31) <= value < 2 ** 31 else TAG_LONG
    if isinstance(value, float):
        return TAG_DOUBLE
    if isinstance(value, (bytes, bytearray)):
        return TAG_BYTE_ARRAY
    if isinstance(value, dict):
        return TAG_COMPOUND
    if isinstance(value, (list, tuple)):
        return TAG_LIST
    raise NbtError(f"无法推断 NBT 类型: {type(value)!r}")


def copy_tag(value: Any) -> Any:
    tid = tag_id_of(value)
    if tid in (TAG_END, TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG):
        return value
    if tid in (TAG_FLOAT, TAG_DOUBLE):
        return value
    if tid == TAG_STRING:
        return value
    if tid == TAG_BYTE_ARRAY:
        return ByteArray(bytes(value.data))
    if tid == TAG_INT_ARRAY:
        return IntArray(int(v) for v in value)
    if tid == TAG_LONG_ARRAY:
        return LongArray(int(v) for v in value)
    if tid == TAG_LIST:
        return value.copy() if isinstance(value, ListTag) else ListTag(copy_tag(v) for v in value)
    if tid == TAG_COMPOUND:
        return value.copy() if isinstance(value, CompoundTag) else CompoundTag(
            {str(k): copy_tag(v) for k, v in value.items()}
        )
    raise NbtError(f"无法拷贝 NBT 类型: {tid}")


def to_plain(value: Any) -> Any:
    """转换为普通 Python 对象 (便于 JSON 输出)。"""
    tid = tag_id_of(value)
    if tid in (TAG_END,):
        return None
    if tid in (TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG, TAG_FLOAT, TAG_DOUBLE):
        return value
    if tid == TAG_STRING:
        return value
    if tid == TAG_BYTE_ARRAY:
        return list(value.data)
    if tid in (TAG_INT_ARRAY, TAG_LONG_ARRAY):
        return list(value)
    if tid == TAG_LIST:
        return [to_plain(v) for v in value]
    if tid == TAG_COMPOUND:
        return {k: to_plain(v) for k, v in value.items()}
    raise NbtError(f"未知 NBT 类型: {tid}")


# --------------------------------------------------------------------------- #
# Java modified UTF-8
# --------------------------------------------------------------------------- #

def _decode_java_utf(data: bytes) -> str:
    units: list[int] = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b < 0x80:
            units.append(b)
            i += 1
        elif (b & 0xE0) == 0xC0 and i + 1 < n:
            b2 = data[i + 1]
            units.append(((b & 0x1F) << 6) | (b2 & 0x3F))
            i += 2
        elif (b & 0xF0) == 0xE0 and i + 2 < n:
            b2, b3 = data[i + 1], data[i + 2]
            units.append(((b & 0x0F) << 12) | ((b2 & 0x3F) << 6) | (b3 & 0x3F))
            i += 3
        else:
            raise NbtError(f"非法 modified UTF-8 字节 0x{b:02X}")
    # 组合代理对 -> Python 码点; 孤代理用 surrogatepass 保留
    raw = b"".join(struct.pack(">H", u & 0xFFFF) for u in units)
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("utf-16-be", errors="surrogatepass")


def _encode_java_utf(s: str) -> bytes:
    out = bytearray()
    for ch in s:
        cp = ord(ch)
        if cp == 0:
            out += b"\xC0\x80"
        elif cp < 0x80:
            out.append(cp)
        elif cp < 0x800:
            out.append(0xC0 | (cp >> 6))
            out.append(0x80 | (cp & 0x3F))
        elif cp < 0x10000:
            out.append(0xE0 | (cp >> 12))
            out.append(0x80 | ((cp >> 6) & 0x3F))
            out.append(0x80 | (cp & 0x3F))
        else:
            cp -= 0x10000
            hi = 0xD800 + (cp >> 10)
            lo = 0xDC00 + (cp & 0x3FF)
            for u in (hi, lo):
                out.append(0xE0 | (u >> 12))
                out.append(0x80 | ((u >> 6) & 0x3F))
                out.append(0x80 | (u & 0x3F))
    if len(out) > 0xFFFF:
        raise NbtError(f"NBT 字符串超过 65535 字节 (Java readUTF 限制): {len(out)}")
    return bytes(out)


# --------------------------------------------------------------------------- #
# 二进制读取
# --------------------------------------------------------------------------- #

class _Reader:
    __slots__ = ("data", "pos", "limit")

    def __init__(self, data: bytes, limit: int = 512 * 1024 * 1024):
        self.data = data
        self.pos = 0
        self.limit = limit

    def read(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise NbtError(f"NBT 数据截断: 需要 {n} 字节, 剩余 {len(self.data) - self.pos}")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self) -> int:
        return self.read(1)[0]

    def i8(self) -> int:
        return struct.unpack(">b", self.read(1))[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.read(2))[0]

    def i16(self) -> int:
        return struct.unpack(">h", self.read(2))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack(">q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack(">f", self.read(4))[0]

    def f64(self) -> float:
        return struct.unpack(">d", self.read(8))[0]

    def utf(self) -> str:
        length = self.u16()
        return _decode_java_utf(self.read(length))

    def name(self) -> str:
        return self.utf()

    def payload(self, tid: int, depth: int = 0) -> Any:
        if depth > MAX_DEPTH:
            raise NbtError("NBT 嵌套过深")
        if tid == TAG_END:
            return None
        if tid == TAG_BYTE:
            return Byte(self.i8())
        if tid == TAG_SHORT:
            return Short(self.i16())
        if tid == TAG_INT:
            return Int(self.i32())
        if tid == TAG_LONG:
            return Long(self.i64())
        if tid == TAG_FLOAT:
            return Float(self.f32())
        if tid == TAG_DOUBLE:
            return Double(self.f64())
        if tid == TAG_BYTE_ARRAY:
            n = self.i32()
            if n < 0 or n > len(self.data):
                raise NbtError(f"TAG_Byte_Array 长度非法: {n}")
            return ByteArray(self.read(n))
        if tid == TAG_STRING:
            return self.utf()
        if tid == TAG_LIST:
            elem_type = self.u8()
            n = self.i32()
            if n < 0:
                raise NbtError(f"TAG_List 长度非法: {n}")
            # 粗略防呆: 每个元素至少 1 字节
            if n > len(self.data):
                raise NbtError(f"TAG_List 长度过大: {n}")
            items = [self.payload(elem_type, depth + 1) for _ in range(n)]
            return ListTag(items, elem_type)
        if tid == TAG_COMPOUND:
            out = CompoundTag()
            while True:
                child_tid = self.u8()
                if child_tid == TAG_END:
                    return out
                key = self.name()
                out[key] = self.payload(child_tid, depth + 1)
        if tid == TAG_INT_ARRAY:
            n = self.i32()
            if n < 0 or n * 4 > len(self.data):
                raise NbtError(f"TAG_Int_Array 长度非法: {n}")
            return IntArray(self.i32() for _ in range(n))
        if tid == TAG_LONG_ARRAY:
            n = self.i32()
            if n < 0 or n * 8 > len(self.data):
                raise NbtError(f"TAG_Long_Array 长度非法: {n}")
            return LongArray(self.i64() for _ in range(n))
        raise NbtError(f"未知 NBT tag 类型: {tid}")


def read_nbt(data: bytes, decompress: str | None = "auto") -> tuple[str, Any]:
    """读取 NBT 二进制。返回 (根名字, 根 tag)。

    decompress: "auto" 自动识别 gzip/zlib; "gzip"; "zlib"; None 表示未压缩。
    """
    if decompress == "auto":
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        elif data[:2] in (b"\x78\x01", b"\x78\x9c", b"\x78\xda"):
            data = zlib.decompress(data)
    elif decompress == "gzip":
        data = gzip.decompress(data)
    elif decompress == "zlib":
        data = zlib.decompress(data)
    r = _Reader(data)
    tid = r.u8()
    if tid == TAG_END:
        return "", None
    name = r.name()
    tag = r.payload(tid)
    return name, tag


def read_nbt_file(path: str) -> tuple[str, Any]:
    with open(path, "rb") as f:
        return read_nbt(f.read())


# --------------------------------------------------------------------------- #
# 二进制写入
# --------------------------------------------------------------------------- #

def _pack_i64(value: int) -> bytes:
    return struct.pack(">Q", int(value) & MASK64)


class _Writer:
    __slots__ = ("buf",)

    def __init__(self):
        self.buf = io.BytesIO()

    def u8(self, v: int) -> None:
        self.buf.write(bytes((v & 0xFF,)))

    def i8(self, v: int) -> None:
        self.buf.write(struct.pack(">b", _wrap_signed(v, 8)))

    def i16(self, v: int) -> None:
        self.buf.write(struct.pack(">h", _wrap_signed(v, 16)))

    def i32(self, v: int) -> None:
        self.buf.write(struct.pack(">i", _wrap_signed(v, 32)))

    def i64(self, v: int) -> None:
        self.buf.write(_pack_i64(v))

    def f32(self, v: float) -> None:
        self.buf.write(struct.pack(">f", float(v)))

    def f64(self, v: float) -> None:
        self.buf.write(struct.pack(">d", float(v)))

    def utf(self, s: str) -> None:
        b = _encode_java_utf(s)
        self.buf.write(struct.pack(">H", len(b)))
        self.buf.write(b)

    def payload(self, value: Any) -> None:
        tid = tag_id_of(value)
        if tid == TAG_END:
            return
        if tid == TAG_BYTE:
            self.i8(int(value))
        elif tid == TAG_SHORT:
            self.i16(int(value))
        elif tid == TAG_INT:
            self.i32(int(value))
        elif tid == TAG_LONG:
            self.i64(int(value))
        elif tid == TAG_FLOAT:
            self.f32(float(value))
        elif tid == TAG_DOUBLE:
            self.f64(float(value))
        elif tid == TAG_BYTE_ARRAY:
            data = value.data if isinstance(value, ByteArray) else bytes(value)
            self.i32(len(data))
            self.buf.write(data)
        elif tid == TAG_STRING:
            self.utf(str(value))
        elif tid == TAG_LIST:
            items = value if isinstance(value, ListTag) else list(value)
            elem_type = value.elem_type if isinstance(value, ListTag) else None
            if not elem_type:
                elem_type = tag_id_of(items[0]) if items else TAG_END
            if items and elem_type == TAG_END:
                raise NbtError("非空 TAG_List 的元素类型不能是 TAG_End")
            self.u8(elem_type)
            self.i32(len(items))
            for item in items:
                child_tid = tag_id_of(item)
                if child_tid != elem_type:
                    # 与游戏一致: 列表元素类型必须统一
                    raise NbtError(
                        f"TAG_List 元素类型不一致: 期望 {TAG_NAMES.get(elem_type)}, 实际 {TAG_NAMES.get(child_tid)}"
                    )
                self.payload(item)
        elif tid == TAG_COMPOUND:
            for key, child in value.items():
                if not isinstance(key, str):
                    raise NbtError(f"TAG_Compound 键必须是字符串: {key!r}")
                self.u8(tag_id_of(child))
                if tag_id_of(child) == TAG_END:
                    raise NbtError(f"TAG_Compound 键 {key!r} 的值为 TAG_End")
                self.utf(key)
                self.payload(child)
            self.u8(TAG_END)
        elif tid == TAG_INT_ARRAY:
            self.i32(len(value))
            for v in value:
                self.i32(int(v))
        elif tid == TAG_LONG_ARRAY:
            self.i32(len(value))
            for v in value:
                self.i64(int(v))
        else:
            raise NbtError(f"无法写入 NBT 类型: {tid}")


def _wrap_signed(value: int, bits: int) -> int:
    mask = (1 << bits) - 1
    v = int(value) & mask
    if v >= 1 << (bits - 1):
        v -= 1 << bits
    return v


def write_nbt(tag: Any, name: str = "", compression: str | None = None) -> bytes:
    """序列化 NBT。compression: None / "gzip" / "zlib"。"""
    w = _Writer()
    tid = tag_id_of(tag)
    if tid == TAG_END:
        w.u8(TAG_END)
        raw = w.buf.getvalue()
    else:
        w.u8(tid)
        w.utf(name)
        w.payload(tag)
        raw = w.buf.getvalue()
    if compression == "gzip":
        return gzip.compress(raw, mtime=0)
    if compression == "zlib":
        return zlib.compress(raw, 6)
    return raw


def write_nbt_file(path: str, tag: Any, name: str = "", compression: str = "gzip") -> None:
    with open(path, "wb") as f:
        f.write(write_nbt(tag, name, compression))


# --------------------------------------------------------------------------- #
# SNBT (Stringified NBT) 解析与输出
# --------------------------------------------------------------------------- #

_INT_RE = re.compile(r"([-+]?\d+)([bBsSlL])?\Z")
_FLOAT_RE = re.compile(
    r"([-+]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[eE][-+]?\d+)?)([fFdD])?\Z"
)
_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9._+-]+\Z")
_SAFE_STRING_RE = re.compile(r"[A-Za-z0-9._+:/\[\]{}()-]+\Z")


class SnbtError(NbtError):
    pass


class _SnbtParser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.n = len(text)

    # --- 基础 ---
    def error(self, msg: str) -> SnbtError:
        return SnbtError(f"SNBT 解析错误 (位置 {self.i}): {msg}\n{self.s}\n{' ' * self.i}^")

    def skip_ws(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t\r\n":
            self.i += 1

    def peek(self) -> str:
        return self.s[self.i] if self.i < self.n else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise self.error(f"期望 {ch!r}, 实际 {self.peek()!r}")
        self.i += 1

    def parse(self) -> Any:
        self.skip_ws()
        tag = self.parse_tag()
        self.skip_ws()
        if self.i != self.n:
            raise self.error("结尾存在多余内容")
        return tag

    def parse_tag(self) -> Any:
        self.skip_ws()
        ch = self.peek()
        if ch == "{":
            return self.parse_compound()
        if ch == "[":
            return self.parse_list()
        if ch in "\"'":
            return self.parse_quoted()
        return self.parse_bare()

    def parse_quoted(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        out: list[str] = []
        while self.i < self.n:
            ch = self.s[self.i]
            if ch == "\\":
                self.i += 1
                if self.i >= self.n:
                    raise self.error("字符串转义不完整")
                esc = self.s[self.i]
                mapping = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
                           '"': '"', "'": "'", "\\": "\\", "/": "/"}
                if esc in mapping:
                    out.append(mapping[esc])
                    self.i += 1
                elif esc == "u":
                    hexs = self.s[self.i + 1:self.i + 5]
                    if len(hexs) != 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", hexs):
                        raise self.error("非法的 \\u 转义")
                    out.append(chr(int(hexs, 16)))
                    self.i += 5
                else:
                    raise self.error(f"未知转义 \\{esc}")
            elif ch == quote:
                self.i += 1
                return "".join(out)
            else:
                out.append(ch)
                self.i += 1
        raise self.error("字符串没有闭合")

    def read_bare_token(self) -> str:
        start = self.i
        while self.i < self.n and self.s[self.i] not in " \t\r\n,]}":
            self.i += 1
        return self.s[start:self.i]

    def parse_bare(self) -> Any:
        token = self.read_bare_token()
        if not token:
            raise self.error("缺少值")
        return classify_snbt_token(token)

    def parse_compound(self) -> CompoundTag:
        self.expect("{")
        out = CompoundTag()
        self.skip_ws()
        if self.peek() == "}":
            self.i += 1
            return out
        while True:
            self.skip_ws()
            if self.peek() in "\"'":
                key = self.parse_quoted()
            else:
                start = self.i
                while self.i < self.n and self.s[self.i] not in " \t\r\n:{}[],\"":
                    self.i += 1
                key = self.s[start:self.i]
                if not key:
                    raise self.error("缺少 compound 键")
            self.skip_ws()
            self.expect(":")
            out[key] = self.parse_tag()
            self.skip_ws()
            ch = self.peek()
            if ch == ",":
                self.i += 1
                continue
            if ch == "}":
                self.i += 1
                return out
            raise self.error(f"compound 中期望 ',' 或 '}}', 实际 {ch!r}")

    def parse_list(self) -> Any:
        self.expect("[")
        self.skip_ws()
        # 类型化数组 [B; ...] [I; ...] [L; ...]
        if self.peek() in "BIL" and self.i + 1 < self.n and self.s[self.i + 1] == ";":
            kind = self.s[self.i]
            self.i += 2
            values: list[int] = []
            self.skip_ws()
            if self.peek() == "]":
                self.i += 1
            else:
                while True:
                    self.skip_ws()
                    token = self.read_bare_token()
                    if not token:
                        raise self.error("数组缺少数值")
                    try:
                        values.append(int(token, 10))
                    except ValueError:
                        raise self.error(f"数组中的非法整数: {token!r}") from None
                    self.skip_ws()
                    if self.peek() == ",":
                        self.i += 1
                        continue
                    if self.peek() == "]":
                        self.i += 1
                        break
                    raise self.error("数组中期望 ',' 或 ']'")
            if kind == "B":
                return ByteArray(v & 0xFF for v in values)
            if kind == "I":
                return IntArray(values)
            return LongArray(values)
        items: list[Any] = []
        self.skip_ws()
        if self.peek() == "]":
            self.i += 1
            return ListTag()
        while True:
            items.append(self.parse_tag())
            self.skip_ws()
            if self.peek() == ",":
                self.i += 1
                continue
            if self.peek() == "]":
                self.i += 1
                return ListTag(items)
            raise self.error("list 中期望 ',' 或 ']'")


def classify_snbt_token(token: str) -> Any:
    if token == "true":
        return Byte(1)
    if token == "false":
        return Byte(0)
    m = _INT_RE.match(token)
    if m:
        value = int(m.group(1))
        suffix = (m.group(2) or "").lower()
        if suffix == "b":
            if not -128 <= value <= 255:
                raise SnbtError(f"byte 超出范围: {value}")
            return Byte(_wrap_signed(value, 8))
        if suffix == "s":
            return Short(_wrap_signed(value, 16))
        if suffix == "l":
            return Long(_wrap_signed(value, 64))
        return Int(value) if -(2 ** 31) <= value < 2 ** 31 else Long(value)
    m = _FLOAT_RE.match(token)
    if m and (("." in token) or ("e" in token.lower())):
        value = float(m.group(1))
        suffix = (m.group(2) or "").lower()
        if suffix == "f":
            return Float(value)
        return Double(value)
    if token in ("NaN", "Infinity", "-Infinity", "+Infinity"):
        return Double(float(token.replace("Infinity", "inf")))
    return token


def parse_snbt(text: str) -> Any:
    return _SnbtParser(text.strip()).parse()


def quote_snbt(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def to_snbt(value: Any) -> str:
    tid = tag_id_of(value)
    if tid == TAG_END:
        return ""
    if tid == TAG_BYTE:
        return f"{int(value)}b"
    if tid == TAG_SHORT:
        return f"{int(value)}s"
    if tid == TAG_INT:
        return str(int(value))
    if tid == TAG_LONG:
        return f"{int(value)}L"
    if tid == TAG_FLOAT:
        v = float(value)
        text = repr(v)
        if "e" in text or "E" in text:
            text = text.replace("e", "E")
        return f"{text}f"
    if tid == TAG_DOUBLE:
        return f"{float(value)!r}d"
    if tid == TAG_STRING:
        return quote_snbt(str(value))
    if tid == TAG_BYTE_ARRAY:
        return "[B;" + ",".join(str(v) for v in value.to_list()) + "]"
    if tid == TAG_INT_ARRAY:
        return "[I;" + ",".join(str(int(v)) for v in value) + "]"
    if tid == TAG_LONG_ARRAY:
        return "[L;" + ",".join(str(int(v)) for v in value) + "]"
    if tid == TAG_LIST:
        return "[" + ",".join(to_snbt(v) for v in value) + "]"
    if tid == TAG_COMPOUND:
        parts = []
        for k, v in value.items():
            key = k if _SAFE_KEY_RE.match(k) else quote_snbt(k)
            parts.append(f"{key}:{to_snbt(v)}")
        return "{" + ",".join(parts) + "}"
    raise NbtError(f"无法序列化为 SNBT: {tid}")


def to_snbt_pretty(value: Any, indent: int = 2, max_inline_width: int = 100) -> str:
    """把 NBT 值格式化为多行、带缩进的 SNBT（类似格式化 JSON），便于阅读。

    输出仍是合法 SNBT，可被 :func:`parse_snbt` 原样解析回相同 NBT。
    规则:
      - compound 一律多行缩进, 每项 `key: value`;
      - list 中的元素若都是标量且单行长度允许, 保持 `[a, b, c]` 单行;
        含 compound/list 或过长时多行;
      - ByteArray/IntArray/LongArray 保持 `[B;...]` 形式。
    """
    if indent < 1:
        raise ValueError("indent 必须 >= 1")
    if max_inline_width < 1:
        raise ValueError("max_inline_width 必须 >= 1")

    def pad(level: int) -> str:
        return " " * (level * indent)

    def render(item: Any, level: int) -> list[str]:
        tid = tag_id_of(item)
        if tid == TAG_COMPOUND:
            if not item:
                return [pad(level) + "{}"]
            lines = [pad(level) + "{"]
            for key, child in item.items():
                key_text = key if _SAFE_KEY_RE.match(key) else quote_snbt(key)
                child_lines = render(child, level + 1)
                child_pad = pad(level + 1)
                # 把子节点第一行改成 "  key: ..."（保留其多行缩进）
                first = child_lines[0]
                if first.startswith(child_pad):
                    first = first[len(child_pad):]
                child_lines = list(child_lines)
                child_lines[0] = child_pad + key_text + ": " + first
                lines.extend(child_lines)
                lines[-1] += ","
            if lines[-1].endswith(","):
                lines[-1] = lines[-1][:-1]
            lines.append(pad(level) + "}")
            return lines

        if tid == TAG_LIST:
            if not item:
                return [pad(level) + "[]"]
            # 全是标量且不太长 -> 单行
            if all(tag_id_of(v) not in (TAG_COMPOUND, TAG_LIST) for v in item):
                inline = "[" + ", ".join(to_snbt(v) for v in item) + "]"
                if len(pad(level)) + len(inline) <= max_inline_width:
                    return [pad(level) + inline]
            lines = [pad(level) + "["]
            for child in item:
                child_lines = render(child, level + 1)
                lines.extend(child_lines)
                lines[-1] += ","
            if lines[-1].endswith(","):
                lines[-1] = lines[-1][:-1]
            lines.append(pad(level) + "]")
            return lines

        # 标量 / 数组: 直接单行
        return [pad(level) + to_snbt(item)]

    return "\n".join(render(value, 0))


# --------------------------------------------------------------------------- #
# NBT 路径访问 (用于命令行 get/set)
# --------------------------------------------------------------------------- #

_PATH_PART_RE = re.compile(r"([^.\[\]]+)((?:\[\d+\])*)\Z")


def parse_nbt_path(path: str) -> list[tuple[str, list[int]]]:
    if path in ("", ".", "$"):
        return []
    tokens: list[tuple[str, list[int]]] = []
    for part in path.split("."):
        m = _PATH_PART_RE.match(part)
        if not m:
            raise NbtError(f"非法 NBT 路径片段: {part!r}")
        key = m.group(1)
        indices = [int(x) for x in re.findall(r"\[(\d+)\]", m.group(2))]
        tokens.append((key, indices))
    return tokens


def nbt_get_path(root: Any, path: str) -> Any:
    cur: Any = root
    tokens = parse_nbt_path(path)
    if not tokens:
        return root
    for key, indices in tokens:
        if not isinstance(cur, CompoundTag):
            raise NbtError(f"路径 {path!r}: {key!r} 处不是 compound")
        if key not in cur:
            raise KeyError(f"路径 {path!r}: 键 {key!r} 不存在")
        cur = cur[key]
        for idx in indices:
            if not isinstance(cur, ListTag):
                raise NbtError(f"路径 {path!r}: {key!r} 不是列表")
            if idx >= len(cur):
                raise IndexError(f"路径 {path!r}: 列表下标越界 {idx}")
            cur = cur[idx]
    return cur


def nbt_set_path(root: Any, path: str, value: Any, create: bool = False) -> None:
    tokens = parse_nbt_path(path)
    if not tokens:
        raise NbtError("不能替换根节点")
    cur: Any = root
    for pos, (key, indices) in enumerate(tokens):
        is_last = pos == len(tokens) - 1
        if not isinstance(cur, CompoundTag):
            raise NbtError(f"路径 {path!r}: {key!r} 的父节点不是 compound")
        if is_last and not indices:
            cur[key] = value
            return
        if key not in cur:
            if not create:
                raise KeyError(f"路径 {path!r}: 键 {key!r} 不存在")
            cur[key] = ListTag() if indices else CompoundTag()
        cur = cur[key]
        for j, idx in enumerate(indices):
            if not isinstance(cur, ListTag):
                raise NbtError(f"路径 {path!r}: {key!r} 不是列表")
            while idx >= len(cur):
                if not create:
                    raise IndexError(f"路径 {path!r}: 列表下标越界 {idx}")
                cur.append(CompoundTag())
            if is_last and j == len(indices) - 1:
                cur[idx] = value
                return
            cur = cur[idx]
    raise NbtError(f"无法设置路径: {path!r}")


def coerce_typed(value: Any, type_hint: str | None, existing: Any = None) -> Any:
    """把用户输入/SNBT 值转换成指定 NBT 类型。

    type_hint: byte/short/int/long/float/double/string/boolean/byte_array/
               int_array/long_array/compound/list/auto
    """
    if type_hint in (None, "", "auto"):
        # 尽量保持已有节点的数值类型, 否则按 SNBT/Python 自然类型
        if existing is not None and isinstance(existing, (int, float)) and isinstance(value, (int, float)):
            eid = tag_id_of(existing)
            if eid in (TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG):
                if isinstance(value, bool) or isinstance(value, float):
                    value = int(value)
                return _make_number(eid, int(value))
            if eid in (TAG_FLOAT, TAG_DOUBLE):
                return _make_number(eid, float(value))
        return value
    hint = type_hint.lower()
    if hint in ("byte", "b"):
        return Byte(_wrap_signed(_to_int(value), 8))
    if hint in ("short", "s"):
        return Short(_wrap_signed(_to_int(value), 16))
    if hint in ("int", "i", "integer"):
        return Int(_wrap_signed(_to_int(value), 32))
    if hint in ("long", "l"):
        return Long(_wrap_signed(_to_int(value), 64))
    if hint in ("float", "f"):
        return Float(_to_float(value))
    if hint in ("double", "d"):
        return Double(_to_float(value))
    if hint in ("string", "str"):
        return str(value)
    if hint in ("bool", "boolean"):
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return Byte(1)
            if low in ("false", "0", "no", "off"):
                return Byte(0)
            raise NbtError(f"无法解析布尔值: {value!r}")
        return Byte(1 if value else 0)
    if hint in ("byte_array", "bytearray", "b_array"):
        if isinstance(value, ByteArray):
            return value
        if isinstance(value, (bytes, bytearray)):
            return ByteArray(bytes(value))
        if isinstance(value, (list, tuple)):
            return ByteArray(int(v) & 0xFF for v in value)
        raise NbtError("byte_array 需要列表或 bytes")
    if hint in ("int_array", "intarray"):
        if isinstance(value, IntArray):
            return value
        if isinstance(value, str):
            value = parse_snbt(value)
        return IntArray(int(v) for v in value)
    if hint in ("long_array", "longarray"):
        if isinstance(value, LongArray):
            return value
        if isinstance(value, str):
            value = parse_snbt(value)
        return LongArray(int(v) for v in value)
    if hint == "compound":
        if isinstance(value, CompoundTag):
            return value
        if isinstance(value, str):
            parsed = parse_snbt(value)
            if not isinstance(parsed, CompoundTag):
                raise NbtError("期望 compound SNBT, 实际不是")
            return parsed
        raise NbtError("compound 需要 SNBT 字符串")
    if hint == "list":
        if isinstance(value, ListTag):
            return value
        if isinstance(value, str):
            parsed = parse_snbt(value)
            if not isinstance(parsed, ListTag):
                raise NbtError("期望 list SNBT, 实际不是")
            return parsed
        raise NbtError("list 需要 SNBT 字符串")
    raise NbtError(f"未知类型提示: {type_hint}")


def _make_number(tid: int, value: Any) -> Any:
    if tid == TAG_BYTE:
        return Byte(_wrap_signed(int(value), 8))
    if tid == TAG_SHORT:
        return Short(_wrap_signed(int(value), 16))
    if tid == TAG_INT:
        return Int(_wrap_signed(int(value), 32))
    if tid == TAG_LONG:
        return Long(_wrap_signed(int(value), 64))
    if tid == TAG_FLOAT:
        return Float(float(value))
    return Double(float(value))


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError:
            return int(float(value))
    raise NbtError(f"无法转换为整数: {value!r}")


def _to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise NbtError(f"无法转换为浮点数: {value!r}")
