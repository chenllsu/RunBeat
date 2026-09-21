"""端到端测试：走完整 HTTP 流程 —— 上传 → 检测 → 预览 → 导出 → 下载。

用法（服务需已在 127.0.0.1:8000 启动）：
    .venv\\Scripts\\python.exe scripts\\e2e_test.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import librosa
import numpy as np
import requests
import soundfile as sf

from app import audio, config

BASE = "http://127.0.0.1:8000"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if condition else 'BAD'}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(f"{label} {detail}".strip())


def make_click_track(path: Path, bpm: float = 120.0, seconds: float = 24.0) -> Path:
    sr = 44100
    t = np.arange(int(sr * seconds)) / sr
    period = 60.0 / bpm
    phase = np.mod(t, period)
    hi = 0.55 * np.sin(2 * np.pi * 1000.0 * t) * np.exp(-45 * phase)
    kick = 0.60 * np.sin(2 * np.pi * (55.0 + 45.0 * np.exp(-14 * phase)) * t) * np.exp(-11 * phase)
    pad = 0.12 * np.sin(2 * np.pi * 220.0 * t)
    signal = hi + kick + pad
    signal = signal / (np.max(np.abs(signal)) + 1e-9) * 0.9
    sf.write(str(path), np.stack([signal, signal], axis=1), sr, subtype="PCM_16")
    return path


def make_tone_sections(path: Path, sections, sr: int = 44100) -> Path:
    """每段一个固定主频，外加均匀鼓点 —— 专门用来验证「裁剪截的确实是那一段」。

    成品的主频应当等于该段的频率。鼓点是均匀的 120 BPM，保证 BPM 仍检测得出来。
    """
    parts = []
    for freq, seconds in sections:
        t = np.arange(int(sr * seconds)) / sr
        tone = 0.32 * np.sin(2 * np.pi * freq * t)
        phase = np.mod(t, 0.5)
        kick = 0.45 * np.sin(2 * np.pi * 80.0 * t) * np.exp(-14 * phase)
        parts.append(tone + kick)
    sig = np.concatenate(parts)
    sig = sig / (np.max(np.abs(sig)) + 1e-9) * 0.9
    sf.write(str(path), np.stack([sig, sig], axis=1), sr, subtype="PCM_16")
    return path


def dominant_freq(path: Path, lo: float = 200.0, hi: float = 2000.0) -> float:
    """取 lo~hi Hz 里能量最强的频率。用来判断成品是「哪一段」截出来的。"""
    y, sr = librosa.load(str(path), sr=22050, mono=True)
    a = int(0.3 * sr)                       # 掐掉首尾，避开编码器的起停瞬态
    b = max(a + 1, y.size - int(0.3 * sr))
    seg = y[a:b]
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
    freqs = np.fft.rfftfreq(seg.size, 1.0 / sr)
    band = (freqs >= lo) & (freqs <= hi)
    return float(freqs[band][np.argmax(spec[band])])


def wait_for_server(timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            res = requests.get(f"{BASE}/api/health", timeout=5)
            if res.ok:
                return True
        except Exception:
            time.sleep(2)
    return False


def main() -> int:
    print("等待服务就绪 ...")
    if not wait_for_server():
        print("服务没起来，测试中止")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="runbeat_e2e_"))

    print("\n--- 1. 健康检查 ---")
    health = requests.get(f"{BASE}/api/health", timeout=20).json()
    print("   ffmpeg :", health["ffmpeg"])
    print("   输出格式:", health["output"])

    print("\n--- 2. 上传 ---")
    src = make_click_track(workdir / "click_120.wav", bpm=120.0, seconds=24.0)
    with src.open("rb") as handle:
        res = requests.post(
            f"{BASE}/api/upload",
            files={"file": (src.name, handle, "audio/wav")},
            timeout=300,
        )
    check("HTTP 200", res.status_code == 200, f"实际 {res.status_code} {res.text[:200]}")
    if res.status_code != 200:
        return 1
    info = res.json()
    print("   file_id :", info["file_id"])
    print("   时长    :", info["duration"], "秒")
    print("   检测 BPM:", info["detected_bpm"], " 候选:", info["bpm_candidates"])
    check("时长约 24 秒", abs(info["duration"] - 24.0) < 0.3, f"实际 {info['duration']}")
    check("BPM 接近 120", abs(info["detected_bpm"] - 120.0) < 3.5, f"实际 {info['detected_bpm']}")

    base_payload = {
        "file_id": info["file_id"],
        "source_bpm": info["detected_bpm"],
        "mapping": "1:1",
        "min_ratio": 0.85,
        "max_ratio": 1.15,
    }

    print("\n--- 3. 波形接口 ---")
    res = requests.get(f"{BASE}/api/waveform/{info['file_id']}", timeout=120)
    check("HTTP 200", res.status_code == 200, res.text[:200])
    wf = res.json()
    if res.status_code == 200:
        print(f"   峰值点数: {wf['buckets']}  时长: {wf['duration']}s  窗口: {wf['window']}s")
        check("峰值点数等于配置值", wf["buckets"] == config.WAVEFORM_BUCKETS, f"实际 {wf['buckets']}")
        check("峰值数量与 buckets 一致", len(wf["peaks"]) == wf["buckets"])
        check("峰值已归一化到 0~1000",
              min(wf["peaks"]) >= 0 and max(wf["peaks"]) == 1000,
              f"min={min(wf['peaks'])} max={max(wf['peaks'])}")

    print("\n--- 4. 拍点接口 ---")
    res = requests.get(f"{BASE}/api/beats/{info['file_id']}",
                       params={"bpm": info["detected_bpm"]}, timeout=180)
    check("HTTP 200", res.status_code == 200, res.text[:200])
    bt = res.json()
    if res.status_code == 200:
        first, last = bt["beats"][0], bt["beats"][-1]
        print(f"   拍点数: {bt['beat_count']} （检测到 {bt.get('detected_count')}）"
              f"  间隔波动: {bt['interval_cv']}")
        print(f"   首拍 {first}s  末拍 {last}s  总时长 {info['duration']}s")
        check("拍点覆盖到开头（不因前奏被清空）", first < 0.6, f"首拍 {first}s")
        check("拍点覆盖到结尾", last > info["duration"] - 1.2,
              f"末拍 {last}s，时长 {info['duration']}s")
        gaps = np.diff(np.asarray(bt["beats"], dtype=float))
        med = float(np.median(gaps))
        check("拍点间隔约 0.5 秒", abs(med - 0.5) < 0.02, f"中位间隔 {med:.4f}s")

        half = requests.get(f"{BASE}/api/beats/{info['file_id']}",
                            params={"bpm": info["detected_bpm"] / 2}, timeout=180).json()
        print(f"   半速拍点数: {half['beat_count']}")
        check("半速拍点数约为一半", abs(half["beat_count"] * 2 - bt["beat_count"]) <= 2,
              f"{half['beat_count']} vs {bt['beat_count']}")

        again = requests.get(f"{BASE}/api/beats/{info['file_id']}",
                             params={"bpm": info["detected_bpm"]}, timeout=180).json()
        check("同一 BPM 结果稳定", again["beats"] == bt["beats"])

    print("\n--- 5. 听节拍对齐 ---")
    met_payload = {
        "file_id": info["file_id"],
        "source_bpm": info["detected_bpm"],
        "start": 4.0,
        "length": 8.0,
    }
    t0 = time.time()
    res = requests.post(f"{BASE}/api/metronome", json=met_payload, timeout=300)
    check("HTTP 200", res.status_code == 200, res.text[:300])
    if res.status_code == 200:
        met = res.json()
        print(f"   url: {met['url']}  窗口内拍点 {met['beats_in_window']}  用时 {time.time() - t0:.2f}s")
        check("窗口内拍点数合理", 12 <= met["beats_in_window"] <= 22, f"{met['beats_in_window']}")

        media = requests.get(f"{BASE}{met['url']}", timeout=120)
        check("节拍片段可下载", media.status_code == 200 and len(media.content) > 1000,
              f"HTTP {media.status_code}, {len(media.content)} 字节")
        mp = workdir / "metro.mp3"
        mp.write_bytes(media.content)
        mi = audio.probe(mp)
        print(f"   节拍片段: {mi.codec} {mi.duration}s")
        check("节拍片段时长等于截取长度（不参与变速）", abs(mi.duration - 8.0) < 0.3,
              f"实际 {mi.duration}")

        again = requests.post(f"{BASE}/api/metronome", json=met_payload, timeout=120).json()
        check("第二次命中缓存", again.get("cached") is True, f"cached={again.get('cached')}")

        # 信号级验证：渲染两版（有 click / 无 click），两者走完全相同的编码链路，
        # 相减就只剩 click 本身。这样能验证「click 真的打在拍点上」而不是只看到文件生成。
        ref = audio.render_metronome(src, workdir / "metro_ref.mp3", [], 4.0, 8.0,
                                     config.METRONOME_GAIN)
        real = audio.render_metronome(src, workdir / "metro_real.mp3", bt["beats"], 4.0, 8.0,
                                      config.METRONOME_GAIN)
        y_ref, sr_a = librosa.load(str(ref), sr=22050, mono=True)
        y_real, _ = librosa.load(str(real), sr=22050, mono=True)
        n_min = min(y_ref.size, y_real.size)
        diff = y_real[:n_min] - y_ref[:n_min]

        win = int(0.04 * sr_a)
        in_win = [b for b in bt["beats"] if 4.0 <= b < 8.0]
        on_idx = [int((b - 4.0) * sr_a) for b in in_win]
        on_idx = [i for i in on_idx if 0 <= i and i + win < diff.size]
        mids = [(a + b) / 2 for a, b in zip(in_win[:-1], in_win[1:])]
        off_idx = [int((m - 4.0) * sr_a) for m in mids]
        off_idx = [i for i in off_idx if 0 <= i and i + win < diff.size]

        if on_idx and off_idx:
            on = float(np.mean([np.abs(diff[i:i + win]).max() for i in on_idx]))
            off = float(np.mean([np.abs(diff[i:i + win]).max() for i in off_idx]))
            print(f"   差值信号（只剩 click）：拍点处 {on:.4f} / 两拍之间 {off:.4f}")
            check("click 确实打在拍点位置上", on > max(off * 5.0, 0.05),
                  f"拍点 {on:.4f} vs 之间 {off:.4f}")
        else:
            check("差值信号可分析", False, "拍点索引不足")

    print("\n--- 6. 预览（范围内变速） ---")
    payload = dict(base_payload, target_spm=130, start=5.0, length=8.0)
    res = requests.post(f"{BASE}/api/preview", json=payload, timeout=300)
    check("HTTP 200", res.status_code == 200, res.text[:200])
    if res.status_code == 200:
        prev = res.json()
        print("   url    :", prev["url"])
        print("   倍率   :", prev["ratio"], " 实际步频:", prev["actual_spm"], " natural:", prev["natural"])
        check("倍率约 1.083", abs(prev["ratio"] - 1.0833) < 0.01, f"实际 {prev['ratio']}")
        check("未被截断", prev["clamped"] is False)

        media = requests.get(f"{BASE}{prev['url']}", timeout=60)
        check("片段可下载", media.status_code == 200 and len(media.content) > 1000,
              f"HTTP {media.status_code}, {len(media.content)} 字节")

        again = requests.post(f"{BASE}/api/preview", json=payload, timeout=60).json()
        check("第二次命中缓存", again.get("cached") is True, f"cached={again.get('cached')}")

        # 勾上「试听时叠加节拍声」应当另生成一份，不能命中上面那份不带节拍声的缓存
        m_payload = dict(payload, metronome=True, metronome_gain=0.5)
        res = requests.post(f"{BASE}/api/preview", json=m_payload, timeout=300)
        check("带节拍声的预览 HTTP 200", res.status_code == 200, res.text[:200])
        if res.status_code == 200:
            mm = res.json()
            print(f"   含节拍声: 窗口内打点 {mm['clicks_in_window']} 个"
                  f"  gain {mm['metronome_gain']}  cached {mm['cached']}")
            check("返回标记为含节拍声", mm["metronome"] is True)
            check("与不带节拍声的缓存分开", mm["cached"] is False, f"cached={mm['cached']}")
            check("窗口内打点数合理", 12 <= mm["clicks_in_window"] <= 22,
                  f"{mm['clicks_in_window']}")
            check("倍率与不带节拍声时一致", abs(mm["ratio"] - prev["ratio"]) < 1e-9)
            check("输出时长与不带节拍声时一致",
                  abs(mm["output_length"] - prev["output_length"]) < 0.05,
                  f"{mm['output_length']} vs {prev['output_length']}")

            media_m = requests.get(f"{BASE}{mm['url']}", timeout=120)
            check("带节拍声的片段可下载",
                  media_m.status_code == 200 and len(media_m.content) > 1000,
                  f"HTTP {media_m.status_code}, {len(media_m.content)} 字节")
            check("与不带节拍声的片段内容不同",
                  media_m.content != media.content)

            mm2 = requests.post(f"{BASE}/api/preview", json=m_payload, timeout=60).json()
            check("同设置第二次命中缓存", mm2.get("cached") is True, f"cached={mm2.get('cached')}")

    print("\n--- 7. 预览（会被截断的情况） ---")
    payload2 = dict(base_payload, target_spm=190, start=2.0, length=5.0)
    res = requests.post(f"{BASE}/api/preview", json=payload2, timeout=300)
    check("HTTP 200", res.status_code == 200, res.text[:200])
    if res.status_code == 200:
        p2 = res.json()
        print("   倍率   :", p2["ratio"], " 实际步频:", p2["actual_spm"], " clamped:", p2["clamped"])
        check("倍率被截到 1.15", abs(p2["ratio"] - 1.15) < 0.001, f"实际 {p2['ratio']}")
        check("clamped 为真", p2["clamped"] is True)

    print("\n--- 8. 导出整首 ---")
    export_payload = dict(base_payload, target_spm=130)
    res = requests.post(f"{BASE}/api/export", json=export_payload, timeout=600)
    check("HTTP 200", res.status_code == 200, res.text[:300])
    if res.status_code != 200:
        return 1
    exp = res.json()
    print("   文件名 :", exp["filename"])
    print("   倍率   :", exp["ratio"], " 实际步频:", exp["actual_spm"])
    print("   时长   :", exp["source_duration"], "->", exp["output_duration"])
    print("   耗时   :", exp["elapsed"], "秒")
    expected_out = info["duration"] / exp["ratio"]
    check("成品时长正确", abs(exp["output_duration"] - expected_out) < 0.4,
          f"实际 {exp['output_duration']}，期望 {expected_out:.2f}")

    print("\n--- 9. 下载 ---")
    res = requests.get(f"{BASE}{exp['download_url']}", timeout=120)
    check("HTTP 200", res.status_code == 200, f"实际 {res.status_code}")
    check("有内容", len(res.content) > 10000, f"{len(res.content)} 字节")
    disposition = res.headers.get("content-disposition", "")
    check("带下载文件名", "attachment" in disposition.lower(), disposition)

    saved = workdir / Path(exp["filename"]).name
    saved.write_bytes(res.content)
    meta = audio.probe(saved)
    print("   落盘文件:", saved.name, meta.codec, meta.duration, "秒")

    out_analysis = audio.to_analysis_wav(saved, workdir / "out_analysis.wav")
    out_bpm = audio.detect_bpm(out_analysis)["bpm"]
    target_bpm = info["detected_bpm"] * exp["ratio"]
    print(f"   成品 BPM: {out_bpm}（期望约 {target_bpm:.1f}）")
    check("成品 BPM 命中", abs(out_bpm - target_bpm) < 3.5, f"实际 {out_bpm}")

    print("\n--- 9B. 成品叠加节拍声 ---")
    beat_payload = dict(export_payload, metronome=True, metronome_gain=1.0)
    res = requests.post(f"{BASE}/api/export", json=beat_payload, timeout=600)
    check("HTTP 200", res.status_code == 200, res.text[:300])
    if res.status_code == 200:
        bexp = res.json()
        print("   文件名 :", bexp["filename"])
        print("   全曲打点:", bexp["click_count"], " 音量:", bexp["metronome_gain"])
        check("返回标记为含节拍声", bexp["metronome"] is True)
        check("文件名带 _beat 后缀（不会覆盖不带节拍声的成品）",
              "_beat" in bexp["filename"], bexp["filename"])
        check("倍率与不带节拍声时一致", abs(bexp["ratio"] - exp["ratio"]) < 1e-9)
        check("时长与不带节拍声时一致",
              abs(bexp["output_duration"] - exp["output_duration"]) < 0.2,
              f"{bexp['output_duration']} vs {exp['output_duration']}")

        # 拍点网格已补齐到整首，整体除以倍率后仍然全部落在成品时长之内，
        # 所以打点数应当和拍点数基本相同（不是除以倍率）。
        check("打点数等于整曲拍点数", abs(bexp["click_count"] - bt["beat_count"]) <= 1,
              f"{bexp['click_count']} vs 拍点 {bt['beat_count']}")

        beat_bytes = requests.get(f"{BASE}{bexp['url']}", timeout=180).content
        check("带节拍声的成品可下载", len(beat_bytes) > 10000, f"{len(beat_bytes)} 字节")
        beat_file = workdir / "with_beat.mp3"
        beat_file.write_bytes(beat_bytes)

        # 信号级：拿它和不带节拍声那版相减，差值里只剩 click。
        # 搜出整体偏移（mp3 编解码的固定延迟，不影响对齐判断）后，
        # 看「拍点处」是不是比「两拍之间」明显更响。
        y_plain, sr_a = librosa.load(str(saved), sr=22050, mono=True)
        y_beat, _ = librosa.load(str(beat_file), sr=22050, mono=True)
        n_min = min(y_plain.size, y_beat.size)
        diff = y_beat[:n_min] - y_plain[:n_min]
        dur_dec = n_min / float(sr_a)

        click_at = audio.click_times(bt["beats"], exp["ratio"], "1:1")
        win = int(0.03 * sr_a)
        best_delta, on = 0.0, -1.0
        for delta in np.arange(-0.15, 0.151, 0.005):
            vals = [
                float(np.abs(diff[int((t + delta) * sr_a):int((t + delta) * sr_a) + win]).max())
                for t in click_at
                if 0.1 < t + delta < dur_dec - 0.1
            ]
            if vals and float(np.mean(vals)) > on:
                best_delta, on = float(delta), float(np.mean(vals))

        mids = (click_at[:-1] + click_at[1:]) * 0.5
        off_vals = [
            float(np.abs(diff[int((t + best_delta) * sr_a):int((t + best_delta) * sr_a) + win]).max())
            for t in mids
            if 0.1 < t + best_delta < dur_dec - 0.1
        ]
        off = float(np.mean(off_vals)) if off_vals else 0.0
        print(f"   差值信号（只剩 click）：拍点处 {on:.4f} / 两拍之间 {off:.4f}"
              f"  · 整体偏移 {best_delta:+.3f}s")
        check("节拍声落在成品（已变速）的拍点位置上",
              on > max(off * 3.0, 0.05) and abs(best_delta) < 0.15,
              f"拍点 {on:.4f} vs 之间 {off:.4f}，偏移 {best_delta:+.3f}s")

        again = requests.post(f"{BASE}/api/export", json=beat_payload, timeout=600)
        check("重复导出仍然正常", again.status_code == 200, again.text[:200])

    print("\n--- 9C. 导出裁剪区间 ---")
    clip_payload = dict(base_payload, target_spm=130, start=4.0, length=8.0)
    res = requests.post(f"{BASE}/api/export", json=clip_payload, timeout=600)
    check("HTTP 200", res.status_code == 200, res.text[:300])
    if res.status_code == 200:
        cl = res.json()
        print("   文件名 :", cl["filename"])
        print(f"   区间   : {cl['clip_start']}s 起 {cl['clip_length']}s  long，clipped={cl['clipped']}")
        print("   时长   :", cl["clip_length"], "->", cl["output_duration"])
        check("标记为已裁剪", cl["clipped"] is True)
        check("区间起点回显正确", abs(cl["clip_start"] - 4.0) < 0.01, f"{cl['clip_start']}")
        check("区间长度回显正确", abs(cl["clip_length"] - 8.0) < 0.01, f"{cl['clip_length']}")
        check("成品时长 = 区间长度 ÷ 倍率",
              abs(cl["output_duration"] - 8.0 / cl["ratio"]) < 0.4,
              f"实际 {cl['output_duration']}，期望 {8.0 / cl['ratio']:.2f}")
        check("文件名带区间标记", "_0m04s-0m12s" in cl["filename"], cl["filename"])
        check("成品明显短于整首",
              cl["output_duration"] < exp["output_duration"] * 0.6,
              f"{cl['output_duration']} vs 整首 {exp['output_duration']}")

        got = requests.get(f"{BASE}{cl['download_url']}", timeout=180)
        check("裁剪成品可下载", got.status_code == 200 and len(got.content) > 5000,
              f"HTTP {got.status_code}, {len(got.content)} 字节")
        clip_file = workdir / "clip.mp3"
        clip_file.write_bytes(got.content)
        cm = audio.probe(clip_file)
        print("   落盘文件:", clip_file.name, cm.codec, cm.duration, "秒")
        check("落盘时长与回显一致", abs(cm.duration - cl["output_duration"]) < 0.4,
              f"{cm.duration} vs {cl['output_duration']}")

        other = requests.post(
            f"{BASE}/api/export",
            json=dict(base_payload, target_spm=130, start=12.0, length=6.0),
            timeout=600,
        ).json()
        check("不同区间产出不同文件名（不会互相覆盖）",
              other["filename"] != cl["filename"],
              f"{other['filename']} vs {cl['filename']}")

        # 裁剪 + 节拍声：打点数应当只数这一段，而不是整首
        res = requests.post(f"{BASE}/api/export",
                            json=dict(clip_payload, metronome=True, metronome_gain=1.0),
                            timeout=600)
        check("裁剪 + 节拍声 HTTP 200", res.status_code == 200, res.text[:200])
        if res.status_code == 200:
            bc = res.json()
            print(f"   裁剪 + 节拍声: 打点 {bc['click_count']} 声"
                  f"（整首是 {bexp['click_count']} 声）")
            check("打点数只数这一段", 14 <= bc["click_count"] <= 18, f"{bc['click_count']}")
            check("明显少于整曲打点数",
                  bc["click_count"] < bexp["click_count"] * 0.6,
                  f"{bc['click_count']} vs {bexp['click_count']}")
    else:
        clip_file = None

    print("\n--- 9D. 裁剪区间的边界拒绝 ---")
    res = requests.post(f"{BASE}/api/export",
                        json=dict(base_payload, target_spm=130, start=4.0, length=2.0),
                        timeout=60)
    check("区间短于下限被拒", res.status_code == 400, f"实际 {res.status_code} {res.text[:160]}")

    res = requests.post(f"{BASE}/api/export",
                        json=dict(base_payload, target_spm=130, start=info["duration"] - 1.0),
                        timeout=60)
    check("起点贴结尾（剩余不足）被拒", res.status_code == 400,
          f"实际 {res.status_code} {res.text[:160]}")

    print("\n--- 10. 异常输入 ---")
    res = requests.post(f"{BASE}/api/preview", json=dict(base_payload, target_spm=130, file_id="不存在的ID"),
                        timeout=30)
    check("未知 file_id 返回 404", res.status_code == 404, f"实际 {res.status_code}")

    res = requests.post(f"{BASE}/api/export", json=dict(base_payload, target_spm=130, mapping="2:3"), timeout=30)
    check("非法 mapping 被拒", res.status_code == 422, f"实际 {res.status_code}")

    print("\n--- 11. 裁的是不是那一段（频率标记） ---")
    tone_src = make_tone_sections(
        workdir / "tone_sections.wav",
        [(300.0, 8.0), (700.0, 8.0), (1200.0, 8.0)],
    )
    with tone_src.open("rb") as handle:
        res = requests.post(
            f"{BASE}/api/upload",
            files={"file": (tone_src.name, handle, "audio/wav")},
            timeout=300,
        )
    check("分段音频上传 HTTP 200", res.status_code == 200, res.text[:200])
    if res.status_code == 200:
        ti = res.json()
        tpayload = {
            "file_id": ti["file_id"],
            "source_bpm": ti["detected_bpm"],
            "mapping": "1:1",
            "min_ratio": 0.85,
            "max_ratio": 1.15,
        }
        # 倍率取 1.0（目标 = 源 BPM），音高本来就不变，成品主频可以直接和该段比
        for idx, (freq, start) in enumerate(((300.0, 0.0), (700.0, 8.0), (1200.0, 16.0))):
            res = requests.post(
                f"{BASE}/api/export",
                json=dict(tpayload, target_spm=ti["detected_bpm"], start=start, length=8.0),
                timeout=600,
            )
            check(f"第 {idx + 1} 段（{start:.0f}s 起）导出 HTTP 200",
                  res.status_code == 200, res.text[:200])
            if res.status_code != 200:
                continue
            seg_file = workdir / f"seg{idx + 1}.mp3"
            seg_file.write_bytes(
                requests.get(f"{BASE}{res.json()['url']}", timeout=180).content
            )
            got_freq = dominant_freq(seg_file)
            print(f"   [{start:.0f}s, {start + 8:.0f}s) 主频 {got_freq:.1f} Hz（该段应为 {freq:.0f} Hz）")
            check(f"第 {idx + 1} 段截取位置正确",
                  abs(got_freq - freq) < 40.0,
                  f"实际 {got_freq:.1f} Hz，期望 {freq:.0f} Hz")

    print("\n" + "=" * 62)
    if failures:
        print(f"存在 {len(failures)} 个问题：")
        for item in failures:
            print("  -", item)
        return 1
    print("端到端全部通过。")
    print("测试产物目录：", workdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
