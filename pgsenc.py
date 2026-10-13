"""PGS（HDMV 位图字幕）编码器 —— 纯 Python/numpy，无外部依赖。

格式依据（已用真实蓝光 .sup 逐像素验证）：
  * 段头：'PG' + PTS(4 BE) + DTS(4 BE) + 类型(1) + 长度(2 BE)
  * 段类型：0x14 PDS / 0x15 ODS / 0x16 PCS / 0x17 WDS / 0x80 END
  * 每个 display set 顺序：PCS → WDS → [PDS] → ODS → END
  * RLE 游程（见 rle_row）：
        CC                (C≠0)        单像素，颜色 C
        00 00                          换行
        00 0LLLLLL        (L<0x40)      L 个颜色 0
        00 01LLLLLL LL                14 位长度，颜色 0
        00 10LLLLLL C     (L<0x40)      L 个颜色 C
        00 11LLLLLL LL C              14 位长度，颜色 C
  * 调色板项：(索引, Y, Cr, Cb, T)，BT.709 有限范围
"""

import struct
import numpy as np

PCS, WDS, PDS, ODS, END = 0x16, 0x17, 0x14, 0x15, 0x80


def rgb_to_ycc(rgb):
    """RGB(0-255) → (Y, Cr, Cb)，BT.709 有限范围（16-235 / 16-240）。"""
    r, g, b = (float(v) for v in rgb)
    y = 16.0 + (0.2126 * r + 0.7152 * g + 0.0722 * b) * 219.0 / 255.0
    cb = 128.0 + (-0.1146 * r - 0.3854 * g + 0.5000 * b) * 224.0 / 255.0
    cr = 128.0 + (0.5000 * r - 0.4542 * g - 0.0458 * b) * 224.0 / 255.0
    return (int(round(min(235.0, max(16.0, y)))),
            int(round(min(240.0, max(16.0, cr)))),
            int(round(min(240.0, max(16.0, cb)))))


def build_palette(rgba_list):
    """[RGBA, ...] → 256 项调色板字节串（每项 5 字节：索引, Y, Cr, Cb, T）。

    索引 0 固定为全透明；超过 255 个颜色时截断。
    """
    entries = [bytes((0, 16, 128, 128, 0))]                   # 0 号 = 透明
    for i, rgba in enumerate(rgba_list[:255], start=1):
        r, g, b = int(rgba[0]), int(rgba[1]), int(rgba[2])
        t = int(rgba[3]) if len(rgba) > 3 else 255
        y, cr, cb = rgb_to_ycc((r, g, b))
        entries.append(bytes((i, y, cr, cb, max(0, min(255, t)))))
    while len(entries) < 256:
        entries.append(bytes((len(entries), 16, 128, 128, 0)))
    return b''.join(entries)


def _emit_run(out, c, n):
    if c != 0:
        if n == 1:
            out.append(c)                                     # CC
        elif n <= 0x3F:
            out += bytes((0x00, 0x80 | n, c))                 # 00 10LLLLLL C
        else:
            out += bytes((0x00, 0xC0 | (n >> 8), n & 0xFF, c))
    else:
        if n <= 0x3F:
            out += bytes((0x00, n))                           # 00 0LLLLLL
        else:
            out += bytes((0x00, 0x40 | (n >> 8), n & 0xFF))


def rle_row(row):
    """单行索引数据 → RLE 字节（末尾含 00 00 换行）。

    ffmpeg 的 decode_rle 用全局 pixel_count 统计，要求显式写出的像素总数
    正好等于 w*h（EOL 只推进行号、不补像素），否则报 "Insufficient RLE data"。
    因此：每一行（含全透明行）都必须显式写满对象宽度，行尾透明像素并入最后一个游程。
    """
    width = len(row)
    out = bytearray()
    nz = np.nonzero(row)[0]
    if len(nz) == 0:
        _emit_run(out, 0, width)
        return bytes(out) + b'\x00\x00'
    last = int(nz[-1])
    seg = row[:last + 1]
    ch = np.nonzero(seg[1:] != seg[:-1])[0] + 1
    starts = np.concatenate(([0], ch))
    ends = np.concatenate((ch, [last + 1]))
    runs = [(int(seg[s]), e - s) for s, e in zip(starts.tolist(), ends.tolist())]
    tail = width - (last + 1)
    if tail > 0:
        if runs[-1][0] == 0:
            runs[-1] = (0, runs[-1][1] + tail)
        else:
            runs.append((0, tail))
    for c, n in runs:
        _emit_run(out, c, n)
    out += b'\x00\x00'
    return bytes(out)


def rle_encode(idx):
    """(h, w) uint8 索引图 → RLE 字节流。"""
    return b''.join(rle_row(row) for row in idx)


class PgsWriter:
    def __init__(self, path, vw=3840, vh=1638, fps_code=0x80):
        self.f = open(path, 'wb')
        self.vw, self.vh, self.fps_code = vw, vh, fps_code
        self.comp = 0
        self.objver = 0
        self.palver = 0
        self.bytes_written = 0

    def _seg(self, stype, pts, body):
        head = b'PG' + struct.pack('>II', pts & 0xFFFFFFFF, 0) + bytes((stype,)) + struct.pack('>H', len(body))
        self.f.write(head + body)
        self.bytes_written += len(head) + len(body)

    def display_set(self, pts, x, y, w, h, rle, palette=None,
                    state=0x00, n_obj=1):
        # PCS
        b = struct.pack('>HHBHB', self.vw, self.vh, self.fps_code, self.comp, state)
        b += bytes((0, 0, n_obj))
        if n_obj:
            b += struct.pack('>HBBHH', 0, 0, 0, x, y)
        self._seg(PCS, pts, b)
        # WDS
        self._seg(WDS, pts, bytes((1, 0)) + struct.pack('>HHHH', x, y, w, h))
        # PDS（可选）
        if palette is not None:
            self._seg(PDS, pts, bytes((0, self.palver)) + palette)
            self.palver = (self.palver + 1) & 0xFF
        # ODS（超过 65535 字节时分片，seq 标志 0x80 首 / 0x40 尾 / 0x00 中）
        if n_obj:
            total = len(rle) + 4
            # 【坑·段长是 16 位，判据要算上段头】ODS 的 body 实际是
            #     4(对象头) + 3(对象数据长度字段) + 4(w,h) + len(rle) = len(rle) + 11
            #   而原判据只算了"对象数据长度" = len(rle) + 4，没算段头那 7 字节 ⇒
            #   len(rle) 落在 65525~65531（total 65529~65535）时**进了不切片分支**，
            #   于是 struct.pack('>H', len(body)) 直接
            #   struct.error: 'H' format requires 0 <= number <= 65535。
            #   实测：全片编码跑到第 12240 帧炸掉（2026-10-02）。
            #   切片分支里每片 body ≤ 65008，仍然安全。
            if len(rle) + 11 <= 65535:
                body = struct.pack('>HBB', 0, self.objver, 0xC0)
                body += total.to_bytes(3, 'big')
                body += struct.pack('>HH', w, h) + rle
                self._seg(ODS, pts, body)
            else:
                CH = 65000
                chunks = [rle[i:i + CH] for i in range(0, len(rle), CH)]
                for ci, chunk in enumerate(chunks):
                    first = (ci == 0)
                    last = (ci == len(chunks) - 1)
                    seq = (0x80 if first else 0x00) | (0x40 if last else 0x00)
                    body = struct.pack('>HBB', 0, self.objver, seq)
                    if first:
                        body += total.to_bytes(3, 'big') + struct.pack('>HH', w, h)
                    body += chunk
                    self._seg(ODS, pts, body)
            self.objver = (self.objver + 1) & 0xFF
        # END
        self._seg(END, pts, b'')
        self.comp = (self.comp + 1) & 0xFFFF

    def close(self):
        self.f.close()
