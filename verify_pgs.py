# -*- coding: utf-8 -*-
"""验收：`.sup` 往返自检 —— **不需要任何源片、原片、第三方素材**。

原理：同一条字幕，用【黑底】和【白底】各渲一遍，就能反解出播放器真正合成用的 (c, a)：
      黑底 B = c·a                （= 播出时真正加上去的光）
      白底 W = 255·(1-a) + c·a
    ⇒ a = 1 - (W-B)/255,  c = B/a
把这份「参考 (c,a)」与「`.sup` 解码回来的 (c,a)」在**预乘空间**逐像素比，就是本脚本的判据。
（gen_pgs.solve_ca 用的就是同一条公式；这套口径经第三张灰底渲染独立验证，预测/实测 99.8%。）

用法：
    python gen_pgs.py palette --ass examples/demo.ass --size 1920x1080 --crop 0,200,1400,800 --step 0.25 --pal _smoke/pal.npy
    python gen_pgs.py encode  --ass examples/demo.ass --size 1920x1080 --crop 0,200,1400,800 --pal _smoke/pal.npy --out _smoke/demo.sup --frames 120
    python verify_pgs.py --sup _smoke/demo.sup --ass examples/demo.ass --size 1920x1080 --crop 0,200,1400,800 --frames 120

    # 加一条"位流标准合规"的旁证：让 ffmpeg 自己的 PGS 解码器把 .sup 吃一遍
    python verify_pgs.py --sup _smoke/demo.sup --ass examples/demo.ass --crop 0,200,1400,800 --ffmpeg-check

三条判据（阈值就在下面的 PASS/FAIL 里，可直接当 CI 用）：
    ① 预乘空间 RMSE(0-255)     均值 < 8
    ② Σα 保真比 **均值**        0.95 ~ 1.05   （单帧 min/max 只作参考，见下）
    ③ 解码帧里出现的 α 档位    ≥ 8    ← 调色板是否真的把淡部铺开（淡部塌了会明显小于 8）

⚠️ 为什么必须比"预乘空间"、而且必须比"字幕层"：
   ① PGS 存的 (c,a) 在播放器里是 **c·a 参与加法**，直乘空间比会把淡部误差放大/掩盖；
   ② 字幕只占画面千分之几，**整帧 RMSE 会被背景吃掉**（本项目实测三档口径在整帧上只差 3%，
      必须减掉同一刻的无字幕帧才看得见差别）。
"""
import argparse
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sup_decode as DEC          # noqa: E402
import gen_pgs as G            # noqa: E402


NAMES = {0x14: 'PDS', 0x15: 'ODS', 0x16: 'PCS', 0x17: 'WDS', 0x80: 'END'}


def scan_segments(path):
    """按段头扫一遍 .sup，返回 (PCS 的 PTS 列表, 段类型计数)。

    段头**定长 13 字节**（`PG` + PTS(4 BE) + DTS(4 BE) + 类型(1) + 长度(2 BE)），
    所以这里不需要任何 vint 解析 —— 自写 vint 解析器遇到首字节 0x00 会死循环（踩过）。
    """
    raw = open(path, 'rb').read()
    i, cnt, pcs = 0, {}, []
    while i + 13 <= len(raw):
        if raw[i:i + 2] != b'PG':
            i += 1
            continue
        t = raw[i + 10]
        ln = int.from_bytes(raw[i + 11:i + 13], 'big')
        cnt[t] = cnt.get(t, 0) + 1
        if t == 0x16:
            pcs.append(int.from_bytes(raw[i + 2:i + 6], 'big'))
        i += 13 + ln
    return pcs, cnt


def ffmpeg_frame_count(suppath):
    """让 ffmpeg/ffprobe 自己数一遍 .sup 里的帧 —— 位流合规的旁证。"""
    ffprobe = os.path.join(os.path.dirname(G.FFMPEG), 'ffprobe.exe')
    if not os.path.exists(ffprobe):
        ffprobe = 'ffprobe'
    cmd = [ffprobe, '-v', 'error', '-count_frames', '-select_streams', 's',
           '-show_entries', 'stream=nb_read_frames', '-of', 'default=nw=1:nk=1', suppath]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return r.stdout.strip() or r.stderr.strip()
    except Exception as e:                                   # pragma: no cover
        return '（ffprobe 调用失败：%s）' % e


def main():
    ap = argparse.ArgumentParser(description='PGS(.sup) 往返自检：不依赖任何源片')
    ap.add_argument('--sup', required=True, help='待验收的 .sup')
    ap.add_argument('--ass', required=True, help='生成它用的 ASS（参考渲染用同一条）')
    ap.add_argument('--size', default='1920x1080', help='画布 = 编码时的 --size')
    ap.add_argument('--crop', default=None, help='渲染窗口 x,y,w,h（要和编码时一致）')
    ap.add_argument('--fps', type=float, default=60.0)
    ap.add_argument('--frames', type=int, default=120)
    ap.add_argument('--al-min', type=int, default=None, help='α 阈值，默认与 gen_pgs 一致（2）')
    ap.add_argument('--rmse-max', type=float, default=8.0, help='判据① 阈值')
    ap.add_argument('--ffmpeg-check', action='store_true', help='额外用 ffprobe 数帧做合规旁证')
    a = ap.parse_args()

    if not os.path.exists(a.sup):
        sys.exit('找不到 .sup：%s' % a.sup)
    if not os.path.exists(a.ass):
        sys.exit('找不到 ASS：%s' % a.ass)

    # 把参数灌进 gen_pgs 的模块级配置（与编码时的取值必须一致，否则比的是两条不同的轨）
    G.ASS = a.ass
    G.CANVAS_W, G.CANVAS_H = (int(v) for v in a.size.lower().split('x'))
    if a.crop:
        G.CX, G.CY, G.CW, G.CH = (int(v) for v in a.crop.split(','))
    else:
        G.CX, G.CY, G.CW, G.CH = 0, 0, G.CANVAS_W, G.CANVAS_H
    G.FPS = a.fps
    if a.al_min is not None:
        G.AL_MIN = a.al_min
    n = a.frames
    G.DURATION = n / a.fps + 0.5

    t0 = time.time()
    print('[配置] sup=%s | ass=%s | 画布 %dx%d | 窗口 %d,%d %dx%d | %g fps | %d 帧'
          % (os.path.basename(a.sup), os.path.basename(a.ass), G.CANVAS_W, G.CANVAS_H,
             G.CX, G.CY, G.CW, G.CH, G.FPS, n), flush=True)

    sets = DEC.decode(a.sup)
    if not sets:
        sys.exit('FAIL：.sup 里一个 display set 都没解出来')
    kk = sorted(sets)
    pcs, otypes = scan_segments(a.sup)
    if not pcs:
        sys.exit('FAIL：扫不到任何 PCS 段，这不是一个 PGS(.sup) 位流')
    print('位流：%d 个 display set（PCS），PTS %.3f ~ %.3f s；解出对象的 %d 组%s'
          % (len(pcs), pcs[0] / 90000.0, pcs[-1] / 90000.0, len(kk),
             '' if all(b > x for x, b in zip(pcs, pcs[1:])) else '   ⚠️ PTS 非严格递增'))
    if len(pcs) > len(kk):
        print('      末 %d 个 display set 是**擦除集**（n_obj=0、无 PDS/ODS，只把画面清掉）'
              % (len(pcs) - len(kk)))
    print('      段类型：' + '  '.join('%s×%d' % (NAMES.get(k, hex(k)), v)
                                      for k, v in sorted(otypes.items())))
    if a.ffmpeg_check:
        print('      ffmpeg 侧帧数（旁证）：%s' % ffmpeg_frame_count(a.sup))

    pb = G.render(0, frames=n, duration=G.DURATION)
    pw = G.render(255, frames=n, duration=G.DURATION)
    fsz = G.CW * G.CH * 3
    errs, ratios, alphas = [], [], set()
    empty_ref = 0
    for i in range(n):
        bb = pb.stdout.read(fsz)
        wb = pw.stdout.read(fsz)
        if len(bb) < fsz or len(wb) < fsz:
            print('  渲染只到第 %d 帧（参考源提前结束）' % i)
            break
        ref = G.solve_ca(np.frombuffer(bb, np.uint8), np.frombuffer(wb, np.uint8))
        ca = DEC.flatten(sets, G.CANVAS_W, G.CANVAS_H, int(round(i * 90000.0 / G.FPS)))
        ca = ca[G.CY:G.CY + G.CH, G.CX:G.CX + G.CW]
        ar = ref[:, :, 3] / 255.0
        ao = ca[:, :, 3] / 255.0
        m = ar >= (G.AL_MIN / 255.0)
        if not m.any():
            empty_ref += 1
            if ao.max() * 255 > G.AL_MIN:
                print('  ⚠️ 帧 %d：参考为空但 PGS 有内容（αmax=%.0f）' % (i, ao.max() * 255))
            continue
        pr = ref[:, :, :3][m] * ar[m, None]          # 预乘：c·a 才是真正加上去的光
        po = ca[:, :, :3][m] * ao[m, None]
        errs.append(float(np.sqrt(((pr - po) ** 2).mean())))
        ratios.append(float(ao[m].sum() / max(ar[m].sum(), 1e-9)))
        alphas.update(np.unique(np.round(ao[m] * 255).astype(int)).tolist())
    pb.wait()
    pw.wait()

    e = np.array(errs)
    ok_frames = len(errs)
    print('\n—— 判据 ——')
    if not ok_frames:
        sys.exit('FAIL：%d 帧里参考全空，说明 --ass/--size/--crop 与编码时不一致' % n)
    rmse_mean = float(e.mean())
    rmean = float(np.mean(ratios))
    rmin, rmax = float(min(ratios)), float(max(ratios))
    wide = sum(1 for r in ratios if r < 0.90 or r > 1.10)
    na = len(alphas)
    print('① 预乘空间 RMSE(0-255)：均值 %.3f  中位 %.3f  p95 %.3f  最大 %.3f'
          % (rmse_mean, float(np.median(e)), float(np.percentile(e, 95)), float(e.max())))
    print('   有内容帧 %d / %d（参考为空的 %d 帧已跳过）' % (ok_frames, n, empty_ref))
    print('② Σα 保真比（PGS/参考）：均值 %.4f  最小 %.4f  最大 %.4f'
          % (rmean, rmin, rmax))
    print('   单帧比值落在 0.90~1.10 外的 %d/%d 帧 —— 淡部像素正好卡在 α 阈上，'
          '单帧正负偏差属正常，判据看均值' % (wide, ok_frames))
    print('③ 解码帧 α 档位数 = %d（最高 α = %d）' % (na, max(alphas) if alphas else 0))

    bad = []
    if rmse_mean >= a.rmse_max:
        bad.append('① RMSE 均值 %.2f ≥ %.2f' % (rmse_mean, a.rmse_max))
    if not (0.95 <= rmean <= 1.05):
        bad.append('② Σα 保真比均值 %.4f 不在 0.95~1.05' % rmean)
    if na < 8:
        bad.append('③ α 档位只有 %d 个' % na)
    print('\n%s（%.0fs）' % ('PASS ✔' if not bad else 'FAIL ✘：' + '；'.join(bad), time.time() - t0))
    sys.exit(0 if not bad else 1)


if __name__ == '__main__':
    main()
