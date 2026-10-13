"""把 libass 渲染的 ASS 编码成 PGS(.sup) 位图字幕轨（HDMV PGS）。

两趟：
  1) palette：抽样收集颜色 → 预乘空间中值切分 → **按内容自适应**的 255 色调色板
  2) encode ：流式渲染 → 逐帧量化到该调色板 → RLE → PGS display set

PGS 只有 256 色调色板，而半透明淡部（α≈2~60）要占掉大量档位。本工程的做法是
**按这条字幕自己的内容建板 + 在预乘空间里量化**，淡部的层次才保得住；细节见 docs/design.md。

坐标：ASS 的 PlayRes = `--size`；可选用 `--crop x,y,w,h` 只渲染内容所在的窗口（提速），
PGS 对象会按窗口偏移摆放。

用法：
    python gen_pgs.py palette --ass 字幕.ass --size 3840x1638 --pal pal.npy
    python gen_pgs.py encode  --ass 字幕.ass --size 3840x1638 --pal pal.npy --out 字幕.sup
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgsenc import PgsWriter, build_palette, rle_encode


def find_ffmpeg():
    """FFMPEG 环境变量 → PATH → 报错退出。"""
    p = os.environ.get('FFMPEG')
    if p and os.path.exists(p):
        return p
    p = shutil.which('ffmpeg')
    if p:
        return p
    sys.exit('找不到 ffmpeg：请装好并放进 PATH，或设环境变量 FFMPEG=<ffmpeg 可执行文件>')


FFMPEG = find_ffmpeg()
ASS = os.environ.get('ASS_FILE') or None    # 输入 ASS，也可用 --ass


def ass_end_time(path):
    """粗读 ASS，取最后一条 Dialogue 的结束时间（秒）。

    用来给渲染定长 —— lavfi 的 color 源是无界的，不给 `-t` 就会一直跑下去。
    """
    import re
    pat = re.compile(r'^Dialogue:\s*[^,]*,\s*'
                     r'(\d+):(\d{2}):(\d{2})[.:](\d{2}),\s*'
                     r'(\d+):(\d{2}):(\d{2})[.:](\d{2})')
    end = 0.0
    with open(path, 'r', encoding='utf-8-sig', errors='replace') as fh:
        for line in fh:
            m = pat.match(line.strip())
            if m:
                g = [int(v) for v in m.groups()]
                end = max(end, g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 100.0)
    return end


# 可选：OVERLAY_SUP=已有一条 PGS，垫在本 ASS 下面一起编码（做单轨用）。
# 该 .sup 的 PTS 必须是**绝对时间轴**（与最终视频 PTS 对齐），否则会整体错位。
OVERLAY_SUP = os.environ.get('OVERLAY_SUP') or None
# 可选：字体目录，锁死字体（避免 libass 回退到别的字体）
ASS_FONTS_DIR = os.environ.get('ASS_FONTS_DIR') or None

CANVAS_W, CANVAS_H = 1920, 1080               # 画布（= ASS PlayRes），由 --size 覆盖
CX, CY, CW, CH = 0, 0, CANVAS_W, CANVAS_H     # 渲染窗口，由 --crop 覆盖
FPS = 60.0
DURATION = None     # 渲染时长上限（秒）；None = 读到流尾
ABS_PTS = False     # True = PTS 用绝对时间轴（t0 起算）
AL_MIN = 2          # 低于此 α 视为透明（淡透明像素大多落在 2~30，别砍）
MARGIN = 8          # 动态窗口外扩
WA = 1.0            # 预乘空间中 α 维度权重（播出误差含 255·Δα，必须与颜色同权）
FAINT_BOOST = float(os.environ.get('FAINT_BOOST', '1.0'))  # 低 α 像素计数加权；1.0=不加权
# 注：FAINT_BOOST > 1 会把中值切分的质心往淡端拽，亮像素被映射到偏暗的格子 → 亮端整体被压暗
#（实测 t=25.9 亮像素 PGS/源 = 0.67，跨亮度档一条平线）。淡端分级靠 AL_MIN + 预乘空间已经够。
FPS_CODE = {23.976: 0x10, 24.0: 0x20, 25.0: 0x30, 29.97: 0x40,
            30.0: 0x50, 50.0: 0x60, 59.94: 0x70, 60.0: 0x80}   # PCS frame_rate 字段


def solve_ca(black_rgb, white_rgb):
    """由【黑底】和【白底】两张不透明渲染反解第 (c, a)：
         黑底 B = c*a          （= 播出时真正加上去的光）
         白底 W = 255*(1-a) + c*a
       => a = 1 - (W-B)/255,  c = B/a
    这套反解经第三张灰底渲染独立验证（预测/实测 = 99.8%），给出的就是播放器
    合成时用的 (c, a)。此前用"透明画布 + alpha=1"取 alpha 通道，实测该通道只有
    真实 alpha 的 63%（各 alpha 档一致 58~65%），导致整条字幕被系统性压暗到 2/3。
    """
    B = black_rgb.astype(np.float32).reshape(CH, CW, 3)
    W = white_rgb.astype(np.float32).reshape(CH, CW, 3)
    a = np.clip(1.0 - (W - B).mean(2) / 255.0, 0.0, 1.0)
    out = np.empty((CH, CW, 4), np.uint8)
    nz = a > (1.5 / 255.0)
    c = np.zeros_like(B)
    c[nz] = np.clip(B[nz] / a[nz, None], 0, 255)
    out[:, :, :3] = np.round(c).astype(np.uint8)
    out[:, :, 3] = np.round(a * 255.0).astype(np.uint8)
    return out


def _ass_filter():
    """ffmpeg 滤镜里的路径：正斜杠 + 转义冒号 + 单引号包裹。"""
    p = ASS.replace('\\', '/').replace(':', r'\:')
    f = ''
    if ASS_FONTS_DIR:
        fd = ASS_FONTS_DIR.replace('\\', '/').replace(':', r'\:')
        f = ":fontsdir='%s'" % fd
    return "ass='%s'%s:alpha=1" % (p, f)


def render(bg, select=None, t0=0.0, frames=None, duration=None):
    """启动 ffmpeg 渲染进程，stdout 输出 rgb24 原始帧（底色为不透明 bg）。

    duration：输入时长上限（lavfi 源不限时会长跑）。
    OVERLAY_SUP 非空时改用 filter_complex：本 ASS 画在底色上，再用 overlay 把
    那条已有 PGS 压在下面一层，最后统一 crop 到渲染窗口。
    """
    chain = []
    if select:
        chain.append("select='%s'" % select)
    if t0:
        chain.append('setpts=PTS+%g/TB' % t0)
    chain.append(_ass_filter())
    crop = 'crop=%d:%d:%d:%d' % (CW, CH, CX, CY)
    cmd = [FFMPEG, '-v', 'error', '-nostdin']
    if duration:
        cmd += ['-t', '%.3f' % duration]
    cmd += ['-f', 'lavfi', '-i', 'color=c=0x%02x%02x%02x:s=%dx%d:r=%g' % (
        bg, bg, bg, CANVAS_W, CANVAS_H, FPS)]
    if OVERLAY_SUP:
        # 坑：用了 -filter_complex 就不能再给 -vf，且必须显式 -map "[v]"。
        # overlay 对稀疏的位图字幕流天然正确：framesync 会一直保持上一张字幕帧
        # 直到下一张到来，正好等于 PGS 的显示集语义。
        if duration:
            cmd += ['-t', '%.3f' % duration]
        cmd += ['-i', OVERLAY_SUP]
        fc = '[0:v]%s[a];[a][1:s:0]overlay=format=auto[b];[b]%s[v]' % (
            ','.join(chain), crop)
        cmd += ['-filter_complex', fc, '-map', '[v]']
    else:
        chain.append(crop)
        cmd += ['-vf', ','.join(chain)]
    if frames:
        cmd += ['-frames:v', str(frames)]
    cmd += ['-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=CW * CH * 3 * 4)


def premul_feat(rgba):
    p = rgba[:, :3].astype(np.float64) * (rgba[:, 3:4].astype(np.float64) / 255.0)
    return np.concatenate([p, rgba[:, 3:4].astype(np.float64) * WA], axis=1)


def median_cut(cols, wts, n):
    boxes = [(cols, wts)]
    while len(boxes) < n:
        best, bi = -1.0, -1
        for i, (c, w) in enumerate(boxes):
            if len(c) < 2:
                continue
            s = w.sum() * (c.max(0) - c.min(0)).max()
            if s > best:
                best, bi = s, i
        if bi < 0:
            break
        c, w = boxes.pop(bi)
        ax = int(np.argmax(c.max(0) - c.min(0)))
        o = np.argsort(c[:, ax])
        c, w = c[o], w[o]
        cum = np.cumsum(w)
        k = max(1, min(len(c) - 1, int(np.searchsorted(cum, cum[-1] / 2.0)) + 1))
        boxes.append((c[:k], w[:k]))
        boxes.append((c[k:], w[k:]))
    return np.array([np.average(c, axis=0, weights=w) for c, w in boxes], dtype=np.float64)


def build_global_palette(out_npy, seconds_step=1.0, duration=None):
    """第一趟：抽样收集颜色，建立预乘空间的全局调色板。

    抽样在 **Python 侧**做（每 seconds_step 秒取一帧）。原来的实现靠 ffmpeg 的
    ``select`` 滤镜抽样，有两个坑：① lavfi 的 color 源是无界的，一旦没把时长传下去
    就会**无限读下去**（实测 2.5s 的窗口读到 3000+ 帧）；② 时长若写成函数默认参数
    ``duration=DURATION``，会在模块导入时就把值固定住，命令行后来改 DURATION 根本不生效。
    ⇒ 改成"按上限渲染 + Python 抽样"，行为可预测。
    """
    if duration is None:
        duration = DURATION
    if not duration:
        sys.exit('建板需要知道时长：给 --duration / --frames（CLI 会自动按 ASS 末条事件定长）')
    step = max(1, int(round(seconds_step * FPS)))
    pb = render(0, duration=duration)
    pw = render(255, duration=duration)
    fsize = CW * CH * 3
    hist = {}
    n = 0
    kept = 0
    t0 = time.time()
    while True:
        buf = pb.stdout.read(fsize)
        wbuf = pw.stdout.read(fsize)
        if len(buf) < fsize or len(wbuf) < fsize:
            break
        if n % step:                      # 抽样在 Python 侧做（见函数注释）
            n += 1
            continue
        f = solve_ca(np.frombuffer(buf, dtype=np.uint8), np.frombuffer(wbuf, dtype=np.uint8)).reshape(-1, 4)
        m = f[:, 3] >= AL_MIN
        if m.any():
            packed = (f[m, 0].astype(np.uint32) << 24) | (f[m, 1].astype(np.uint32) << 16) | \
                     (f[m, 2].astype(np.uint32) << 8) | f[m, 3]
            u, cnt = np.unique(packed, return_counts=True)
            for k, v in zip(u.tolist(), cnt.tolist()):
                hist[k] = hist.get(k, 0) + v
        n += 1
        kept += 1
        if kept % 25 == 0:
            print('  抽样 %d 帧（共读 %d 帧），颜色 %d 种（%.0fs）'
                  % (kept, n, len(hist), time.time() - t0), flush=True)
    pb.wait()
    pw.wait()
    print('抽样完成：抽了 %d 帧（共读 %d 帧），%d 种颜色' % (kept, n, len(hist)))

    packed = np.array(list(hist.keys()), dtype=np.uint32)
    wts = np.array(list(hist.values()), dtype=np.float64)
    cols = np.stack([(packed >> 24) & 255, (packed >> 16) & 255,
                     (packed >> 8) & 255, packed & 255], axis=1).astype(np.float64)
    feat = premul_feat(cols)
    # 低 alpha 端加权：暗晕像素数量巨大，不加权会把 255 色几乎全花在中高 alpha 上，
    # 结果就是淡粒子只有几档可落 → 发平、发闷。
    w = wts * (1.0 + FAINT_BOOST / (1.0 + cols[:, 3] / 8.0))
    cen = median_cut(feat, w, 255)
    P = cen[:, :3]
    A = np.clip(cen[:, 3] / WA, 0, 255)
    scale = np.where(A > 0.5, A / 255.0, 1.0)[:, None]
    pal = np.concatenate([np.clip(np.round(P / scale), 0, 255), np.round(A)[:, None]], axis=1)
    np.save(out_npy, pal.astype(np.uint8))
    a = pal[:, 3].astype(int)
    print('调色板已存 %s：%d 色；alpha<16 有 %d 色、<32 有 %d 色、最低 %d' % (
        out_npy, len(pal), int((a < 16).sum()), int((a < 32).sum()), int(a.min())))
    return pal


def encode(pal_npy, out_sup, frames=None, t0=0.0, duration=None):
    if duration is None:                  # ⚠️ 别写成默认参数 duration=DURATION：
        duration = DURATION               #    默认参数在模块导入时求值，CLI 改不动它
    pal = np.load(pal_npy).astype(np.float64)
    pal_feat = premul_feat(pal)
    pal_bytes = build_palette(pal)
    fps_code = FPS_CODE[min(FPS_CODE, key=lambda k: abs(k - float(FPS)))]
    wtr = PgsWriter(out_sup, CANVAS_W, CANVAS_H, fps_code)
    proc = render(0, t0=t0, frames=frames, duration=duration)
    procw = render(255, t0=t0, frames=frames, duration=duration)
    fsize = CW * CH * 3
    cache = {}
    i = 0
    prev_had = False
    # PCS composition_state 语义：0x80=epoch start，0x00=normal（本 display set 就是完整合成），
    # 0x40=acquisition point（合成不完整，解码器保留之前对象再叠加）。整条流每个 set 都自带完整
    # 调色板和唯一对象，所以除首个 set 用 0x80 外，其余必须用 0x00；用 0x40 会让每帧把上一帧的
    # 对象一起画出来 → 细颗粒出现灰色拖影（实测解码帧像素数正好翻倍）。
    first = [True]

    def st():
        s = 0x80 if first[0] else 0x00
        first[0] = False
        return s
    t0w = time.time()
    while True:
        buf = proc.stdout.read(fsize)
        wbuf = procw.stdout.read(fsize)
        if len(buf) < fsize or len(wbuf) < fsize:
            break
        f = solve_ca(np.frombuffer(buf, dtype=np.uint8), np.frombuffer(wbuf, dtype=np.uint8))
        a = f[:, :, 3]
        mask = a >= AL_MIN
        pts = int(round(((t0 + i / float(FPS)) if ABS_PTS else (i / float(FPS))) * 90000.0))
        if not mask.any():
            if prev_had:                      # 上一帧有内容 → 发空 display set 擦除
                wtr.display_set(pts, 0, 0, 1, 1, b'', palette=None, state=st(), n_obj=0)
                prev_had = False
            i += 1
            continue
        ys, xs = np.nonzero(mask)
        y0 = max(0, int(ys.min()) - MARGIN); y1 = min(CH, int(ys.max()) + MARGIN + 1)
        x0 = max(0, int(xs.min()) - MARGIN); x1 = min(CW, int(xs.max()) + MARGIN + 1)
        sub = f[y0:y1, x0:x1]
        flat = sub.reshape(-1, 4)
        m = flat[:, 3] >= AL_MIN
        packed = (flat[:, 0].astype(np.uint32) << 24) | (flat[:, 1].astype(np.uint32) << 16) | \
                 (flat[:, 2].astype(np.uint32) << 8) | flat[:, 3]
        u, inv = np.unique(packed[m], return_inverse=True)
        idx_u = np.empty(len(u), dtype=np.uint8)
        miss = []
        for j, k in enumerate(u.tolist()):
            v = cache.get(k)
            if v is None:
                miss.append(j)
            else:
                idx_u[j] = v
        if miss:
            mi = np.array(miss, dtype=np.int64)
            mc = np.stack([(u[mi] >> 24) & 255, (u[mi] >> 16) & 255,
                           (u[mi] >> 8) & 255, u[mi] & 255], axis=1).astype(np.float64)
            mf = premul_feat(mc)
            jj = np.empty(len(mi), dtype=np.uint8)
            for s0 in range(0, len(mi), 4096):        # 分块，避免大矩阵爆内存
                s1 = min(len(mi), s0 + 4096)
                d = ((mf[s0:s1, None, :] - pal_feat[None, :, :]) ** 2).sum(-1)
                jj[s0:s1] = d.argmin(1).astype(np.uint8) + 1
            ul = u.tolist()
            for t, j in enumerate(miss):
                idx_u[j] = jj[t]
                cache[ul[j]] = int(jj[t])
        img = np.zeros(len(flat), dtype=np.uint8)
        img[m] = idx_u[inv]
        if os.environ.get('PGDEBUG') and int(os.environ['PGDEBUG']) > 0:
            os.environ['PGDEBUG'] = str(int(os.environ['PGDEBUG']) - 1)
            sel = pal[idx_u[inv].astype(np.int64) - 1]          # 每个被映射像素落到哪个调色板条目
            sc = f[:, :, :3][mask].astype(np.float64)
            sa = f[:, :, 3][mask].astype(np.float64)
            print('  DBG 帧%5d: 源 Σα %8.2f Σ(c*a) %8.2f  ->  调色板 Σα %8.2f Σ(C*A) %8.2f   (比 %.3f / %.3f)' % (
                i, sa.sum() / 255.0, (sc * (sa / 255.0)[:, None]).sum() / 765.0,
                sel[:, 3].sum() / 255.0, (sel[:, :3].sum(1) * sel[:, 3] / 255.0).sum() / 765.0,
                (sel[:, 3].sum() / 255.0) / max(sa.sum() / 255.0, 1e-9),
                ((sel[:, :3].sum(1) * sel[:, 3] / 255.0).sum() / 765.0) /
                max((sc * (sa / 255.0)[:, None]).sum() / 765.0, 1e-9)), flush=True)
        rle = rle_encode(img.reshape(sub.shape[0], sub.shape[1]))
        wtr.display_set(pts, CX + x0, CY + y0, x1 - x0, y1 - y0, rle,
                        palette=pal_bytes, state=st())
        prev_had = True
        i += 1
        if i % 300 == 0:
            el = time.time() - t0w
            left = ('剩余约 %.0f 分钟' % ((frames - i) / max(0.1, i / el) / 60.0)) if frames else ''
            print('  帧 %6d  输出 %6.1f MB  %.1f 帧/秒  缓存 %d 色  %s' % (
                i, wtr.bytes_written / 1e6, i / el, len(cache), left), flush=True)
    if prev_had:
        last = (t0 + i / float(FPS)) if ABS_PTS else (i / float(FPS))
        wtr.display_set(int(round(last * 90000.0)), 0, 0, 1, 1, b'',
                        palette=None, state=0x00, n_obj=0)
    wtr.close()
    proc.wait()
    procw.wait()
    print('完成：%d 帧 → %s（%.1f MB，%.1f 分钟）' % (
        i, out_sup, wtr.bytes_written / 1e6, (time.time() - t0w) / 60.0))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='ASS → HDMV PGS(.sup)')
    ap.add_argument('mode', choices=['palette', 'encode'])
    ap.add_argument('--ass', default=ASS, help='输入 ASS（也可用环境变量 ASS_FILE）')
    ap.add_argument('--pal', default='palette.npy', help='调色板 .npy（palette 写、encode 读）')
    ap.add_argument('--out', default='out.sup', help='输出 .sup')
    ap.add_argument('--size', default='1920x1080', help='画布 = ASS PlayRes，如 3840x1638')
    ap.add_argument('--crop', default=None, help='只渲染窗口 x,y,w,h（提速）；留空 = 整幅')
    ap.add_argument('--fps', type=float, default=60.0)
    ap.add_argument('--t0', type=float, default=0.0, help='从 ASS 的第几秒开始')
    ap.add_argument('--frames', type=int, default=None, help='渲染多少帧')
    ap.add_argument('--duration', type=float, default=None, help='渲染时长上限（秒）')
    ap.add_argument('--abs-pts', action='store_true',
                    help='PTS 用绝对时间轴（默认从 0 起算）；相对 PTS 的分段 .sup 不能直接拼')
    ap.add_argument('--step', type=float, default=1.0, help='建板抽样间隔（秒）')
    ap.add_argument('--fonts-dir', default=ASS_FONTS_DIR, help='字体目录（锁死字体）')
    ap.add_argument('--al-min', type=int, default=None, help='α 阈值（0-255），默认 2')
    ap.add_argument('--margin', type=int, default=None, help='对象窗口外扩像素，默认 8')
    ap.add_argument('--faint-boost', type=float, default=None, help='淡部计数加权，默认 1.0')
    a = ap.parse_args()

    CANVAS_W, CANVAS_H = (int(v) for v in a.size.lower().split('x'))
    if a.crop:
        CX, CY, CW, CH = (int(v) for v in a.crop.split(','))
    else:
        CX, CY, CW, CH = 0, 0, CANVAS_W, CANVAS_H
    FPS = a.fps
    ABS_PTS = a.abs_pts
    ASS_FONTS_DIR = a.fonts_dir
    if a.al_min is not None:
        AL_MIN = a.al_min
    if a.margin is not None:
        MARGIN = a.margin
    if a.faint_boost is not None:
        FAINT_BOOST = a.faint_boost
    if not a.ass:
        sys.exit('要指定输入字幕：--ass <字幕.ass>（或环境变量 ASS_FILE）')
    if not os.path.exists(a.ass):
        sys.exit('ASS 不存在：%s' % a.ass)
    ASS = a.ass
    if a.frames:
        DURATION = a.t0 + a.frames / float(FPS) + 1.0
    elif a.duration:
        DURATION = a.duration
    else:
        DURATION = ass_end_time(ASS) + 0.5      # 不给长度就按 ASS 末条事件自动定长
        if DURATION <= 0.5:
            sys.exit('读不出 ASS 的事件时间，请显式给 --duration 或 --frames')

    print('[配置] %s | 画布 %dx%d | 窗口 %d,%d %dx%d | %g fps | t0=%.3f | %s%s'
          % (os.path.basename(ASS), CANVAS_W, CANVAS_H, CX, CY, CW, CH, FPS, a.t0,
             ('%d 帧' % a.frames) if a.frames else
             ('时长 %.2fs' % DURATION if DURATION else '到流尾'),
             ' | 绝对 PTS' if ABS_PTS else ''), flush=True)
    if a.mode == 'palette':
        build_global_palette(a.pal, a.step)
    else:
        encode(a.pal, a.out, a.frames, a.t0)
