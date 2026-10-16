"""程序员计算器背后的纯逻辑：进制转换、位操作、IEEE-754 拆解、定标换算。

电气/电力电子工程师天天要跟 DSP 寄存器打交道：

* 从报文里抠位段（控制字第 3..0 位是哪种工作模式）；
* 把寄存器读到的 0x43AF0000 还原成工程量（IEEE-754 反推）；
* 确认某个工程量能不能被 float32 精确表示（定标精度够不够）；
* 反复换算 Q 格式的定标系数（2 的幂次）。

这些计算与界面无关，所以独立成模块：能单独测，也能被别的工具（示波器、
脚本、点位详情）直接复用。**本模块不依赖 Qt。**

约定
----
* 「位模式」一律是 **无符号整数**（``0 .. 2**width-1``）；负数用二进制补码表示，
  例如 8 位下的 ``-1`` 就是 ``0xFF``。有符号/无符号只影响**显示与解析范围**，
  不影响内部存放。
* 出错一律抛 :class:`CalcError`（``ValueError`` 的子类），消息是中文的，
  界面可以直接拿去显示 —— 宁可报错，也不静默截断。
"""

from __future__ import annotations

import math
import struct
import unicodedata
from dataclasses import dataclass
from typing import Mapping

# --------------------------------------------------------------- 常量

#: 支持的整数位宽
WIDTHS: tuple[int, ...] = (8, 16, 32, 64)

#: 支持的浮点位宽（IEEE-754 单精度 / 双精度）
FLOAT_WIDTHS: tuple[int, ...] = (32, 64)

#: 各进制的写法前缀
BASE_PREFIX: dict[int, str] = {2: "0b", 8: "0o", 10: "", 16: "0x"}

#: 各进制的中文名（拼报错信息用）
BASE_NAME: dict[int, str] = {2: "二进制", 8: "八进制", 10: "十进制", 16: "十六进制"}

#: 前缀 -> 进制（解析文本时用；由 BASE_PREFIX 推出来，免得两处各写一份）
_PREFIX_BASE: dict[str, int] = {BASE_PREFIX[b]: b for b in (16, 2, 8)}

#: IEEE-754 各精度布局：位宽 -> (指数位宽, 尾数位宽)
FLOAT_LAYOUT: dict[int, tuple[int, int]] = {32: (8, 23), 64: (11, 52)}

#: 分隔符。工程师是从报文、表格、文档里连空格一起复制的，
#: 所以这些字符一律先去掉再解析，而不是要求对方手工清理。
_SEPARATORS = " \t\r\n_,，、'"

#: 位宽 -> struct 打包格式（大端 = 位模式的高字节在前）
_PACK = {32: ">f", 64: ">d"}


class CalcError(ValueError):
    """输入不合法（非法字符、超出位宽、缩放为 0……）。

    继承 ``ValueError``，调用方可以只 catch ValueError；消息是中文的，
    界面直接显示即可。
    """


# --------------------------------------------------------------- 小工具


def check_width(width: int) -> int:
    """校验整数位宽，返回它本身（拿来做行内断言）。"""
    if width not in WIDTHS:
        raise CalcError(f"不支持的位宽 {width}，只能是 {list(WIDTHS)}")
    return width


def check_float_width(width: int) -> int:
    """校验浮点位宽（只支持 32 / 64，对应单精度与双精度）。"""
    if width not in FLOAT_WIDTHS:
        raise CalcError(f"不支持的浮点位宽 {width}，只能是 {list(FLOAT_WIDTHS)}")
    return width


def mask_of(width: int) -> int:
    """位宽对应的掩码，例如 8 位 -> 0xFF。"""
    return (1 << check_width(width)) - 1


def _normalize(text: str) -> str:
    """全角转半角 + 去掉分隔符。

    中文文档里复制出来的数字经常是**全角**的（``０ｘ１Ｆ``），
    NFKC 归一化能把全角数字/字母/符号转回 ASCII，省得用户自己重打。
    """
    full_width = unicodedata.normalize("NFKC", str(text)).strip()
    return "".join(ch for ch in full_width if ch not in _SEPARATORS)


def number_text(value: float) -> str:
    """浮点数 -> 给人看的文本（整数不显示小数点，其余用最短往返表示）。

    界面显示误差、工程量时也用它，保证「同一个数在哪儿看起来都一样」。
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "-∞" if value < 0 else "∞"
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def to_unsigned(value: int, width: int) -> int:
    """整数 -> width 位的无符号位模式（负数走二进制补码）。"""
    limit = 1 << check_width(width)
    if value < 0:
        if value < -(limit >> 1):
            raise CalcError(f"{value} 小于 {width} 位有符号最小值 {-(limit >> 1)}")
        value += limit
    if value >= limit:
        raise CalcError(f"0x{value:X}（{value}）超出 {width} 位，最大 0x{limit - 1:X}")
    return value


def to_signed(value: int, width: int) -> int:
    """位模式 -> 二进制补码解释下的有符号整数。"""
    limit = 1 << check_width(width)
    value &= limit - 1
    if value >= limit >> 1:
        value -= limit
    return value


# --------------------------------------------------------------- 解析


def parse_int(text: str, width: int = 32, signed: bool = False, base: int = 0) -> int:
    """文本 -> 位模式（``0 .. 2**width-1``）。

    * ``base=0``：按前缀判断进制（``0x``/``0b``/``0o``，没有前缀按十进制），
      适合「浮点位模式」这类混合输入框；
    * 给 ``base``：强制按该进制解析，但仍接受对应前缀 ——
      十六进制框里填裸的 ``3F800000`` 也认得出来；
    * ``signed=True`` 时允许负数（按补码折算），也允许写满位宽的位模式写法
      （例如 32 位有符号下填 ``0xFFFFFFFF`` 就是 -1 的位模式），
      但**超出位宽一定报错**，不静默截断。
    """
    check_width(width)
    body = _normalize(text)
    if not body:
        raise CalcError("请输入数值")

    sign = 1
    if body[0] in "+-":
        sign = -1 if body[0] == "-" else 1
        body = body[1:]
    if not body:
        raise CalcError(f"「{text}」不是合法的数值")

    if base:
        name = BASE_NAME.get(base, f"{base} 进制")
        try:
            value = int(body, base)
        except ValueError as exc:
            raise CalcError(f"「{text}」不是合法的{name}数值") from exc
    else:
        lowered = body.lower()
        prefix = next((p for p in _PREFIX_BASE if lowered.startswith(p)), "")
        if prefix:
            # 显式前缀：按该进制解析（注意不能直接用 int(body, 0)，那样 "010" 会报错）
            try:
                value = int(body[len(prefix):], _PREFIX_BASE[prefix])
            except ValueError as exc:
                raise CalcError(f"「{text}」不是合法的数值") from exc
        else:
            try:
                value = int(body, 10)
            except ValueError as exc:
                raise CalcError(f"「{text}」不是合法的数值（带 0x/0b/0o 前缀才能写其它进制）") from exc
    value *= sign

    limit = 1 << width
    if signed:
        if value < -(limit >> 1):
            raise CalcError(
                f"{value} 小于 {width} 位有符号最小值 {-(limit >> 1)}"
            )
    elif value < 0:
        raise CalcError(f"{value} 是无符号位宽下不合法的负数（可切到「有符号」）")
    if value >= limit:
        raise CalcError(f"0x{value:X}（{value}）超出 {width} 位，最大 0x{limit - 1:X}")
    return to_unsigned(value, width)


def parse_float(text: str) -> float:
    """文本 -> 浮点数。

    与 :func:`parse_int` 一样容忍空格/下划线/全角字符与 ``0x`` 前缀
    （``0x10`` 就是 16.0），另外支持 ``1e-3`` / ``inf`` / ``nan`` ——
    填定标系数时这些写法都会出现。
    """
    body = _normalize(text)
    if not body:
        raise CalcError("请输入数值")

    sign = ""
    if body[0] in "+-":
        sign, body = body[0], body[1:]
    if not body:
        raise CalcError(f"「{text}」不是合法的数值")

    lowered = body.lower()
    for prefix, base in _PREFIX_BASE.items():
        if lowered.startswith(prefix):
            digits = body[len(prefix):]
            if not digits:
                raise CalcError(f"「{text}」不是合法的数值")
            try:
                return float(f"{sign}{int(digits, base)}")
            except ValueError as exc:
                raise CalcError(f"「{text}」不是合法的数值") from exc
    try:
        return float(f"{sign}{body}")
    except ValueError as exc:
        raise CalcError(f"「{text}」不是合法的数值") from exc


# --------------------------------------------------------------- 格式化


def _digit_count(value: int, width: int | None, bits_per_digit: int) -> int:
    """要补零补到几位：有位宽就补满位宽，否则用最短表示。

    格式化不看「位宽是不是 8/16/32/64」：位段（5 位、13 位）也要能排版，
    所以除了解析/位操作之外，格式化允许任意正整数位宽。
    """
    if width is None:
        return max(1, (value.bit_length() + bits_per_digit - 1) // bits_per_digit)
    if width < 1:
        raise CalcError(f"位宽必须为正整数，收到 {width}")
    return (int(width) + bits_per_digit - 1) // bits_per_digit


def format_hex(value: int, width: int | None = None, prefix: bool = True) -> str:
    """十六进制文本（大写）。``width=None`` 用最短位数（位段结果这样更好读）。

    这里**不做范围检查、也不截断**：它只负责排版，越界是调用方的事。
    """
    digits = _digit_count(value, width, 4)
    text = f"{value:0{digits}X}"
    return f"0x{text}" if prefix else text


def format_octal(value: int, width: int | None = None, prefix: bool = True) -> str:
    """八进制文本。按位宽补零到 3 位一组的整位数。"""
    digits = _digit_count(value, width, 3)
    text = f"{value:0{digits}o}"
    return f"0o{text}" if prefix else text


def format_binary(value: int, width: int | None = None, group: bool = True) -> str:
    """二进制文本；``group=True`` 时 4 位一组用空格分开（肉眼对位用）。

    ``width=None`` 取最短位数（不补零），用于显示位段这种窄字段。
    """
    digits = _digit_count(value, width, 1)
    text = f"{value:0{digits}b}"
    if not group:
        return text
    return " ".join(text[i : i + 4] for i in range(0, len(text), 4))


def format_decimal(value: int, width: int, signed: bool = False) -> str:
    """十进制文本；``signed=True`` 时按补码显示负数。"""
    check_width(width)
    return str(to_signed(value, width) if signed else value)


def format_value(
    value: int,
    width: int,
    base: int,
    signed: bool = False,
    group: bool = True,
    prefix: bool = True,
) -> str:
    """按进制格式化一个位模式（界面四个进制框共用这一个出口）。

    二进制/八进制/十六进制对位宽没要求（窄位段也能排版）；十进制要按补码解释，
    所以只认 8/16/32/64 位 —— 位段的十进制显示直接用它的数值就行。
    """
    if base == 2:
        return format_binary(value, width, group)
    if base == 8:
        return format_octal(value, width, prefix)
    if base == 16:
        return format_hex(value, width, prefix)
    if base == 10:
        return format_decimal(value, width, signed)
    raise CalcError(f"不支持的进制 {base}")


# --------------------------------------------------------------- 位操作


@dataclass(frozen=True)
class BitState:
    """一位的状态（界面的位网格按它画）。"""

    #: 位号，0 = 最低位
    index: int
    #: 0 / 1
    value: int
    #: 属于第几个字节（0 = 最低字节）
    byte_index: int
    #: 字节内的位号（0..7）
    byte_bit: int
    #: 显示名；给了位定义表就用表里的名字
    name: str

    @property
    def is_set(self) -> bool:
        return bool(self.value)

    @property
    def hex_text(self) -> str:
        """这一位的掩码，例如 bit3 -> 0x8（写命令码时直接抄）。"""
        return f"0x{1 << self.index:X}"


def bit_names(
    value: int,
    width: int,
    labels: Mapping[int, str] | None = None,
) -> list[BitState]:
    """每一位的状态，**从低位到高位**（bit0 在最前）。

    ``labels`` 是「位号 -> 名称」的映射，可以拿点位参数表里的「位定义」列
    直接喂进来，这样界面能显示「bit3 使能」而不是干巴巴的「bit3」。
    """
    check_width(width)
    value &= mask_of(width)
    names = labels or {}
    result: list[BitState] = []
    for index in range(width):
        result.append(
            BitState(
                index=index,
                value=value >> index & 1,
                byte_index=index // 8,
                byte_bit=index % 8,
                name=names.get(index, f"bit{index}"),
            )
        )
    return result


def test_bit(value: int, index: int, width: int = 64) -> bool:
    """某一位是不是 1。"""
    return bool(value >> _check_index(index, width) & 1)


def set_bit(value: int, index: int, width: int = 64, on: bool = True) -> int:
    """置位 / 清位（``on=False`` 等价于 :func:`clear_bit`）。"""
    bit = 1 << _check_index(index, width)
    return (value | bit) & mask_of(width) if on else clear_bit(value, index, width)


def clear_bit(value: int, index: int, width: int = 64) -> int:
    """清掉某一位（写 DSP 的使能位常用）。"""
    bit = 1 << _check_index(index, width)
    return value & ~bit & mask_of(width)


def toggle_bit(value: int, index: int, width: int = 64) -> int:
    """翻转某一位（界面点方格就是它）。"""
    return (value ^ (1 << _check_index(index, width))) & mask_of(width)


def extract_bits(value: int, high: int, low: int, width: int = 64) -> int:
    """取位段 ``[high:low]``（闭区间），结果右对齐。

    读位域寄存器天天用：``extract_bits(cmd, 3, 0)`` 就是「低 4 位的模式码」。
    """
    _check_range(high, low, width)
    return value >> low & ((1 << (high - low + 1)) - 1)


def bit_field_mask(high: int, low: int, width: int = 64) -> int:
    """位段掩码（写回位域时要先 ``& ~mask`` 清场再或上新值）。"""
    _check_range(high, low, width)
    return ((1 << (high - low + 1)) - 1) << low


def _check_range(high: int, low: int, width: int) -> None:
    check_width(width)
    if high < low:
        raise CalcError(f"高位 {high} 不能小于低位 {low}")
    for name, index in (("高位", high), ("低位", low)):
        if index < 0 or index >= width:
            raise CalcError(f"{name} {index} 超出 0..{width - 1} 范围")


def _check_index(index: int, width: int) -> int:
    check_width(width)
    if index < 0 or index >= width:
        raise CalcError(f"位号 {index} 超出 0..{width - 1} 范围")
    return index


# --------------------------------------------------------------- IEEE-754


def float_layout(width: int) -> tuple[int, int]:
    """``(指数位宽, 尾数位宽)``，32 位 -> (8, 23)。"""
    return FLOAT_LAYOUT[check_float_width(width)]


def float_bias(width: int) -> int:
    """指数偏移（32 位 -> 127，64 位 -> 1023）。"""
    (exp_width, _) = float_layout(width)
    return (1 << (exp_width - 1)) - 1


def float_to_bits(value: float, width: int = 32) -> int:
    """浮点数 -> 位模式（IEEE-754 大端编码）。

    超出该精度可表示范围时报错 —— IEEE 会变成 ±∞，但对上位机来说
    「给定值根本存不下」比静默变 ∞ 更值得知道。
    """
    check_float_width(width)
    try:
        raw = struct.pack(_PACK[width], float(value))
    except OverflowError as exc:
        limit = 3.4028235e38 if width == 32 else 1.7976931348623157e308
        raise CalcError(
            f"{number_text(float(value))} 超出 {width} 位浮点可表示范围（最大约 {limit:.6g}）"
        ) from exc
    return int.from_bytes(raw, "big")


def bits_to_float(bits: int, width: int = 32) -> float:
    """位模式 -> 浮点数（把寄存器里读到的 0x43AF0000 还原成工程量）。"""
    check_float_width(width)
    limit = 1 << width
    if bits < 0 or bits >= limit:
        raise CalcError(f"0x{bits:X} 超出 {width} 位位模式范围（最大 0x{limit - 1:X}）")
    return struct.unpack(_PACK[width], int(bits).to_bytes(width // 8, "big"))[0]


@dataclass(frozen=True)
class FloatInfo:
    """一个 IEEE-754 浮点数的完整拆解。

    ``value`` 是该位模式**实际表示**的浮点数，``source`` 是原始输入值：
    用 float32 去存 0.1 时二者不等，差值就是 :attr:`error` ——
    定标精度够不够，看这一项。
    """

    width: int
    bits: int
    #: 符号位原始值（0 / 1）
    sign: int
    sign_text: str
    #: 指数位原始值（无符号）
    exponent_bits: int
    #: 指数位原始二进制串
    exponent_text: str
    #: 尾数位原始值
    mantissa_bits: int
    #: 「隐含位 + 尾数位」的二进制串，如 ``1.011``
    mantissa_text: str
    #: 减掉偏移后的指数真值；次正规数/零按 IEEE 规定取 1-偏移
    exponent: int
    #: 隐含 1 之后的小数部分（0.011 里的 0.011）
    mantissa: float
    #: 该位模式表示的浮点数
    value: float
    #: 原始输入值（由位反推时与 value 相同）
    source: float
    hex_text: str
    #: 二进制串（4 位一组空格分隔）
    binary_text: str
    #: 一句话说清这个数是怎么拼出来的
    formula: str
    #: normal / subnormal / zero / inf / nan
    kind: str

    # -------------------------------------------------- 类别

    @property
    def is_normal(self) -> bool:
        return self.kind == "normal"

    @property
    def is_subnormal(self) -> bool:
        return self.kind == "subnormal"

    @property
    def is_zero(self) -> bool:
        return self.kind == "zero"

    @property
    def is_inf(self) -> bool:
        return self.kind == "inf"

    @property
    def is_nan(self) -> bool:
        return self.kind == "nan"

    @property
    def is_negative(self) -> bool:
        return bool(self.sign)

    # -------------------------------------------------- 误差

    @property
    def error(self) -> float:
        """用该精度表示原值时的绝对误差（实际值 - 原值）。

        对 ±∞ 只要符号一致就算「精确」（误差 0）；有限数存成了 ±∞ 则记为
        无穷大误差；NaN 谈不上误差，返回 NaN。
        """
        if math.isnan(self.value) or math.isnan(self.source):
            return math.nan
        if math.isinf(self.value) or math.isinf(self.source):
            return 0.0 if self.value == self.source else math.inf
        return self.value - self.source

    @property
    def relative_error(self) -> float | None:
        """相对误差；原值为 0 或 ±∞ 时没有意义，返回 None。"""
        if self.source == 0 or math.isinf(self.source) or math.isnan(self.error):
            return None
        return self.error / abs(self.source)

    @property
    def exact(self) -> bool:
        """该精度能否**精确**表示原值（定标精度够不够就看它）。"""
        return self.error == 0.0


def decode_bits(bits: int, width: int = 32, source: float | None = None) -> FloatInfo:
    """位模式 -> :class:`FloatInfo`（从寄存器读数反推时用）。

    ``source`` 用来算误差；由位反推时结果本身就是真值，不用传。
    """
    check_float_width(width)
    value = bits_to_float(bits, width)  # 顺便校验范围
    bits &= (1 << width) - 1
    exp_width, mant_width = FLOAT_LAYOUT[width]
    bias = (1 << (exp_width - 1)) - 1
    sign = bits >> (width - 1) & 1
    exponent_bits = bits >> mant_width & ((1 << exp_width) - 1)
    mantissa_bits = bits & ((1 << mant_width) - 1)
    mantissa = mantissa_bits / (1 << mant_width)

    top = (1 << exp_width) - 1
    if exponent_bits == 0:
        kind = "zero" if mantissa_bits == 0 else "subnormal"
        leading = "0."
        # 次正规数与零按 IEEE-754 用 2^(1-偏移)，不是 2^(0-偏移)
        exponent = 1 - bias
        exponent_term = f"2^(1-{bias})"
    elif exponent_bits == top:
        kind = "nan" if mantissa_bits else "inf"
        leading = "1."
        exponent = exponent_bits - bias
        exponent_term = f"2^({exponent_bits}-{bias})"
    else:
        kind = "normal"
        leading = "1."
        exponent = exponent_bits - bias
        exponent_term = f"2^({exponent_bits}-{bias})"

    mantissa_text = f"{leading}{mantissa_bits:0{mant_width}b}"
    if kind == "nan":
        formula = "NaN（指数位全 1、尾数位非 0，不表示具体数值）"
    elif kind == "inf":
        formula = f"(-1)^{sign} × ∞"
    elif kind == "zero":
        formula = f"(-1)^{sign} × 0（指数位与尾数位全 0 → {'负' if sign else '正'}零）"
    elif kind == "subnormal":
        formula = (
            f"(-1)^{sign} × {mantissa_text} × {exponent_term}"
            f" = {number_text(value)}"
        )
    else:
        formula = (
            f"(-1)^{sign} × {mantissa_text} × {exponent_term}"
            f" = {number_text(1.0 + mantissa)} × 2^{exponent}"
            f" = {number_text(value)}"
        )

    return FloatInfo(
        width=width,
        bits=bits,
        sign=sign,
        sign_text=str(sign),
        exponent_bits=exponent_bits,
        exponent_text=f"{exponent_bits:0{exp_width}b}",
        mantissa_bits=mantissa_bits,
        mantissa_text=mantissa_text,
        exponent=exponent,
        mantissa=mantissa,
        value=value,
        source=value if source is None else float(source),
        hex_text=format_hex(bits, width),
        binary_text=format_binary(bits, width),
        formula=formula,
        kind=kind,
    )


def decode_float(value: float, width: int = 32) -> FloatInfo:
    """浮点数 -> :class:`FloatInfo`（含「该精度能不能精确表示」的误差）。"""
    return decode_bits(float_to_bits(value, width), width, source=float(value))


# --------------------------------------------------------------- 定标换算


def raw_to_engineering(raw: float, scale: float = 1.0, offset: float = 0.0) -> float:
    """原始值 -> 工程量：``工程量 = 原始值 × 缩放 + 偏移``。"""
    return raw * scale + offset


def engineering_to_raw(engineering: float, scale: float = 1.0, offset: float = 0.0) -> float:
    """工程量 -> 原始值（写寄存器时用）。"""
    if scale == 0:
        raise CalcError("缩放系数不能为 0，否则无法由工程量反算原始值")
    return (engineering - offset) / scale


def power_of_two(exponent: int) -> float:
    """2 的幂。"""
    return 2.0 ** exponent


@dataclass(frozen=True)
class ScaleRow:
    """速查表的一行。"""

    #: 名称 / 表达式，如 ``2^-3``
    name: str
    #: 系数
    factor: float
    #: 系数的十进制文本
    text: str
    #: 该系数在 float32 里的位模式
    hex32: str
    #: float32 能否精确表示
    exact32: bool


def scale_table() -> list[ScaleRow]:
    """常用定标速查表：2^20..2^-20，外加几个现场常见的十进制系数。

    2 的幂在 float32 里全部精确（二进制浮点天生如此），十进制系数多数不精确 ——
    这一列正是拿来回答「这个定标系数会不会引入误差」的。
    """
    rows: list[ScaleRow] = []
    for exponent in range(20, -21, -1):
        factor = power_of_two(exponent)
        bits = float_to_bits(factor, 32)
        rows.append(
            ScaleRow(
                name=f"2^{exponent}",
                factor=factor,
                text=number_text(factor),
                hex32=format_hex(bits, 32),
                exact32=bits_to_float(bits, 32) == factor,
            )
        )
    for name, factor in (
        ("0.5（0..1 系数）", 0.5),
        ("0.1（0..1 系数）", 0.1),
        ("0.01", 0.01),
        ("0.001", 0.001),
        ("1/3", 1 / 3),
    ):
        bits = float_to_bits(factor, 32)
        rows.append(
            ScaleRow(
                name=name,
                factor=factor,
                text=number_text(factor),
                hex32=format_hex(bits, 32),
                exact32=bits_to_float(bits, 32) == factor,
            )
        )
    return rows
