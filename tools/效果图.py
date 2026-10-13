# -*- coding: utf-8 -*-
"""生成 README 里那两张效果图：① 往返对照（参考 / PGS / 差值×6） ② 12 档 α 阶梯逐档读数。

用法（先编一条全画布的 .sup，别用带 --crop 的，否则窗口外的阶梯档没有内容）：
    python gen_pgs.py palette --ass examples/demo.ass --size 1920x1080 --step 0.25 --pal _smoke/pal_full.npy --frames 120
    python gen_pgs.py encode  --ass examples/demo.ass --size 1920x1080 --pal _smoke/pal_full.npy --out _smoke/demo_full.sup --frames 120
    python tools/效果图.py --sup _smoke/demo_full.sup --ass examples/demo.ass --frame 45 --outdir docs

输出：<outdir>/roundtrip.png、<outdir>/alpha-ladder.png
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gen_pgs as G          # noqa: E402
import sup解码 as DEC         # noqa: E402

BG = (46, 48, 54)            # 对照图底片色（中暗灰，淡部能看出来）
LADDER_BG = (96, 98, 104)    # 阶梯底色用更亮的灰：α≈7 在纯黑上几乎看不见


def font(sz):
    for p in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    return ImageFont.load_default()


def label(im, xy, text, sz=17, fill=(238, 238, 242)):
    ImageDraw.Draw(im).text(xy, text, font=font(sz), fill=fill)


def comp(rgba, bg):
    """按播放器公式合成到不透明底色上：out = bg·(1-a) + c·a。"""
    a = rgba[:, :, 3:4] / 255.0
    return np.clip(np.asarray(bg, np.float32) * (1.0 - a) + rgba[:, :, :3] * a, 0, 255)


def main():
    ap = argparse.ArgumentParser(description='生成 README 的效果图')
    ap.add_argument('--sup', required=True)
    ap.add_argument('--ass', required=True)
    ap.add_argument('--size', default='1920x1080', help='画布（= 编码时的 --size）')
    ap.add_argument('--fps', type=float, default=60.0)
    ap.add_argument('--frame', type=int, default=45, help='取哪一帧（t = frame/fps）')
    ap.add_argument('--al-min', type=int, default=2)
    ap.add_argument('--outdir', default='docs')
    a = ap.parse_args()

    G.ASS = a.ass
    G.CANVAS_W, G.CANVAS_H = (int(v) for v in a.size.lower().split('x'))
    G.CX, G.CY, G.CW, G.CH = 0, 0, G.CANVAS_W, G.CANVAS_H   # 参考渲染走全画布
    G.FPS = a.fps
    G.AL_MIN = a.al_min
    t0 = a.frame / G.FPS
    # 只要这一帧：用 t0 让 libass 直接渲那一刻，别从 0 一帧帧读到目标帧
    pb = G.render(0, t0=t0, frames=1, duration=t0 + 0.2)
    pw = G.render(255, t0=t0, frames=1, duration=t0 + 0.2)
    fsz = G.CW * G.CH * 3
    bb, wb = pb.stdout.read(fsz), pw.stdout.read(fsz)
    pb.wait()
    pw.wait()
    if len(bb) < fsz or len(wb) < fsz:
        sys.exit('第 %d 帧渲染失败（拿到 %d / %d 字节）' % (a.frame, len(bb), fsz))
    ref = G.solve_ca(np.frombuffer(bb, np.uint8), np.frombuffer(wb, np.uint8))

    sets = DEC.decode(a.sup)
    pgs = DEC.flatten(sets, G.CANVAS_W, G.CANVAS_H, int(round(t0 * 90000.0)))
    m = ref[:, :, 3] >= G.AL_MIN
    ar, ao = ref[:, :, 3] / 255.0, pgs[:, :, 3] / 255.0
    pr = ref[:, :, :3][m] * ar[m, None]
    po = pgs[:, :, :3][m] * ao[m, None]
    rmse = float(np.sqrt(((pr - po) ** 2).mean()))
    ratio = float(ao[m].sum() / max(ar[m].sum(), 1e-9))
    na = len(np.unique(np.round(ao[m] * 255).astype(int)))

    # 只取内容所在的那块（避免大片空白），按画布比例换算
    sx, sy = G.CANVAS_W / 1920.0, G.CANVAS_H / 1080.0
    X0, Y0 = int(100 * sx), int(240 * sy)
    X1, Y1 = int(1900 * sx), int(980 * sy)
    sub_r, sub_p = ref[Y0:Y1, X0:X1], pgs[Y0:Y1, X0:X1]
    d_rgb = np.abs(sub_r[:, :, :3] * (sub_r[:, :, 3:4] / 255.0)
                   - sub_p[:, :, :3] * (sub_p[:, :, 3:4] / 255.0)) * 6.0
    d = np.clip(d_rgb + np.abs(sub_r[:, :, 3:4] - sub_p[:, :, 3:4]) / 255.0 * 255.0, 0, 255)
    panels = [comp(sub_r, BG), comp(sub_p, BG), d]
    titles = ['① 参考（libass 渲染 → 反解 (c,α) → 按播放器公式合成）',
              '② PGS 解码回来 → 同一公式合成',
              '③ 差值 ×6（|c·α 之差|，含 α 差）—— 黑=完全一致']
    W, Hh, HEAD = X1 - X0, Y1 - Y0, 74
    out = Image.new('RGB', (W, HEAD + 3 * (Hh + 30) + 12), (24, 25, 28))
    label(out, (10, 8), 'ASS → HDMV PGS 往返对照  ·  %s 第 %d 帧（t=%.3f s）'
          % (os.path.basename(a.ass), a.frame, t0), 20)
    label(out, (10, 36), '预乘空间 RMSE = %.3f / 255      Σα 保真比 = %.4f      '
          '解码帧 α 档位数 = %d（阈值：RMSE<8、比值 0.95~1.05、档位≥8）' % (rmse, ratio, na),
          16, (168, 214, 176))
    y = HEAD
    for t, p in zip(titles, panels):
        label(out, (10, y), t, 16, (216, 216, 220))
        out.paste(Image.fromarray(p.astype(np.uint8)), (0, y + 24))
        y += Hh + 30
    os.makedirs(a.outdir, exist_ok=True)
    p1 = os.path.join(a.outdir, 'roundtrip.png')
    out.save(p1)
    print('已写 %s  (%dx%d)' % (p1, out.width, out.height))

    # ---- α 阶梯：逐档标「参考 α / PGS α」 ----
    vals = [0xF8, 0xF0, 0xE8, 0xE0, 0xD0, 0xC0, 0xA8, 0x90, 0x70, 0x50, 0x30, 0x10]
    LY0, LY1 = int(884 * sy), int(976 * sy)
    BX0, BX1 = int(100 * sx), int(140 * sx)      # 第一档左边界 / 档宽
    BY0, BY1 = int(900 * sy), int(960 * sy)
    SH = LY1 - LY0
    YR, YP = 58, 58 + SH + 6
    im2 = Image.new('RGB', (int(1920 * sx), YP + SH + 92), (24, 25, 28))
    label(im2, (10, 6), 'α 阶梯（12 档，1:1）：PGS 只有 255 色可花，淡部还剩几档 —— 这就是建板那一步的成败', 19)
    label(im2, (10, 32), '上=参考（libass）  下=PGS 解码      每档下方：参考 α / PGS α', 15, (200, 200, 205))
    im2.paste(Image.fromarray(comp(ref[LY0:LY1], LADDER_BG).astype(np.uint8)), (0, YR))
    im2.paste(Image.fromarray(comp(pgs[LY0:LY1], LADDER_BG).astype(np.uint8)), (0, YP))
    d2 = ImageDraw.Draw(im2)
    ft, ft2 = font(14), font(13)
    bad = 0
    for i, v in enumerate(vals):
        x = BX0 + BX1 * i
        rr = float(ref[BY0:BY1, x:x + BX1, 3].mean())
        pp = float(pgs[BY0:BY1, x:x + BX1, 3].mean())
        bad += (abs(pp - rr) > 6)
        d2.rectangle([x, YR, x + BX1 - 1, YP + SH], outline=(90, 90, 96))
        yb = YP + SH + 6
        d2.text((x + 4, yb), 'ASS \\alpha&H%02X&' % v, font=ft2, fill=(150, 150, 158))
        d2.text((x + 4, yb + 20), '参考 %.0f' % rr, font=ft, fill=(240, 200, 120))
        d2.text((x + 4, yb + 40), 'PGS  %.0f' % pp, font=ft,
                fill=(120, 230, 140) if abs(pp - rr) <= 6 else (255, 120, 120))
    label(im2, (10, YP + SH + 68), '12 档里 参考/PGS 相差 >6 的有 %d 档 —— 淡部（α 7~63）也在其中，'
          '说明不是"只保住亮部"' % bad, 15, (200, 200, 205))
    p2 = os.path.join(a.outdir, 'alpha-ladder.png')
    im2.save(p2)
    print('已写 %s  (%dx%d)' % (p2, im2.width, im2.height))


if __name__ == '__main__':
    main()
