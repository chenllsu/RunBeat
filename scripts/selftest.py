"""自检脚本：验证 ffmpeg 能力、BPM 检测、倍率换算与时间伸缩是否都正确。

用法（在 runbeat 目录下）：
    .venv\\Scripts\\python.exe scripts\\selftest.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import librosa
import numpy as np
import soundfile as sf

from app import audio, config


def make_click_track(path: Path, bpm: float, seconds: float = 24.0) -> Path:
    """合成一段固定 BPM 的打击音轨，用来验证检测与变速是否准确。"""
    sr = 44100
    t = np.arange(int(sr * seconds)) / sr
    period = 60.0 / bpm

    # 每拍一个高频点击 + 一个低频底鼓，模拟鼓点明显的音乐
    phase = np.mod(t, period)
    hi = 0.55 * np.sin(2 * np.pi * 1000.0 * t) * np.exp(-45 * phase)
    kick = 0.60 * np.sin(2 * np.pi * (55.0 + 45.0 * np.exp(-14 * phase)) * t) * np.exp(-11 * phase)
    pad = 0.12 * np.sin(2 * np.pi * 220.0 * t)

    signal = hi + kick + pad
    signal = signal / (np.max(np.abs(signal)) + 1e-9) * 0.9
    stereo = np.stack([signal, signal], axis=1)
    sf.write(str(path), stereo, sr, subtype="PCM_16")
    return path


def section(title: str) -> None:
    print()
    print("=" * 62)
    print(title)
    print("=" * 62)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="runbeat_selftest_"))
    failures: list[str] = []

    section("1. 环境与 ffmpeg")
    print("ffmpeg 路径 :", audio.ffmpeg())
    codec_args, ext = audio.output_settings()
    print("输出格式   :", ext, codec_args)
    if ext != ".mp3":
        failures.append(f"ffmpeg 没有 mp3 编码器，实际输出 {ext}")
    print("分析采样率 :", config.ANALYSIS_SR)

    section("2. atempo 滤镜串生成")
    for ratio in (0.5, 0.85, 1.0, 1.15, 2.0, 3.0, 0.3):
        print(f"  ×{ratio:<5} -> {audio.atempo_chain(ratio)}")

    section("3. 合成测试音频并检测 BPM")
    src = workdir / "click_120.wav"
    make_click_track(src, bpm=120.0, seconds=24.0)
    info = audio.probe(src)
    print(f"探测结果   : 时长 {info.duration:.2f}s / {info.sample_rate} Hz / {info.codec}")
    if abs(info.duration - 24.0) > 0.2:
        failures.append(f"时长探测不准：期望约 24s，实际 {info.duration:.2f}s")

    analysis = audio.to_analysis_wav(src, workdir / "analysis.wav")
    detected = audio.detect_bpm(analysis)
    print(f"检测 BPM   : {detected['bpm']}  (真值 120.0)")
    print(f"原始检测值 : {detected['bpm_raw']}   裁决结论: {detected['adjudicated']}")
    print(f"候选值     : {detected['candidates']}")
    print(f"清晰度     : {detected['clarity']}")
    if abs(detected["bpm"] - 120.0) > 2.0:
        failures.append(f"BPM 检测偏差过大：期望约 120，实际 {detected['bpm']}")
    # 这条是回归闸：本来就读对的曲子，裁决**不许**自作聪明改掉
    if detected["adjudicated"] not in ("keep", "keep-margin"):
        failures.append(
            f"120 BPM 的干净样本被裁决改判了：{detected['bpm_raw']} -> {detected['bpm']}"
            f"（{detected['adjudicated']}）"
        )

    section("4. BPM 半速 / 倍速裁决")
    # 真值越接近 120 的自相关先验，librosa 越容易只读到它的一半 —— 175 / 184.6
    # 这些「跑者常用步频」正是重灾区（实测分别被读成 87.6 / 92.3）。
    # 裁决的目标不是「永远改判」，而是**最终值要回到真值附近**。
    adj_cases = [100.0, 120.0, 140.0, 160.0, 175.0, 184.6]
    for truth in adj_cases:
        sample = workdir / f"adj_{truth}.wav"
        make_click_track(sample, truth, 20.0)
        ana = audio.to_analysis_wav(sample, workdir / f"adj_ana_{truth}.wav")
        got = audio.analyze(ana, buckets=0)
        err = abs(got["bpm"] - truth)
        ok = err <= 3.0
        print(
            f"  [{'OK ' if ok else 'BAD'}] 真值 {truth:>5.1f} | 检测 {got['bpm_raw']:>5.1f} "
            f"-> 裁决 {got['bpm']:>5.1f} ({got['adjudicated']:<12}) 误差 {err:.1f}"
        )
        if not ok:
            failures.append(
                f"BPM 裁决没能修正半速误判：真值 {truth}，检测 {got['bpm_raw']}，"
                f"裁决后 {got['bpm']}"
            )

    section("5. 均匀网格（周期拟合 + 相位搜索）")
    # 拍点走严格等间隔的网格。三条断言：
    #   ① 间隔**严格**相等 —— 跟着跑的节拍必须稳，这是整个改动的目的
    #   ② 相位搜索能把网格整体对齐到鼓点上
    #   ③ 周期拟合比 beat_track 的 tempo 准（tempo 被量化过，会累积漂移）
    grid_sample = workdir / "grid_120.wav"
    make_click_track(grid_sample, 120.0, 20.0)
    grid_ana = audio.to_analysis_wav(grid_sample, workdir / "grid_analysis.wav")

    gy, gsr = librosa.load(str(grid_ana), sr=config.ANALYSIS_SR, mono=True)
    gy, _gtrim = librosa.effects.trim(gy, top_db=40.0)
    genv = librosa.onset.onset_strength(y=gy, sr=gsr, hop_length=config.ANALYSIS_HOP)
    gdur = gy.size / float(gsr)
    gperiod = 60.0 / 120.0

    ggrid, ghit, gphase = audio._uniform_grid(
        genv, config.ANALYSIS_HOP, gsr, gperiod, gdur
    )
    ggaps = np.diff(np.asarray(ggrid, dtype=np.float64))
    spread = float(np.max(np.abs(ggaps - gperiod))) * 1000 if ggaps.size else -1.0
    flat_ok = bool(ggaps.size >= 10 and spread < 1e-6)
    print(
        f"  间隔严格相等：{ggaps.size} 个间隔，最大偏离周期 {spread:.9f} ms  "
        f"{'OK ' if flat_ok else 'BAD'}"
    )
    if not flat_ok:
        failures.append(f"均匀网格的间隔不是严格相等的，最大偏离 {spread:.6f} ms")

    # 合成鼓点落在 i*period 上，所以网格相位对 period 取模应该回到 0 附近
    rest = float(gphase) % gperiod
    phase_err = min(rest, gperiod - rest) * 1000
    phase_ok = phase_err < 20.0
    print(
        f"  相位对齐鼓点：偏差 {phase_err:.1f} ms（允许 20 ms），贴合度 {ghit:.2f}x  "
        f"{'OK ' if phase_ok else 'BAD'}"
    )
    if not phase_ok:
        failures.append(f"均匀网格的相位没对齐到鼓点：偏差 {phase_err:.1f} ms")

    ginfo = audio.analyze(grid_ana, buckets=0)
    pf = float(ginfo["grid_period"])
    perr = abs(pf - gperiod) / gperiod * 100
    period_ok = perr < 0.5
    print(
        f"  周期拟合：{pf*1000:.4f} ms（真值 {gperiod*1000:.4f} ms）误差 {perr:.3f}%，"
        f"bpm {ginfo['bpm']}  {'OK ' if period_ok else 'BAD'}"
    )
    if not period_ok:
        failures.append(f"周期拟合偏差过大：{pf*1000:.4f} ms vs 真值 {gperiod*1000:.4f} ms")

    section("6. 步频 -> 倍率 换算")
    cases = [
        # (原曲BPM, 目标步频, 换算, min, max, 期望倍率)
        (120.0, 130.0, "1:1", 0.85, 1.15, 1.083333),
        (120.0, 170.0, "1:1", 0.85, 1.15, 1.15),
        (120.0, 170.0, "1:2", 0.85, 1.15, 0.85),
        (170.0, 170.0, "1:1", 0.85, 1.15, 1.0),
        (170.0, 170.0, "1:2", 0.85, 1.15, 0.85),
    ]
    for source_bpm, spm, mapping, lo, hi, expect in cases:
        plan = audio.plan_ratio(source_bpm, spm, mapping, lo, hi)
        ok = abs(plan.ratio - expect) < 1e-4
        flag = "OK " if ok else "BAD"
        print(
            f"  [{flag}] 原曲{source_bpm:>5.1f} 目标{spm:>5.0f} {mapping} "
            f"范围{lo}~{hi} -> 倍率 {plan.ratio:.4f} "
            f"({plan.natural}, 需 {plan.requested_ratio:.4f}) 实际 {plan.actual_spm:.1f} spm"
        )
        if not ok:
            failures.append(f"倍率换算错误：{source_bpm}/{spm}/{mapping} -> {plan.ratio}，期望 {expect}")

    section("7. 时间伸缩 + 结果复检")
    plans = [
        (120.0, 130.0, "1:1"),   # 范围内的正常变速
        (120.0, 170.0, "1:1"),   # 会被截断到 1.15x
    ]
    for source_bpm, spm, mapping in plans:
        plan = audio.plan_ratio(source_bpm, spm, mapping, 0.85, 1.15)
        out = workdir / f"out_{spm}_{mapping.replace(':', '-')}{ext}"
        audio.render(src, out, plan.ratio, None, None)

        out_info = audio.probe(out)
        expected_dur = info.duration / plan.ratio
        dur_ok = abs(out_info.duration - expected_dur) < 0.4

        out_analysis = audio.to_analysis_wav(out, workdir / f"ana_{spm}_{mapping.replace(':', '-')}.wav")
        out_bpm = audio.detect_bpm(out_analysis)["bpm"]
        expected_bpm = source_bpm * plan.ratio
        bpm_ok = abs(out_bpm - expected_bpm) < 3.0

        print(
            f"  倍率 ×{plan.ratio:.4f} | 时长 {info.duration:.2f}s -> {out_info.duration:.2f}s "
            f"({'OK ' if dur_ok else 'BAD'}, 期望 {expected_dur:.2f}s) | "
            f"BPM 120 -> {out_bpm} ({'OK ' if bpm_ok else 'BAD'}, 期望 {expected_bpm:.1f})"
        )
        if not dur_ok:
            failures.append(f"变速后时长不对：{out_info.duration:.2f}s，期望 {expected_dur:.2f}s")
        if not bpm_ok:
            failures.append(f"变速后 BPM 不对：{out_bpm}，期望 {expected_bpm:.1f}")

    section("8. 片段截取（预览）")
    clip = workdir / f"clip{ext}"
    audio.render(src, clip, 1.10, start=8.0, length=6.0)
    clip_info = audio.probe(clip)
    expected_clip = 6.0 / 1.10
    ok = abs(clip_info.duration - expected_clip) < 0.35
    print(
        f"  取原曲 8.0s 起 6.0s，按 ×1.10 变速 -> {clip_info.duration:.2f}s "
        f"({'OK ' if ok else 'BAD'}，期望 {expected_clip:.2f}s)"
    )
    if not ok:
        failures.append(f"片段时长不对：{clip_info.duration:.2f}s，期望 {expected_clip:.2f}s")

    section("9. 节拍声：先变速再打点，位置对不对")
    # ---- 纯换算：成品时间轴上 click 应该正好落在「拍点 ÷ 倍率」处
    ratio = 1.10
    beats = detected["beats"]
    one_to_one = audio.click_times(beats, ratio, "1:1")
    expect = np.asarray(beats, dtype=float) / ratio
    ok_11 = one_to_one.size == len(beats) and np.allclose(one_to_one, expect, atol=1e-9)
    print(f"  1:1 打点 {one_to_one.size} 个，位置 = 拍点 ÷ {ratio}：{'OK ' if ok_11 else 'BAD'}")
    if not ok_11:
        failures.append("1:1 打点位置换算错误")

    one_to_two = audio.click_times(beats, ratio, "1:2")
    mids = (np.asarray(beats[:-1], dtype=float) + np.asarray(beats[1:], dtype=float)) / 2.0 / ratio
    ok_12 = (
        one_to_two.size == 2 * len(beats) - 1
        and bool(np.all(np.diff(one_to_two) > 0))
        and bool(np.allclose(one_to_two[1::2], mids, atol=1e-9))
    )
    print(f"  1:2 打点 {one_to_two.size} 个（拍点 {len(beats)} + 两拍中点 {len(beats) - 1}），"
          f"严格递增且中点插在正中间：{'OK ' if ok_12 else 'BAD'}")
    if not ok_12:
        failures.append("1:2 的两拍中点补点不正确")

    # ---- 信号级：两版走「变速 + 混音 + 限幅」同一条链路，只差 click 轨内容，
    #      相减就只剩 click 本身。这样验的是 click 真的落在成品的鼓点上，
    #      而不是「文件能生成」。
    with_click = workdir / f"clickon{ext}"
    without = workdir / f"clickoff{ext}"
    audio.render(src, without, ratio, None, None, [], "1:1", 0.0)
    audio.render(src, with_click, ratio, None, None, beats, "1:1", 1.0)

    y_off, sr_dec = librosa.load(str(without), sr=22050, mono=True)
    y_on, _ = librosa.load(str(with_click), sr=22050, mono=True)
    n_dec = min(y_off.size, y_on.size)
    diff = y_on[:n_dec] - y_off[:n_dec]
    duration_dec = n_dec / float(sr_dec)

    # mp3 编解码会带来一个固定的整体延迟，所以先搜出这个偏移量，
    # 再看「拍点处」比「两拍之间」响多少。固定延迟不影响对齐，越走越偏才影响。
    win = int(0.03 * sr_dec)
    best_delta, best_score = 0.0, -1.0
    for delta in np.arange(-0.15, 0.151, 0.005):
        vals = [
            float(np.abs(diff[int((t + delta) * sr_dec):int((t + delta) * sr_dec) + win]).max())
            for t in expect
            if 0.1 < t + delta < duration_dec - 0.1
        ]
        if vals and float(np.mean(vals)) > best_score:
            best_delta, best_score = float(delta), float(np.mean(vals))

    flat = expect[:-1] * 0.5 + expect[1:] * 0.5
    between = [
        float(np.abs(diff[int((t + best_delta) * sr_dec):int((t + best_delta) * sr_dec) + win]).max())
        for t in flat
        if 0.1 < t + best_delta < duration_dec - 0.1
    ]
    between_mean = float(np.mean(between)) if between else 0.0
    print(f"  差值信号（只剩 click）：拍点处 {best_score:.4f} / 两拍之间 {between_mean:.4f}"
          f"  · 整体偏移 {best_delta:+.3f}s（编解码固定延迟）")
    aligned = best_score > max(between_mean * 5.0, 0.05) and abs(best_delta) < 0.15
    if not aligned:
        failures.append(
            f"节拍声没落在成品拍点上：拍点处 {best_score:.4f} vs 之间 {between_mean:.4f}，"
            f"偏移 {best_delta:+.3f}s"
        )
    print(f"  click 落在变速后的拍点位置上：{'OK ' if aligned else 'BAD'}")

    section("10. mp3 输出可解码性")
    produced = sorted(workdir.glob(f"out_*{ext}"))
    if not produced:
        failures.append("没有找到任何导出产物")
    for path in produced:
        meta = audio.probe(path)
        print(f"  {path.name}: {meta.codec} / {meta.sample_rate} Hz / {meta.duration:.2f}s  OK")

    section("结论")
    if failures:
        print(f"存在 {len(failures)} 个问题：")
        for item in failures:
            print("  -", item)
        return 1
    print("全部通过。")
    print("测试文件目录：", workdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
