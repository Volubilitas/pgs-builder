# -*- coding: utf-8 -*-
"""最小 PGS(.sup) 解码器 —— 用来验收：把编出来的字幕轨解回 RGBA，和"烧录版"逐帧比 RMSE。

只实现本工程用到的部分：PCS / WDS / PDS / ODS / END，
RLE 四种游程（见 pgsenc.rle_row 的注释），BT.709 有限范围反变换回 RGB。
"""
import struct
import numpy as np


def ycc_to_rgb(y, cr, cb):
    y = (y - 16.0) * 255.0 / 219.0
    cr = (cr - 128.0) * 255.0 / 224.0
    cb = (cb - 128.0) * 255.0 / 224.0
    r = y + 1.5748 * cr
    g = y - 0.1873 * cb - 0.4681 * cr
    b = y + 1.8556 * cb
    return np.clip([r, g, b], 0, 255)


def rle_decode(data, w, h):
    img = np.zeros((h, w), np.uint8)
    x = y = 0
    i = 0
    L = len(data)
    while i < L and y < h:
        c = data[i]
        if c != 0:
            if x < w:
                img[y, x] = c
            x += 1
            i += 1
            continue
        if i + 1 >= L:
            break
        b1 = data[i + 1]
        if b1 == 0:
            y += 1
            x = 0
            i += 2
            continue
        if (b1 & 0xC0) == 0x00:      # 00 0LLLLLL  → L 个颜色 0
            n = b1 & 0x3F
            i += 2
        elif (b1 & 0xC0) == 0x40:    # 00 01LLLLLL LL
            n = ((b1 & 0x3F) << 8) | data[i + 2]
            i += 3
        elif (b1 & 0xC0) == 0x80:    # 00 10LLLLLL C → L 个颜色 **C**
            # ⚠️ 【2026-10-10 修】原来这里只写 `x += n`（把颜色 C 当成"跳过"），
            #    于是**所有长度 ≥2 的同色游程都被丢掉**，只有孤立单像素（CC 形式）能解回来。
            #    实测：写侧 328619 个非零像素 → 解回来只剩 5043 个（正好是孤立点）。
            #    它躲过历次验收的原因是口径：字幕只占画面 ~0.16%，整帧 RMSE 根本看不见。
            #    ⇒ 验收"解码器"必须用**填满的图形**（长游程），不能用稀疏点阵。
            n = b1 & 0x3F
            if n:
                img[y, x:min(x + n, w)] = data[i + 2]
            x += n
            i += 3
            continue
        else:                        # 00 11LLLLLL LL C → L 个颜色 **C**
            n = ((b1 & 0x3F) << 8) | data[i + 2]
            if n:
                img[y, x:min(x + n, w)] = data[i + 3]
            x += n
            i += 4
            continue
        x += n
    return img


def decode(path, want_pts=None):
    """→ {pts: (x, y, rgba float32 HxWx4)}（每个 display set 的完整合成结果）。

    ⚠️ **擦除集**（PCS 里 `n_obj = 0`）会记成 `(0, 0, 空数组)`：它的语义是"把画面清空"。
    以前这里直接跳过它 ⇒ 最后一张字幕会被无限期挂着（解码器侧假象，位流本身是对的）：
    实测 300 帧的验收里，参考只到第 120 帧，第 121~299 帧却报"PGS 还有内容 αmax=245"。
    """
    buf = open(path, "rb").read()
    i = 0
    pal = {}
    obj = None
    objpos = None
    pending_erase = False
    out = {}
    while i + 13 <= len(buf):
        if buf[i:i + 2] != b"PG":
            i += 1
            continue
        pts, _dts = struct.unpack(">II", buf[i + 2:i + 10])
        stype = buf[i + 10]
        ln = struct.unpack(">H", buf[i + 11:i + 13])[0]
        body = buf[i + 13:i + 13 + ln]
        i += 13 + ln
        if stype == 0x14:                              # PDS
            # body = [palette_id, version] + N*5 字节（索引, Y, Cr, Cb, T）
            n = (len(body) - 2) // 5
            for k in range(n):
                o = 2 + k * 5
                pal[body[o]] = (body[o + 1], body[o + 2], body[o + 3], body[o + 4])
        elif stype == 0x15:                            # ODS
            if len(body) < 11:
                continue
            _oid, _ver, seq = struct.unpack(">HBB", body[:4])
            if seq & 0x80:
                total = int.from_bytes(body[4:7], "big")
                w, h = struct.unpack(">HH", body[7:11])
                obj = bytearray(body[11:])
                objwh = (w, h)
            elif obj is not None:
                obj += body[4:]
        elif stype == 0x16:                            # PCS
            vw, vh, _fr, _cmp, state = struct.unpack(">HHBHB", body[:8])
            n_obj = body[10]
            if n_obj:
                _o, _w, _f, x, y = struct.unpack(">HBBHH", body[11:20])
                objpos = (x, y)
                pending_erase = False
            else:
                pending_erase = True                   # 擦除集：本 set 之后画面为空
        elif stype == 0x80:                            # END
            if pending_erase:
                out[pts] = (0, 0, np.zeros((0, 0, 4), np.float32))
                obj, objpos = None, None
                pending_erase = False
                continue
            if obj is None or objpos is None:
                continue
            if want_pts is not None and pts != want_pts:
                obj = None
                continue
            w, h = objwh
            idx = rle_decode(bytes(obj), w, h)
            lut_rgb = np.zeros((256, 3), np.float32)
            lut_a = np.zeros(256, np.float32)
            for k, (y, cr, cb, t) in pal.items():
                lut_rgb[k] = ycc_to_rgb(y, cr, cb)
                lut_a[k] = t
            rgba = np.concatenate([lut_rgb[idx], lut_a[idx][:, :, None]], axis=2)
            out[pts] = (objpos[0], objpos[1], rgba)
            obj = None
    return out


def flatten(sets, W, H, pts):
    """取 <= pts 的最后一个 display set 的合成图（简单版：set 自带完整对象）。"""
    ks = [k for k in sets if k <= pts]
    if not ks:
        return np.zeros((H, W, 4), np.float32)
    x, y, rgba = sets[max(ks)]
    canvas = np.zeros((H, W, 4), np.float32)
    h, w = rgba.shape[:2]
    if w <= 2 and h <= 2:
        return canvas
    canvas[y:y + h, x:x + w] = rgba
    return canvas
